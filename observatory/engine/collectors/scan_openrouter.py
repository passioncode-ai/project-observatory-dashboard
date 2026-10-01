#!/usr/bin/env python3
"""Every OpenRouter key this account holds, as facts — and never as values.

WHY. Four destinations on this machine read an OpenRouter key and each learned
it separately: the tick, claude-mem, the gateway, and the provisioning key that
governs the rest. Nothing listed them together, so "which key is spending, what
is it capped at, and is any of them dead" took four commands and a guess — and
on 2026-09-12 the answer turned out to be that one had been dead for days while
a shell variable quietly shadowed a live one.

WHAT IT RECORDS, AND WHAT IT REFUSES TO.

  recorded   name, the provider's own label, limit, usage, reset,
             disabled, created — everything a person needs to decide
  refused    the key itself, and the provisioning hash

The hash is refused on purpose. It is not a credential — but it is the handle
`PATCH`/`DELETE` take, and this file's output feeds `registry/credentials.json`,
which is IN GIT. A handle that acts, committed to a repository, is a capability
in the wrong place. The raw file is gitignored and keeps it; the registry never
sees it, and `tools/keyserver.py` resolves a key by its tail against the raw
file at the moment it acts.

WHICH DESTINATION EACH KEY SERVES is answered by reading the four files and
comparing the PROVIDER'S OWN LABEL with the listing — never by transmitting or
storing what they hold. A destination whose key the account does not list is
reported: that is either a key from another workspace or a dead one, and both
are worth saying out loud.

    scan_openrouter.py store/raw/openrouter.json
"""
from __future__ import annotations
import json, pathlib, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic  # noqa: E402
import paths  # noqa: E402

API = "https://openrouter.ai/api/v1"
PAGE = 100
#: A ceiling on paging, not a cap on the estate: this account's other workspace
#: holds hundreds of `PRODUCTION_user_…` keys minted by a different product, and
#: walking all of them on every tick would be minutes of nothing. When the page
#: budget runs out the file says so rather than presenting a partial list as
#: whole.
MAX_PAGES = 12
#: How much further to look, ONCE, for a consumer's key the bounded listing did
#: not reach. The listing is newest first, and on one account another product
#: mints about three hundred keys a day, so a consumer's key a few weeks old sat
#: past key 7,400 — and was reported as "no longer exists" by a listing that had
#: stopped at 1,200. A key found this way is remembered by its hash and asked
#: about directly afterwards, so the deep walk is paid once per key, not per run.
DEEP_PAGES = 200

#: Where a key is read from, and by whom. The paths are facts about this
#: machine; `tools/install_key.py` owns the first three and the gateway's
#: `servers.yaml` the fourth.
DESTINATIONS = {
    "observatory": paths.STATE / ".openrouter-key",
    # The legacy location, when the state is redirected (PB-091): a key left there
    # is still a key on this machine and must be seen.
    **({"observatory-legacy": paths.STORE / ".openrouter-key"} if paths.STATE != paths.STORE else {}),
    "claude-mem": paths.source_path("companion_home", paths.HOME / "disabled/companion") / ".env",
    "gateway": paths.source_path("secret_store", paths.SECRETS) / 'openrouter',
    "provisioning": paths.source_path("secret_store", paths.SECRETS) / 'openrouter-provisioning',
}


#: THE PROVIDER'S OWN LABEL, derived locally. A listing entry carries the key's
#: prefix, the three characters after it AND the last three, and the same string
#: is computable from a key value without asking anything — which is why a local
#: key can be matched to a listing entry without ever sending the key.
#:
#: WHY NOT THE TAIL ALONE. Three hex characters collide by the birthday bound
#: across a large account, and a match on them alone gives a CONFIDENT WRONG
#: ANSWER — a stranger's key labelled as this project's. Six characters over the
#: same population is a different order of risk, and a collision is REPORTED
#: rather than resolved by picking one.
TAIL = 3


def label_of(value: str) -> str:
    """What the provider will call this key, computed without sending it."""
    value = (value or "").strip()
    if not value.startswith("sk-or-v1-") or len(value) < 16:
        return ""
    return "sk-or-v1-" + value[9:12] + "..." + value[-TAIL:]


