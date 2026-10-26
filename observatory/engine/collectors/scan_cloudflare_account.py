#!/usr/bin/env python3
"""What every Cloudflare account the estate holds an inventory reader for runs (OBS-271).

    scan_cloudflare_account.py store/raw/cloudflare_account.json

WHY. `scan_cloudflare.py` reads which zones exist, for the domain registry. The Cloudflare
page asks the wider question an account's owner asks: which Workers, Pages projects, R2
buckets and D1 databases it holds, and for each zone its plan and price, how many DNS
records it carries and when its edge certificate expires. That needs rights the analytics
token must not have, so it reads through its own token.

CREDENTIALS. One token per account, minted by the door with the read-only preset
`inventory-read` (`tools/cloudflare.py issue --preset inventory-read --account <slug>`)
and filed in `cloudflare-inventory.d/<account>` with its record beside it. The value lives
in this process's memory for its requests and is never written: not to the raw file, not
to a log. The door's `_request` strips every error to an endpoint and a status.

DEGRADATION. Each failure is NAMED in `degraded` and the rest still lands: no reader
issued (the page then names the door's command), a token Cloudflare refuses (the whole
account), a group the token lacks (403: that listing, with the preset to issue again), the
step's deadline. A listing that failed is `null`, never an empty list: unread is not
"holds none".
"""
from __future__ import annotations

import pathlib
import re
import sys
import urllib.parse
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))
sys.path.insert(0, str(ROOT / "tools"))
import atomic  # noqa: E402
import private_io  # noqa: E402
import step_budget  # noqa: E402
import cloudflare as door  # noqa: E402

