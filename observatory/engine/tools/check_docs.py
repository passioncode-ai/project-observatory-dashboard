#!/usr/bin/env python3
"""Is the documentation true? — as an exit code, not as a sentence in a report.

Fourteen drifts were found by an audit on 2026-09-06 and every one of them was a
statement that had been true once. `README.md` said the LLM layer was "not built
yet" while the agent had spent real money for three days; `observatory.py`'s own
docstring documented a step that does not exist; `FABRIC-CONFORMANCE.md` sat a
revision behind the manifest it certifies; and `fabric/probes/assertions.md` —
published to any Fabric host that reads it — described three probes when four
run.

Fixing fourteen sentences by hand is one afternoon. The class comes back the
following week, because nothing measures it. So the fixes are what this file
DEMANDS rather than what somebody remembered, and "the docs are in sync" becomes
a command with an exit code.

Every rule is chosen because it is mechanically decidable, and `--list` is the
one place they are enumerated. This docstring deliberately states no count and
no list: it opened with "Five rules" and the numbered five while eleven ran —
a drift inside the drift checker, and the one sentence here that nothing could
measure, since rule 2 reads documents and rule 5 reads named phrases. Removing
the number removes the class instead of the instance.

Rule 5 is the brittle one and it is deliberately narrow: each entry carries the
measurement that killed the claim, so the next reader can tell a retired
sentence from a typo. Rules 10 and 11 read their subject as TEXT rather than as
imported objects, because both defects are invisible once Python has parsed
them — a collapsed duplicate key, and a header that no code consults.

    check_docs.py            report and exit non-zero on any drift
    check_docs.py --list     what is checked, without checking it
"""
from __future__ import annotations
import argparse, json, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# AFTER the path insert, not before. Above it the import resolved only because
# the tick happens to run with the repository as its cwd — and a checker that
# works from one directory is a checker that stops working the first time
# somebody runs it from another.
import paths              

#: (file, phrase, why it is false now). Absence is the assertion.
RETIRED_CLAIMS = [
    ("README.md", "not built yet",
     "the agent has been scheduled and spending since 2026-09-04; "
     "`store/wallet.json` and 106 ledger rows are the evidence"),
    ("observatory.py", "The LLM layer is a separate entry point and is not built yet",
     "the `agent` step is in the table fifty lines below the sentence"),
    ("observatory.py", "No model participates in any of these",
     "`agent` and `index` both spend; the docstring contradicted its own table"),
    # INVERTED on 2026-09-07, and the inversion is the point. This entry
    # forbade saying claude-mem was read, on the ground that "no code reads
    # claude-mem". `collectors/scan_sessions.py` now does (SRC-0012), so the
    # justification expired and the rule became a stale claim about the
    # codebase — a drift checker holding a rule whose reason has run out is the
    # same defect it exists to catch, one level up. What is forbidden now is the
    # opposite sentence: the one that says the collector is unbuilt.
    # The document that argues for dated claims carried an undated one for a day
    # after the code falsified it: the Heroku collector shipped in `a5a415a` while
    # `docs/heroku-estate.md` still said wiring it in had not been done. Found by
    # an audit, not by a rule — which is what this list is for.
    ("docs/heroku-estate.md", "it has not been done",
     "the collector shipped on 2026-09-09 in a5a415a: collectors/scan_heroku.py, "
     "the `heroku` step, registry/heroku-apps.json and four finding rules"),
    ("docs/ARCHITECTURE.md", "claude-mem` collector is designed and NOT built",
     "`collectors/scan_sessions.py` reads that store read-only and records one "
     "`session` event per (session, project); 57 projects carry a "
     "`last_session_on` from it and six changed activity tier"),
    ("docs/ARCHITECTURE.md", "grep -rn claude-mem` over the Python returns nothing",
     "it returns `collectors/scan_sessions.py` and the SRC-0012 declaration in "
     "`collectors/emit_registry.py`"),
    ("docs/ARCHITECTURE.md", "the vault's `tools/validate_inventory.py`",
     "removed from the vault when the registry moved here"),
]