def _label(text: str) -> str:
    """The label of a key found in a file. Never the key."""
    text = (text or "").strip()
    if not text:
        return ""
    if "OPENROUTER_API_KEY" in text:                      # a .env, not a bare key
        for line in text.splitlines():
            if line.strip().startswith("OPENROUTER_API_KEY"):
                text = line.split("=", 1)[-1].strip().strip('"').strip("'")
                break
    return label_of(text)


def destinations() -> tuple[dict[str, str], list[dict], list[dict]]:
    """destination -> label, a degradation for each unreadable one, and the absent ones.

    A consumer whose DIRECTORY is missing is not on this machine: its key file
    has nowhere to be, and saying "this consumer has none" in the words used for
    an installed consumer with an empty slot sent a person to repair a tool that
    is not there. That one goes on `not_applicable`; a directory without its key
    file stays a degradation.
    """
    out, degraded, absent = {}, [], []
    for name, path in DESTINATIONS.items():
        if not path.is_file() and not path.parent.is_dir():
            absent.append({"source": f"openrouter:{name}",
                           "reason": (f"{name} is not installed on this machine: {path.parent} "
                                      f"does not exist, so there is no key of its to match")})
            continue
        if not path.is_file():
            degraded.append({"source": f"openrouter:{name}",
                             "reason": f"no key at {path} — this consumer has none"})
            continue
        try:
            tail = _label(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            degraded.append({"source": f"openrouter:{name}",
                             "reason": f"unreadable: {type(exc).__name__}"})
            continue
        if not tail:
            degraded.append({"source": f"openrouter:{name}",
                             "reason": "the file holds no OpenRouter-shaped key"})
            continue
        out[name] = tail
    return out, degraded, absent


def listing(prov: str, offset: int = 0, pages: int = MAX_PAGES,
            until=None) -> tuple[list[dict], str | None]:
    """The keys the provisioning key governs from `offset`, or the reason there are none.

    `until(rows)` ends the walk early once it returns True — the deep search's
    stop as soon as every missing consumer is found.
    """
    rows = []
    for _ in range(pages):
        req = urllib.request.Request(f"{API}/keys?offset={offset}",
                                     headers={"Authorization": f"Bearer {prov}"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                page = json.loads(r.read().decode())["data"]
        except urllib.error.HTTPError as e:
            return rows, f"GET /keys failed: HTTP {e.code}"
        except Exception as e:                                    # noqa: BLE001
            return rows, f"GET /keys failed: {type(e).__name__}"
        rows.extend(page)
        if len(page) < PAGE:
            return rows, None
        if until is not None and until(rows):
            return rows, None
        offset += len(page)
        time.sleep(0.2)
    return rows, (f"stopped after {pages} pages ({len(rows)} keys); the account "
                  f"holds more and this list is partial")


def _get(path: str, prov: str) -> tuple[dict | None, int | None, str | None]:
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {prov}"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return (json.loads(r.read().decode()).get("data") or {}), None, None
    except urllib.error.HTTPError as e:
        return None, e.code, f"HTTP {e.code}"
    except Exception as e:                                        # noqa: BLE001
        return None, None, type(e).__name__


def whoami(prov: str) -> tuple[dict | None, str | None]:
    """What the provider says about the provisioning key itself (`GET /key`).

    Listings never include a provisioning key — measured: none among 8,000
    listed keys — so "not in the listing" is no evidence about it. Asking about
    itself is: the call carries only the key this collector already sends.
    """
    data, _code, why = _get("/key", prov)
    return data, why


def key_by_hash(prov: str, h: str) -> tuple[dict | None, int | None, str | None]:
    """One key by the hash a previous run found it under (`GET /keys/{hash}`)."""
    return _get(f"/keys/{h}", prov)


def remembered(out_path: pathlib.Path) -> dict[str, dict]:
    """The hash each consumer's key was found under on an earlier run."""
    try:
        doc = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    got = doc.get("destination_hashes") if isinstance(doc, dict) else None
    return {n: v for n, v in (got or {}).items()
            if isinstance(v, dict) and v.get("label") and v.get("hash")}


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("openrouter"):
        print("openrouter: not configured (integration disabled)")
        return 0
    out_path = pathlib.Path(argv[1]) if len(argv) > 1 else paths.SCRATCH / "openrouter.json"
    started = datetime.now(timezone.utc).isoformat()
    dests, degraded, not_applicable = destinations()

    prov_path = DESTINATIONS["provisioning"]
    if not prov_path.is_file():
        # HONEST DEGRADATION. Without the provisioning key nothing can be listed
        # — but what each destination holds is still knowable, and saying that
        # much beats writing an empty file that reads as "no keys exist".
        atomic.write_json(out_path, {
            "schema_version": 1, "scanned_at": started, "keys": [],
            "destinations": dests, "unlisted_destinations": sorted(dests),
            "degraded": degraded + [{"source": "openrouter:listing",
                                     "reason": "no provisioning key, so the account's "
                                               "keys cannot be listed at all"}],
            "not_applicable": not_applicable,
        }, indent=1)
        print(f"openrouter.json: no provisioning key; {len(dests)} destination(s) read",
              file=sys.stderr)
        return 0

    prov = prov_path.read_text(encoding="utf-8").strip()
    rows, why = listing(prov)
    verified: dict[str, str] = {}
    gone: dict[str, str] = {}
    unknown: dict[str, str] = {}

    # THE PROVISIONING KEY is never in a listing, so it is confirmed by asking
    # about itself. Its label must be the one in the file; anything else is a
    # different key in that slot, which is worth saying.
    if "provisioning" in dests:
        me, err = whoami(prov)
        if me is not None and me.get("label") == dests["provisioning"]:
            verified["provisioning"] = "the provider describes this key as itself (GET /key)"
        elif me is not None:
            degraded.append({"source": "openrouter:provisioning",
                             "reason": "GET /key answered for a key whose label differs from "
                                       "the one in this slot"})
        else:
            degraded.append({"source": "openrouter:provisioning",
                             "reason": f"could not be confirmed: GET /key failed ({err})"})

    listed_labels = {r.get("label") or "" for r in rows}
    missing = [n for n, lab in dests.items() if n != "provisioning" and lab not in listed_labels]
    extra: list[dict] = []

    # A KEY FOUND ONCE IS ASKED ABOUT BY ITS HASH: one request, and a 404 there
    # is the provider saying the key was deleted — a real problem for whichever
    # consumer still holds it, and reported as one.
    memory = remembered(out_path)
    for n in list(missing):
        m = memory.get(n)
        if not m or m.get("label") != dests[n]:
            continue
        row, code, err = key_by_hash(prov, m["hash"])
        if row is not None and row.get("label") == dests[n]:
            extra.append({**row, "hash": row.get("hash") or m["hash"]})
            verified[n] = "found by the hash an earlier run recorded"
            missing.remove(n)
        elif code == 404:
            gone[n] = ("deleted: the provider answers 404 for the hash this key was found "
                       "under earlier, so the consumer holds a dead key")
            missing.remove(n)
        else:
            unknown[n] = f"its recorded hash could not be asked about ({err})"

    # A BOUNDED LISTING IS NOT PROOF OF ABSENCE. Look further, once, for what is
    # still missing, and stop as soon as all of it is found.
    searched, complete = len(rows), why is None
    if missing and not complete:
        want = {dests[n] for n in missing}
        deeper, deeper_why = listing(
            prov, offset=len(rows), pages=DEEP_PAGES,
            until=lambda got: want <= {r.get("label") for r in got})
        searched += len(deeper)
        # Walked to the end, or stopped early because everything was found.
        complete = deeper_why is None
        if deeper_why and not deeper_why.startswith("stopped after"):
            why = f"{why}; the deeper search then failed: {deeper_why}"
        for r in deeper:
            if r.get("label") in want:
                extra.append(r)
        found = {r.get("label") for r in extra}
        for n in list(missing):
            if dests[n] in found:
                verified[n] = f"found past the bounded listing, among the newest {searched} keys"
                missing.remove(n)
    seen_hashes = {r.get("hash") for r in rows}
    rows = rows + [r for r in extra if r.get("hash") not in seen_hashes]

    if why:
        if missing or unknown:
            degraded.append({"source": "openrouter:listing", "reason": why})
        else:
            not_applicable.append({
                "source": "openrouter:listing",
                "reason": (f"{why.split(';')[0]}, by design: only the newest {MAX_PAGES} pages "
                           f"are read each run, and every key a consumer on this machine holds "
                           f"was matched, so the unread older keys hold none of them")})

    # A LABEL SHARED BY TWO KEYS IS NAMED, never resolved by preference: the
    # destination is then unattributable, and saying so is the only honest
    # answer available.
    seen: dict[str, int] = {}
    for k in rows:
        lab = k.get("label") or ""
        seen[lab] = seen.get(lab, 0) + 1
    collisions = {lab for lab, n in seen.items() if n > 1}
    by_label = {lab: name for name, lab in dests.items() if lab not in collisions}
    for name, lab in dests.items():
        if lab in collisions:
            degraded.append({"source": f"openrouter:{name}",
                             "reason": (f"{seen[lab]} keys in this account share the label "
                                        f"{lab}, so which one this consumer holds cannot "
                                        f"be told from metadata alone")})
    keys = []
    for k in rows:
        label = k.get("label") or ""
        keys.append({
            "label": label,
            "tail": label[-TAIL:],
            "name": k.get("name"),
            "serves": by_label.get(label),
            "limit": k.get("limit"),
            "limit_reset": k.get("limit_reset"),
            "usage": round(k.get("usage") or 0, 6),
            "remaining": k.get("limit_remaining"),
            "disabled": bool(k.get("disabled")),
            "is_provisioning": bool(k.get("is_provisioning_key")),
            "created_at": (k.get("created_at") or "")[:19],
        })
    keys.sort(key=lambda x: (x["serves"] is None, x["serves"] or "", x["name"] or ""))

    for n in sorted(missing):
        if complete:
            reason = ("its key is not in this account's listing — either it belongs to "
                      "another workspace or it no longer exists")
        else:
            reason = (f"its key is not among the newest {searched} keys the account lists, and "
                      f"the listing goes further, so whether it still exists is unknown")
        if n in unknown:
            reason += f"; {unknown[n]}"
        degraded.append({"source": f"openrouter:{n}", "reason": reason})
    for n, reason in sorted(gone.items()):
        degraded.append({"source": f"openrouter:{n}", "reason": reason})
    orphan_dests = sorted(set(missing) | set(gone))
    hash_of = {(r.get("label") or ""): r.get("hash") for r in rows}
    destination_hashes = {n: {"label": lab, "hash": hash_of[lab]} for n, lab in dests.items()
                          if lab in by_label and hash_of.get(lab)}

    atomic.write_json(out_path, {
        "schema_version": 1,
        "scanned_at": started,
        # The hash stays HERE and never reaches the registry: gitignored, and
        # `tools/keyserver.py` resolves a tail to a hash at the moment it acts.
        "hashes": {(k.get("label") or ""): k.get("hash") for k in rows},
        "keys": keys,
        "destinations": dests,
        # THE PATHS TOO, because where a key file LIVES is how it is tied to a
        # project: `store/.openrouter-key` sits inside the observatory's folder.
        "destination_paths": {n: str(p) for n, p in DESTINATIONS.items() if p.is_file()},
        "unlisted_destinations": orphan_dests,
        # How each consumer's key was confirmed when the listing alone did not,
        # and the hash it was found under, so the next run asks about it directly.
        "verified": verified,
        "destination_hashes": destination_hashes,
        "degraded": degraded,
        "not_applicable": not_applicable,
    }, indent=1)
    served = sum(1 for k in keys if k["serves"])
    print(f"openrouter.json: {len(keys)} key(s), {served} serving a known consumer, "
          f"{len(orphan_dests)} destination(s) not in the listing, "
          f"{len(degraded)} degradation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
