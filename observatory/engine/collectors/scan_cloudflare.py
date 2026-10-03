#!/usr/bin/env python3
"""Every zone in every Cloudflare account the estate holds a token for.

    scan_cloudflare.py store/raw/cloudflare_zones.json

WHY A COLLECTOR AND NOT THE PLUGIN. `plugins/cloudflare_analytics.py` reads
traffic and drops what it cannot attribute; this reads POSSESSION — which
zones exist, in which account, on which plan, registered where — and keeps all
of it, because "what do we own" is a registry question before it is a traffic
one. Measured 2026-09-13: 89 zones across three accounts, of which the
transcribed domain registry knew 53 and no project claimed 79.

Tokens come from the same place the plugin reads (`cloudflare-analytics.d/`),
issued by `tools/cloudflare.py`; this collector never holds an admin token.
One account failing is reported in `degraded` and the others still land — the
rule every collector here follows.
"""
from __future__ import annotations
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))
import atomic                                                                   
import cloudflare_analytics as cf                                               


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def zone_rows(token: str, account_label: str) -> list[dict]:
    """Every zone the token sees, with the fields a registry can use."""
    out, page = [], 1
    while True:
        d = cf._call(f"/zones?per_page=50&page={page}", token)
        for z in d.get("result", []):
            acc = z.get("account") or {}
            out.append({
                "name": z["name"], "zone_id": z["id"],
                "account_id": acc.get("id"), "account_name": acc.get("name"),
                "account_label": account_label,
                "status": z.get("status"), "paused": bool(z.get("paused")),
                "type": z.get("type"),
                "plan": ((z.get("plan") or {}).get("legacy_id")
                         or (z.get("plan") or {}).get("name")),
                "original_registrar": z.get("original_registrar"),
                "name_servers": z.get("name_servers") or [],
                "created_on": (z.get("created_on") or "")[:10] or None,
                "modified_on": (z.get("modified_on") or "")[:10] or None,
                "records": dns_records(token, z["id"]),
            })
        info = d.get("result_info") or {}
        if page >= (info.get("total_pages") or 1):
            return out
        page += 1


def dns_records(token: str, zone_id: str) -> list[dict]:
    """A, AAAA and CNAME records — where the zone's names actually point.

    Needs `DNS Read` on the token; a token issued before that joined the preset
    answers 403 here, which is reported as an empty list plus a note rather than
    a dead zone, because the zone itself was read fine.
    """
    try:
        d = cf._call(f"/zones/{zone_id}/dns_records?per_page=200&type=A,AAAA,CNAME", token)
    except RuntimeError as exc:
        return [{"error": str(exc)[:120]}]
    return [{"name": r["name"], "type": r["type"], "content": r.get("content"),
             "proxied": bool(r.get("proxied"))}
            for r in d.get("result", []) if r.get("type") in ("A", "AAAA", "CNAME")]


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("cloudflare"):
        print("cloudflare: not configured (integration disabled)")
        return 0
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    accounts = cf.tokens()
    zones: list[dict] = []
    degraded: list[dict] = []
    seen: set[str] = set()
    for label, token in accounts:
        try:
            for z in zone_rows(token, label):
                if z["zone_id"] in seen:
                    continue
                seen.add(z["zone_id"])
                zones.append(z)
        except RuntimeError as exc:
            degraded.append({"source": f"cloudflare:{label}", "reason": str(exc)[:200]})
    if not accounts:
        degraded.append({"source": "cloudflare", "reason":
                         "no issued token — `python \"$(project-observatory full-path)/tools/cloudflare.py\" issue --preset analytics`"})
    zones.sort(key=lambda z: z["name"])
    atomic.write_json(argv[1], {"scanned_at": now(), "accounts": [l for l, _ in accounts],
                                "zones": zones, "degraded": degraded})
    by_acc: dict[str, int] = {}
    for z in zones:
        by_acc[z["account_label"]] = by_acc.get(z["account_label"], 0) + 1
    print(f"cloudflare: {len(zones)} zone(s) across {len(accounts)} account(s) — "
          + ", ".join(f"{k} {v}" for k, v in sorted(by_acc.items())))
    for d in degraded:
        print(f"  degraded {d['source']}: {d['reason'][:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
