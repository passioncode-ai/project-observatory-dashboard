#!/usr/bin/env python3
"""A credential about to expire, or expired, on the board before a consumer finds out.

WHY (OBS-191). Since the doors mint every credential with an expiry, 90 days by
default, a key that nobody rotates stops working on a date the estate already
knows. Recording that date is half the job; this is the other half, the row a
person sees while there is still time.

`credential.expiring`  the recorded expiry is within `WARN_DAYS` days: `warning`.
`credential.expired`   the recorded expiry has passed: `critical`, since whatever
                       reads it is already failing.

WHERE THE DATES COME FROM, metadata only, never a value:

  the vault slots   `projects/<project>/<env>/<NAME>.meta.json` (`expires_at`,
                    `expiry_by`: the door that recorded it, `vault.record_expiry`)
  door records      what reaches no vault slot: a Cloudflare plugin token's
                    `.meta.json` (found by its `issued_by`), an OpenRouter ledger row delivered to a named
                    destination, a DigitalOcean collector token's record

A credential delivered to a slot is read from the slot alone, so one key is one
row. Each row's action is the door's own rotate command; the date is the one the
record holds, compared with the day the findings are built.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys as _sys

_DASHBOARD = str(pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

#: Days before a recorded expiry at which the board says so.
WARN_DAYS = 14

TOOL = 'python "$(project-observatory full-path)/tools/{door}"'
SLOT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")


def _read(path: pathlib.Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _date(stamp) -> datetime.date | None:
    if not isinstance(stamp, str) or len(stamp) < 10:
        return None
    try:
        return datetime.date.fromisoformat(stamp[:10])
    except ValueError:
        return None


def slot_action(door: str | None, slot: str, ledgers: dict) -> str:
    """The rotate command of the door that minted a slot's value; no value in it."""
    project, env, name = slot.split("/")
    if door == "openrouter.py":
        names = [n for n, r in (ledgers.get("openrouter") or {}).items()
                 if isinstance(r, dict) and r.get("destination") == f"vault:{slot}"]
        return (f"`{TOOL.format(door='openrouter.py')} rotate {names[0] if names else '<name>'}` "
                "mints the successor with the key's lifetime and delivers it before the old one is deleted")
    if door == "heroku.py":
        return (f"`{TOOL.format(door='heroku.py')} issue` again with the slot's preset and `--vault {slot}`: "
                "it replaces an authorization whose lifetime differs and renews it")
    if door == "cloudflare.py":
        return (f"`{TOOL.format(door='cloudflare.py')} issue` again with the same preset and "
                f"`--vault {slot}`: a re-issue rolls the token and renews its expiry")
    if door == "digitalocean.py":
        labels = [n for n, r in (ledgers.get("digitalocean") or {}).items()
                  if isinstance(r, dict) and r.get("vault") == slot]
        return ("mint a new token in DigitalOcean's control panel, then pipe it to "
                f"`{TOOL.format(door='digitalocean.py')} rotate {labels[0] if labels else '<label>'} "
                "--expires-on YYYY-MM-DD`")
    return (f"replace the value through the door that minted it, or put a new one with "
            f"`{TOOL.format(door='vault.py')} rotate {project} {env} {name}`")


def _plugin_tokens(store: pathlib.Path) -> list[tuple[str, dict]]:
    """Plugin tokens the Cloudflare door issued, found by the record each carries
    (`issued_by`), never by a plugin's folder name: a core file that names a plugin
    is the coupling `tests/test_metric_series.py` forbids."""
    out = []
    if not store.is_dir():
        return out
    for folder in sorted(p for p in store.iterdir() if p.is_dir() and p.name != "projects"):
        for meta in sorted(folder.glob("*.meta.json")):
            doc = _read(meta)
            if doc.get("issued_by") == "tools/cloudflare.py" and doc.get("kind") == "issued":
                out.append((meta.name[:-len(".meta.json")], doc))
    return out


