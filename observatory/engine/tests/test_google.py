#!/usr/bin/env python3
"""The Google inventory: what the join is made of, and what never leaves it.

TWO CLAIMS ARE WORTH TESTING HERE and they pull in opposite directions.

The first is that the join is MEASURED. Most properties were invisible while
the mapping was a short hand-written file, so the rules
that read a property's own declarations — a web stream's host, an app stream's
bundle id, the display name — are the feature; each is driven here on planted
data, in the order they are allowed to fire, because a rule that silently wins
over the operator's own file would be the worst kind of "automatic".

The second is that a credential stays a credential. The service account signs a
JWT and what reaches a document is the `client_email` it states about itself —
a public identifier — never the key, never a token, never a raw response body.
The refusal path matters as much: Google answers `accessNotConfigured` when an
API is switched off, which READS like a permission problem and is not one.

Values here are composed, never written: `tools/check_secrets.py` cannot tell a
fixture from a live credential and is right not to try.
"""
from __future__ import annotations
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable


#: Location overrides a runner may export; a workspace of our own drops them so
#: every location follows OBSERVATORY_HOME.
SELECTORS = ("OBSERVATORY_REGISTRY", "OBSERVATORY_DB", "OBSERVATORY_STATE", "OBSERVATORY_SCRATCH")


def workspace(*, google: bool = False) -> pathlib.Path:
    """A fresh private workspace, initialized by the real `init`.

    The console addresses and the integration switch are workspace
    configuration, so every test here reads its own rather than a machine's.
    """
    home = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-google-home-")).resolve() / "home"
    env = {k: v for k, v in os.environ.items() if k not in SELECTORS}
    env["OBSERVATORY_HOME"] = str(home)
    subprocess.run([PY, str(ROOT / "observatory.py"), "init"], cwd=ROOT, env=env,
                   capture_output=True, timeout=120, check=True)
    if google:
        settings = home / "config/settings.json"
        doc = json.loads(settings.read_text(encoding="utf-8"))
        doc.setdefault("integrations", {})["google"] = True
        settings.write_text(json.dumps(doc), encoding="utf-8")
    return home


# The modules below read `paths` at import; select a workspace first. The host
# join is the registry's own, so the registry claims the one host a property's
# stream names — the shape a real emit would have written.
_HOME = workspace()
_projects = _HOME / "registry/projects.json"
_doc = json.loads(_projects.read_text(encoding="utf-8"))
_doc["projects"] = [{"id": "project:alpha-web", "name": "alpha-web",
                     "sites": [{"host": "alpha.example.com", "evidence": ["fixture"]}]}]
_projects.write_text(json.dumps(_doc), encoding="utf-8")
os.environ["OBSERVATORY_HOME"] = str(_HOME)
for _name in SELECTORS:
    os.environ.pop(_name, None)
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _k(tag: str) -> str:
    return "-".join(("fixture", tag, "value", "not", "a", "real", "one"))


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PROJECTS = [
    {"id": "project:alpha-web", "name": "alpha-web",
     "local_folders": ["alpha-web"], "sites": [{"host": "alpha.example.com"}]},
    {"id": "project:beta-api", "name": "beta-api", "local_folders": ["beta-api"]},
    {"id": "project:gammaboard", "name": "gammaboard", "local_folders": ["gammaboard"]},
]


def test_the_rules_fire_in_the_order_the_evidence_ranks_them() -> None:
    gr = load("collectors/google_registry.py", "google_registry")
    names = {"alpha-web": "project:alpha-web",
             "beta-api": "project:beta-api", "gammaboard": "project:gammaboard"}
    hosts = {"alpha.example.com": "project:alpha-web"}

    def owner(h):
        return hosts.get(h)

    declared = {"properties/1": {"property": "properties/1", "project": "project:beta-api",
                                 "_why": "the operator said so"}}
    # the operator's word outranks a measurement that disagrees
    pid, rule, why = gr.match({"property": "properties/1", "hosts": ["alpha.example.com"],
                               "app_ids": [], "name": "whatever"}, declared, owner, names)
    check("a declared project wins over a host that resolves elsewhere",
          pid == "project:beta-api" and rule == "declared", f"{pid} via {rule}")
    check("and the row says whose decision it was", "operator said so" in why, why)

    pid, rule, _ = gr.match({"property": "properties/2", "hosts": ["alpha.example.com"],
                             "app_ids": [], "name": "x"}, {}, owner, names)
    check("a web stream's host resolves through the registry",
          pid == "project:alpha-web" and rule == "stream-host", f"{pid} via {rule}")

    pid, rule, _ = gr.match({"property": "properties/3", "hosts": [],
                             "app_ids": ["com.example.gammaboard"], "name": "x"}, {}, owner, names)
    check("an app stream's bundle id ending in a project's own name",
          pid == "project:gammaboard" and rule == "app-id", f"{pid} via {rule}")

    pid, rule, _ = gr.match({"property": "properties/4", "hosts": [], "app_ids": [],
                             "name": "Beta API"}, {}, owner, names)
    check("a display name that IS a project's name", pid == "project:beta-api"
          and rule == "name", f"{pid} via {rule}")

    pid, rule, _ = gr.match({"property": "properties/5", "hosts": ["nobody.example"],
                             "app_ids": ["com.other.thing"], "name": "Somebody Else"},
                            {}, owner, names)
    check("and nothing matched stays nothing — never a guess", pid is None and rule == "", f"{pid}")
    check("a two-letter bundle tail cannot match a project",
          gr.match({"property": "p", "hosts": [], "app_ids": ["com.x.ab"], "name": "?"},
                   {}, owner, {"ab": "project:ab"})[0] is None,
          "a short token matches too much to be evidence")


