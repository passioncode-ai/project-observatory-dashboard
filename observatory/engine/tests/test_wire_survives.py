#!/usr/bin/env python3
"""The one branch nothing drove: the wire staying alive when the store will not open.

A walk over the degradation sites in the collectors and in `survey.py` looked for
branches whose condition can no longer be true, and found none: every site is an
exception handler (reachable by construction), a test of measured data, or a
detector built to be reachable on purpose.

What it did find is one production handler that no suite exercised, in
`survey.py`'s store connection:

    except Exception as exc:
        degraded.append({"source": "store", "reason": f"unavailable: {exc}"})

That branch keeps the WIRE alive when the store cannot be opened at all — a
corrupt file, a truncated one, a path that is not a database. Pointing
`OBSERVATORY_DB` at garbage makes the survey answer `scanId: registry-only`,
degrade with `unavailable: file is not a database`, and still report the
registry's projects. Correct, and verified by nothing — so a later change could
take the wire down with the store and no suite would notice.

Runs over the synthetic estate the portable runner builds; the registry half it
asserts is that estate's, not any operational one.
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup                    # noqa: E402
portable_setup()
import tmp as tmpdir                                                  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def survey_with(db: pathlib.Path | None) -> dict:
    """`survey.survey` in a subprocess, with the store pointed wherever we like.

    A subprocess because `paths.DB` is resolved at import: the tick and the MCP
    server both start fresh, so this is how they meet a broken store.
    """
    code = ("import sys, json; sys.path.insert(0, '.');"
            "import survey; print(json.dumps(survey.survey({'kind': 'estate'})))")
    env = {**os.environ}
    if db is not None:
        env["OBSERVATORY_DB"] = str(db)
    p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        return {"_crashed": (p.stdout + p.stderr)[-400:]}
    return json.loads(p.stdout)


# ─────────── a store that will not open ────────────────────────────────

def test_a_garbage_store_degrades_rather_than_raising() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-wire-"))
    bad = d / "not-a-database.db"
    bad.write_bytes(b"this is not a sqlite file" * 40)
    got = survey_with(bad)
    check("the wire answers at all", "_crashed" not in got,
          got.get("_crashed", ""))
    if "_crashed" in got:
        return
    check("and says the store is unavailable",
          any(x["source"] == "store" and "unavailable" in x["reason"]
              for x in got.get("degraded") or []),
          json.dumps(got.get("degraded"), ensure_ascii=False)[:200])
    check("`scanId` says the answer came from the registry alone",
          got["scanId"] == "registry-only", got["scanId"])
    # THE POINT of degrading rather than failing: the registry half is still
    # served. An answer of nothing would be a worse lie than a partial one.
    check("and the registry half is still answered",
          got["counts"]["projects"] > 0, str(got["counts"]))
    check("with the projects themselves, not only a count",
          len(got.get("projects") or []) > 0, str(len(got.get("projects") or [])))


def test_an_absent_store_is_not_the_same_answer() -> None:
    """A path that does not exist is a different fact from a file that will not
    parse, and sqlite treats them differently: it CREATES the missing one. So
    this case asserts what the wire actually does rather than assuming the two
    collapse."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-wire-absent-"))
    got = survey_with(d / "never-existed.db")
    check("the wire answers", "_crashed" not in got, got.get("_crashed", ""))
    if "_crashed" in got:
        return
    store_deg = [x for x in got.get("degraded") or [] if x["source"] == "store"]
    check("and either serves an empty store or says why, never silence",
          got["scanId"] == "registry-only" or bool(store_deg)
          or got["scanId"].startswith(("fingerprint", "events", "scan")),
          f"scanId={got['scanId']!r} degraded={store_deg}")
    check("the registry half is answered either way",
          got["counts"]["projects"] > 0, str(got["counts"]))


def test_the_branch_is_still_where_this_suite_aims() -> None:
    """The handler is located by its shape rather than by a coverage marker: the
    public source carries no `pragma: no cover` comment, so a structural match on
    the connect call and the degradation it appends is what keeps this suite
    aimed at the branch it drives."""
    src = (ROOT / "survey.py").read_text(encoding="utf-8")
    m = re.search(r"conn = store_db\.connect\(\)\s*\n\s*except Exception as exc:[^\n]*\n"
                  r"\s*degraded\.append\(\{\"source\": \"store\", \"reason\": f\"unavailable: \{exc\}\"\}\)",
                  src)
    check("the store-connect handler is still where this suite thinks it is", m is not None,
          "if this fires the handler moved and this suite is aimed at nothing")


if __name__ == "__main__":
    print("the wire — alive when the store is not\n")
    for fn in (test_a_garbage_store_degrades_rather_than_raising,
               test_an_absent_store_is_not_the_same_answer,
               test_the_branch_is_still_where_this_suite_aims):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma broken store costs the store half, not the answer\033[0m")
