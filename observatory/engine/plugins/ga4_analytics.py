#!/usr/bin/env python3
"""GA4 sessions and active users per property, attributed to projects.

Same service account as the Search Console plugin — one Google credential on
this machine, two readers. GA4 adds one thing GSC does not need: a MAPPING
FILE, because properties are numeric ids that no registry host can be derived
from. The mapping is the workspace's `config/ga4_properties.json`, which `full
init` seeds from the shipped default `defaults/ga4_properties.json` (an empty
list). Add one row per property:

    {"properties": [
        {"property": "properties/<PROPERTY_ID>", "host": "example.com"},
        {"property": "properties/<OTHER_PROPERTY_ID>", "project": "project:example-app"}
    ]}

`host` goes through `plugins/hostmap.py` (ownership follows the registry);
`project` pins it outright for a property whose site the registry does not
serve. Grant the service account's client_email Viewer on each property.

The window is the last complete UTC day — GA4's intraday tables wobble, its
daily ones do not.
"""
from __future__ import annotations
import datetime
import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import paths                                            
import google_auth                                                              
import hostmap                                                                  

KEY_FILE = paths.source_path("secret_store", paths.SECRETS) / 'google-service-account.json'
# The workspace's config, never beside the plugins: the runner reads EVERY
# `plugins/*.json` as a manifest, so a data file there is reported as five
# missing manifest fields (measured 2026-09-12).
MAP_FILE = paths.config_file("ga4_properties.json")
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


def token() -> str:
    """A bearer token for this plugin's scope.

    Through `plugins/google_auth.py`, which does the JWT exchange over the
    standard library: the documented `google.auth.transport.requests.Request`
    needs `requests`, and a launchd tick would have reported that as a broken
    plugin (measured 2026-09-12).
    """
    return google_auth.access_token(KEY_FILE, SCOPE)


def _call(url: str, tok: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # url + status only; a header in an error is the 2026-09-12 leak again.
        raise RuntimeError(f"ga4 answered {e.code} for {url.split('?')[0]}") from None
    except OSError as e:
        raise RuntimeError(f"ga4 unreachable: {type(e).__name__}") from None


def yesterday(today: datetime.date | None = None) -> str:
    t = today or datetime.datetime.now(datetime.timezone.utc).date()
    return (t - datetime.timedelta(days=1)).isoformat()


def fetch_day(tok: str, prop: str, day: str) -> dict:
    d = _call(f"https://analyticsdata.googleapis.com/v1beta/{prop}:runReport", tok,
              {"dateRanges": [{"startDate": day, "endDate": day}],
               "metrics": [{"name": "sessions"}, {"name": "activeUsers"}]})
    row = (d.get("rows") or [{}])[0]
    vals = [v.get("value", "0") for v in row.get("metricValues", [])] or ["0", "0"]
    return {"sessions": float(vals[0]), "users": float(vals[1] if len(vals) > 1 else 0)}


def rows_from(mapping: list[dict], per_prop: dict[str, dict], day: str,
              table: dict[str, str]) -> tuple[list[dict], list[str]]:
    """(metric rows, unmappable entries) — pure, testable without Google."""
    rows, unmapped = [], []
    at = f"{day}T00:00:00Z"
    for m in mapping:
        prop = m.get("property", "")
        stats = per_prop.get(prop)
        if stats is None:
            continue
        pid = m.get("project") or hostmap.resolve(m.get("host", ""), table)
        if not pid:
            unmapped.append(prop)
            continue
        rows.append({"project_id": pid, "metric": "ga.sessions_1d", "at": at,
                     "value": stats["sessions"], "unit": "sessions",
                     "payload": {"property": prop}})
        rows.append({"project_id": pid, "metric": "ga.users_1d", "at": at,
                     "value": stats["users"], "unit": "users",
                     "payload": {"property": prop}})
    return rows, unmapped


def main() -> int:
    import configuration
    if not configuration.enabled("ga4"):
        print("integration ga4 disabled", file=sys.stderr)
        return 0
    mapping = json.loads(MAP_FILE.read_text(encoding="utf-8")).get("properties", [])
    if not mapping:
        print(f"{paths.config_label('ga4_properties.json')} declares no properties", file=sys.stderr)
        return 0
    tok = token()
    day = yesterday()
    per_prop = {m["property"]: fetch_day(tok, m["property"], day)
                for m in mapping if m.get("property")}
    rows, unmapped = rows_from(mapping, per_prop, day, hostmap.build())
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    if unmapped:
        print(f"unmappable properties ({len(unmapped)}): {', '.join(unmapped)} — "
              f"their mapping rows name neither a resolvable host nor a project — "
              f"outside the estate's projects until one adopts them",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