def test_a_property_the_operator_has_already_placed_is_not_asked_about_again() -> None:
    gr = load("collectors/google_registry.py", "google_registry")
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-google-"))
    (d / "host_boundary.json").write_text(json.dumps({
        "hosts": {"corp.example.com": {"status": "outside", "why": "the company's main site"}}}),
        encoding="utf-8")
    got = gr.boundary(d / "host_boundary.json")
    check("the boundary file is read as the mapping it is",
          got.get("corp.example.com", {}).get("status") == "outside", str(got))
    check("a missing file is empty rather than an error", gr.boundary(d / "nope.json") == {})


def test_the_document_carries_names_verdicts_and_console_links_only() -> None:
    gr = load("collectors/google_registry.py", "google_registry")
    scan = {"scanned_at": "2026-09-14T08:00:00Z",
            "credentials": [{"file": "google-service-account.json",
                             "client_email": "acct@example.iam.gserviceaccount.com",
                             "cloud_project": "ga-report-1"}],
            "accounts": [{"account": "accounts/1", "name": "A", "properties": 1}],
            "properties": [
                {"property": "properties/7", "name": "alpha.example.com", "account": "accounts/1",
                 "account_name": "A", "hosts": ["alpha.example.com"], "app_ids": [],
                 "users_30d": 447, "sessions_30d": 619, "views_30d": 3207},
                {"property": "properties/8", "name": "Somebody Else", "account": "accounts/1",
                 "account_name": "A", "hosts": ["nobody.example"], "app_ids": [],
                 "users_30d": 12, "sessions_30d": 20, "views_30d": 30}],
            "search_console": [{"site": "sc-domain:delta.example.org", "permission": "siteFullUser"}],
            "degraded": []}
    rows = gr.rows(scan, PROJECTS, pathlib.Path(tmpdir.mkdtemp()) / "nonexistent.json")
    doc = gr.document(scan, rows, "2026-09-14")
    blob = json.dumps(doc)
    check("a property is joined and says by which rule",
          rows[0]["project"] == "project:alpha-web"
          and rows[0]["link_rule"] == "stream-host", str(rows[0])[:160])
    check("one nothing claims says so, with the reason",
          rows[1]["project"] is None and rows[1]["standing"] == "unclaimed"
          and "operator's estate rule" in (rows[1]["unlinked_reason"] or ""), str(rows[1])[:160])
    check("every row carries the console it opens",
          rows[0]["report_url"].endswith("/p7/reports/intelligenthome")
          and "a1p7" in (rows[0]["admin_url"] or ""), str(rows[0]["admin_url"]))
    check("the credential is carried as its public identifier and its Cloud console",
          "acct@example.iam.gserviceaccount.com" in blob
          and "project=ga-report-1" in blob, "")
    check("Search Console sites carry their console link",
          doc["search_console"][0]["console_url"].endswith("resource_id=sc-domain:delta.example.org"), "")
    # The addresses are DATA in the workspace (config/google_consoles.json,
    # seeded from defaults/): one of Google's paths spells an installed
    # plugin's id, which the seam rule forbids in core code. A file that is
    # missing makes rows without links, never an emit that dies.
    urls = gr.consoles()
    check("the console addresses come from the data file, all four",
          all(urls[k] for k in ("ga4_report", "ga4_admin", "search_console_site", "cloud_project")),
          str(urls))
    check("and a missing file degrades to rows without links, not a crash",
          gr.consoles(pathlib.Path(tmpdir.mkdtemp()) / "no-such-consoles.json")
          == {"ga4_report": "", "ga4_admin": "", "search_console_site": "", "cloud_project": ""}
          and gr._fill("", pid="7") is None, "")
    check("the totals separate linked from unclaimed and carry the loose users",
          doc["totals"]["linked_to_a_project"] == 1 and doc["totals"]["unclaimed"] == 1
          and doc["totals"]["users_30d_unclaimed"] == 12, str(doc["totals"]))
    for forbidden in ("private_key", "BEGIN PRIVATE KEY", "Bearer ", "Authorization"):
        check(f"no {forbidden!r} anywhere in the document", forbidden not in blob, "")


