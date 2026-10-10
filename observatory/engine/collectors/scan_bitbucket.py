#!/usr/bin/env python3
"""List a Bitbucket workspace, or say precisely why it could not be listed.

WHAT IS AND IS NOT REACHABLE WITHOUT A CREDENTIAL, measured 2026-09-05:

    GET /2.0/repositories/<workspace>        -> HTTP 200, "size": 0
    GET /2.0/repositories/<workspace>/<repo> -> HTTP 404
    Authorization: Bearer <anything>         -> 401, with an OAuth realm challenge

The 200 is not access. Anonymous listing returns only PUBLIC repositories, and
this workspace has none, so the endpoint answers and reveals nothing. That is
worth writing down because a status code alone reads like coverage.

So the repositories under one workspace are known from one thing only: a
local clone points at them. Their default branch and whether the checkout is
current come from `scan_remotes.py`, which needs no credential. Everything else
— description, visibility, language, dates, and above all **repositories that
were never cloned to this machine** — needs the API, and this collector is what
uses it the moment a credential exists.

THE CREDENTIAL. The secret store's `bitbucket` file, mode 600 or the file is
REFUSED rather than used. Two accepted forms, and the collector reports which
it used, never the value:

    username:app_password     -> HTTP Basic  (also the shape of an API token,
                                 which Bitbucket sends as email:token)
    <token>                   -> Bearer

An absent credential is a STATE, not a failure: the collector writes an empty
listing with a named degradation and exits 0, because launchd would otherwise
retry a missing file forever.

    scan_bitbucket.py store/raw/bitbucket.json
"""
from __future__ import annotations
import base64, json, pathlib, stat, sys, urllib.error, urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402
import osprivacy  # noqa: E402
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import listing_guard  # noqa: E402
import local_scan  # noqa: E402

SECRET = paths.source_path("secret_store", paths.SECRETS) / 'bitbucket'
API = "https://api.bitbucket.org/2.0/repositories/"
FIELDS = ("full_name", "description", "is_private", "language", "updated_on",
          "created_on", "size", "has_issues", "has_wiki")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def workspaces() -> list[str]:
    """Derived from what the machine actually points at, never hardcoded."""
    # `paths.SCRATCH`, not `ROOT / "store" / "raw"`: the second is the one input a
    # test cannot redirect, which is the same defect the validator's registrar
    # export had and the survey's liveness file had after it.
    src = paths.SCRATCH / "local.json"
    if not src.is_file():
        return []
    found = set()
    for r in local_scan.folders(src):
        u = r.get("remote") or ""
        if "bitbucket.org" in u:
            tail = u.split("bitbucket.org", 1)[1].lstrip(":/")
            if "/" in tail:
                found.add(tail.split("/", 1)[0])
    return sorted(found)


def credential() -> tuple[str | None, str, str]:
    """(header value, how it was formed, why it is absent) — never the value."""
    if not SECRET.is_file():
        return None, "", (f"no credential at {SECRET}. Create a Bitbucket API token or "
                          f"app password, write it there as `username:token`, chmod 600.")
    if not osprivacy.private(SECRET):
        return None, "", (f"{SECRET} is mode {osprivacy.describe(SECRET).lstrip('0')} — readable beyond its owner. "
                          f"A credential that exposed is refused, not used.")
    raw = SECRET.read_text().strip()
    if not raw:
        return None, "", f"{SECRET} is empty"
    if ":" in raw:
        token = base64.b64encode(raw.encode()).decode()
        return f"Basic {token}", "basic (username:token)", ""
    return f"Bearer {raw}", "bearer (bare token)", ""


def fetch(url: str, auth: str) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, headers={
        "Authorization": auth, "Accept": "application/json",
        "User-Agent": "project-observatory-collector"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read() or b"{}"), ""
    except urllib.error.HTTPError as e:
        body = (e.read() or b"").decode(errors="replace")[:200]
        return e.code, {}, body
    except OSError as e:
        return 0, {}, str(e)


def list_workspace(ws: str, auth: str) -> tuple[list[dict], str]:
    repos: list[dict] = []
    url = f"{API}{ws}?pagelen=100&sort=-updated_on"
    while url:
        code, body, err = fetch(url, auth)
        if code != 200:
            return repos, (f"HTTP {code} listing {ws}"
                           + (f": {err}" if err else "")
                           + (" — the token has no read access to this workspace"
                              if code in (401, 403) else ""))
        for v in body.get("values", []):
            row = {k: v.get(k) for k in FIELDS}
            row["default_branch"] = (v.get("mainbranch") or {}).get("name", "")
            row["project"] = (v.get("project") or {}).get("name", "")
            repos.append(row)
        url = body.get("next")
    return repos, ""


def previous(dest: pathlib.Path) -> tuple[list[dict], str]:
    """The last listing and when it was taken, or ([], "") if there is none.

    Read before anything is written, because this collector rewrites the WHOLE
    document on every run: with no credential it wrote `repositories: []` and a
    named degradation, which is honest about the run and destructive about the
    estate. A workspace that was listed yesterday and 401s today would have gone
    from sixteen repositories to zero, with the reason recorded beside the hole.
    """
    if not dest.is_file():
        return [], ""
    try:
        doc = json.loads(dest.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return [], ""
    rows = doc.get("repositories")
    return (rows if isinstance(rows, list) else []), doc.get("scanned_at", "")


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("bitbucket"):
        print("bitbucket: not configured (integration disabled)")
        return 0
    if len(argv) < 2:
        sys.exit(__doc__)
    dest = pathlib.Path(argv[1])
    was, was_at = previous(dest)
    ws = workspaces()
    auth, how, why = credential()
    out = {"scanned_at": now(), "workspaces": ws, "auth": how,
           "repositories": [], "degraded": []}

    if not ws:
        out["degraded"].append({"source": "bitbucket",
                                "reason": "no local clone points at bitbucket.org, "
                                          "so no workspace to list"})
    elif auth is None:
        # Named per workspace: a host reading `degraded` must be able to tell
        # WHICH surface is dark, not merely that something is.
        for w in ws:
            out["degraded"].append({"source": f"bitbucket:{w}", "reason": why})
    else:
        for w in ws:
            repos, err = list_workspace(w, auth)
            out["repositories"].extend(repos)
            if err:
                out["degraded"].append({"source": f"bitbucket:{w}", "reason": err})

    # The same rule GitHub applies, in the shape this document has: one file
    # holding both the rows and the degradations, so the previous rows are
    # CARRIED FORWARD rather than left unwritten. `stale_since` says out loud
    # that they were measured by an earlier run — an answer that is older beats
    # one that is wrong, but only if it admits its age.
    reason = listing_guard.empty_would_lose(
        "bitbucket", len(was), len(out["repositories"]),
        f"Delete {dest.name} by hand if the workspaces really are empty.")
    if reason:
        out["repositories"] = was
        out["stale_since"] = was_at
        out["degraded"].append({"source": "bitbucket", "reason": reason})

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"bitbucket: {len(out['repositories'])} repo(s) across {len(ws)} workspace(s) "
          f"-> {dest}" + (f" (carried forward from {was_at})" if reason else ""))
    for d in out["degraded"]:
        print(f"  degraded {d['source']}: {d['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
