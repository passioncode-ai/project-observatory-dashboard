#!/usr/bin/env python3
"""How quiet a project has gone — measured, and separate from what it declares.

`lifecycle` answers a DECLARED question: has the owner marked this finished. It
had two values in practice, `active` on 156 projects and `archived` on 3 (measured 2026-09-08; 153 and 3
when this was written), and
the second came only from every repository carrying GitHub's `isArchived` flag.
Nothing anywhere held a staleness threshold, so a project untouched for **3,954
days** — nearly eleven years — was `lifecycle: active`, truthfully, because
nobody had archived it. The registry could say what its projects claimed and not
what they were doing.

This is the other axis, and it stays a separate field for the reason this
codebase keeps rediscovering: a field that answers two questions answers neither.
A project can be archived and have moved last week; another can be unarchived and
untouched for a decade. Both facts are true, and both are worth reading.

The thresholds live in `collectors/activity_tiers.json` so the operator can move
them in one place — the same shape as `store/retention.json`, which the collector
and the pruner both read after they spent a live tick undoing each other.
"""
from __future__ import annotations
import functools, json, pathlib
from datetime import date, datetime, timezone

import paths
CONFIG = paths.config_file("activity_tiers.json")


@functools.lru_cache(maxsize=1)
def config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def tiers() -> list[dict]:
    return config()["tiers"]


def active_window_days() -> int:
    """The boundary of `active`, in days — and the window any "recent" number
    should be summed over.

    ONE definition of recent, not two. `estate.survey` needed a period to sum
    the weekly rollup over, and a hardcoded 30 there would have given a host two
    meanings of the word: one for the tier it colours a row by and one for the
    number it sorts the column by. The config already fixes this boundary, with
    the distribution that chose it recorded beside it, so the survey reads it
.
    """
    return int(tiers()[0]["max_days"])


def unknown_id() -> str:
    return config()["unknown_id"]


#: What the unmeasured tier means when the configuration carries no note of its
#: own. The shipped defaults have none, and an empty meaning left the one tier
#: a legend most needs to explain as the one it could not.
UNKNOWN_MEANS = ("no measurable last-activity date — not the same as cold, "
                 "which would assert an absence that was never measured")


def unknown_means() -> str:
    return config().get("unknown_note") or UNKNOWN_MEANS


def tier_ids() -> list[str]:
    return [t["id"] for t in tiers()] + [unknown_id()]


def days_since(last_activity_on: str | None, *, today: date | None = None) -> int | None:
    """Whole days, or None when there is no measurable date.

    None is not zero and not infinity: it means nothing measured this, and the
    caller must say so rather than bucket it with the oldest."""
    if not last_activity_on:
        return None
    try:
        seen = date.fromisoformat(str(last_activity_on)[:10])
    except ValueError:
        return None
    return ((today or datetime.now(timezone.utc).date()) - seen).days


def tier_of(last_activity_on: str | None, *, today: date | None = None) -> str:
    """The tier a date falls in. Pure, recomputable, and never sticky: the
    validator derives it again and fails on a value typed by hand, because a tier
    that can be typed is a tier that can disagree with the date beside it."""
    age = days_since(last_activity_on, today=today)
    if age is None:
        return unknown_id()
    for t in tiers():
        if t["max_days"] is None or age <= t["max_days"]:
            return t["id"]
    return tiers()[-1]["id"]


def describe(tier: str) -> str:
    for t in tiers():
        if t["id"] == tier:
            return t["means"]
    return unknown_means() if tier == unknown_id() else ""


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import paths
    projects = json.loads((paths.REGISTRY / "projects.json")
                          .read_text(encoding="utf-8"))["projects"]
    counts: dict[str, int] = {}
    for p in projects:
        t = p.get("activity_tier") or tier_of(p.get("last_activity_on"))
        counts[t] = counts.get(t, 0) + 1
    for t in tier_ids():
        if counts.get(t):
            print(f"  {t:9} {counts[t]:4}   {describe(t)[:64]}")
