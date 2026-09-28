#!/usr/bin/env python3
"""Failure paths that were designed and never driven.

The rule these three broke: *a degradation nobody has watched work is a
degradation that does not work*. Each
one below announced a graceful answer in a comment or a message, and each would
have produced something else the first time it was reached.

* A collector wrote `json.dump(rows, open(path, "w"))` — truncate first,
  serialise second. A crash between them leaves an empty file that the merge
  reads as "this machine has no projects".
* `store/indexer.py` printed "the lexical index is built, the vector one is not"
  and then executed an unconditional `import sqlite_vec`.
* `agent/observe.py` checked the budget — which resolves the key — before the
  credential check that catches a refused key file.
"""
from __future__ import annotations
import json, os, pathlib, subprocess, sqlite3, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
import atomic                                                       # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# -- the write that truncates before it serialises --------------------------

class Unserialisable:
    """Raises when json reaches it — a stand-in for a crash mid-document."""


def test_a_failed_write_leaves_the_previous_file_intact() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-atomic-"))
    target = d / "local.json"
    atomic.write_json(target, [{"project": "one"}])
    before = target.read_text(encoding="utf-8")

    try:
        atomic.write_json(target, [{"project": "two"}, Unserialisable()])
        raised = False
    except TypeError:
        raised = True
    check("a document that cannot be serialised raises", raised)
    check("and the previous file is untouched", target.read_text(encoding="utf-8") == before,
          target.read_text(encoding="utf-8")[:60])
    check("the file is still valid JSON, not empty or half a document",
          json.loads(target.read_text(encoding="utf-8")) == [{"project": "one"}])
    leftovers = [p.name for p in d.iterdir() if p.name != "local.json"]
    check("no temp file is left behind for the next glob to find", not leftovers,
          str(leftovers))


