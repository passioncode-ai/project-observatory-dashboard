#!/usr/bin/env python3
"""The LLM layer. It reads deltas, writes proposals, and cannot promote them.

Delta-driven, not poll-driven: with an empty delta table it exits having spent
nothing, which is what makes a scheduled agent affordable on a quiet machine.

**Nothing here names a model or a price.** Both live behind `agent/providers.py`,
which resolves the chain from `agent/models.json` and the numbers from the
provider's own catalogue. A price table in source is wrong the day a provider
changes one, and nothing notices.

**The ceiling is enforced on the write, in credits.** Three independent
guardrails converge on one answer: a daily cap, a monthly cap, and spend
velocity over a rolling window, because the first two catch a runaway tomorrow.

Degradation is honest in four places, because each is a real state:
  * no credential      -> collectors-only, deltas left unconsumed, said out loud
  * guardrail reached  -> same, plus which guardrail and the spend behind it
  * every model failed -> same; the chain is marked unhealthy and re-probed later
  * one call fails     -> that project's delta stays unconsumed, the others proceed

Nothing here approves anything. Every row lands `proposed` with confidence < 1;
promotion is the operator's or a second corroboration's.
"""
from __future__ import annotations
import argparse, json, os, sys, pathlib, sqlite3
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import paths
from store import db as store_db
from store import ledger as L
import store_faults
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import providers
import atomic
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "collectors"))
try:
    from compute_deltas import meaning_of as _meaning
except Exception:                                                                  
    def _meaning(_kind: str) -> str:
        return ""

OWNER = "agent:observer"
#: The ceiling on what the agent may assert about its own reading. Certainty
#: is the operator's to grant, and `proposed` at 1.0 reads as settled.
AGENT_MAX_CONFIDENCE = 0.95

#: The shape the model must return. Declared once, sent as the provider's
#: json_schema, and `strict: true` — so an answer that does not fit is the
#: provider's 400, not our parsing bug.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    # `required` lists the keys and enforces nothing else: it does not forbid
    # an empty `why_not`, and the schema cannot express the cross-field rules
    # its own descriptions state (an empty reason exactly when the answer is
    # worth recording). Strict structured outputs accept a subset of JSON
    # Schema with no `if`/`then`, so those rules live in the runtime loop: an
    # answer that contradicts itself is counted `malformed` and its deltas stay
    # unconsumed, and a decline with no reason is counted `unreasoned` in
    # `store/raw/agent.json`.
    "required": ["worth_recording", "interpretation", "confidence", "why_not"],
    "properties": {
        "worth_recording": {
            "type": "boolean",
            "description": "False when nothing non-obvious happened. Prefer false over a "
                           "manufactured reason."},
        "interpretation": {
            "type": "string",
            "description": "One or two sentences on what the change MEANS. Empty string "
                           "when worth_recording is false."},
        "confidence": {
            "type": "number", "minimum": 0, "maximum": 1,
            "description": "How well the delta supports this reading. Below 0.5 is a guess."},
        "why_not": {
            "type": "string",
            "description": "When worth_recording is false, why the change needs no note — "
                           "name what made it mechanical. Empty string when recording."},
    },
}

SYSTEM = """You interpret changes in a software estate for its single operator.

You are given what MOVED in one project between two scans, plus the typed facts
already known about it and any reasoning already recorded. Your job is the one
thing the diff cannot contain: what the change MEANS.

Rules, and they are not stylistic:
- Do not restate the delta. "5 files changed" is already recorded; repeating it
  in prose fills the field so nobody looks again, and carries no information.
- Say `worth_recording: false` whenever nothing non-obvious happened. A mechanical
  bump, a dependency refresh, a rename — these need no interpretation, and an
  empty record is better than a manufactured one.
- Never invent a cause. You see a diff summary, not the work. If the delta
  supports two readings, say both, or say you cannot tell. A fabricated reason is
  read as true by everything downstream and nothing can distinguish it later.
- Confidence is an assessment, not permission to hide doubt. Below 0.5 means you
  are guessing, and a guess should usually be `worth_recording: false`.
- One or two sentences. This is a note in a ledger, not a report.
- Write in English so the shared ledger can be reviewed consistently. Preserve
  proper names and quoted evidence in their original language."""


