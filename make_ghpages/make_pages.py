"""Generate HTML pages for the OPTIMADE providers list."""
import datetime
import json
import os
import shutil
import signal
import string
import traceback
import urllib.request
from contextlib import contextmanager

from jinja2 import Environment, PackageLoader, select_autoescape
from optimade import __version__
from optimade.models import IndexInfoResponse, LinksResponse
from optimade.utils import get_providers
from optimade.validator import ImplementationValidator
from optimade.validator.utils import ResponseError

# Subfolders
OUT_FOLDER = "out"
STATIC_FOLDER = "static"
HTML_FOLDER = (
    "providers"  # Name for subfolder where HTMLs for providers are going to be sitting
)
TEMPLATES_FOLDER = "templates"

# Absolute paths
pwd = os.path.split(os.path.abspath(__file__))[0]
STATIC_FOLDER_ABS = os.path.join(pwd, STATIC_FOLDER)

VALIDATION_TIMEOUT = 600


HTTP_USER_AGENT_HEADER = {
    "User-Agent": "urllib.request (automated dashboard build for providers.optimade.org)"
}


class DashboardTimeoutException(ResponseError):
    pass


@contextmanager
def time_limit(timeout: float):
    """A simple context manager that uses the signal module to raise
    an exception if the timeout is exceeded.

    Arguments:
        timeout: The desired timeout in seconds.

    """

    def signal_handler(signal_number, frame):
        raise DashboardTimeoutException(
            f"Validation timed out after {timeout} seconds. The validation run did not complete and results should be externally verified."
        )

    signal.signal(signal.SIGALRM, signal_handler)
    signal.alarm(timeout)
    try:
        yield
    finally:
        signal.alarm(0)


def extract_url(value):
    """To be used in the URLs of the sub databases.

    Indeed, sometimes its a AnyUrl, sometimes a Link(AnyUrl)
    """
    try:
        u = value.href
    except AttributeError:
        u = value
    return u.strip("/")