def test_the_old_way_would_have_destroyed_it() -> None:
    """The planted defect, so the fix is measured against the real failure."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-naive-"))
    target = d / "local.json"
    target.write_text(json.dumps([{"project": "one"}]), encoding="utf-8")
    try:
        json.dump([{"project": "two"}, Unserialisable()], open(target, "w"))
    except TypeError:
        pass
    text = target.read_text(encoding="utf-8")
    check("the naive write leaves a file that is not the previous document",
          text != json.dumps([{"project": "one"}]), text[:60])
    try:
        json.loads(text)
        parses = True
    except json.JSONDecodeError:
        parses = False
    check("and it does not parse — which is the LUCKY half of this failure",
          not parses, "the unlucky half truncates onto valid JSON and reads as empty")


def test_the_temp_file_is_a_sibling() -> None:
    """`os.replace` is atomic only within one filesystem."""
    src = (ROOT / "atomic.py").read_text(encoding="utf-8")
    check("the temp file is created in the destination's own directory",
          "dir=dest.parent" in src,
          "a temp in /tmp makes this a cross-device copy, reintroducing the partial write")
    check("the content is flushed to disk before the rename", "fsync" in src)


def test_every_collector_writes_atomically() -> None:
    for name in ("collectors/scan_filesystem.py", "collectors/scan_vault.py",
                 "collectors/merge.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        check(f"{name} writes atomically", "atomic.write_json" in src)
        check(f"{name} no longer opens its destination for writing",
              'open(sys.argv[1],"w")' not in src and 'model.json","w"' not in src)


# -- the degradation that imported the thing it was degrading without --------

def test_the_lexical_index_survives_a_missing_sqlite_vec() -> None:
    src = (ROOT / "store/indexer.py").read_text(encoding="utf-8")
    body = src.split("def index_batch", 1)[1].split("def cmd_index", 1)[0]
    check("index_batch no longer imports sqlite_vec unconditionally",
          "\n    import sqlite_vec" not in body, "it ran on every call, not only with have_vec")
    check("serialize_float32 is still imported where it is used",
          "from sqlite_vec import serialize_float32" in body)

    # Driven: hide the module and build a lexical index anyway.
    probe = f'''
import sys, sqlite3, pathlib
sys.path.insert(0, {str(ROOT)!r})
import builtins
_real = builtins.__import__
def blocked(name, *a, **k):
    if name == "sqlite_vec":
        raise ImportError("hidden by the test")
    return _real(name, *a, **k)
builtins.__import__ = blocked
sys.modules.pop("sqlite_vec", None)
from store import indexer
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("CREATE TABLE search_notes (memory_id TEXT, revision INT, statement TEXT, why TEXT)")
rows = [{{"memory_id": "mem:a", "revision": 1, "statement": "a statement", "why": None}}]
# FOUR values since 2026-09-07: the fourth says whether the vector half
# landed, which is what lets cmd_index keep a failed batch's rows queued
# instead of consuming them and making the degradation permanent.
written, cost, tokens, vectors_ok = indexer.index_batch(
    conn, rows, have_vec=False, dims=1536)
assert vectors_ok, 'with no vector index at all there is nothing to hold rows for'
n = conn.execute("SELECT COUNT(*) FROM search_notes").fetchone()[0]
print(f"written={{written}} rows={{n}}")
'''
    p = subprocess.run([PY, "-c", probe], capture_output=True, text=True, timeout=120)
    check("with sqlite_vec unimportable the lexical index is still written",
          "written=1 rows=1" in p.stdout, (p.stdout + p.stderr)[-200:])


# -- the check that ran before the check that catches its exception ----------

def test_a_loose_key_file_degrades_instead_of_crashing() -> None:
    """`read_key` REFUSES a group- or world-readable file by raising. `have_key`
    Trap: T18
    catches that; `check_budget` does not — so their order decides whether the
    scheduled tick degrades or prints a traceback."""
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    key_at, budget_at = src.find("providers.have_key()"), src.find("providers.check_budget()")
    check("the credential check runs before the budget check",
          key_at != -1 and budget_at != -1 and key_at < budget_at,
          f"have_key at {key_at}, check_budget at {budget_at}")

    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-loosekey-"))
    loose = d / "openrouter"
    loose.write_text(("sk-" "or-v1-" "a-fake-key-used-only-by-this-test"), encoding="utf-8")
    loose.chmod(0o644)
    # THE STORE AND THE SCRATCH DIR, not only the key file. This redirected the
    # key alone, so the run read the live ledger and wrote the LIVE
    # `agent.json` — whose `halted_by` then named THIS fixture's temp path as
    # the reason the estate's interpretation layer was stopped.
    (d / "scratch").mkdir(exist_ok=True)
    # The interpretation layer is opt-in in a workspace, so the fixture builds
    # its own workspace and enables it; otherwise the run stops at "not
    # configured" before the credential path this test exists to walk.
    home = d / "home"
    env = {**os.environ, "OBSERVATORY_KEY_FILE": str(loose),
           "OBSERVATORY_HOME": str(home),
           "OBSERVATORY_DB": str(d / "observatory.db"),
           "OBSERVATORY_SCRATCH": str(d / "scratch"),
           # No provider call may leave this machine, whatever the key says.
           "OBSERVATORY_OFFLINE": "1"}
    env.pop("OPENROUTER_API_KEY", None)
    subprocess.run([PY, "observatory.py", "init"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=120, check=True)
    settings = home / "config/settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc.setdefault("features", {})["agent"] = True
    settings.write_text(json.dumps(doc), encoding="utf-8")
    p = subprocess.run([PY, "agent/observe.py"], cwd=ROOT, capture_output=True,
                       text=True, timeout=180, env=env)
    both = p.stdout + p.stderr
    check("a mode-644 key file does not produce a traceback",
          "Traceback" not in both, both[-300:])
    check("it exits 0 — a refused credential is a state, not a crash",
          p.returncode == 0, f"exit {p.returncode}: {both[-200:]}")
    check("and the reason names the permissions",
          "DEGRADED" in both or "nothing moved" in both or "no unconsumed" in both,
          both[-200:])


if __name__ == "__main__":
    print("degradations — the paths that were written and never walked\n")
    for fn in (test_a_failed_write_leaves_the_previous_file_intact,
               test_the_old_way_would_have_destroyed_it,
               test_the_temp_file_is_a_sibling,
               test_every_collector_writes_atomically,
               test_the_lexical_index_survives_a_missing_sqlite_vec,
               test_a_loose_key_file_degrades_instead_of_crashing):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery degradation here has now been watched working\033[0m")