#: Above this share of non-Latin LETTERS, a statement is not in English. The
#: threshold is wide because the live data has no middle: measured 2026-09-07
#: across 112 waiting records, three sat at 98–100% non-Latin (one Chinese, two
#: Russian) and every other record at 0%. Half leaves room for an English
#: sentence quoting a Cyrillic project name or a Chinese repository description
#: — which is a real thing in this estate and must not be flagged.
FOREIGN_LETTER_SHARE = 0.5


def non_latin_share(text: str) -> float:
    """The share of a statement's letters that are not Latin. 0.0 when none.

    Letters only: digits, punctuation and the file paths a statement quotes are
    script-neutral and would dilute the measure toward zero on a short sentence.
    """
    letters = [ch for ch in text or "" if ch.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for ch in letters if ord(ch) < 0x250)
    return 1.0 - latin / len(letters)


class _Answer:
    """The provider returns a validated dict; the loop wants attributes."""

    def __init__(self, d: dict) -> None:
        self.worth_recording = bool(d.get("worth_recording"))
        self.interpretation = (d.get("interpretation") or "").strip()
        self.confidence = float(d.get("confidence") or 0.0)
        self.why_not = d.get("why_not")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def project_facts(pid: str) -> dict:
    try:
        projects = json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    for p in projects["projects"]:
        if p["id"] == pid:
            return {k: p.get(k) for k in
                    ("name", "description", "ownership", "lifecycle", "stack",
                     "local_folders", "membership_rules")}
    return {}


def already_recorded(conn, pid: str) -> list[str]:
    return [r["statement"] for r in conn.execute(
        "SELECT statement FROM ledger WHERE project_id = ? AND " + L.not_workflow("")
        + " ORDER BY created_at DESC LIMIT 5",
        (pid,))]


def fold(deltas: list) -> list[dict]:
    """One movement per KIND: oldest `before`, newest `after`, steps between.

    **Why this is a fold and not a bigger cap.** Sending only the oldest few
    deltas misrepresents a window: the oldest steps of a rise-then-commit cycle
    are all rises, so a sample can point the wrong way, not merely understate
    the work. Nothing downstream can catch that, because the project facts
    carry no quantitative field and these lines are the model's only handle on
    how much moved.

    Deltas of one kind over one window ARE a range, so folding drops nothing a
    cap was hiding, makes `provenance.deltas` true, and shortens every prompt,
    which is the point, because the budget is what binds this whole layer.

    **Arrival order, by `seq` where the caller supplies it.** "appeared then
    disappeared" and the reverse are different histories. The caller queries
    `ORDER BY rowid`; sorting on `seq` here is what makes that guarantee travel
    instead of resting on the list's accident. With no `seq` every key is equal
    and Python's stable sort keeps the given order.
    """
    out: dict[str, dict] = {}
    for i, d in enumerate(sorted(deltas, key=lambda r: r.get("seq", 0))):
        m = out.get(d["kind"])
        if m is None:
            out[d["kind"]] = {"kind": d["kind"], "before_json": d["before_json"],
                              "after_json": d["after_json"], "changes": 1, "at": i}
        else:
            m["after_json"] = d["after_json"]
            m["changes"] += 1
    return sorted(out.values(), key=lambda m: m["at"])