def get_index_metadb_data(base_url):
    """Return some info after inspecting the base_url of this index_metadb."""
    versions_to_test = ["v1", "v0.10", "v0"]

    provider_data = {}
    for version in versions_to_test:
        info_endpoint = f"{base_url}/{version}/info"
        try:
            info_req = urllib.request.Request(
                info_endpoint, headers=HTTP_USER_AGENT_HEADER
            )
            with urllib.request.urlopen(info_req) as url_response:
                response_content = url_response.read()
            provider_data["info_endpoint"] = info_endpoint
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            else:
                provider_data["state"] = "problem"
                provider_data[
                    "tooltip_lines"
                ] = "Generic error while fetching the data:\n{}".format(
                    traceback.format_exc()
                ).splitlines()
                provider_data["color"] = "light-red"
                return provider_data
    else:
        # Did not break: no version found
        provider_data["state"] = "not found"
        provider_data["tooltip_lines"] = [
            "I couldn't find the index meta-database, I tried the following versions: {}".format(
                ", ".join(versions_to_test)
            )
        ]
        provider_data["color"] = "light-red"
        return provider_data

    provider_data["state"] = "found"
    provider_data["color"] = "green"
    provider_data["version"] = version

    provider_data["default_subdb"] = None
    # Let's continue, it was found
    try:
        json_response = json.loads(response_content)
        IndexInfoResponse(**json_response)
    except Exception:
        # Adapt the badge info
        provider_data["state"] = "validation error"
        provider_data["color"] = "orange"
        provider_data[
            "tooltip_lines"
        ] = "Error while validating the Index MetaDB:\n{}".format(
            traceback.format_exc()
        ).splitlines()
        provider_data["version"] = version
    else:
        try:
            # For now I use this way of getting it
            provider_data["default_subdb"] = json_response["data"]["relationships"][
                "default"
            ]["data"]["id"]
        except Exception:
            # For now, whatever the error, I just ignore it
            pass

    links_endpoint = f"{base_url}/{version}/links"
    try:
        links_req = urllib.request.Request(
            links_endpoint, headers=HTTP_USER_AGENT_HEADER
        )
        with urllib.request.urlopen(links_req) as url_response:
            response_content = url_response.read()
    except urllib.error.HTTPError:
        provider_data["links_state"] = "problem"
        provider_data[
            "links_tooltip_lines"
        ] = "Generic error while fetching the /links endpoint:\n{}".format(
            traceback.format_exc()
        ).splitlines()
        provider_data["links_color"] = "light-red"
        return provider_data

    provider_data["links_endpoint"] = links_endpoint
    provider_data["links_state"] = "found"
    provider_data["links_color"] = "green"

    try:
        links_json_response = json.loads(response_content)
        LinksResponse(**links_json_response)
    except Exception:
        # Adapt the badge info
        provider_data["links_state"] = "validation error"
        provider_data["links_color"] = "orange"
        provider_data[
            "links_tooltip_lines"
        ] = "Error while validating the /links endpoint of the Index MetaDB:\n{}".format(
            traceback.format_exc()
        ).splitlines()
        return provider_data

    # We also filter out any non-child DB link type.
    all_linked_dbs = links_json_response["data"]
    subdbs = [
        subdb
        for subdb in all_linked_dbs
        if subdb["attributes"].get("link_type", "UNKNOWN") == "child"
    ]
    print(
        f"    [{len(all_linked_dbs)} links found, of which {len(subdbs)} child sub-dbs]"
    )

    # Order putting the default first, and then the rest in alphabetical order (by key)
    # Note that False gets before True.
    provider_data["subdbs"] = sorted(
        subdbs,
        key=lambda subdb: (subdb["id"] != provider_data["default_subdb"], subdb["id"]),
    )

    # Count the non-null ones
    non_null_subdbs = [
        subdb for subdb in provider_data["subdbs"] if subdb["attributes"]["base_url"]
    ]
    provider_data["num_non_null_subdbs"] = len(non_null_subdbs)

    provider_data["subdb_validation"] = {}
    provider_data["subdb_properties"] = {}
    provider_data["num_structures"] = 0
    for subdb in non_null_subdbs:
        url = subdb["attributes"]["base_url"]
        if (aggregate := subdb["attributes"].get("aggregate")) is None:
            aggregate = "ok"
        if aggregate != "ok":
            results = {}
            print(f"\t\tSkipping {subdb['id']} as aggregate is set to {aggregate}.")
            results["failure_count"] = 0
            results["failure_messages"] = []
            results["success_count"] = 0
            results["internal_failure_count"] = 0
            results["no_aggregate_reason"] = subdb["attributes"].get(
                "no_aggregate_reason", "No details given"
            )
            properties = {}

        else:
            v1_url = url.strip("/") + "/v1" if not url.endswith("/v1") else ""
            properties = get_child_properties(v1_url)
            results = validate_childdb(v1_url)
            results["num_structures"] = _get_structure_count(v1_url)
            provider_data["num_structures"] += results["num_structures"]

        results["aggregate"] = aggregate

        provider_data["subdb_validation"][url] = results
        provider_data["subdb_properties"][url] = properties
        provider_data["subdb_validation"][url]["valid"] = not results.get("failure_count", 0)
        # Count errors apart from internal errors
        provider_data["subdb_validation"][url]["total_count"] = (
            results.get("success_count", 0) + results.get("failure_count", 0)
        )
        try:
            ratio = results.get("success_count", 0) / (
                results.get("success_count", 0) + results.get("failure_count", 0)
            )
        except ZeroDivisionError:
            ratio = 0
        # Use the red/green values from the badge css
        ratio = 2 * (max(0.5, ratio) - 0.5)
        green = (77, 175, 74)
        red = (228, 26, 28)
        colour = list(green)

        for ind, channel in enumerate(colour):
            gradient = red[ind] - green[ind]
            colour[ind] += gradient * (1 - ratio)

        colour = [str(int(channel)) for channel in colour]
        provider_data["subdb_validation"][url][
            "_validator_results_colour"
        ] = f"rgb({','.join(colour)});"

        if provider_data["subdb_validation"][url].get("aggregate", "ok") != "ok":
            provider_data["subdb_validation"][url][
                "_validator_results_colour"
            ] = "DarkGrey"

    return provider_data


def get_html_provider_fname(provider_id):
    """Return a valid html filename given the provider ID."""
    valid_characters = set(string.ascii_letters + string.digits + "_-")

    simple_string = "".join(c for c in provider_id if c in valid_characters)

    return "{}.html".format(simple_string)


def validate_childdb(url: str) -> dict:
    """Run the optimade-python-tools validator on the child database.

    Parameters:
        url: the URL of the child database.

    Returns:
        dictionary representation of the validation results.

    """
    import dataclasses
    from traceback import print_exc

    validator = ImplementationValidator(
        base_url=url,
        run_optional_tests=False,
        verbosity=0,
        read_timeout=100,
        http_headers={
            "User-Agent": f"optimade-python-tools validator/{__version__} (automated dashboard build for providers.optimade.org)"
        },
    )

    try:
        with time_limit(VALIDATION_TIMEOUT):
            validator.validate_implementation()
    except DashboardTimeoutException:
        validator.results.failure_count += 1
        validator.results.failures_messages += [
            f"ImplementationValidator for this provider ({url}) timed out after the configured {VALIDATION_TIMEOUT} seconds."
        ]
    except (Exception, SystemExit):
        print_exc()

    return dataclasses.asdict(validator.results)

