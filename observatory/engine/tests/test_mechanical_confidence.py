#!/usr/bin/env python3
"""A certain fact carried `confidence 0.5`, in the field a reviewer reads as one.

`./observatory.py digest` is where the operator meets the review queue, and it
prints the confidence beside every record. It once read:

    18  project:alpha-bot   (88–90d left)
        newest: example/alpha-bot on main: 0 files changed …
        mem:0123456789abcdef  confidence 0.5

**"0 files changed" is not fifty per cent likely.** `tools/record_turn.py`
hardcoded `confidence=0.5` on every session record, and the digest
prints that number in a column whose other rows carry real judgements from 0.01
to 0.95. A reviewer reads it as "the writer is half sure" about a `git status`
they could re-run.

`confidence` is nullable and NULL is the honest third answer: **this is not a
judgement.** Two of its three consumers already anticipated that, which is what
says the design intended it — `tools/review.py` renders None as `—`, and the
dashboard prints a confidence only when it is not null. Only the digest and the
single-record view printed it raw, and would have said `None`.

**And two premises this iteration started with, both refuted by measurement:**

* `interpretation.unreasoned` is sound. One decline gave no reason, and the
  finding's own action says "no action if occasional".
* The observer's empty `why` is not a missing justification. The grounds are the
  statement plus typed delta evidence — five references with kinds like
  `ownership-changed`, `repos-changed`, `stack-changed` — and the one record that
  HAS a `why` reads "confidence corrected: an automated writer may not assert
  certainty", which is a note about the REVISION. An empty `why` is a record
  nobody has had to revise.

So `why` gets its meaning stated rather than its writers changed: it is the
writer's note about the RECORD — why it was written, or why a revision changed it
— as against `statement`, which is the claim. Both live uses fit; the field had
no stated meaning at all, and a field with none collects a third use.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

# A private workspace of this suite's own, so the review tool reads shipped
# default configuration rather than a machine's; each test then redirects the
# registry, scratch and store to its own planted copies.
for _name in ("OBSERVATORY_REGISTRY", "OBSERVATORY_DB", "OBSERVATORY_STATE", "OBSERVATORY_SCRATCH"):
    os.environ.pop(_name, None)
os.environ["OBSERVATORY_HOME"] = str(pathlib.Path(tmpdir.mkdtemp(prefix="observatory-conf-home-")).resolve() / "home")
subprocess.run([PY, str(ROOT / "observatory.py"), "init"], cwd=ROOT,
               capture_output=True, timeout=120, check=True)
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── a mechanical record carries no confidence ─────────────────

def store_with(rows: list[dict]) -> tuple[pathlib.Path, dict]:
    """A planted store holding ledger rows, and the env that points at it."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-conf-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps({"projects": [
        {"id": "project:p", "name": "p", "anchor": "vault-folder",
         "ownership": "owned", "lifecycle": "active", "local_folders": [],
         "membership_rules": [], "sites": [], "stack": []}]}))
    for name, body in (("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}'),
                       ("sources.json", '{"sources": []}')):
        (d / "registry" / name).write_text(body)
    db = d / "observatory.db"
    env = dict(os.environ, OBSERVATORY_DB=str(db),
               OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_STATE=str(d / "scratch"))
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    con = sqlite3.connect(db)
    for i, r in enumerate(rows):
        con.execute(
            "INSERT INTO ledger (memory_id, revision, kind, project_id, owner,"
            " function, scope, statement, why, state, confidence, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"mem:{i:016x}", 1, r.get("kind", "session"), "project:p",
             r.get("owner", "agent:claude-code"), "episodic", "project",
             r["statement"], r.get("why"), r.get("state", "proposed"),
             r.get("confidence"), "2026-09-07T00:00:00Z"))
    con.commit()
    con.close()
    return d, env