def gather(vault_root: pathlib.Path, store: pathlib.Path) -> list[dict]:
    """Every recorded expiry as `{subject, key, expires_at, action}`, slots first."""
    issued = _read(store / "openrouter-issued.json").get("issued")
    ledgers = {
        # A ledger of another shape is no ledger: one damaged file must not stop the board's build.
        "openrouter": issued if isinstance(issued, dict) else {},
        "digitalocean": {p.name[:-len(".meta.json")]: _read(p)
                         for p in sorted((store / "digitalocean-issued").glob("*.meta.json"))}
        if (store / "digitalocean-issued").is_dir() else {},
    }
    out: list[dict] = []
    if vault_root.is_dir():
        for meta in sorted(vault_root.glob("*/*/*.meta.json")):
            name = meta.name[:-len(".meta.json")]
            if not SLOT_NAME.fullmatch(name) or not meta.with_name(name).is_file():
                continue
            doc = _read(meta)
            if not doc.get("expires_at"):
                continue
            slot = f"{meta.parent.parent.name}/{meta.parent.name}/{name}"
            out.append({"subject": f"credential:vault/{slot}", "key": slot, "expires_at": doc["expires_at"],
                        "action": slot_action(doc.get("expiry_by"), slot, ledgers)})
    for label, doc in _plugin_tokens(store):
        if doc.get("expires_at"):
            out.append({"subject": f"credential:cloudflare/{label}", "key": f"cloudflare plugin token {label}",
                        "expires_at": doc["expires_at"],
                        "action": f"`{TOOL.format(door='cloudflare.py')} rotate {label}` rolls it and "
                                  "renews its expiry"})
    for name, row in sorted(ledgers["openrouter"].items()):
        if isinstance(row, dict) and row.get("expires_at") \
                and not str(row.get("destination") or "").startswith("vault:"):
            out.append({"subject": f"credential:openrouter/{name}", "key": f"OpenRouter key {name}",
                        "expires_at": row["expires_at"],
                        "action": f"`{TOOL.format(door='openrouter.py')} rotate {name}` mints the successor "
                                  "with the key's lifetime and delivers it before the old one is deleted"})
    for label, rec in sorted(ledgers["digitalocean"].items()):
        if isinstance(rec, dict) and rec.get("expires_at") and not rec.get("vault"):
            out.append({"subject": f"credential:digitalocean/{label}", "key": f"DigitalOcean token {label}",
                        "expires_at": rec["expires_at"],
                        "action": "mint a new token in DigitalOcean's control panel, then pipe it to "
                                  f"`{TOOL.format(door='digitalocean.py')} rotate {label} --expires-on YYYY-MM-DD`"})
    return out


def findings(entries: list[dict], today: str) -> list[dict]:
    """One row per credential whose recorded expiry is within `WARN_DAYS` days or past."""
    day = _date(today)
    if day is None:
        return []
    out = []
    for e in entries:
        at = _date(e.get("expires_at"))
        if at is None:
            continue
        left = (at - day).days
        if left > WARN_DAYS:
            continue
        expired = left < 0  # a key that expires later today still works today: a warning, not critical
        out.append({
            "type": "credential.expired" if expired else "credential.expiring",
            "subject": e["subject"],
            "severity": "critical" if expired else "warning",
            **(titled("{key} expired on {date}", key=e["key"], date=at.isoformat()) if expired
               else titled("{key} expires on {date}", key=e["key"], date=at.isoformat())),
            "detail": (f"The expiry recorded when it was minted is {e['expires_at']}"
                       + (", so whatever reads it is refused now." if expired
                          else f", {left} day(s) from {today}; whatever reads it stops working then.")
                       + " The date is the one recorded beside the credential (the provider's answer, or the date"
                       " typed in for a token minted by hand), never its value."),
            "action": e["action"],
        })
    return out


def from_store(vault_root: pathlib.Path, store: pathlib.Path, today: str) -> list[dict]:
    """The rule over this workspace's own stores. Absent stores give no rows, not a clean bill."""
    return findings(gather(vault_root, store), today)