def test_the_scan_is_cached_and_refuses_to_re_ask_google() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-gscan-"))
    out = d / "google.json"
    import datetime
    out.write_text(json.dumps({"scanned_at": datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "properties": [],
        "accounts": [], "credentials": [], "search_console": [], "degraded": []}), encoding="utf-8")
    before = out.read_text(encoding="utf-8")
    # The integration is switched on in a workspace of its own: a disabled one
    # returns before the cache is consulted, which would test nothing here.
    enabled = {**os.environ, "OBSERVATORY_HOME": str(workspace(google=True))}
    p = subprocess.run([PY, str(ROOT / "collectors/scan_google.py"), str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300, env=enabled)
    check("a fresh scan is not re-fetched", p.returncode == 0 and "not re-fetched" in p.stdout,
          (p.stdout + p.stderr)[-200:])
    check("and the file is untouched", out.read_text(encoding="utf-8") == before)
    src = (ROOT / "collectors/scan_google.py").read_text(encoding="utf-8")
    check("`--force` exists and the reason for the gate is written down",
          "--force" in src and "seventy-six" in src,
          "a cache with no stated reason is a number the next reader deletes")
    check("the scan never writes a response body into the file",
          "_reason(" in src and '"message"' in src,
          "a body can echo a request header; only the provider's own sentence is carried")


def test_a_disabled_api_is_not_reported_as_a_permission_problem() -> None:
    gf = load("tools/google_findings.py", "google_findings")
    doc = {"properties": [{"name": "X", "standing": "linked", "users_30d": 1}],
           "scanned_on": "2026-09-14",
           "degraded": [{"source": "search console via acct@example",
                         "reason": "HTTP 403: Google Search Console API has not been used in "
                                   "project 100000000001 before or it is disabled.",
                         "remedy": "enable the Search Console API in Cloud project 100000000001"}]}
    rows = gf.findings(doc, "2026-09-14")
    api = [f for f in rows if f["type"] == "analytics.api_disabled"]
    check("a switched-off API is its own row", len(api) == 1, str(rows))
    check("and it says the credential has the rights", "is not one" in api[0]["detail"], "")
    check("with the remedy from the provider's own sentence",
          "100000000001" in api[0]["action"], api[0]["action"])


def test_the_unclaimed_row_leads_with_the_traffic_behind_it() -> None:
    gf = load("tools/google_findings.py", "google_findings")
    doc = {"scanned_on": "2026-09-14", "degraded": [], "properties": [
        {"name": "Big", "standing": "unclaimed", "users_30d": 3_114_562},
        {"name": "Small", "standing": "unclaimed", "users_30d": 4},
        {"name": "Mine", "standing": "linked", "users_30d": 10},
        {"name": "Theirs", "standing": "outside", "users_30d": 99}]}
    rows = gf.findings(doc, "2026-09-14")
    row = next(f for f in rows if f["type"] == "analytics.property_unclaimed")
    check("only the unclaimed are counted — outside is an answered question",
          "2 analytics properties" in row["title"], row["title"])
    check("and the title carries the users behind them", "3,114,566" in row["title"], row["title"])
    check("worst first", row["detail"].index("Big") < row["detail"].index("Small"), "")
    check("it is info, because the boundary rule is the operator's",
          row["severity"] == "info", row["severity"])
    check("and the remedy names both answers",
          "ga4_properties.json" in row["action"] and "host_boundary.json" in row["action"], row["action"])
    check("nothing unclaimed raises nothing",
          not [f for f in gf.findings({"scanned_on": "2026-09-14", "degraded": [], "properties": [
              {"name": "Mine", "standing": "linked", "users_30d": 10}]}, "2026-09-14")
               if f["type"] == "analytics.property_unclaimed"], "")
    stale = gf.findings(doc, "2026-09-30")
    check("a scan older than three days says the tick has not run",
          any(f["type"] == "analytics.stale" for f in stale), str([f["type"] for f in stale]))


if __name__ == "__main__":
    print("the Google inventory — a measured join, and a credential that stays one\n")
    for fn in (test_the_rules_fire_in_the_order_the_evidence_ranks_them,
               test_a_property_the_operator_has_already_placed_is_not_asked_about_again,
               test_the_document_carries_names_verdicts_and_console_links_only,
               test_the_scan_is_cached_and_refuses_to_re_ask_google,
               test_a_disabled_api_is_not_reported_as_a_permission_problem,
               test_the_unclaimed_row_leads_with_the_traffic_behind_it):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mproperties joined by what they say about themselves\033[0m")
