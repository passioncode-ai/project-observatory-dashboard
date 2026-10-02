#!/usr/bin/env python3
"""`registry/remote-env.json` — what production holds, as verdicts only.

THE DIVISION OF LABOUR IS THE POINT. `scan_remote_env.py` holds the
fingerprints and lives in `store/raw/`, which is gitignored; this module reads
them, compares, and writes WORDS. Four of them:

    same_as_local   production and the checkout hold the same value
    differs         both hold the name, the values are not equal
    remote_only     production has a variable the checkout does not
    local_only      the checkout has one production does not

and a fifth that is honest rather than tidy — `not_compared`, for a variable
both sides hold whose class is not `secret`: the local inventory fingerprints
secrets only (two projects agreeing that `APP_ENV` is `production` is a
coincidence of vocabulary, not a shared credential), so there is nothing to
compare against. Printing "same" there would be a guess.

Two more states exist for the same reason. `no_local_checkout` marks an app
whose source is not on this disk, so nothing can be compared at all. And a
secret both sides hold is also `not_compared`, with a `why`, when the two scans
did not fingerprint under the same salt (`fingerprint_namespace`): equal values
would not produce equal fingerprints, so any verdict would be invented.

`differs` IS THE ORDINARY CASE AND NOT A FINDING. A production database URL
should not equal the one in a developer's checkout. What is worth a row is the
opposite: `same_as_local` on a secret — production running the value that also
sits in a local `.env` — and a value production still holds that the vault has
already rotated away.
"""
from __future__ import annotations


def _local_index(env_scan: dict, folders: list[str]) -> dict[str, dict]:
    """name -> {class, fingerprints, paths} for the checkout(s) of one app.

    Only live `.env` files: a `.env.example` holds a placeholder by
    construction, and comparing production against a template would report
    every application as differing from itself.
    """
    wanted = {f.rstrip("/").rsplit("/", 1)[-1] for f in folders}
    out: dict[str, dict] = {}
    for rec in env_scan.get("files") or []:
        if rec.get("kind") != "env" or rec.get("project") not in wanted:
            continue
        for v in rec.get("variables") or []:
            row = out.setdefault(v["name"], {"class": v.get("class"),
                                             "fingerprints": set(), "paths": []})
            if v.get("fingerprint"):
                row["fingerprints"].add(v["fingerprint"])
            row["paths"].append(rec.get("path"))
    return out


# The `why` a row carries when its verdict was withheld because the two scans'
# fingerprint namespaces do not match.
NAMESPACE_WHY = "fingerprint-namespace"


def namespace_state(scan: dict, env_scan: dict) -> tuple[str, str]:
    """Whether the two scans' fingerprints can be compared, and why not if not.

    Returns `matched`, `mismatched` (different salts) or `absent` (a scan names
    no namespace), with a reason for the last two. Fingerprints made under
    different salts never match, so comparing them would report every shared
    secret as `differs` — a confident, false answer.
    """
    remote, local = scan.get("fingerprint_namespace"), env_scan.get("fingerprint_namespace")
    if not remote or not local:
        missing = " and ".join(
            w for w, got in (("the production scan", remote), ("the local scan", local)) if not got)
        return "absent", (f"{missing} carries no fingerprint namespace, so the salt behind "
                          f"its fingerprints is unknown")
    if remote != local:
        return "mismatched", ("the two scans fingerprint under different salts, so equal "
                              "values would not produce equal fingerprints")
    return "matched", ""