def build_prompt(pid: str, deltas: list, facts: dict, prior: list[str]) -> str:
    lines = [f"# Project\n{pid}", "", "## Typed facts already known",
             json.dumps(facts, ensure_ascii=False, indent=1), "", "## What moved"]
    for m in fold(deltas):
        # The MEANING beside the numbers. `collectors/compute_deltas.py` has held
        # a table of what each field's change means "in words the agent will
        # read" since it was written, and the agent never received it — only the
        # keys were used, to list which fields moved. `commits-changed: 885 ->
        # 886` and "commits were recorded" are different amounts of help.
        means = _meaning(m["kind"])
        # NO STEP COUNT ON A SINGLE CHANGE. When the agent runs on its own
        # schedule almost every movement is one step, so `(over 1 changes)` would
        # be noise on nearly every prompt — and wrong grammar on all of them.
        span = f"   (over {m['changes']} changes)" if m["changes"] > 1 else ""
        lines.append(f"- {m['kind']}: {m['before_json']} -> {m['after_json']}"
                     + span + (f"   ({means})" if means else ""))
    if prior:
        lines += ["", "## Already recorded about this project (do not repeat)"]
        lines += [f"- {s[:200]}" for s in prior]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20, help="projects per run")
    ap.add_argument("--model", default=None,
                    help="override the configured chain with one model id. The caller is the "
                         "highest of the three selection levels and the choice is logged.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent and spend nothing")
    args = ap.parse_args()

    conn = store_db.connect()
    try:
        #: One entry per project the agent could not interpret. The COUNT was
        #: already in the report and read by nothing — the other `rec["failed"]`
        #: in tools/build_findings.py belongs to retention's receipt — and a
        #: number saying "3 projects failed" sends the reader to a log to find
        #: out what happened, which is the journey the report exists to spare
        #: them.
        faults: list[dict] = []

        def note_fault(pid: str, kind: str, reason) -> None:
            """Why one project was left uninterpreted.

            `kind` is the REMEDY's address, not the exception's name: `store` and
            `model-unavailable` are fixed in different places, and a reader who
            has only a count cannot tell which of the two happened.
            """
            faults.append({"project": pid, "kind": kind,
                           "reason": (f"{type(reason).__name__}: {reason}"
                                      if isinstance(reason, BaseException)
                                      else str(reason))[:200]})
            # AND DURABLY, for a store fault. `agent.json#faults` is one run's
            # report and the next run overwrites it; the four store failures of
            # 2026-09-05..07 were each visible for thirty minutes and then only
            # as a line in the tick log, with the machine's state — free space,
            # WAL size, holders — never recorded at all. `store_faults` never
            # raises, so this cannot turn a handled fault into an unhandled one.
            if kind == "store":
                store_faults.record("agent", reason if isinstance(reason, BaseException)
                                    else None,
                                    detail=f"project {pid}" if isinstance(reason, BaseException)
                                    else f"project {pid}: {reason}")

        def report(halted: str | None, rec: int = 0, skip: int = 0, fail: int = 0,
                   bad: int = 0, mute: int = 0,
                   retired: list[str] | None = None,
                   handled: list[str] | None = None,
                   waiting: list[str] | None = None, foreign: int = 0) -> None:
            """The run's outcome as a fact, on EVERY path.

            Written by a helper rather than at the end, because the degradations
            below `return 0` before the end is reached, and they are the cases
            that most need reporting: a halted interpretation layer must be
            visible outside the log, and a report reachable only on the happy
            path would hide exactly that.
            """
            # PROJECTS, not only deltas: the cost of draining this queue is one
            # call per PROJECT, not per delta, because the fold keeps a project's
            # prompt from growing with its backlog. A delta count alone cannot be
            # turned into money.
            left = conn.execute(
                "SELECT count(*) n, count(DISTINCT subject_id) projects,"
                "       min(s.started_at) since FROM deltas d"
                " LEFT JOIN scans s ON s.id = d.to_scan"
                " WHERE d.consumed_at IS NULL").fetchone()
            atomic.write_json(paths.SCRATCH / "agent.json", {
                "ran_at": now(), "recorded": rec, "skipped": skip, "failed": fail,
                # TWO SHAPES OF A BAD ANSWER, counted separately because they
                # need different remedies. `malformed` contradicts itself and
                # leaves its deltas unconsumed; `unreasoned` is a usable
                # judgement with no reason given, and its delta IS consumed.
                "malformed": bad, "unreasoned": mute,
                # THE REASONS, not only the count. Bounded by the finding rather
                # than here: this file is gitignored scratch, and truncating the
                # evidence at the source would leave the reader nothing to widen.
                "faults": faults,
                "chain_retired": retired or [],
                "halted_by": halted,
                "unconsumed": left["n"] if left else 0,
                "projects_unconsumed": left["projects"] if left else 0,
                "oldest_unconsumed_scan": (left["since"] if left else None),
                # WHOSE WORK WAS READ, and whose was not. The report said
                # "recorded 3, skipped 17" and named nobody — so a run that
                # served the same twenty projects every time, leaving the rest
                # for ever, produced a receipt indistinguishable from one that
                # was working through a backlog. Starvation has to be visible in
                # the record or it is only visible in a complaint.
                # A RECORD ITS READER CANNOT READ occupies the review queue
                # and cannot be judged, so the count belongs beside the other
                # two shapes of a bad answer.
                "not_english": foreign,
                "projects_handled": handled or [],
                "projects_waiting": waiting or [],
                "waiting_count": len(waiting or [])})

        # `rowid` COMES TOO, because the queue is served oldest-first below and
        # `deltas` carries no timestamp of its own — `from_scan`/`to_scan` name
        # the fingerprints, and `rowid` is monotonic in insertion order, which
        # is exactly the question "which of these arrived first" (the same
        # property `compute_deltas` relies on for its own ordering).
        rows = [dict(r) for r in conn.execute(
            "SELECT rowid AS seq, id, subject_id, kind, before_json, after_json"
            " FROM deltas WHERE consumed_at IS NULL ORDER BY rowid")]
        if not rows:
            print("no unconsumed deltas — nothing moved, and this run spent nothing")
            # REPORTED, because this is the state that CLEARS the stall finding.
            # Leaving the previous run's report on disk would keep
            # `interpretation.halted` lit after the queue drained. Every exit
            # must report.
            report(None)
            return 0

        by_project: dict[str, list] = {}
        for r in rows:
            by_project.setdefault(r["subject_id"], []).append(r)
        # OLDEST FIRST, not alphabetically. `sorted(by_project)[:limit]` took the
        # alphabetically first 20 of however many were waiting — and the head of
        # the alphabet is re-filled constantly, because the busiest projects
        # keep producing deltas. So a project whose name sorts late was NEVER
        # interpreted: measured 2026-09-07, 22 projects were waiting behind a
        # limit of 20, the oldest delta was fifteen hours old, and
        # `interpretation.halted` had been lit for exactly that long. A queue
        # served in any order but arrival order starves its tail.
        #
        # The tiebreak is the project id, so two projects whose oldest delta
        # arrived in the same transaction still order deterministically.
        order = sorted(by_project, key=lambda pid: (min(r["seq"] for r in by_project[pid]), pid))
        projects = order[:args.limit]
        waiting = order[args.limit:]
        print(f"{len(rows)} delta(s) across {len(by_project)} project(s); "
              f"this run takes {len(projects)}, oldest first"
              + (f"; {len(waiting)} project(s) wait for the next run" if waiting else ""))

        # A dry run spends nothing and needs no credential, so it answers before
        # either is consulted.
        if args.dry_run:
            for pid in projects:
                p = build_prompt(pid, by_project[pid], project_facts(pid),
                                 already_recorded(conn, pid))
                print(f"\n{'=' * 70}\n{p}")
            print(f"\n--- dry run: {len(projects)} prompt(s), 0 tokens spent")
            # NOT reported, deliberately: a dry run spends nothing and decides
            # nothing, so overwriting the record of the last REAL run would
            # erase the reason a stall is being reported. Same rule as
            # `tools/corroborate.py`'s `if not dry`.
            return 0

        # The credential check comes BEFORE the budget check, and the order is
        # load-bearing. `check_budget` reads the provider's own counters, so it
        # resolves the key — and `read_key` REFUSES a key file the group or world
        # can read, by raising. `have_key()` catches that; `check_budget()` does
        # not. With the checks the other way round, a key at mode 644 produced an
        # uncaught traceback in the scheduled tick instead of the degradation
        # written for exactly that case. Three of the four degradations here were
        # driven by tests; this was the fourth.
        if not providers.have_key():
            print(f"DEGRADED: {providers.key_status()}\n"
                  f"The collectors already recorded the facts; only the interpretation is "
                  f"missing. {len(rows)} delta(s) stay unconsumed and will be interpreted "
                  f"on the next run.", file=sys.stderr)
            report(f"no credential: {providers.key_status()}")
            return 0

        stop = providers.check_budget()
        if stop:
            print(f"DEGRADED: {stop}. Deltas are left unconsumed for the next run; "
                  f"collectors are unaffected.", file=sys.stderr)
            report(f"spend guardrail: {stop}")
            return 0

        recorded = failed = skipped = malformed = unreasoned = foreign = 0
        # THE CHAIN'S OWN LOSSES, in the report a reader opens. A configured
        # model missing from the catalogue is skipped rather than fatal — the
        # point of a chain is surviving one model leaving — but it was skipped
        # SILENTLY, so a three-model chain could become one and the only visible
        # sign would be the bill.
        retired_models: list[str] = []
        try:
            _chain, _lvl, _prov = providers.resolve_chain()
            retired_models = list((_chain[0] if _chain else {}).get("chain_retired") or [])
            if retired_models:
                print(f"  chain: {len(retired_models)} configured model(s) are not in "
                      f"the catalogue and were skipped: {', '.join(retired_models)}",
                      file=sys.stderr)
        except Exception as exc:
            # Not fatal here: `complete` resolves the chain itself and will fail
            # loudly if it cannot. This is only for the report.
            print(f"  chain could not be resolved for the report: {exc}", file=sys.stderr)
        halted_by = None
        first_call = True
        for pid in projects:
            prompt = build_prompt(pid, by_project[pid], project_facts(pid),
                                  already_recorded(conn, pid))
            try:
                result = providers.complete(
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
                    schema_name="interpretation", schema=SCHEMA,
                    requested=args.model,
                    # The key and the chain are a property of the RUN, not of each
                    # project. Printed once, they are provenance; printed per
                    # project, they are noise that hides the results.
                    log=(lambda m: print(m)) if first_call else (lambda m: None))
                first_call = False
            except providers.BudgetExceeded as exc:
                print(f"  guardrail mid-run: {exc}; the rest stays unconsumed",
                      file=sys.stderr)
                halted_by = f"spend guardrail: {exc}"
                break
            except providers.CredentialError as exc:
                halted_by = f"credential refused: {exc}"
                print(f"  DEGRADED: the credential was refused — {exc}\n"
                      f"  No model is marked unhealthy: the key is the fault, not the model. "
                      f"Every delta stays unconsumed.", file=sys.stderr)
                note_fault(pid, "credential", exc)
                failed += 1; break
            except providers.Fatal as exc:
                print(f"  {pid}: fatal — {exc}", file=sys.stderr)
                note_fault(pid, "model-fatal", exc)
                failed += 1; break
            except providers.Retryable as exc:
                print(f"  {pid}: every model failed — {exc}", file=sys.stderr)
                note_fault(pid, "model-unavailable", exc)
                failed += 1; break
            except Exception as exc:
                print(f"  {pid}: {type(exc).__name__}: {exc}", file=sys.stderr)
                note_fault(pid, "unexpected", exc)
                failed += 1; continue

            parsed = _Answer(result["parsed"])
            model_used, spent_here = result["model"], result["cost"]
            ids = [d["id"] for d in by_project[pid]]

            # AN ANSWER THAT CONTRADICTS ITSELF is a model failure, not a
            # conclusion. `worth_recording: true` with an empty interpretation
            # would have appended a blank row for an operator to adjudicate:
            # the schema requires the four KEYS and cannot express the
            # cross-field rule, because OpenAI's `strict: true` structured
            # outputs accept a subset of JSON Schema without `if`/`then`. So the
            # runtime is the only place this can live, and until now it lived
            # nowhere.
            #
            # The deltas stay UNCONSUMED: the change was not interpreted, so a
            # later run — possibly a healthier model — should see it again.
            if parsed.worth_recording and not parsed.interpretation:
                malformed += 1
                note_fault(pid, "malformed", "the answer contradicted its own schema")
                failed += 1
                print(f"  {pid}: MALFORMED — worth_recording with no interpretation; "
                      f"the deltas stay unconsumed", file=sys.stderr)
                continue

            if not parsed.worth_recording:
                with conn:
                    conn.executemany("UPDATE deltas SET consumed_at = ? WHERE id = ?",
                                     [(now(), i) for i in ids])
                skipped += 1
                # A DECLINE WITH NO REASON IS COUNTED, not only printed. The
                # schema's own comment says `why_not` is required "so a decline
                # has to say something" — and `required` does not forbid `""`,
                # which is exactly the defect that comment describes: the first
                # live run declined eight projects with an empty reason. Making
                # the key required did not fix it.
                #
                # It is NOT treated as malformed. The judgement — nothing worth
                # recording — is usable, and re-asking would likely produce the
                # same answer while the queue stalled. So the delta is consumed
                # and the gap becomes a number the run reports, which is the
                # difference between a console line and a fact.
                if not (parsed.why_not or "").strip():
                    unreasoned += 1
                print(f"  {pid}: declined — {parsed.why_not or 'NO REASON GIVEN (a gap)'}")
                continue
            # ONE record per project per DAY, corrected — not a new one per tick.
            # Measured 2026-09-07: 106 proposed conclusions sat in 63 (project,
            # day) buckets, one project holding TEN separate rows about a single
            # day, and the queue was growing about fifty a day while the only way
            # out is an operator at a terminal. That is not an adjudication
            # problem, it is a volume one — nobody reads fifty interpretations a
            # day, so they would all leave by retention, which `corroborate.py`
            # itself calls "not review".
            #
            # The pattern is already proven here: `tools/record_turn.py` keeps one
            # record per SESSION and corrects it by compare-and-swap as the work
            # grows, for the same reason. Append-only is preserved —
            # a correction is a new REVISION, not a new record, so every earlier
            # reading stays readable.
            try:
                prior = conn.execute(
                    # A tombstoned record takes no further revisions,
                    # and correcting one would raise inside the agent's write loop.
                    "SELECT memory_id, MAX(revision) AS revision, statement FROM ledger"
                    " WHERE project_id = ? AND kind = 'observation' AND owner = ?"
                    "   AND substr(created_at, 1, 10) = ?"
                    "   AND memory_id NOT IN (SELECT memory_id FROM tombstones)"
                    " GROUP BY memory_id ORDER BY revision DESC LIMIT 1",
                    (pid, OWNER, now()[:10])).fetchone()
            except sqlite3.DatabaseError as exc:
                # A FAILED READ IS NOT "no prior". Defaulting to None would append
                # a SECOND record for this project today, exactly the duplicate
                # this lookup exists to prevent, so the delta stays unconsumed
                # and the reason is named. That is this loop's own stated
                # contract: "one call fails -> that project's delta stays
                # unconsumed, the others proceed".
                #
                # A transient `database disk image is malformed` here once took
                # the whole scheduled run down with a traceback, although the
                # store checked clean afterwards. A broken read is a
                # degradation, not a traceback.
                print(f"  {pid}: the store could not be read for the duplicate "
                      f"check — {type(exc).__name__}: {str(exc)[:80]}. Its delta "
                      f"stays unconsumed; the other projects proceed.",
                      file=sys.stderr)
                note_fault(pid, "store", exc)
                failed += 1
                continue
            if prior is not None and prior["statement"] == parsed.interpretation:
                # The same sentence twice adds a revision that says nothing new,
                # and append-only is a reason to be careful about what is appended.
                with conn:
                    conn.executemany("UPDATE deltas SET consumed_at = ? WHERE id = ?",
                                     [(now(), i) for i in ids])
                skipped += 1
                print(f"  {pid}: unchanged since this morning's reading — not re-recorded")
                continue
            # ONE value, computed once and used by both the write and the log.
            # The log printed `parsed.confidence` while the row stored the
            # capped figure, so a model returning 1.0 produced the line
            # `confidence 1.00` over a row holding 0.95 — a journal reporting
            # something other than what was written.
            # A THIRD SHAPE OF A BAD ANSWER, counted rather than refused. The
            # system prompt now pins English and gives the reason; a prompt is
            # advice to a model, so the outcome is measured. It is recorded
            # anyway — the content may be right, the deltas must be consumed so
            # the queue does not loop on a model that keeps answering the same
            # way, and `unreasoned` set that precedent: a usable judgement with
            # a gap is counted, not thrown away.
            if non_latin_share(parsed.interpretation) > FOREIGN_LETTER_SHARE:
                foreign += 1
                print(f"  {pid}: NOT IN ENGLISH — recorded, and counted; every "
                      f"reader of this ledger is English-language",
                      file=sys.stderr)
            stored_confidence = max(0.01, min(AGENT_MAX_CONFIDENCE,
                                              parsed.confidence))
            try:
                res = L.append(
                    conn, owner=OWNER, kind="observation",
                    memory_id=prior["memory_id"] if prior else None,
                    expected_revision=prior["revision"] if prior else None,
                    statement=parsed.interpretation, project_id=pid,
                    function="episodic", scope="project", state="proposed",
                    # Capped BELOW 1: an automated writer may not claim certainty.
                    # The model returned exactly 1.0 on a real run and it was let
                    # through, because the only check was that the source contained
                    # a min() call — a test that read the code instead of the data.
                    confidence=stored_confidence,
                    # `deltas` is what the row CONSUMED; `movements` is what the
                    # model actually read. They were the same number by
                    # assumption and were not: the prompt carried the oldest
                    # twelve of 41 while this said 41. Folded, the
                    # first is true again — and the second is recorded beside it
                    # so a reader can still tell the two facts apart.
                    provenance=[{"source": "agent/observe", "model": model_used,
                                 "cost_credits": round(spent_here, 8),
                                 "selection_level": result["selection_level"],
                                 "deltas": len(by_project[pid]),
                                 "movements": len(fold(by_project[pid]))}],
                    evidence=[{"uri": f"delta:{d['id']}", "kind": d["kind"]}
                              for d in by_project[pid]])
            except L.LedgerError as exc:
                print(f"  {pid}: ledger refused the write — {exc}", file=sys.stderr)
                note_fault(pid, "ledger", exc)
                failed += 1; continue
            with conn:
                conn.executemany("UPDATE deltas SET consumed_at = ? WHERE id = ?",
                                 [(now(), i) for i in ids])
            recorded += 1
            print(f"  {pid}: {res['memoryId']}@{res['revision']} proposed "
                  f"(confidence {stored_confidence:.2f}, {model_used}, "
                  f"{spent_here:.6f} credits)")
            if result["stop"]:
                print(f"  guardrail now says stop: {result['stop']}; the rest stays unconsumed")
                break

        w = providers.wallet_state()
        # THE RUN'S OUTCOME AS A FACT. Everything below this line went to stdout,
        # which the tick swallows into `store/logs/tick.log` — and the log is not
        # something a person reads. Measured 2026-09-07: the interpretation layer
        # had been halted since 01:30 by a ceiling belonging to a SHARED key that
        # another consumer had taken to 36.03 of 2.00, with 77 deltas queued, and
        # nothing anywhere told the operator. The collectors were unaffected, so
        # the estate's facts stayed current while its narrative silently stopped.
        report(halted_by, recorded, skipped, failed, malformed, unreasoned,
               retired_models, handled=list(projects), waiting=list(waiting),
               foreign=foreign)
        print(f"\nrecorded {recorded} · skipped {skipped} · failed {failed}")
        # THE FIGURES THAT DECIDED, which are this project's own.
        # This printed `w['today']` — the KEY's daily total — beside this
        # project's ceiling, so the line read "110.82 of 2.00 credits today"
        # immediately after the guardrail had correctly PERMITTED the run. A
        # summary that contradicts the decision it reports is worse than no
        # summary; the key's figure belongs beside it, named as the key's.
        print(f"wallet: {w['local_today']:.6f} of {w['daily_ceiling']:.2f} "
              f"{w['denomination']} today, "
              f"{w['local_month']:.4f} of {w['monthly_ceiling']:.2f} this month, "
              f"{w['window_spend']:.6f} in the last {w['window_minutes']}min "
              f"(ceiling {w['velocity_ceiling']:.2f})"
              + (f" · the shared KEY shows {w['key_today']:.2f} today, "
                 f"{w['key_month']:.2f} this month"
                 if w.get('key_today') is not None else ""))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    import configuration
    if (True) and not configuration.enabled("agent", "features"):
        print("Not configured: enable features.agent explicitly")
        raise SystemExit(0)

    raise SystemExit(main())