def review(env: dict, *args: str) -> str:
    p = subprocess.run([PY, "tools/review.py", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    return p.stdout + p.stderr


def test_the_recorder_asserts_no_confidence_in_a_git_status() -> None:
    """THE CODE, not the prose about it. The first version of this asserted
    `"confidence=0.5" not in src` over the raw file — and tripped on the comment
    that EXPLAINS the removal, which quotes the old literal. A source assertion
    that misfires on text rather than code is a known class here, and the
    repository ships the instrument: `tests/source_reader.code_only()` blanks
    strings and comments."""
    import source_reader
    raw = (ROOT / "tools/record_turn.py").read_text(encoding="utf-8")
    code = source_reader.code_only(raw)
    check("the hardcoded 0.5 is gone from the CODE",
          "confidence=0.5" not in code,
          "a `git status` re-runnable in a second is not 50% likely")
    check("and None is passed deliberately", "confidence=None" in code, "")
    check("with the reason beside it, in the prose",
          "not a judgement" in raw.lower() or "not a judgment" in raw.lower(),
          "a NULL with no reason reads as a field somebody forgot")


def test_the_digest_does_not_print_none() -> None:
    d, env = store_with([{"statement": "o/r on main: 0 files changed, +0/-0"}])
    out = review(env, "digest")
    check("the record is listed", "mem:" in out, out[:240])
    check("and `None` is nowhere in it", "None" not in out, out[:300])
    check("a record with no judgement says so",
          "no confidence" in out or "—" in out or "not a judgement" in out,
          out[:300])


def test_a_real_judgement_still_shows_its_number() -> None:
    d, env = store_with([{"statement": "the project was archived", "kind": "observation",
                          "owner": "agent:observer", "confidence": 0.85}])
    out = review(env, "digest")
    check("the number is printed", "0.85" in out, out[:300])
    check("and not as a dash", "0.85" in out and out.count("—") <= 2, out[:300])


def test_the_list_and_the_single_view_agree() -> None:
    d, env = store_with([{"statement": "o/r on main: 1 file changed"},
                         {"statement": "a reading", "kind": "observation",
                          "owner": "agent:observer", "confidence": 0.42}])
    for args in (("list",), ("show", "mem:0000000000000000")):
        out = review(env, *args)
        check(f"`{' '.join(args)}` prints no None", "None" not in out,
              out[:240])
    out = review(env, "show", "mem:0000000000000001")
    check("and the judgement's number survives the same path", "0.42" in out,
          out[:240])


# ─────────── `why` gets a stated meaning ───────────────────────────────

def test_the_ledger_says_what_why_is_for() -> None:
    """A field with no stated meaning collects a third use. Measured: the
    observer writes a REVISION note into it, `record_lost_projects` writes why
    the record is worth keeping, the companion writes nothing — and
    `store/ledger.py` defined neither."""
    src = (ROOT / "store/ledger.py").read_text(encoding="utf-8")
    check("the meaning is written down", "`why`" in src or "why:" in src, "")
    check("and it distinguishes the note from the claim",
          "about the record" in src.lower() or "note about" in src.lower(),
          "`statement` is the claim; `why` is the note about the record")
    check("the revision use is named",
          "revision" in src.lower() and "why" in src.lower(),
          "the one live example is a revision note")


def test_both_live_uses_of_why_fit_the_definition() -> None:
    lost = (ROOT / "tools/record_lost_projects.py").read_text(encoding="utf-8")
    check("`record_lost_projects` says why the record exists",
          "WHY = (" in lost, "")
    obs = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    check("and the observer passes none on a first write",
          "why=" not in obs.split("L.append(")[1][:400] if "L.append(" in obs else True,
          "its grounds are the statement plus typed delta evidence")


if __name__ == "__main__":
    print("mechanical confidence — a certain fact is not half likely\n")
    for fn in (test_the_recorder_asserts_no_confidence_in_a_git_status,
               test_the_digest_does_not_print_none,
               test_a_real_judgement_still_shows_its_number,
               test_the_list_and_the_single_view_agree,
               test_the_ledger_says_what_why_is_for,
               test_both_live_uses_of_why_fit_the_definition):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mno certain fact carries a fabricated confidence\033[0m")
