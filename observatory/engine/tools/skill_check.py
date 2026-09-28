#!/usr/bin/env python3
"""The skill-version handshake: a session says what it read, and learns if it is stale.

    tools/skill_check.py handling-secrets 0.3.0
    tools/skill_check.py --report            # what sessions have reported lately

WHY. A Claude Code session snapshots its skills at START and never re-reads
them: `claude plugin update` refreshes the installed copy, but every session
already running keeps the text it loaded — silently. So two agents can obey two
different versions of a MANDATORY rule set and neither knows. The remedy is a
handshake the skill itself makes obligatory: on first use in a session, the
agent runs this tool with the version printed in the SKILL.md it actually read.

WHAT IT DOES with that claim:

  * compares it against the SHIPPED version (this checkout's SKILL.md, the
    source the installed plugin is built from);
  * prints OK, or STALE with the exact remedy (update + restart);
  * records the sighting in `skill-sessions.json` under the raw store, so
    `tools/build_findings.py` can raise `skill.stale_session` while any session
    is known to be working from old text: the operator sees drift on the board
    instead of discovering it in a rule followed the old way.

BACKWARD COMPATIBILITY POLICY, enforced by wording here and versioning there:
the tools a skill names (`vault.py`, `install_key.py`) keep old invocation
shapes working within a MAJOR version; a skill's PATCH/MINOR bump means added
or clarified text (an old session is safe, just poorer); a MAJOR bump means a
rule changed meaning, and this tool says so in stronger words.
"""
from __future__ import annotations
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                                   
import paths                                                                    

RECEIPT = paths.SCRATCH / "skill-sessions.json"
SKILLS = ROOT / "skill/plugins/observatory-log/skills"
#: How long a stale sighting stays interesting. A session that reported an old
#: version nine days ago has almost certainly ended; the finding should not
#: outlive the session it warns about.
STALE_WINDOW_HOURS = 48


def now_z() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shipped_version(skill: str) -> str | None:
    f = SKILLS / skill / "SKILL.md"
    if not f.is_file():
        return None
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("version:"):
            # The frontmatter quotes the scalar (`version: "0.12.0"`). Kept, the
            # quotes made every report parse against (0,), so even the current
            # version read as AHEAD and the handshake could never say OK.
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def parse(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def record(skill: str, reported: str, verdict: str) -> None:
    doc = {"sessions": {}}
    if RECEIPT.is_file():
        try:
            doc = json.loads(RECEIPT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {"sessions": {}}
    key = f"{skill}@{reported}"
    row = doc["sessions"].get(key) or {"first_seen": now_z(), "count": 0}
    row.update({"skill": skill, "reported": reported, "verdict": verdict,
                "last_seen": now_z(), "count": row.get("count", 0) + 1})
    doc["sessions"][key] = row
    atomic.write_json(RECEIPT, doc)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or 'Compare a loaded companion skill version with the installed version.').splitlines()[0])
    ap.add_argument("skill", nargs="?")
    ap.add_argument("version", nargs="?",
                    help="the version printed in the SKILL.md this session READ")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args(argv[1:])

    if a.report or not (a.skill and a.version):
        doc = {}
        if RECEIPT.is_file():
            try:
                doc = json.loads(RECEIPT.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
        rows = (doc.get("sessions") or {}).values()
        if not rows:
            print("no session has reported a skill version yet")
            return 0
        for r in sorted(rows, key=lambda r: r.get("last_seen", "")):
            print(f"  {r.get('verdict', '?'):6} {r.get('skill')}@{r.get('reported')}"
                  f"  last {r.get('last_seen')}  ×{r.get('count')}")
        return 0

    shipped = shipped_version(a.skill)
    if shipped is None:
        print(f"UNKNOWN — no skill named {a.skill!r} ships from this checkout; "
              f"the ones that do: "
              + ", ".join(sorted(p.name for p in SKILLS.glob("*") if p.is_dir())))
        record(a.skill, a.version, "unknown")
        return 2
    mine, theirs = parse(shipped), parse(a.version)
    if theirs == mine:
        print(f"OK — {a.skill} {a.version} is current")
        record(a.skill, a.version, "ok")
        return 0
    if theirs > mine:
        print(f"AHEAD — the session read {a.version}, this checkout ships {shipped}: "
              f"the checkout is behind its own installed plugin. `git pull` here.")
        record(a.skill, a.version, "ahead")
        return 1
    major_change = theirs[0] != mine[0]
    print(f"STALE — the session read {a.skill} {a.version}, shipped is {shipped}.")
    if major_change:
        print("  A MAJOR version apart: a rule has CHANGED MEANING since this "
              "session started. Do not act on the old text — restart the session.")
    else:
        print("  The text this session follows is older but compatible; finish the "
              "task, then refresh:")
    print("    claude plugin marketplace update observatory-log")
    print("    claude plugin update observatory-log@observatory-log")
    print("    (then restart the session — skills are read at session start)")
    record(a.skill, a.version, "stale-major" if major_change else "stale")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
