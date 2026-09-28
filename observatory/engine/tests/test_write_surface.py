#!/usr/bin/env python3
"""The wire's two write tools, and the four things they accepted while saying otherwise.

`observatory_record` and `observatory_propose` are the only tools that write, and
their guards are careful about the one thing that matters most — neither can claim
the operator's identity. Before this suite, everything else was documentation:

    propose(targetId="project:also-not-real", …)          -> "proposed"
    patch={"activity_tier": …, "id": …, "source_refs": …}   -> "proposed"
    patch with no evidence at all                           -> "proposed"
    record(statement="x" * 2_000_000)                       -> accepted

The first names a subject that does not exist. The second asks to change a
DERIVED field the emitter recomputes on the next tick, the record's own id, and
its provenance. The third contradicts the tool's own field description — *"A
patch with no evidence is a guess"*. The fourth would put two megabytes in a row
no index can carry: the embedding request fails for ever while the outbox keeps
retrying it.

**And the queue had a reporter with no decider.** `review.py` could promote,
reject and tombstone a LEDGER row by `memory_id`; nothing anywhere could accept
a registry proposal. A caller was told "proposed" for a row that could only ever
be listed.

**Where an accepted proposal lands is the interesting part.** Not the registry:
`registry/*.json` is rewritten whole from the model on every emit, so a patch
written there survives until the next tick and no longer. The curation files —
`project_overrides.json`, `repo_overrides.json` — are what the emitter applies on
top of what it measured, so that is where a human decision belongs, and the
appliable field set is DERIVED from them rather than restated: with the fixture
policy below, that is `name`, `description`, `canonical_page` for a project and
`description`, `source_refs` for a repository. `domain:` accepts nothing and says why —
`registry/domains.json` is the operator's transcription of documents they
supplied, and there is no overrides file an accepted change could land in.

**On the pty below.** `require_terminal` is crossed here by allocating one, and
that is not a weakening: `review.py`'s own docstring says *"Requiring a TTY does
not stop a determined process — it can allocate one — and that is not the claim.
The claim is that approval is an ACT."* The gate exists so that approval cannot
happen as a side effect of a script; a test that deliberately performs the act is
the case the gate was designed around. No test had ever crossed it, so the accept
path would otherwise have shipped undriven.
"""
from __future__ import annotations
import importlib.util, json, os, pathlib, pty, shutil, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []
EV = [{"uri": "git:abc123", "note": "the commit that showed it"}]
import paths  # noqa: E402

# The appliable field set is derived from the selected workspace's curation, so
# the fixture owns that curation: a synthetic policy that uses every field the
# assertions below name. The workspace is the runner's throwaway one.
paths.config_file("project_overrides.json").write_text(json.dumps({"projects": {
    "project:alpha-web": {"name": "Alpha Web", "description": "Synthetic",
                          "canonical_page": "https://example.com/alpha",
                          "why": "Synthetic policy"}}}), encoding="utf-8")