def get_child_properties(url: str) -> dict:
    """Get the properties served by the child database.

    Parameters:
        url: the URL of the child database.

    Returns:
        dictionary representation of the properties of the child database,
        broken down by entrypoint.

    """
    try:
        properties = {}
        info_req = urllib.request.Request(
            f"{url}/info", headers=HTTP_USER_AGENT_HEADER
        )
        with urllib.request.urlopen(info_req) as url_response:
            response_content = json.loads(url_response.read())
        entry_types = response_content.get("data", {}).get("attributes", {}).get("entry_types_by_format", {}).get("json", [])
        for _type in entry_types:
            entry_info_req = urllib.request.Request(
            f"{url}/info/{_type}", headers=HTTP_USER_AGENT_HEADER
        )
            with urllib.request.urlopen(entry_info_req) as url_response:
                response_content = json.loads(url_response.read())
            properties[_type] = response_content.get("data", {}).get("properties", {})

        return properties

    except Exception as exc:
        print(exc)
        return {}


def _get_structure_count(url: str) -> int:
    """Try to get the number of structures hosted at the given URL."""
    try:
        structures_req = urllib.request.Request(
            f"{url}/structures", headers=HTTP_USER_AGENT_HEADER
        )
        with urllib.request.urlopen(structures_req) as url_response:
            response_content = json.loads(url_response.read())
            # account for inconsistencies in the metadata by taking largest of available/returned data
            return max(
                response_content.get("meta", {}).get("data_available", 0),
                response_content.get("meta", {}).get("data_returned", 0),
            )

    except Exception as exc:
        print(exc)
        return 0


def make_pages():
    """Create the rendered pages (index, and per-provider detail page)."""

    # Create output folder, copy static files
    if os.path.exists(OUT_FOLDER):
        shutil.rmtree(OUT_FOLDER)
    os.mkdir(OUT_FOLDER)
    os.mkdir(os.path.join(OUT_FOLDER, HTML_FOLDER))
    shutil.copytree(STATIC_FOLDER_ABS, os.path.join(OUT_FOLDER, STATIC_FOLDER))

    env = Environment(
        loader=PackageLoader("mod"),
        autoescape=select_autoescape(["html", "xml"]),
    )

    env.filters["extract_url"] = extract_url

    providers = get_providers()
    if not providers:
        raise RuntimeError("Unable to retrieve providers list.")

    last_check_time = datetime.datetime.utcnow().strftime("%A %B %d, %Y at %H:%M UTC")

    all_provider_data = []
    # Create HTML view for each provider
    for provider in providers:
        provider_data = {"id": provider["id"], "last_check_time": last_check_time}
        print("  - {}".format(provider["id"]))

        subpage = os.path.join(HTML_FOLDER, get_html_provider_fname(provider["id"]))
        subpage_abspath = os.path.join(OUT_FOLDER, subpage)

        provider_data["subpage"] = subpage
        provider_data["attributes"] = provider

        base_url = provider.get("base_url")

        if base_url is None:
            provider_data["index_metadb"] = {
                "state": "unspecified",
                "tooltip_lines": [
                    "The provider did not specify a base URL for the Index Meta-Database"
                ],
                "color": "dark-gray",
            }
        else:
            provider_data["index_metadb"] = {}
            try:
                index_metadb_data = get_index_metadb_data(base_url)
                provider_data["index_metadb"] = index_metadb_data
            except Exception as exc:
                print(exc)
                provider_data["index_metadb"] = {
                    "state": "unknown",
                    "tooltip_lines": "Generic error while fetching the data:\n{}".format(
                        traceback.format_exc()
                    ).splitlines(),
                    "color": "orange",
                }

        provider_data[
            "title"
        ] = f'{provider_data["attributes"].get("name")}: OPTIMADE provider dashboard'
        provider_data["num_structures"] = provider_data["index_metadb"].get(
            "num_structures", 0
        )

        # Write provider html
        provider_html = env.get_template("singlepage.html").render(**provider_data)
        with open(subpage_abspath, "w") as f:
            f.write(provider_html)
        all_provider_data.append(provider_data)
        print("    - Page {} generated.".format(subpage))

    all_data = {}
    all_data["providers"] = sorted(
        all_provider_data, key=lambda provider: provider["id"]
    )
    all_data["globalsummary"] = {
        "with_base_url": sum(
            1 for d in all_data["providers"] if d["attributes"].get("base_url")
        ),
        "num_sub_databases": sum(
            [
                provider_data.get("index_metadb", {}).get("num_non_null_subdbs", 0)
                for provider_data in all_provider_data
            ]
        ),
        "num_structures": sum(prov["num_structures"] for prov in all_provider_data),
    }

    # Write main overview index
    print("[main index]")
    rendered = env.get_template("main_index.html").render(**all_data)
    outfile = os.path.join(OUT_FOLDER, "index.html")
    with open(outfile, "w") as f:
        f.write(rendered)
    print("  - index.html generated")


if __name__ == "__main__":
    make_pages()
