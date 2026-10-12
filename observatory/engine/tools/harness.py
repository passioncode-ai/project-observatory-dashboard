#!/usr/bin/env python3
"""The agent harness on this machine, as the last scan saw it (docs/design/HARNESS.md).

    project-observatory full harness                  # budget, largest skill entries, rule hits, changes
    project-observatory full harness --accept KEY     # record a changed skill's current digest as reviewed
    project-observatory full harness --accept-all     # the same for every changed skill

The scan itself is `full scan-harness` (and every tick); this reads its result and the pins.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic  # noqa: E402
import paths  # noqa: E402

RAW = paths.SCRATCH / "harness.json"
PINS = paths.STATE / "harness-pins.json"


def load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def summary() -> int:
    doc = load(RAW)
    if not doc:
        print("no harness scan yet: run `project-observatory full scan-harness`", file=sys.stderr)
        return 1
    print(f"skills offered to the model: {doc.get('skills_offered')}")
    print(f"skill listing: {doc.get('listing_chars')} of {doc.get('budget_chars')} characters"
          + (f" — {len(doc.get('dropped') or [])} descriptions do not fit" if doc.get("dropped") else ""))
    top = sorted(doc.get("skills") or [], key=lambda s: -s.get("listing_chars", 0))[:15]
    print("largest entries:")
    for s in top:
        print(f"  {s['listing_chars']:5}  {s['name']}  ({s['source']}, tier {s['tier']})")
    for i in doc.get("instructions") or []:
        print(f"instructions: {i['path']} — {i['lines']} lines, {i['chars']} characters")
    hits = doc.get("security") or []
    print(f"rule hits: {len(hits)}")
    for h in sorted(hits, key=lambda h: (h["severity"] != "critical", h["skill"]))[:20]:
        print(f"  {h['severity']:8} {h['skill']}: {h['rule']} in {h['file']}" + (f" ({h['host']})" if h.get("host") else ""))
    for c in doc.get("changed") or []:
        print(f"changed: {c['skill']} — `project-observatory full harness --accept {c['key']}`")
    codex = doc.get("codex") or {}
    if codex.get("skills"):
        print(f"codex: {codex['skills']} skills, {codex['listing_chars']} description characters")
    usage = doc.get("usage") or {}
    if usage.get("transcripts"):
        print(f"skill use over {usage.get('days')} days of {usage.get('transcripts')} transcripts: "
              f"{usage.get('unused_offered')} offered skills never invoked")
    hooks = doc.get("hooks") or []
    print(f"hooks: {len(hooks)} ({sum(1 for h in hooks if h['event'] == 'SessionStart')} at SessionStart, "
          f"{sum(1 for h in hooks if h['event'] == 'PreToolUse')} before tool calls)")
    for h in doc.get("hook_security") or []:
        print(f"  {h['severity']:8} {h['source']} {h['event']} hook ({h['program']}): {h['rule']}")
    return 0


def accept(keys: list[str], every: bool) -> int:
    doc, pins = load(RAW), load(PINS)
    current = {s["key"]: s["digest"] for s in doc.get("skills") or []}
    targets = [c["key"] for c in doc.get("changed") or []] if every else keys
    unknown = [k for k in targets if k not in current]
    if unknown:
        print("not in the last scan: " + ", ".join(unknown), file=sys.stderr)
        return 2
    stamp = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for k in targets:
        pins[k] = {**pins.get(k, {}), "digest": current[k], "accepted_at": stamp}
    atomic.write_json(PINS, pins)
    print(f"accepted {len(targets)} skill(s); the next scan reports them only if they change again")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="project-observatory full harness", description=__doc__.split("\n\n")[0])
    parser.add_argument("--accept", metavar="KEY", help="record this changed skill's current digest as reviewed")
    parser.add_argument("--accept-all", action="store_true", help="the same for every changed skill")
    args = parser.parse_args(argv)
    if args.accept and args.accept_all:
        parser.error("--accept and --accept-all are exclusive")
    if args.accept or args.accept_all:
        return accept([args.accept] if args.accept else [], args.accept_all)
    return summary()

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