#: path -> why it may be named and not exist. A set would become a bin; a reason
#: per entry is what stops the exemption list from being the drift.
#: Path PREFIXES whose contents a fresh clone legitimately lacks, with the
#: reason. `store/raw/` is one .gitignore line (`store/raw/`) and holds zero
#: tracked files — verified with `git check-ignore`, not assumed — so
#: enumerating its members one by one was a maintenance step that every new
#: collector had to remember. Two did not: `sessions.json` (SRC-0012) and
#: `corroboration.json`, and the second failed the gate the day it was written.
PATH_PREFIX_ALLOWLIST = {
    "store/raw/": "collector and tool output; the whole directory is gitignored and "
                  "written by a run, so a fresh clone has none of it",
    "store/logs/": "written by launchd",
}

PATH_ALLOWLIST = {
    # Generated or volatile, and gitignored: a fresh clone has none of these.
    "store/observatory.db": "the store is gitignored and rebuilt",
    "store/observatory.db.backup-2026-09-06T2150Z": "a dated backup, gitignored",
    "store/wallet.json": "written by the provider boundary",
    "store/provider-health.json": "written by the provider boundary",
    "store/key-usage.json": "the KEY's own account state, written by the provider "
                            "boundary whenever it answers; gitignored",
    "store/.openrouter-key": "a secret the reader is told to create",
    "store/logs/tick.log": "written by launchd", "store/logs/tick.err": "written by launchd",
    "store/budget.json": "named by an early design that a later one superseded",
    "docs/projects-dashboard.html": "generated, gitignored",
    # Deliberately gone, and the document says so.
    "tools/validate_inventory.py": "removed from the vault when the registry moved here",
    "tools/build_dashboard.py": "the vault's copy, removed for the same reason",
    # In ANOTHER repository, cited with its pin.
}

#: The repository's own top-level directories, computed rather than listed —
#: a hand-written list is the next thing to drift.
TOP_LEVEL = {p.name for p in ROOT.iterdir()
             if p.is_dir() and not p.name.startswith((".", "_"))}
PATH_RX = re.compile(r"`([a-zA-Z0-9_./-]+\.(?:py|json|md|sql|sh|js|jsonl|yaml|css|html))`")


#: A scale claim — "N projects across M repositories and K owners". The README
#: carried one measured 2026-09-03 while the registry held 159/177/16, and it was
#: STALE rather than dishonest: it dates its own claim, so a reader could tell.
#: What would be dishonest is an UNDATED figure, which reads as current for ever.
#:
#: Currency is deliberately NOT demanded. The estate grows most days, so a rule
#: requiring the figure to match would turn the gate red as a matter of routine —
#: a chore rather than a check, and this repository already learned what a red
#: nobody can clear does to a red that matters.
SCALE_CLAIM_RX = re.compile(r"\*\*\d+ projects across \d+ repositories and \d+ owners\*\*")


def scale_claim_failures(text: str, rel: str) -> list[str]:
    """A scale claim must carry the date it was measured on. Empty when none.

    **SCOPE, stated because the docstring used to overreach it.** This matches
    ONE sentence shape and is applied to three entry documents — README,
    AGENTS.md, ARCHITECTURE. That is deliberate: those are where a newcomer
    meets the estate's size and reads it as current. Docstrings deeper in the
    tree carry dated receipts of past defects, and a rule firing on every
    numeral in them would report hundreds of legitimate frozen measurements.
    Widening this was considered and refused on that ground.

    Currency is NOT demanded — only the date. The counts move daily, so
    requiring them to be current would turn every entry document into a chore
    the next tick invalidates.
    """
    if not SCALE_CLAIM_RX.search(text):
        return []
    # BOTH FORMS, because the house writes the other one. This accepted only
    # `measured on <date>`; every dated claim elsewhere in the repository reads
    # `Measured 2026-09-07` — `store/retention.json`, a dozen docstrings, the
    # decision log throughout. The README happens to use `Measured on`, so the
    # rule passed by luck: rewrite that one sentence in the project's own style
    # and a green check would have gone red over nothing.
    if re.search(r"[Mm]easured(?: on)? \d{4}-\d{2}-\d{2}", text):
        return []
    return [f"{rel} states a scale claim with no measurement date — an undated "
            f"figure reads as current for ever, and this one was five days stale "
            f"the day it was noticed"]


