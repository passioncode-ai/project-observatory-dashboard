#!/usr/bin/env python3
"""Every Google surface this machine's service accounts can see.

    scan_google.py store/raw/google.json [--force] [--max-age-hours N]

WHY. The estate's traffic lives in Google Analytics and its search visibility in
Search Console, and until now the observatory read FIVE properties — the five a
hand-written mapping file named. The operator had meanwhile added one service
account as an editor to every analytics account they own, so the honest number
was thirty-eight across four accounts: thirty-three properties were invisible to
a system whose whole job is to say what exists (measured 2026-09-14).

WHAT IT READS, AND WHAT IT NEVER WRITES. One pass per credential:

  * `accountSummaries` — accounts and properties, with their display names;
  * `dataStreams` per property — the web hosts and the app bundle ids the
    property itself declares. This is what makes the project match MEASURED
    rather than declared: a web stream's host goes through the same
    `plugins/hostmap.py` join the rest of the estate uses;
  * one 30-day report per property — active users, sessions, views. Thirty days
    because the question is "is this alive", and a day's number on a small
    property is noise;
  * Search Console's site list per credential.

The credential is read to sign a JWT and never leaves this process; what is
written is the account's own `client_email` — a public identifier, the thing an
operator grants and revokes — never the key, never a token, never a raw
response body.

WHY IT IS CACHED. Thirty-eight properties is seventy-six HTTPS calls and about
a minute; a dashboard that asked Google on every build would be slow, rate
limited, and no more correct — analytics settle daily. The scan refuses to
re-fetch inside the age window and says so; `--force` is the refresh button's
path, and `project-observatory full google --force` is the operator's.
"""
from __future__ import annotations
import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))
import paths                                            
import atomic                                                                   
import google_auth                                                              

#: Analytics settle daily; a shorter window buys nothing and costs a minute.
MAX_AGE_HOURS = 12
#: A bounded refusal: four accounts and thirty-eight properties is the size this
#: was measured on. A run that would walk a thousand is a misconfiguration.
MAX_PROPERTIES = 500
GA_READ = "https://www.googleapis.com/auth/analytics.readonly"
GSC_READ = "https://www.googleapis.com/auth/webmasters.readonly"
ADMIN = "https://analyticsadmin.googleapis.com/v1beta"
DATA = "https://analyticsdata.googleapis.com/v1beta"
GSC = "https://searchconsole.googleapis.com/webmasters/v3"
#: Where this machine keeps the credentials collectors authenticate with.
SECRET_STORE = paths.source_path("secret_store", paths.SECRETS)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_since(stamp: str) -> float | None:
    try:
        t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def _reason(exc: urllib.error.HTTPError) -> str:
    """The provider's own sentence, cut to a line and stripped of anything that
    is not prose. A body is never carried whole: it can echo a request header."""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:                                                             
        return f"HTTP {exc.code}"
    import re
    m = re.search(r'"message"\s*:\s*"([^"]{0,240})', body)
    return f"HTTP {exc.code}: {m.group(1)}" if m else f"HTTP {exc.code}"


def get(url: str, tok: str, *, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, method="POST" if payload is not None else "GET",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": f"Bearer {tok}",
                 **({"Content-Type": "application/json"} if payload is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"__error__": _reason(exc)}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"__error__": f"{type(exc).__name__}"}


def paged(url: str, tok: str, key: str) -> tuple[list, str | None]:
    out, page = [], None
    while True:
        u = url + ("&" if "?" in url else "?") + "pageSize=200" + (f"&pageToken={page}" if page else "")
        d = get(u, tok)
        if "__error__" in d:
            return out, d["__error__"]
        out += d.get(key) or []
        page = d.get("nextPageToken")
        if not page:
            return out, None