def compare(app: dict, env_scan: dict, retired: list[dict], *,
            comparable: bool = True, current: list[dict] | None = None) -> dict:
    """The verdict for every variable of one production app.

    Each production variable gets one of the verdicts described in the module
    docstring; variables only the checkout holds are added as `local_only`. A
    fingerprint that matches a retired vault value is listed in `retired_in_use`.
    """
    folders = app.get("folders") or []
    local = _local_index(env_scan, folders) if folders else {}
    retired_by_fp: dict[str, dict] = {r["fingerprint"]: r for r in retired}
    # The CURRENT vault values, fingerprinted by the same scan under the same
    # salt (PB-129): a production var equal to one is that slot read at run time.
    current_by_fp: dict[str, list[dict]] = {}
    for c in current or []:
        current_by_fp.setdefault(c["fingerprint"], []).append(c)
    rows, in_use, vault_in_use = [], [], []
    seen = set()
    for v in app.get("vars") or []:
        name, cls = v["name"], v.get("class")
        why = ""
        seen.add(name)
        if not folders:
            # NO CHECKOUT IS NOT A MISMATCH. An application whose source is not
            # on this disk has nothing to be compared against, and calling that
            # `remote_only` would put nineteen applications' worth of variables
            # on the board as a difference nobody can act on.
            verdict = "no_local_checkout"
        elif name not in local:
            verdict = "remote_only"
        elif cls != "secret" or not v.get("fingerprint"):
            verdict = "not_compared"
        elif not local[name]["fingerprints"]:
            verdict = "not_compared"
        elif not comparable:
            # The fingerprints were made under different (or unknown) salts, so
            # an inequality would be meaningless and an equality impossible.
            # Withhold the verdict and say why rather than report `differs`.
            verdict, why = "not_compared", NAMESPACE_WHY
        elif v["fingerprint"] in local[name]["fingerprints"]:
            verdict = "same_as_local"
        else:
            verdict = "differs"
        rows.append({"name": name, "class": cls, "verdict": verdict,
                     **({"why": why} if why else {}),
                     **({"empty": True} if v.get("empty") else {})})
        # A RETIRED VALUE STILL IN USE is checked for every variable, whatever
        # its verdict: production running a value the vault has already rotated
        # away is the most expensive thing this comparison can find, and it does
        # not depend on whether a local checkout exists.
        for c in current_by_fp.get(v.get("fingerprint") or "", []) if cls == "secret" else []:
            vault_in_use.append({"name": name, "slot": f"{c['project']}/{c['env']}/{c['name']}"})
        r = retired_by_fp.get(v.get("fingerprint") or "")
        if r:
            in_use.append({"name": name, "retired_on": r["retired_on"],
                           "slot": f"{r['project']}/{r['env']}/{r['name']}"})
    if folders:
        for name, row in sorted(local.items()):
            if name not in seen:
                rows.append({"name": name, "class": row["class"], "verdict": "local_only"})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"app": app["app"], "compared_with": folders,
            "error": app.get("error"), "vars": rows, "counts": counts,
            "retired_in_use": in_use, "vault_in_use": vault_in_use}


def document(scan: dict, env_scan: dict, obs_date: str) -> dict:
    state, reason = namespace_state(scan, env_scan)
    comparable = state == "matched"
    apps = [compare(a, env_scan, scan.get("retired") or [], comparable=comparable,
                    current=scan.get("current") or [])
            for a in scan.get("apps") or []]
    def total(word: str) -> int:
        return sum(a["counts"].get(word, 0) for a in apps)
        # WITHHELD rows are counted separately. They sit inside `not_compared`,
        # but for a different reason than a non-secret: the two sides might hold
        # the same value and the fingerprints cannot say, so the gap is reported
        # as degraded coverage with the command that closes it, not as a fact.
    withheld = sum(1 for a in apps for v in a["vars"] if v.get("why") == NAMESPACE_WHY)
    degraded = list(scan.get("degraded") or [])
    if withheld:
        degraded.append({
            "source": "fingerprint namespace",
            "reason": reason,
            "effect": (f"{withheld} production secret(s) that both sides hold are "
                       f"reported as not compared instead of same or different; "
                       f"re-read production with `collectors/scan_remote_env.py "
                       f"store/raw/remote-env.json --force` and re-run `env`, then emit"),
        })
    return {
        "schema_version": 1,
        "updated_on": obs_date,
        "note": ("What production is configured with, compared against the "
                 "checkout on this machine. NAMES, CLASSES AND VERDICTS ONLY: "
                 "the comparison is made from salted fingerprints that live in "
                 "the gitignored scan and never in this file, and the values "
                 "themselves exist only in the scanning process. `differs` is "
                 "the ordinary case for a production secret; `same_as_local` on "
                 "one is not, and neither is a value the vault has retired. "
                 "`fingerprint_namespace` says whether the comparison was allowed "
                 "to happen at all: when the two scans do not name the same salt, "
                 "a row both sides hold reads `not_compared` with `why`, and "
                 "`withheld` counts those rows inside the `not_compared` total."),
        "source_refs": ["SRC-0013", "SRC-0015"],
        "scanned_on": (scan.get("scanned_at") or "")[:10],
        "provider": scan.get("provider"),
        "totals": {
            "apps": len(apps),
            "apps_with_a_checkout": sum(1 for a in apps if a["compared_with"]),
            "variables": sum(len(a["vars"]) for a in apps),
            "same_as_local": total("same_as_local"),
            "differs": total("differs"),
            "remote_only": total("remote_only"),
            "local_only": total("local_only"),
            "not_compared": total("not_compared"),
            "no_local_checkout": total("no_local_checkout"),
            "withheld_for_namespace": withheld,
            "retired_still_deployed": sum(len(a["retired_in_use"]) for a in apps),
        },
        # Whether the comparison was allowed to happen at all, and what to run
        # when it was not — a reader of `totals` needs this to tell a withheld
        # comparison from a clean one.
        "fingerprint_namespace": {
            "state": state,
            "withheld": withheld,
            **({"reason": reason} if state != "matched" else {}),
            **({"action": "collectors/scan_remote_env.py store/raw/remote-env.json --force, "
                          "then project-observatory full env and project-observatory full emit"}
               if withheld else {}),
        },
        "apps": apps,
        "degraded": degraded,
    }
