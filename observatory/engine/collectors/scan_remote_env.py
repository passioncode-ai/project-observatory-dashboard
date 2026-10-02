#!/usr/bin/env python3
"""What production is configured with — names, classes and salted fingerprints.

    scan_remote_env.py store/raw/remote-env.json [--force]

WHY. Every other inventory here answers a question about this machine. The one
question none of them could answer is the one an operator actually asks before
touching anything: **what is production running on, and is it the same thing I
have locally?** A variable renamed on Heroku and never renamed in the checkout,
a key rotated in the vault and never redeployed, a `.env` holding the same value
production holds — none of those is visible from any file on this disk, and the
first two are exactly the kind of drift that causes an outage.

WHAT LEAVES THIS PROCESS, AND WHAT DOES NOT. Heroku's config-vars endpoint
answers with names AND values in one object; there is no names-only form of it.
So the values are TRANSIT: they exist in this process long enough to be
classified and fingerprinted, and nothing else. What is written is the NAME, the
CLASS, and — for a secret — a fingerprint salted with the same machine-local
salt `collectors/scan_env.py` uses, which is what makes "production holds the
same value as this checkout" measurable without either value being recorded.

The fingerprints live HERE, in `store/raw/`, which is gitignored. The registry
document built from this file (`registry/remote-env.json`, which IS in git)
carries only the VERDICT — same, different, only there, only here — because a
fingerprint is a lookup against every table anybody has ever built the moment
the salt leaks, and a registry in git is exactly where that would be found.

WHY IT IS GATED TO ONCE A DAY. Each run pulls every production secret of every
application into one process. Doing that hourly multiplies the exposure and
learns nothing: config vars change on deploys, not on ticks. `--force` is for
the moment after a rotation, when the answer has actually changed.

THE SCAN NAMES ITS SALT. Every scan records a `fingerprint_namespace` derived
from the salt, so a reader can tell whether two sets of fingerprints are
comparable without either side revealing the salt. A cached scan made under a
different salt is never compared against, however fresh it is; it is left in
place and reported, and `--force` re-reads production under the current one.

THE RETIRED VALUES ARE FINGERPRINTED HERE TOO, from the vault's own
`.retired-<stamp>` archives, and for one reason: "production is still running
the key we rotated away" is the single most expensive thing this inventory can
find, and it cannot be found without both halves. Same salt, same file, same
rule: fingerprints only, never a value.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                       # noqa: E402
import paths                                                        # noqa: E402

#: Re-fetching sooner than this learns nothing and re-exposes everything.
MAX_AGE_HOURS = 24
#: A bounded refusal: an estate of sixty applications is the size this was
#: measured on, and a run that would walk a thousand is a configuration
#: accident rather than a scan.
MAX_APPS = 400


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_since(stamp: str) -> float | None:
    try:
        t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def fingerprint(pepper: str, value: str) -> str:
    """The same derivation `collectors/scan_env.py` uses, so the two inventories
    can be compared at all. Sixteen hex characters of a salted SHA-256."""
    return hashlib.sha256((pepper + value).encode("utf-8")).hexdigest()[:16]


def retired_values(pepper: str, store: pathlib.Path) -> list[dict]:
    """Every value the vault has rotated away, as a fingerprint and its slot.

    `tools/vault.py rotate` keeps the old value beside the slot as
    `<NAME>.retired-<stamp>` at mode 600 — deliberately, so a rotation can be
    undone and so the operator can revoke the old value at its provider. That
    archive is also the only way to ask whether production is still holding it.
    """
    out: list[dict] = []
    if not store.is_dir():
        return out
    for slot in sorted(store.rglob("*.retired-*")):
        if not slot.is_file():
            continue
        rel = slot.relative_to(store).parts
        if len(rel) != 3:
            continue                                  # not a project/env/NAME slot
        name, _, stamp = rel[2].partition(".retired-")
        try:
            value = slot.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not value:
            continue
        # `tools/vault.py` names the archive with a UTC timestamp whose leading
        # ten characters are the date (`YYYY-MM-DD…`). A stamp of another shape
        # yields no date rather than a truncated one: a partial string read as
        # a date is worse than saying nothing.
        on = stamp[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp[:10]) else ""
        out.append({"project": rel[0], "env": rel[1], "name": name,
                    "retired_on": on, "fingerprint": fingerprint(pepper, value)})
    return out


def current_values(pepper: str, store: pathlib.Path) -> list[dict]:
    """Every value the vault holds now, as a fingerprint and its slot (PB-129).

    The same shape and the same salt as `retired_values`, so the registry step
    can tell a production variable that holds a slot's current value (that slot
    is read at run time) from one that holds a retired value. Metadata, archives
    and temporary files beside a slot are not values.
    """
    out: list[dict] = []
    if not store.is_dir():
        return out
    for slot in sorted(store.rglob("*")):
        if not slot.is_file() or slot.name.endswith((".meta.json", ".tmp")) or ".retired-" in slot.name:
            continue
        rel = slot.relative_to(store).parts
        if len(rel) != 3:
            continue                                  # not a project/env/NAME slot
        try:
            value = slot.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            out.append({"project": rel[0], "env": rel[1], "name": rel[2], "fingerprint": fingerprint(pepper, value)})
    return out


def scan_heroku_apps(pepper: str) -> tuple[list[dict], list[dict]]:
    """(one record per application, degradations). Values never leave this."""
    hk = _load("collectors/scan_heroku.py", "scan_heroku")
    env = _load("collectors/scan_env.py", "scan_env")
    tok, why = hk.token()
    if not tok:
        return [], [{"source": "heroku", "reason": why or "no token",
                     "effect": "production configuration is unknown, not empty"}]
    raw = paths.SCRATCH / "heroku.json"
    try:
        apps = json.loads(raw.read_text(encoding="utf-8")).get("apps", [])
    except (OSError, ValueError) as exc:
        return [], [{"source": "heroku", "reason": f"{type(exc).__name__} reading {raw.name}",
                     "effect": "no application list, so nothing to ask about; "
                               "run `project-observatory full heroku` first"}]
    if len(apps) > MAX_APPS:
        return [], [{"source": "heroku", "reason": f"{len(apps)} applications is past "
                                                   f"the {MAX_APPS} this scan will walk",
                     "effect": "refused rather than run for an hour"}]
    rows, degraded = [], []
    for app in apps:
        got = hk.get(f"{hk.API}/apps/{app['id']}/config-vars", tok)
        if not isinstance(got, dict) or "__error__" in got:
            reason = (got or {}).get("__error__", "unreadable") if isinstance(got, dict) else "unreadable"
            rows.append({"app": app["name"], "vars": [], "error": str(reason)[:120]})
            continue
        variables = []
        for name, value in sorted(got.items()):
            value = "" if value is None else str(value)
            cls = env.classify(name, value)
            row = {"name": name, "class": cls, "empty": not value}
            if cls == "secret" and value:
                row["fingerprint"] = fingerprint(pepper, value)
            variables.append(row)
        rows.append({"app": app["name"],
                     "folders": [str(f) for f in (app.get("local_folders") or [])],
                     "vars": variables, "error": None})
        # The values in `got` go out of scope here, and nothing above kept one.
    bad = [r for r in rows if r["error"]]
    if bad:
        degraded.append({"source": "heroku config-vars",
                         "reason": f"{len(bad)} application(s) would not answer",
                         "effect": "their production configuration is unknown, not empty"})
    return rows, degraded


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("remote_env"):
        print("remote_env: not configured (integration disabled)")
        return 0
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="where to write the scan (gitignored)")
    ap.add_argument("--force", action="store_true",
                    help=f"re-fetch even if the last scan is under {MAX_AGE_HOURS}h old")
    a = ap.parse_args(argv[1:])
    out = pathlib.Path(a.out)

    # THE SALT IS RESOLVED BEFORE THE CACHE IS CONSULTED. Whether a cached scan
    # is still usable depends on which salt made its fingerprints: a scan taken
    # under another salt (or before namespaces were recorded) cannot be compared
    # with this machine's local inventory, however fresh it is. An unusable salt
    # is not a reason to crash either — the cached file is left alone and the
    # message says why, so the next step degrades rather than compares garbage.
    env = _load("collectors/scan_env.py", "scan_env")
    pepper: str | None = None
    identity_error = ""
    try:
        pepper = env.salt()
    except (SystemExit, env.IdentityError) as exc:
        identity_error = str(exc)
    here = env.namespace(pepper) if pepper else None

    if not a.force and out.is_file():
        try:
            cached = json.loads(out.read_text(encoding="utf-8"))
            age = hours_since(cached.get("scanned_at", ""))
        except (OSError, ValueError):
            cached, age = {}, None
        if age is not None and age < MAX_AGE_HOURS:
            theirs = cached.get("fingerprint_namespace")
            if here is None:
                print(f"remote-env: {age:.1f}h old, and the salt that would name its "
                      f"fingerprints is unusable — {identity_error} The cached scan is "
                      f"left untouched and nothing is compared against it.")
            elif theirs == here:
                print(f"remote-env: {age:.1f}h old, not re-fetched — config vars change on "
                      f"deploys, and every fetch pulls every production secret into one "
                      f"process. `--force` after a rotation.")
            else:
                why = ("carries no fingerprint namespace, so it was taken before one was "
                       "recorded" if not theirs else
                       "was taken under a different fingerprint salt")
                print(f"remote-env: {age:.1f}h old and {why}. "
                      "Its fingerprints cannot be compared with this machine's. "
                      "No verdict is derived from them and the file is left as it is. "
                      "Re-read production with `--force` when you want the comparison "
                      "back — each run pulls every production secret into one process, "
                      "so it stays a decision.")
            return 0

    if pepper is None:
        print(f"remote-env: the fingerprint salt is unusable — {identity_error}",
              file=sys.stderr)
        return 1
    rows, degraded = scan_heroku_apps(pepper)
    vault = _load("tools/vault.py", "vault_mod")
    retired = retired_values(pepper, vault.STORE)
    current = current_values(pepper, vault.STORE)
    doc = {"scanned_at": now(), "provider": "heroku",
           # The namespace names the salt these fingerprints were made under,
           # without revealing it. The registry step compares it with the local
           # scan's and refuses to derive verdicts when they differ, because
           # fingerprints under different salts never match.
           "fingerprint_namespace": env.namespace(pepper),
           "apps": rows,
           "retired": retired, "current": current, "degraded": degraded,
           "note": "names, classes and salted fingerprints; never a value. "
                   "This file is gitignored; registry/remote-env.json carries "
                   "only the verdicts derived from it."}
    atomic.write_json(out, doc)
    secrets = sum(1 for r in rows for v in r["vars"] if v["class"] == "secret")
    print(f"remote-env: {len(rows)} application(s), "
          f"{sum(len(r['vars']) for r in rows)} variable(s), {secrets} read as secret, "
          f"{len(retired)} retired value(s) from the vault")
    for d in degraded:
        print(f"  degraded {d['source']}: {d['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