LEDGERS = (("docs/DECISIONS.md", "DEC"), ("docs/OPEN_QUESTIONS.md", "OQ"))

# A `**Vacant ids:**` line followed by backticked ids: the ledger accounting for
# an id it does not carry. A hole can be of opposite kinds: an id that was never
# minted (an off-by-two when a session resumed, cited by nothing), or an entry
# that lives elsewhere by decision. Renumbering to close a hole would
# invalidate every citation of the later ids, so a hole is legitimate, once
# DECLARED. Prose does not count: "the file mentions it somewhere" passes on any
# file containing the string, and a rule that cannot fail is not a rule.
VACANT_RX = re.compile(r"\*\*Vacant ids:\*\*(.*)")


def ledger_failures(path: pathlib.Path, prefix: str, rel: str) -> list[str]:
    """One ledger's header against its own contents.

    Callable on a single file so the rule can be WATCHED failing against planted
    defects (`tests/test_ledger_pointer.py`) instead of being trusted because it
    is green over documents that happen to be correct.
    """
    out: list[str] = []
    text = path.read_text(encoding="utf-8")
    minted = [int(m) for m in re.findall(rf"^#+ {prefix}-(\d+)\b", text, re.M)]
    ids = sorted(set(minted))
    # ONE ID, ONE ENTRY. Two decisions once shipped under the same id on
    # consecutive days, and this rule, which counts vacancies, never counted
    # copies: a citation of the id then pointed at whichever the reader found
    # first. A later revision found it, dozens of decisions afterwards.
    for dup in sorted({i for i in minted if minted.count(i) > 1}):
        out.append(f"{rel} mints {prefix}-{dup:04d} {minted.count(dup)} times — one id, "
                   f"one entry; renumber the later one to the next free id and say so "
                   f"in its first line")
    if not ids:
        return out
    top = max(ids)

    ptr = re.search(rf"\*\*Next free ID:\*\* `{prefix}-(\d+)`", text)
    if not ptr:
        out.append(f"{rel} carries no `Next free ID` pointer, so nothing tells the "
                   f"next writer which id is free — last one present is {prefix}-{top:04d}")
    elif int(ptr.group(1)) != top + 1:
        out.append(f"{rel} says `Next free ID: {prefix}-{ptr.group(1)}` while "
                   f"{prefix}-{top:04d} already exists — it should be "
                   f"{prefix}-{top + 1:04d}. This counter was once fixed by hand "
                   f"and it drifted again; bump it in the same edit that appends.")

    # EVERY ENTRY CARRIES A MACHINE-READABLE STATUS, because AGENTS.md advertises
    # exactly that and nothing kept it: an open question shipped with its status
    # written in prose and no `- Status:` line, so a reader counting open
    # questions by the documented spelling came up one short. An audit found it:
    # a claim the docs make that nothing keeps, which is the class this whole
    # file exists for.
    if prefix == "OQ":
        for sec in re.split(r"(?m)^## ", text)[1:]:
            head = sec.split("\n", 1)[0].strip()
            if not re.match(rf"{prefix}-\d+", head):
                continue
            if not re.search(r"(?m)^- Status: ", sec):
                out.append(f"{rel} entry {head[:24]!r} carries no `- Status:` line, "
                           f"so nothing can count what is open without reading prose")

    declared = set()
    m = VACANT_RX.search(text)
    if m:
        declared = {int(x) for x in re.findall(rf"{prefix}-(\d+)", m.group(1))}
    gaps = {i for i in range(1, top + 1) if i not in set(ids)}
    for i in sorted(gaps - declared):
        out.append(f"{rel} has no {prefix}-{i:04d} and does not declare it vacant — "
                   f"a reader hunting for it cannot tell a lost entry from a skipped id")
    for i in sorted(declared - gaps):
        out.append(f"{rel} declares {prefix}-{i:04d} vacant, but it is present — "
                   f"the declaration is stale")
    return out