#: Seconds kept for writing the scan when the tick's deadline nears (OBS-40).
WRITE_RESERVE = 15
#: The command that connects an account, as the page and `degraded` print it.
CONNECT = ('python "$(project-observatory full-path)/tools/cloudflare.py" issue '
           '--preset inventory-read --account <account>')


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def readers() -> list[tuple[str, dict, str]]:
    """(label, record, value) for every inventory reader the door filed.

    A record without an account id cannot be read against anything and is skipped
    by the caller with a reason; a file that cannot be read is the caller's too."""
    store = door.INVENTORY_STORE
    if not store.is_dir():
        return []
    out = []
    for p in sorted(store.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.name.endswith(".meta.json"):
            continue
        out.append((p.name, door.read_meta(p), private_io.read(p).strip()))
    return out


def _call(path: str, token: str) -> dict:
    return door._request(path, token)


#: Pages a listing is followed through before it is called unconfirmed.
MAX_PAGES = 200


class Unconfirmed(list):
    """The rows of a listing whose end could not be confirmed, with the reason."""
    reason = ""


def _paged(path: str, token: str) -> list:
    """Every row of a page-numbered listing (zones, Pages projects, D1).

    Cloudflare's `result_info` and each of its fields are optional (its API reference for
    Pages projects and D1 databases, 2026-10-26: `count`, `page`, `per_page`, `total_count`,
    and `total_pages` for Pages; D1 documents no `total_pages`). So the end is `total_pages`
    when stated, else `total_count`, else the first short page. A listing whose end no
    count confirms comes back as `Unconfirmed`, and its caller names it in `degraded`."""
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    m = re.search(r"[?&]per_page=(\d+)", path)
    per_page = int(m.group(1)) if m else None
    total_count = total_pages = None
    while page <= MAX_PAGES:
        d = _call(f"{path}{sep}page={page}", token)
        res = d.get("result")
        rows = res if isinstance(res, list) else []
        out += rows
        info = d.get("result_info") if isinstance(d.get("result_info"), dict) else {}
        total_pages = info.get("total_pages") if isinstance(info.get("total_pages"), int) else total_pages
        total_count = info.get("total_count") if isinstance(info.get("total_count"), int) else total_count
        if not rows:
            break
        if total_pages is not None:
            more = page < total_pages
        elif total_count is not None:
            more = len(out) < total_count
        else:
            more = per_page is not None and len(rows) >= per_page
        if not more:
            break
        page += 1
    reason = ""
    if page > MAX_PAGES:
        reason = f"stopped after {MAX_PAGES} pages; more may exist"
    elif total_count is not None and len(out) < total_count:
        reason = f"{len(out)} of the {total_count} the provider counts were read"
    elif total_pages is None and total_count is None:
        reason = f"{len(out)} read; the provider stated no count, so the end was not confirmed"
    if reason:
        out = Unconfirmed(out)
        out.reason = reason
    return out


def out_of_time() -> bool:
    """The step's deadline is near enough that what was read must be written now (OBS-40)."""
    return step_budget.remaining() < WRITE_RESERVE


def worker_rows(account_id: str, token: str) -> list[dict]:
    res = _call(f"/accounts/{account_id}/workers/scripts", token).get("result") or []
    return [{"name": w.get("id"), "created_on": (w.get("created_on") or "")[:10] or None,
             "modified_on": (w.get("modified_on") or "")[:10] or None,
             "usage_model": w.get("usage_model")} for w in res if isinstance(w, dict)]


def _keep(listed: list, rows: list) -> list:
    """`rows`, still marked unconfirmed when the listing they came from was."""
    if isinstance(listed, Unconfirmed):
        rows = Unconfirmed(rows)
        rows.reason = listed.reason
    return rows


def pages_rows(account_id: str, token: str) -> list[dict]:
    out = []
    listed = _paged(f"/accounts/{account_id}/pages/projects?per_page=10", token)
    for p in listed:
        if not isinstance(p, dict):
            continue
        cfg = (p.get("source") or {}).get("config") or {}
        repo = f"{cfg['owner']}/{cfg['repo_name']}".lower() if cfg.get("owner") and cfg.get("repo_name") else None
        last = p.get("latest_deployment") or {}
        out.append({"name": p.get("name"), "subdomain": p.get("subdomain"),
                    "source_type": (p.get("source") or {}).get("type"),
                    "domains": p.get("domains") or [], "repository": repo,
                    "production_branch": p.get("production_branch"),
                    "last_deploy_on": (last.get("created_on") or "")[:10] or None,
                    "last_deploy_status": (last.get("latest_stage") or {}).get("status")})
    return _keep(listed, out)


def r2_rows(account_id: str, token: str) -> list[dict]:
    """Buckets, followed through R2's cursor rather than page numbers."""
    out, cursor = [], None
    for _ in range(100):
        q = (f"/accounts/{account_id}/r2/buckets?per_page=1000"
             + (f"&cursor={urllib.parse.quote(str(cursor), safe='')}" if cursor else ""))
        d = _call(q, token)
        res = d.get("result") or {}
        rows = res.get("buckets") if isinstance(res, dict) else res
        out += [{"name": b.get("name"), "created_on": (b.get("creation_date") or "")[:10] or None,
                 "location": b.get("location")} for b in rows or [] if isinstance(b, dict)]
        cursor = (d.get("result_info") or {}).get("cursor")
        if not cursor:
            break
    return out


def d1_rows(account_id: str, token: str) -> list[dict]:
    listed = _paged(f"/accounts/{account_id}/d1/database?per_page=100", token)
    return _keep(listed, [{"uuid": x.get("uuid"), "name": x.get("name"),
                           "created_on": (x.get("created_at") or "")[:10] or None,
                           "file_size": x.get("file_size"), "num_tables": x.get("num_tables")}
                          for x in listed if isinstance(x, dict)])


def certificate(zone_id: str, token: str) -> tuple[str | None, str | None]:
    """(earliest expiry among active edge certificates, the pack's status)."""
    packs = _call(f"/zones/{zone_id}/ssl/certificate_packs?status=all", token).get("result") or []
    soonest, status = None, None
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        status = status or pack.get("status")
        if pack.get("status") != "active":
            continue
        status = "active"
        for cert in pack.get("certificates") or []:
            at = (cert.get("expires_on") or "")[:10]
            if at and (soonest is None or at < soonest):
                soonest = at
    return soonest, status


def zone_rows(account_id: str, token: str, label: str, degraded: list[dict],
              state: dict | None = None) -> list[dict]:
    """Every zone with its plan, then its DNS count and certificate, one zone at a time. When
    the deadline nears, the zones left keep their listed fields, their details stay null, and
    `degraded` names them; `state["complete"]` turns False so the next tick reads again."""
    out = []
    listed = _paged(f"/zones?account.id={account_id}&per_page=50", token)
    _unconfirmed(listed, f"cloudflare-account:{label}/zones", degraded)
    late: list[str] = []
    for z in listed:
        if not isinstance(z, dict) or not z.get("id"):
            continue
        plan = z.get("plan") or {}
        row = {"zone_id": z["id"], "name": z.get("name"), "status": z.get("status"),
               "paused": bool(z.get("paused")),
               "plan": plan.get("legacy_id") or plan.get("name"),
               "plan_price": plan.get("price") if isinstance(plan.get("price"), (int, float)) else None,
               "plan_currency": plan.get("currency"), "plan_frequency": plan.get("frequency"),
               "dns_records": None, "certificate_expires_on": None, "certificate_status": None}
        if late or out_of_time():
            late.append(str(z.get("name")))
            out.append(row)
            continue
        try:
            info = _call(f"/zones/{z['id']}/dns_records?per_page=1", token).get("result_info") or {}
            count = info.get("total_count")
            row["dns_records"] = count if isinstance(count, int) else None
        except RuntimeError as exc:
            degraded.append({"source": f"cloudflare-account:{label}/dns:{z.get('name')}", "reason": str(exc)[:200]})
        try:
            row["certificate_expires_on"], row["certificate_status"] = certificate(z["id"], token)
        except RuntimeError as exc:
            degraded.append({"source": f"cloudflare-account:{label}/certificates:{z.get('name')}",
                             "reason": str(exc)[:200]})
        out.append(row)
    if late:
        if state is not None:
            state["complete"] = False
        degraded.append({"source": f"cloudflare-account:{label}/zones", "reason":
                         f"the step's deadline came first; DNS count and certificate not read for "
                         f"{len(late)} zone(s): {', '.join(late[:20])}" + (" ..." if len(late) > 20 else "")
                         + "; the next tick reads them again"})
    return out


def _unconfirmed(rows: list, source: str, degraded: list[dict]) -> None:
    if isinstance(rows, Unconfirmed):
        degraded.append({"source": source, "reason": rows.reason})


LISTINGS = (("workers", worker_rows), ("pages", pages_rows), ("r2", r2_rows), ("d1", d1_rows))


def read_account(label: str, meta: dict, value: str, degraded: list[dict],
                 state: dict | None = None) -> dict | None:
    """One account's holdings, or None when its record names no account."""
    account_id = meta.get("account_id")
    if not account_id:
        degraded.append({"source": f"cloudflare-account:{label}", "reason":
                         "the reader's record names no account; issue it again: " + CONNECT})
        return None
    acct = {"label": label, "account_id": account_id, "account_name": meta.get("account_name") or label,
            "token_expires_at": meta.get("expires_at")}
    state = state if state is not None else {}
    for i, (field, read) in enumerate(LISTINGS):
        if out_of_time():
            left = [f for f, _r in LISTINGS[i:]] + ["zones"]
            for f in left:
                acct[f] = None
            state["complete"] = False
            degraded.append({"source": f"cloudflare-account:{label}", "reason":
                             "the step's deadline came first; not read: " + ", ".join(left)
                             + "; the next tick reads them again"})
            return acct
        try:
            acct[field] = read(account_id, value)
            _unconfirmed(acct[field], f"cloudflare-account:{label}/{field}", degraded)
            acct[field] = list(acct[field])
        except RuntimeError as exc:
            acct[field] = None
            hint = (f"; the reader lacks this right, issue it again: {CONNECT}"
                    if "HTTP 403" in str(exc) else "")
            degraded.append({"source": f"cloudflare-account:{label}/{field}", "reason": str(exc)[:160] + hint})
            if "HTTP 401" in str(exc):
                # Cloudflare refuses the token itself: nothing else will read either.
                for rest, _r in LISTINGS[LISTINGS.index((field, read)) + 1:]:
                    acct[rest] = None
                acct["zones"] = None
                return acct
    try:
        acct["zones"] = zone_rows(account_id, value, label, degraded, state)
    except RuntimeError as exc:
        acct["zones"] = None
        degraded.append({"source": f"cloudflare-account:{label}/zones", "reason": str(exc)[:200]})
    return acct


def scan(degraded: list[dict], state: dict | None = None) -> list[dict]:
    """Every account's holdings. `state["complete"]` is False when the deadline cut the scan."""
    state = state if state is not None else {}
    state.setdefault("complete", True)
    accounts: list[dict] = []
    try:
        found = readers()
    except (OSError, RuntimeError, ValueError) as exc:
        degraded.append({"source": "cloudflare-account", "reason":
                         f"the inventory readers could not be read ({type(exc).__name__})"})
        return accounts
    if not found:
        degraded.append({"source": "cloudflare-account", "reason": "no inventory reader issued: " + CONNECT})
    for i, (label, meta, value) in enumerate(found):
        if not state["complete"] or out_of_time():
            state["complete"] = False
            degraded.append({"source": "cloudflare-account", "reason":
                             "the step's deadline came first; not read: " + ", ".join(x[0] for x in found[i:])
                             + "; the next tick reads them again"})
            break
        try:
            row = read_account(label, meta, value, degraded, state)
        except Exception as exc:  # noqa: BLE001  one account's surprise never costs the others their scan
            row = None
            degraded.append({"source": f"cloudflare-account:{label}",
                             "reason": f"the account could not be read ({type(exc).__name__}); details withheld"})
        if row:
            accounts.append(row)
    return accounts


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("cloudflare"):
        print("cloudflare-account: not configured (integration disabled)")
        return 0
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    degraded: list[dict] = []
    state: dict = {}
    accounts = scan(degraded, state)
    stamp = now()
    # `complete: false` makes the tick read again at its next run rather than a day later.
    atomic.write_json(argv[1], {"scanned_at": stamp, "scanned_on": stamp[:10], "accounts": accounts,
                                "complete": state.get("complete", True), "degraded": degraded})
    print(f"cloudflare-account: {len(accounts)} account(s): " + ", ".join(
        f"{a['label']} {len(a.get('workers') or [])} worker(s) {len(a.get('pages') or [])} pages "
        f"{len(a.get('r2') or [])} bucket(s) {len(a.get('d1') or [])} d1 {len(a.get('zones') or [])} zone(s)"
        for a in accounts))
    for d in degraded:
        print(f"  degraded {d['source']}: {d['reason'][:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
