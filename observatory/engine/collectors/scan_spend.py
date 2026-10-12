#!/usr/bin/env python3
"""This month's provider spend across every account the doors hold — the estate total the L0 limit is about.

WHY. The meter (meter.py, OBS-55) holds its own reservations, but most spend today happens on keys whose callers
never ask it. L0 — the estate's monthly total across every provider key, USD 1,000 by default (operator,
2026-10-11) — is only meaningful against what the providers actually charged. Each OpenRouter account the door
holds a provisioning key for (`tools/openrouter.py stash`) answers `GET /api/v1/activity` with its daily spend;
this sums the current UTC month per account (OBS-56).

WHAT IT RECORDS. Per account: the door's label, the month's USD, the number of activity rows and the last day they
cover (the provider's activity lags about a day). Never a key, never a model's prompt, never a per-key breakdown.
An account that does not answer is named in `degraded` with its HTTP status, and the total says it is partial.

    scan_spend.py store/raw/spend.json
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import atomic  # noqa: E402
import paths  # noqa: E402

API = "https://openrouter.ai/api/v1"


def activity(key: str) -> list[dict]:
    req = urllib.request.Request(f"{API}/activity", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("data", []) or []


def month_of(rows: list[dict], month: str) -> dict:
    this = [x for x in rows if str(x.get("date", "")).startswith(month)]
    days = sorted({str(x.get("date"))[:10] for x in this})
    return {"usd": round(sum(float(x.get("usage") or 0) for x in this), 6), "rows": len(this),
            "through": days[-1] if days else None}


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("openrouter"):
        print("spend: not configured (integration openrouter disabled)")
        return 0
    out_path = pathlib.Path(argv[1]) if len(argv) > 1 else paths.SCRATCH / "spend.json"
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    import openrouter
    accounts, degraded = [], []
    try:
        stashed = openrouter.admins()
    except (OSError, RuntimeError) as exc:
        stashed = []
        degraded.append({"source": "spend:openrouter", "reason": f"the door's account store: {exc}"})
    for label, _path in stashed:
        try:
            _label, key = openrouter.read_admin(label)
        except (OSError, RuntimeError, ValueError) as exc:
            degraded.append({"source": f"spend:openrouter:{label}", "reason": f"unreadable: {type(exc).__name__}"})
            continue
        try:
            rows = activity(key)
        except urllib.error.HTTPError as exc:
            degraded.append({"source": f"spend:openrouter:{label}", "reason": f"GET /activity HTTP {exc.code}"})
            continue
        except OSError as exc:
            degraded.append({"source": f"spend:openrouter:{label}", "reason": f"unreachable: {type(exc).__name__}"})
            continue
        finally:
            key = None
        accounts.append({"provider": "openrouter", "account": label, "month": month, **month_of(rows, month)})
    total = round(sum(a["usd"] for a in accounts), 6)
    atomic.write_json(out_path, {
        "schema_version": 1, "scanned_at": now.isoformat(), "month": month, "accounts": accounts,
        "total_usd": total, "partial": bool(degraded), "degraded": degraded,
    }, indent=1)
    print(f"spend.json: {month} ${total:.2f} across {len(accounts)} account(s)"
          + (f", {len(degraded)} not read" if degraded else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