def shape_doc_failures() -> list[str]:
    """`docs/REGISTRY_SHAPE.md` against the registry it describes.

    Generated documents drift the moment nothing checks them — the lesson
    `docs/AGENT_SYNC.md` already carries, with its `cfg=` stamp. This compares
    the stamp in the header with the one the live registry produces now.

    The stamp hashes the SHAPE, not the data: the registry changes on every tick
    and its shape almost never does, so hashing the data would make this stale
    hourly. That is the same distinction rule 13 draws between a check and a
    chore.
    """
    doc = ROOT / "docs/REGISTRY_SHAPE.md"
    if not doc.is_file():
        return [f"docs/REGISTRY_SHAPE.md is missing — the registry calls itself "
                f"typed facts and nothing publishes the types; "
                f"`.venv/bin/python tools/registry_shape.py --write` writes it"]
    text = doc.read_text(encoding="utf-8")
    m = re.search(r"data=(\w+)", text)
    if not m:
        return ["docs/REGISTRY_SHAPE.md carries no data= stamp, so nothing can "
                "tell whether it describes the current registry"]
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import registry_shape
        live = registry_shape.stamp(registry_shape.shapes())
    except Exception as exc:                                                        
        return [f"the registry shape could not be computed: "
                f"{type(exc).__name__}: {exc}"]
    if m.group(1) != live:
        return [f"docs/REGISTRY_SHAPE.md describes shape {m.group(1)}, the live "
                f"registry is {live} — a field was added or removed; "
                f"`.venv/bin/python tools/registry_shape.py --write` regenerates it"]
    return []


def conformance_doc_failures() -> list[str]:
    """`fabric/FABRIC-CONFORMANCE.md` against the manifest it describes.

    The document is what a Fabric HOST reads — a stranger who cannot ask — and
    its first six lines were wrong. It named two capabilities where the manifest
    declares four, while its own gate table said "all four capabilities" eleven
    lines below: a host compiling the summary would have missed half the surface.
    Measured 2026-09-08 by writing down what the manifest holds and comparing
.

    Two rules, both mechanical, because the third — is the CURRENT revision
    published? — has a gate step of its own (`project-observatory full contract`) and a
    prose statement here would duplicate a check rather than add one.
    """
    doc = ROOT / "fabric/FABRIC-CONFORMANCE.md"
    manifest = ROOT / "fabric-agent.json"
    if not doc.is_file() or not manifest.is_file():
        return []
    text = doc.read_text(encoding="utf-8")
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"fabric-agent.json will not parse: {exc}"]
    out: list[str] = []
    for cap in m.get("capabilities") or []:
        if cap["name"] not in text:
            out.append(f"fabric/FABRIC-CONFORMANCE.md never names `{cap['name']}`, "
                       f"which fabric-agent.json declares — a host reads this "
                       f"document to learn the surface")
    rev = (m.get("provider") or {}).get("revision")
    if rev is not None and f"revision {rev}" not in text:
        out.append(f"fabric/FABRIC-CONFORMANCE.md does not name revision {rev}, "
                   f"which the manifest declares — the document describes a "
                   f"provider that no longer exists")
    return out