def credentials() -> list[dict]:
    """Every Google service account this machine holds, as facts.

    `client_email` is the identifier an operator grants and revokes in the
    Google console; `project_id` is the Cloud project whose APIs it is billed
    and enabled against, which is what an `accessNotConfigured` refusal is
    about. Neither is a secret; the key beside them is never read for anything
    but signing.
    """
    out = []
    for f in sorted(SECRET_STORE.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("type") != "service_account":
            continue
        out.append({"file": f.name, "client_email": doc.get("client_email"),
                    "cloud_project": doc.get("project_id")})
    return out


def streams_of(prop: str, tok: str) -> tuple[list[str], list[str], str | None]:
    """(web hosts, app ids, error) a property declares for itself."""
    rows, err = paged(f"{ADMIN}/{prop}/dataStreams", tok, "dataStreams")
    hosts, apps = [], []
    for s in rows:
        w = (s.get("webStreamData") or {}).get("defaultUri")
        if w:
            host = w.split("//", 1)[-1].split("/", 1)[0].lower()
            if host and host not in hosts:
                hosts.append(host)
        for k in ("androidAppStreamData", "iosAppStreamData"):
            v = (s.get(k) or {}).get("packageName") or (s.get(k) or {}).get("bundleId")
            if v and v not in apps:
                apps.append(v)
    return hosts, apps, err


def window(prop: str, tok: str) -> dict:
    """Thirty days of a property, as three numbers. Never a dimension."""
    d = get(f"{DATA}/{prop}:runReport", tok, payload={
        "dateRanges": [{"startDate": "30daysAgo", "endDate": "yesterday"}],
        "metrics": [{"name": "activeUsers"}, {"name": "sessions"},
                    {"name": "screenPageViews"}]})
    if "__error__" in d:
        return {"error": d["__error__"]}
    rows = d.get("rows") or []
    if not rows:
        # NO ROWS IS NOT ZERO USERS in every case — a property with no stream
        # ever installed answers the same way as one nobody visited. Both are
        # written as measured zero and the stream list beside it says which.
        return {"users_30d": 0, "sessions_30d": 0, "views_30d": 0, "empty": True}
    v = [m.get("value") for m in rows[0].get("metricValues") or []]
    def n(i):
        try:
            return int(v[i])
        except (IndexError, TypeError, ValueError):
            return None
    return {"users_30d": n(0), "sessions_30d": n(1), "views_30d": n(2)}


def scan_analytics(cred: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(accounts, properties, degradations) for one credential."""
    key = SECRET_STORE / cred["file"]
    try:
        tok = google_auth.access_token(key, GA_READ)
    except Exception as exc:                                                      
        return [], [], [{"source": f"ga4 via {cred['client_email']}",
                         "reason": f"the token exchange failed: {type(exc).__name__}",
                         "effect": "this credential's analytics are unknown, not empty"}]
    summaries, err = paged(f"{ADMIN}/accountSummaries", tok, "accountSummaries")
    if err:
        return [], [], [{"source": f"ga4 via {cred['client_email']}", "surface": "ga4",
                         "reason": err,
                         "effect": "no account list, so no property is known through this credential",
                         "remedy": (f"enable the Google Analytics Admin and Data APIs in Cloud "
                                    f"project {cred.get('cloud_project')}, or leave this "
                                    f"credential to the surfaces it is set up for")}]
    accounts, props, degraded = [], [], []
    for a in summaries:
        ps = a.get("propertySummaries") or []
        accounts.append({"account": a.get("account"), "name": a.get("displayName"),
                         "properties": len(ps), "read_with": cred["client_email"]})
        for p in ps:
            props.append({"property": p.get("property"), "name": p.get("displayName"),
                          "account": a.get("account"), "account_name": a.get("displayName"),
                          "read_with": cred["client_email"]})
    if len(props) > MAX_PROPERTIES:
        return accounts, [], [{"source": "ga4", "reason": f"{len(props)} properties is past "
                               f"the {MAX_PROPERTIES} this scan will walk",
                               "effect": "refused rather than run for an hour"}]
    for p in props:
        hosts, apps, serr = streams_of(p["property"], tok)
        p["hosts"], p["app_ids"] = hosts, apps
        if serr:
            p["streams_error"] = serr
        p.update(window(p["property"], tok))
    bad = [p for p in props if p.get("error")]
    if bad:
        degraded.append({"source": "ga4 runReport",
                         "reason": f"{len(bad)} property(ies) would not report",
                         "effect": "their traffic is unknown here, not zero"})
    return accounts, props, degraded


def scan_search_console(cred: dict) -> tuple[list[dict], list[dict]]:
    key = SECRET_STORE / cred["file"]
    try:
        tok = google_auth.access_token(key, GSC_READ)
    except Exception as exc:                                                      
        return [], [{"source": f"search console via {cred['client_email']}",
                     "reason": f"the token exchange failed: {type(exc).__name__}",
                     "effect": "this credential's Search Console properties are unknown"}]
    d = get(f"{GSC}/sites", tok)
    if "__error__" in d:
        # THE COMMONEST REFUSAL HERE IS NOT ABOUT PERMISSION. A Cloud project
        # with the Search Console API disabled answers 403 `accessNotConfigured`
        # — which reads as "no access to the sites" and is actually "nobody
        # enabled the API", a one-click operator fix. Carrying Google's own
        # sentence is the difference between the two.
        return [], [{"source": f"search console via {cred['client_email']}",
                     "surface": "search_console",
                     "reason": d["__error__"],
                     "effect": "no Search Console site is known through this credential",
                     "remedy": (f"enable the Search Console API in Cloud project "
                                f"{cred.get('cloud_project')}, or grant this account a "
                                f"property in Search Console")}]
    return [{"site": s.get("siteUrl"), "permission": s.get("permissionLevel"),
             "read_with": cred["client_email"]} for s in d.get("siteEntry") or []], []


#: Google's own words for an API switched off in a credential's Cloud project.
DISABLED_API = ("has not been used in project", "accessNotConfigured", "SERVICE_DISABLED")


def api_disabled(reason: str) -> bool:
    return any(marker in str(reason) for marker in DISABLED_API)


def sort_refusals(degraded: list[dict], readers: dict[str, list[str]]) -> tuple[list[dict], list[dict]]:
    """(degraded, not_applicable): an API switched off where another credential reads that surface.

    Every service account is asked about every surface, and each one is usually
    set up for one of them. Measured on one estate: two analytics accounts read
    72 properties and one Search Console account read its site, and each was
    reported "degraded" on the OTHER surface, because its Cloud project never
    enabled that API. Those refusals hide no data that the estate is reading —
    the surface is read through the other credential — so they are not
    applicable, and say who reads the surface instead. When NO credential reads
    a surface, the refusals are the whole story and stay degraded. A refusal for
    any other reason (a permission, a failed exchange) always stays degraded.
    """
    keep, notes = [], []
    for d in degraded:
        surface = d.get("surface")
        others = [r for r in readers.get(surface or "", []) if r not in str(d.get("source"))]
        if surface and others and api_disabled(d.get("reason", "")):
            shown = ", ".join(others[:2]) + (f" and {len(others) - 2} more" if len(others) > 2 else "")
            notes.append({**d, "reason": (f"{d.get('reason')} — this credential is not set up for "
                                          f"{surface.replace('_', ' ')}, which is read through {shown}")})
        else:
            keep.append(d)
    return keep, notes


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("google"):
        print("google: not configured (integration disabled)")
        return 0
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--force", action="store_true", help="re-fetch inside the age window")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS)
    a = ap.parse_args(argv[1:])
    out = pathlib.Path(a.out)
    if not a.force and out.is_file():
        try:
            age = hours_since(json.loads(out.read_text(encoding="utf-8")).get("scanned_at", ""))
        except (OSError, ValueError):
            age = None
        if age is not None and age < a.max_age_hours:
            print(f"google: {age:.1f}h old, not re-fetched — analytics settle daily. "
                  f"`--force` to refresh now.")
            return 0

    creds = credentials()
    if not creds:
        atomic.write_json(out, {"scanned_at": now(), "credentials": [], "accounts": [],
                                "properties": [], "search_console": [],
                                "degraded": [{"source": "google", "reason": "no service account "
                                              f"in {SECRET_STORE}", "effect": "nothing to ask"}]})
        print(f"google: no service account in {SECRET_STORE} — nothing to ask")
        return 0

    accounts, properties, sites, degraded = [], [], [], []
    readers: dict[str, list[str]] = {"ga4": [], "search_console": []}
    for c in creds:
        acc, props, deg = scan_analytics(c)
        accounts += acc
        properties += props
        degraded += deg
        # A READER is a credential that read something there, not merely one that
        # was not refused: an empty answer covers nothing another one missed.
        if acc and not any(d.get("surface") == "ga4" for d in deg):
            readers["ga4"].append(c["client_email"])
        s, deg2 = scan_search_console(c)
        sites += s
        degraded += deg2
        if s and not deg2:
            readers["search_console"].append(c["client_email"])
    degraded, not_applicable = sort_refusals(degraded, readers)
    properties.sort(key=lambda p: -(p.get("users_30d") or 0))
    atomic.write_json(out, {"scanned_at": now(), "credentials": creds,
                            "accounts": accounts, "properties": properties,
                            "search_console": sites, "degraded": degraded,
                            "not_applicable": not_applicable,
                            "note": "Names, ids, hosts and three numbers per property. "
                                    "The credential signs a JWT and never leaves the "
                                    "process; what is recorded is the account's public "
                                    "client_email."})
    users = sum(p.get("users_30d") or 0 for p in properties)
    print(f"google: {len(creds)} service account(s), {len(accounts)} analytics account(s), "
          f"{len(properties)} property(ies), {users:,} active user(s) over 30 days, "
          f"{len(sites)} Search Console site(s)")
    for d in degraded:
        print(f"  degraded {d['source']}: {d['reason'][:120]}")
    for d in not_applicable:
        print(f"  not applicable {d['source']}: {d['reason'][:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