paths.config_file("repo_overrides.json").write_text(json.dumps({"repositories": {
    "repository:example/beta-api": {"description": "Synthetic",
                                    "source_refs": ["SRC-0005"],
                                    "why": "Synthetic policy"}}}), encoding="utf-8")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def wire(db: pathlib.Path):
    """The server module, against a scratch store.

    THE PACKAGE GOES TOO, not only its submodules. Popping `sys.modules["store.db"]`
    leaves the attribute `db` on the cached `store` package object, so a freshly
    executed `mcp/server.py` doing `from store import db as store_db` gets the OLD
    module — bound to the OLD `paths.DB`. The first version of this file did
    exactly that and the wire wrote into a previous test's temp directory, which
    surfaced as `no such table: proposals` and read like a defect in the decider.
    """
    os.environ["OBSERVATORY_DB"] = str(db)
    for mod in [k for k in list(sys.modules)
                if k in ("paths", "srv_write", "proposals", "survey", "store")
                or k.startswith("store.")]:
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location("srv_write", ROOT / "mcp/server.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["srv_write"] = m
    spec.loader.exec_module(m)
    return m


def store() -> tuple[pathlib.Path, object]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-write-"))
    return d, wire(d / "observatory.db")


# ─────────── the propose refusals ──────────────────────────────────────

def test_a_proposal_nothing_could_apply_is_refused() -> None:
    d, m = store()
    cases = [
        ("no evidence", dict(targetId="project:alpha-web",
                             patch={"description": "x"}, evidence=None),
         "no evidence is a guess"),
        ("a derived field", dict(targetId="project:alpha-web",
                                 patch={"activity_tier": "active"}, evidence=EV),
         "activity_tier cannot be applied"),
        ("the record's own id", dict(targetId="project:alpha-web",
                                     patch={"id": "project:hijack"}, evidence=EV),
         "id cannot be applied"),
        ("its provenance", dict(targetId="repository:example/beta-api",
                                patch={"source_refs": ["SRC-1"]}, evidence=EV),
         None),          # source_refs IS appliable for a repository — see below
        ("a domain", dict(targetId="domain:example.com",
                          patch={"description": "x"}, evidence=EV),
         "domains are not derived"),
        ("an unknown kind", dict(targetId="nonsense",
                                 patch={"description": "x"}, evidence=EV),
         "is neither"),
        ("an empty patch", dict(targetId="project:alpha-web",
                                patch={}, evidence=EV),
         "proposes nothing"),
    ]
    for label, kwargs, fingerprint in cases:
        r = m.observatory_propose(owner="agent:test", **kwargs)
        if fingerprint is None:
            check(f"{label} is ACCEPTED, because the curation file supplies it",
                  r.get("status") == "proposed", str(r)[:160])
            continue
        check(f"{label} is refused", r.get("error") == "unappliable-proposal",
              str(r)[:160])
        check(f"…saying {fingerprint!r}", fingerprint in (r.get("detail") or ""),
              (r.get("detail") or "")[:200])
    r = m.observatory_propose(owner="agent:test", targetId="project:alpha-web",
                              patch={"description": "a better sentence"}, evidence=EV)
    check("and a legitimate proposal still lands", r.get("status") == "proposed",
          str(r)[:160])


def test_the_refusal_says_what_IS_appliable() -> None:
    d, m = store()
    r = m.observatory_propose(owner="agent:test", targetId="project:alpha-web",
                              patch={"nonsense": 1}, evidence=EV)
    check("the answer carries the appliable set", "appliable" in r, str(sorted(r)))
    if "appliable" in r:
        check("naming the project fields",
              set(r["appliable"]["project:"]) >= {"description", "name"},
              str(r["appliable"]))
        check("and the repository ones",
              "description" in r["appliable"]["repository:"], str(r["appliable"]))


def test_the_appliable_set_is_derived_from_the_curation_files() -> None:
    import proposals
    got = proposals.appliable()
    for prefix, (name, key) in proposals.LANDS_IN.items():
        doc = json.loads(paths.config_file(name).read_text(encoding="utf-8"))
        fields = set()
        for row in doc[key].values():
            fields |= set(row)
        # Plus the structured fields the engine always admits for a project
        # (organization, resources): agents report what they created through
        # them before any operator row has used them.
        want = (fields - proposals.PROVENANCE) | proposals.STRUCTURED.get(prefix, set())
        check(f"{prefix} matches {name} exactly",
              got[prefix] == want, f"{sorted(got[prefix])} vs {sorted(want)}")
    check("provenance is not proposable", "why" not in got["project:"],
          "`why` is written by the decision, not by the caller")


def test_an_unknown_subject_is_queued_with_a_warning() -> None:
    """Not refused, deliberately: a project may arrive on the next tick."""
    d, m = store()
    r = m.observatory_propose(owner="agent:test", targetId="project:not-yet",
                              patch={"description": "x"}, evidence=EV)
    check("it is accepted", r.get("status") == "proposed", str(r)[:160])
    check("with a warning naming the id", "project:not-yet" in (r.get("warning") or ""),
          str(r.get("warning"))[:160])
    check("saying a typo will never be applied", "typo" in (r.get("warning") or ""),
          str(r.get("warning"))[:160])


# ─────────── the record bound ──────────────────────────────────────────

def test_a_transcript_sized_record_is_refused() -> None:
    d, m = store()
    r = m.observatory_record(owner="agent:test", statement="x" * 2_000_000)
    check("two million characters is refused", r.get("error") == "LedgerError",
          str(r)[:160])
    check("naming the limit and the measurement",
          "the limit is 4,000" in (r.get("detail") or "")
          and "406" in (r.get("detail") or ""), (r.get("detail") or "")[:200])
    r = m.observatory_record(owner="agent:test", statement="a normal claim",
                             why="x" * 5000)
    check("`why` is bounded too", r.get("error") == "LedgerError", str(r)[:120])
    r = m.observatory_record(owner="agent:test", statement="a normal claim")
    check("and an ordinary record still lands", r.get("state") == "proposed",
          str(r)[:120])


def test_the_bound_is_structural_and_not_at_the_call_site() -> None:
    """Every door, not just the wire. `record_turn.py` and `agent/observe.py`
    append too, and a guard one caller can skip is the shape
    the retention policy's own note calls out."""
    import store.ledger as L
    src = (ROOT / "store/ledger.py").read_text(encoding="utf-8")
    check("the limit is declared once", "MAX_TEXT = 4000" in src)
    check("and checked inside `append`", "len(value) > MAX_TEXT" in src,
          "a limit enforced at the wire leaves every other writer unbounded")
    d, m = store()
    from store import db as sdb
    conn = sdb.connect()
    raised = ""
    try:
        L.append(conn, owner="agent:direct", statement="y" * 9000)
    except L.LedgerError as exc:
        raised = str(exc)
    finally:
        conn.close()
    check("a DIRECT append is bounded as well", "the limit is 4,000" in raised,
          raised[:120] or "no refusal")


# ─────────── the decider ───────────────────────────────────────────────

def review(db: pathlib.Path, *args: str) -> tuple[int, str]:
    """Run review.py with a real terminal on stdin.

    See the module docstring: allocating a pty performs the ACT the gate is
    about, rather than bypassing it. No test had crossed this gate before, so
    the accept path would have shipped undriven.
    """
    primary, secondary = pty.openpty()
    try:
        p = subprocess.run([PY, "tools/review.py", *args], cwd=ROOT,
                           stdin=secondary, capture_output=True, text=True,
                           timeout=600, env=dict(os.environ, OBSERVATORY_DB=str(db)))
    finally:
        os.close(primary)
        os.close(secondary)
    return p.returncode, p.stdout + p.stderr


def test_the_terminal_gate_still_refuses_a_pipe() -> None:
    d, _ = store()
    p = subprocess.run([PY, "tools/review.py", "accept-proposal", "x", "--why", "t"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300,
                       env=dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db")))
    check("without a terminal it refuses", p.returncode != 0, str(p.returncode))
    check("naming the authority at stake", "owned by `operator`" in p.stdout + p.stderr,
          (p.stdout + p.stderr)[:160])
    check("and stating there is no --yes", "no --yes" in p.stdout + p.stderr,
          (p.stdout + p.stderr)[-120:])


def test_an_accepted_proposal_lands_in_the_curation_file() -> None:
    """Drive a selected policy and its emitter without editing author curation."""
    import test_curation_paths as fixture
    before = len(fixture.FAILURES)
    fixture.test_selected_proposal_writer_and_emitter()
    check("selected curation writer, provenance and emitter agree",
          len(fixture.FAILURES) == before)


def test_accept_and_reject_decide_the_row() -> None:
    d, m = store()
    db = d / "observatory.db"
    # `proposalId`, not `id`: the wire names it that and the table column is
    # `id`. The first version of this test read `["id"]` and raised a KeyError —
    # a fixture asserting a field name the surface does not use.
    made = [m.observatory_propose(owner="agent:test", targetId="project:alpha-web",
                                  patch={"description": f"candidate {i}"},
                                  evidence=EV)["proposalId"] for i in range(2)]
    code, out = review(db, "reject-proposal", made[1], "--why", "not what I meant")
    check("reject succeeds with a terminal", code == 0, out[-200:])
    check("and says the row stays with the reason", "stays with" in out, out[-160:])
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    got = dict(conn.execute(
        "SELECT status, decided_by, decided_note FROM proposals WHERE id = ?",
        (made[1],)).fetchone())
    conn.close()
    check("the status is rejected", got["status"] == "rejected", str(got))
    check("decided by the operator", got["decided_by"] == "operator", str(got))
    check("with the reason recorded", got["decided_note"] == "not what I meant",
          str(got))
    code, out = review(db, "reject-proposal", made[1], "--why", "again")
    check("a decided proposal cannot be decided twice", code != 0, out[-160:])
    check("saying what it already is", "already" in out, out[-160:])


def test_accept_re_checks_appliability_at_the_decision() -> None:
    """A proposal queued before a curation file changed shape may no longer be
    appliable, and applying it anyway would write a field the emitter ignores."""
    src = (ROOT / "tools/review.py").read_text(encoding="utf-8")
    check("the decision re-runs the refusal check",
          "proposals.refusal(row[\"target_id\"]" in src,
          "trusting the wire's verdict at decision time is trusting a check that "
          "ran against a different file")
    check("and it writes provenance rather than accepting it from the caller",
          'merged["proposed_by"]' in src and 'merged["why"] = args.why' in src,
          "an override with no argument behind it cannot be maintained")
    check("through `atomic.write_json`, so a failed write keeps the old file",
          "atomic.write_json(f, doc)" in src)
    check("and it says the registry changes on the next emit, not now",
          "next emit" in src,
          "a decision that looks immediate but is not is the worse lie")


# ─────────── the orphaned subjects ─────────────────────────────────────

def test_a_note_about_a_missing_project_is_reported() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-orph-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps(
        {"projects": [{"id": "project:real"}]}), encoding="utf-8")
    db = d / "observatory.db"
    env = dict(os.environ, OBSERVATORY_DB=str(db),
               OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    subprocess.run([PY, "-c",
                    "import sys; sys.path.insert(0,'.')\n"
                    "from store import db as sdb\nfrom store import ledger as L\n"
                    "c = sdb.connect()\n"
                    "L.append(c, owner='agent:t', statement='about a ghost',"
                    " project_id='project:vanished')\n"
                    "L.append(c, owner='agent:t', statement='about a real one',"
                    " project_id='project:real')\nc.close()"],
                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    try:
        found = [f for f in json.loads(p.stdout)["findings"]
                 if f["type"] == "memory.orphan_subject"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return
    check("the orphan is raised", len(found) == 1, str(len(found)))
    if found:
        check("naming it with a count", "project:vanished (1 row(s))" in found[0]["detail"],
              found[0]["detail"][:160])
        check("and NOT the one that resolves", "project:real" not in found[0]["detail"],
              found[0]["detail"][:160])
        check("as info, because it may be legitimate history",
              found[0]["severity"] == "info", found[0]["severity"])


if __name__ == "__main__":
    print("the write surface — what it accepted while saying otherwise\n")
    for fn in (test_a_proposal_nothing_could_apply_is_refused,
               test_the_refusal_says_what_IS_appliable,
               test_the_appliable_set_is_derived_from_the_curation_files,
               test_an_unknown_subject_is_queued_with_a_warning,
               test_a_transcript_sized_record_is_refused,
               test_the_bound_is_structural_and_not_at_the_call_site,
               test_the_terminal_gate_still_refuses_a_pipe,
               test_an_accepted_proposal_lands_in_the_curation_file,
               test_accept_and_reject_decide_the_row,
               test_accept_re_checks_appliability_at_the_decision,
               test_a_note_about_a_missing_project_is_reported):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe write surface now refuses what nothing could ever apply\033[0m")