def failures() -> list[str]:
    from publish_contract import local_failures
    out: list[str] = list(local_failures())
    out += conformance_doc_failures()

    # 1 — the CLI's help must not name a step that does not exist.
    import observatory as obs
    doc = obs.__doc__ or ""
    named = set()
    for line in doc.splitlines():
        m = re.match(r"^\s{4}([a-z][a-z0-9-]+)\s{2,}\S", line)
        if m:
            named.add(m.group(1))
    unknown = sorted(named - set(obs.STEPS) - set(obs.GROUPS))
    if unknown:
        out.append(f"observatory.py's help names step(s) that do not exist: {unknown}")

    # 2 — a path a document names must resolve.
    for md in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md", ROOT / "AGENTS.md"]:
        if not md.is_file():
            continue
        for path in sorted(set(PATH_RX.findall(md.read_text(encoding="utf-8")))):
            # ONLY a path whose first segment is a top-level directory of THIS
            # repository. A bare `projects.json` is a name, not a path; a path
            # into a sibling repository belongs to that repository; a reference
            # path inside a skill lives in that skill. Checking those produced
            # dozens of "drifts" of which only a handful were real, and a checker
            # that cries wolf is the one nobody runs (trap T28).
            first = path.split("/", 1)[0]
            if "/" not in path or first not in TOP_LEVEL:
                continue
            if (path in PATH_ALLOWLIST or path.startswith(("http", "~"))
                    or any(path.startswith(pre) for pre in PATH_PREFIX_ALLOWLIST)):
                continue
            if not (ROOT / path).exists():
                out.append(f"{md.relative_to(ROOT)} names {path}, which does not exist")

    # 3 — the conformance report certifies A revision; it must be THIS one.
    manifest = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
    revision = manifest["provider"]["revision"]
    conformance = ROOT / "fabric/FABRIC-CONFORMANCE.md"
    if conformance.is_file():
        text = conformance.read_text(encoding="utf-8")
        if f"revision {revision}" not in text and f"rev-{revision}" not in text:
            out.append(f"FABRIC-CONFORMANCE.md does not name revision {revision}, "
                       f"which is what fabric-agent.json declares")

    # 4 — the PUBLISHED prose contract must list the probes that actually run.
    # `capabilities[i].profile.probes`, one level deeper than the first version
    # looked. Reading `cap["probes"]` found nothing, so the rule built an empty
    # set and passed — green because it had nothing to check, which is the shape
    # this whole file exists to catch. A rule that cannot fail is not a rule, so
    # it now REFUSES an empty set rather than treating it as agreement.
    declared = set()
    for cap in manifest.get("capabilities", []):
        for probe in (cap.get("profile") or {}).get("probes", []):
            declared.add(probe.get("name") or probe.get("id") or "")
    declared.discard("")
    if not declared:
        out.append("fabric-agent.json declares no probes at capabilities[].profile.probes "
                   "— either the manifest lost them or this rule is looking in the "
                   "wrong place, and both are defects")
    prose = ROOT / "fabric/probes/assertions.md"
    if prose.is_file() and declared:
        text = prose.read_text(encoding="utf-8")
        missing = sorted(p for p in declared if p not in text)
        if missing:
            out.append(f"fabric/probes/assertions.md — published to hosts — omits "
                       f"probe(s) the manifest declares: {missing}")
        # TEXT by text, not by count. `tools/run_probes.py` already compares the
        # runner's assertions with the manifest's this way, on the grounds that a
        # count cannot see a SWAPPED assertion — and a swapped one is exactly how
        # this document came to describe a fixture that no longer runs.
        for cap in manifest.get("capabilities", []):
            for probe in (cap.get("profile") or {}).get("probes", []):
                for a in probe.get("assertions", []):
                    if a not in text:
                        out.append(
                            f"assertions.md does not carry {probe.get('id')}'s assertion "
                            f"{a[:60]!r} — a host reads this instead of running the probe")

    # 6 — every table the store actually has must be named in the architecture.
    #     Four were not: `metrics`, `project_week`, `proposals` and `vec_meta`,
    #     two of which are the whole reason a statistic survives its source.
    arch = ROOT / "docs/ARCHITECTURE.md"
    schema = ROOT / "store/schema.sql"
    if arch.is_file() and schema.is_file():
        tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)",
                                schema.read_text(encoding="utf-8")))
        text = arch.read_text(encoding="utf-8")
        missing = sorted(x for x in tables if x not in text)
        if missing:
            out.append(f"docs/ARCHITECTURE.md does not name table(s) the store has: {missing}")

        # 6b — every document IN the registry must be named in the architecture.
        #      The registry is the project's canonical surface, and the list of
        #      its documents is what a reader uses to know what exists at all.
        #      `stale-remotes.json` was added beside `duplicate-repo-names.json`
        #      and the sentence listing them stayed as it was: the document
        #      remained TRUE about every file it named while being incomplete
        #      about the set, which rule 2 cannot see — it checks that a named
        #      path resolves, not that an existing path is named. Directories
        #      are named with a trailing slash (`snapshots/`), and `_raw` and
        #      `_inventory` are internal.
        for f in sorted(paths.REGISTRY.glob("*.json")):
            if f.name not in text:
                out.append(f"docs/ARCHITECTURE.md does not name registry document "
                           f"`{f.name}`, which exists")

        # 6c — the export must SAY that it carries tombstoned text. The file is
        #      committed and distributed, so a reader who believes a tombstone
        #      erased something will find it here; the sentence is the only
        #      thing standing between that belief and the file.
        exp = paths.REGISTRY / "ledger.jsonl"
        if exp.is_file():
            try:
                head = json.loads(exp.read_text(encoding="utf-8").splitlines()[0])
            except (ValueError, IndexError):
                head = {}
            if "TOMBSTONED REVISION" not in (head.get("_tombstoned_text_is_here") or ""):
                out.append("registry/ledger.jsonl's header does not say that it "
                           "carries tombstoned revisions' text, which it does")
            if "unreadable" not in (ROOT / "store/retention.py").read_text(
                    encoding="utf-8"):
                out.append("store/retention.py does not distinguish unreadable "
                           "from unrecoverable, which is what a tombstone does")

        # 7 — every top-level component must appear in the component tree.
        # The COMPONENT TREE, found by its root line — not "the first fenced
        # block", which is a different block and made the rule report four
        # directories the tree was never meant to hold.
        listed = ""
        for block in text.split("```"):
            if "project-observatory/" in block:
                listed = block
                break
        absent = sorted(d for d in TOP_LEVEL if f"{d}/" not in listed)
        if absent:
            out.append(f"docs/ARCHITECTURE.md's component tree omits: {absent}")

        # 8 — and it must quote NO store-row count. An earlier version gave the
        #     store's contents after two ticks; every figure was false the next
        #     afternoon. A design document states invariants, and a measurement
        #     belongs where it can be re-taken.
        volatile = re.findall(r"\b\d[\d,]{2,}\s+(?:events|revisions|scans|deltas|ledger rows)\b",
                              text)
        if volatile:
            out.append(f"docs/ARCHITECTURE.md quotes a store-row count: {volatile[:3]} — "
                       f"name the command that measures it instead")

    # 9 — a GENERATED document must describe the configuration it was generated
    #     from. The audit called `docs/AGENT_SYNC.md` stale because it was
    #     stamped twenty commits back — and that reasoning is wrong at both
    #     boundaries, as agent-sync's own checker says in a comment: a snapshot
    #     is written BEFORE the commit that carries it, and the config is often
    #     added in that same commit. The content hash is exact, so it is what is
    #     checked here. Measured 2026-09-07: the stamp matched, and the finding
    #     was a false alarm.
    snapshot = ROOT / "docs/AGENT_SYNC.md"
    cfg_file = ROOT / ".claude/agent-sync.json"
    if snapshot.is_file() and cfg_file.is_file():
        import hashlib
        head = snapshot.read_text(encoding="utf-8").split("\n", 1)[0]
        stamped = re.search(r"cfg=(\w+)", head)
        actual = hashlib.sha256(cfg_file.read_bytes()).hexdigest()[:12]
        if not stamped:
            out.append("docs/AGENT_SYNC.md carries no cfg= stamp, so nothing can tell "
                       "whether it describes the current configuration")
        elif stamped.group(1) != actual:
            out.append(f"docs/AGENT_SYNC.md describes configuration {stamped.group(1)}, "
                       f"the live one is {actual} — regenerate with `agent_sync.py setup`")

    # 10 — a ledger's header must agree with the ledger. See ledger_failures().
    for rel, prefix in LEDGERS:
        f = ROOT / rel
        if f.is_file():
            out += ledger_failures(f, prefix, rel)

    # 11 — no step name may be declared twice, and no group may list one twice.
    #
    #      Read from the SOURCE, because by import time the defect is gone: a
    #      duplicate dict key is not an error in Python, the last wins, and the
    #      earlier declaration disappears without a word. That happened here on
    #      2026-09-07 — `test-ledger` was declared a second time for a new suite,
    #      so `observatory.py test-ledger` ran the OLD suite, `check` ran it
    #      twice, and the new one ran nowhere. Every existing check passed: the
    #      table was well-formed, the step resolved, the file existed. Nothing
    #      could see it but the text.
    obs_src = (ROOT / "observatory.py").read_text(encoding="utf-8")
    keys = re.findall(r'^    "([a-z0-9-]+)": \[', obs_src, re.M)
    for name in sorted({k for k in keys if keys.count(k) > 1}):
        out.append(f"observatory.py declares step {name!r} {keys.count(name)} times — "
                   f"Python keeps the last and drops the rest silently; rename one")
    groups_blk = re.search(r"^GROUPS = \{(.*?)^\}", obs_src, re.S | re.M)
    if groups_blk:
        for gname, gbody in re.findall(r'"([a-z0-9-]+)":\s*\[(.*?)\]',
                                       groups_blk.group(1), re.S):
            listed = re.findall(r'"([a-z0-9-]+)"', gbody)
            for name in sorted({x for x in listed if listed.count(x) > 1}):
                out.append(f"observatory.py group {gname!r} lists {name!r} "
                           f"{listed.count(name)} times — it would run that many times")

    # 12 — every `test-*` step must be reachable from some group.
    #
    #      A suite that no group runs is a suite nobody runs.
    #      `tests/test_gate_purity.py` was declared as a step and listed in no
    #      group, and its assertion about `FOREIGN_WRITES_IGNORED` named one
    #      entry while the constant grew to four — red for two iterations while
    #      the gate was reported green, which was true of the group and false of
    #      the whole. It cannot sit inside `check`, because it drives a whole
    #      `./observatory.py check` as its fixture; that is a reason for a
    #      different group, not for none.
    #
    #      Scoped to `test-*` deliberately. `index-status`, `retention-apply`
    #      and `skip-sites` are also in no group and belong there: an operator
    #      command run on demand is not a check that stopped running.
    grouped = set()
    if groups_blk:
        for _g, gbody in re.findall(r'"([a-z0-9-]+)":\s*\[(.*?)\]',
                                    groups_blk.group(1), re.S):
            grouped |= set(re.findall(r'"([a-z0-9-]+)"', gbody))
        for name in sorted(k for k in set(keys) if k.startswith("test-")):
            if name not in grouped:
                out.append(f"observatory.py declares step {name!r} and no group runs it "
                           f"— a suite outside every group is a suite that has stopped "
                           f"being run, and it goes red without anybody noticing")

    # 13 — a scale claim carries the date it was measured on. See
    #      `scale_claim_failures` for why currency itself is not demanded.
    for rel in ("README.md", "AGENTS.md", "docs/ARCHITECTURE.md"):
        f = ROOT / rel
        if f.is_file():
            out += scale_claim_failures(f.read_text(encoding="utf-8"), rel)

    # 14 — the published registry shape describes the live registry.
    out += shape_doc_failures()

    # 5 — sentences the code has made false.
    for rel, phrase, why in RETIRED_CLAIMS:
        f = ROOT / rel
        if not f.is_file():
            continue
        # A retired claim QUOTED in the explanation of its retirement is not the
        # claim. The first version failed on its own replacement text, which is
        # the same distinction `tests/test_plugins.py` had to learn about a
        # plugin name in a comment: check the assertion, not the prose about it.
        text = "\n".join(
            re.sub(r'"[^"]*"', "", line) for line in f.read_text(encoding="utf-8").splitlines())
        if phrase in text:
            out.append(f"{rel} still says {phrase!r} — {why}")

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="what is checked")
    if ap.parse_args().list:
        print("1  every step the CLI's help names exists in its step table")
        print("2  every repository path a document names resolves")
        print("3  FABRIC-CONFORMANCE.md names the manifest's current revision")
        print("4  fabric/probes/assertions.md lists every declared probe")
        print(f"5  {len(RETIRED_CLAIMS)} retired claim(s) are absent")
        print("6  ARCHITECTURE.md names every table the store has")
        print("7  its component tree lists every top-level directory")
        print("8  and it quotes no store-row count")
        print("9  docs/AGENT_SYNC.md describes the LIVE agent-sync config")
        print("10 each ledger's `Next free ID` and vacant-id declaration "
              "agree with the ids it carries")
        print("11 no step name is declared twice, and no group lists one twice")
        print("12 every `test-*` step is reachable from at least one group")
        print("13 a scale claim carries the date it was measured on")
        print("14 docs/REGISTRY_SHAPE.md describes the LIVE registry's fields")
        return 0
    bad = failures()
    if not bad:
        print("documentation is current against every rule this file checks")
        return 0
    print(f"{len(bad)} documentation drift(s):", file=sys.stderr)
    for b in bad:
        print(f"  - {b}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
