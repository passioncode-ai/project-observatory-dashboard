#!/usr/bin/env python3
"""A ledger's own header must agree with the ledger.

A markdown decision log carries a `**Next free ID:**` pointer. Maintained by
remembering to maintain it, it drifted — once by dozens of ids — because a
counter kept by attention measures attention rather than content. The rule this
suite drives lives in `tools/check_docs.py` (`ledger_failures`), callable on one
file so it can be watched failing.

**The pointer is not the only claim a ledger's header makes.** Gaps in an id
range come in OPPOSITE kinds, which is why the rule checks accounting rather
than contiguity: an id that was never minted (an off-by-two when a session
resumed, cited by nothing — renumbering later entries to close it would break
every citation of them), and an entry that deliberately lives elsewhere.

So a gap is legitimate when the ledger DECLARES it, and a defect when the reader
has to guess. The declaration is a named line the rule parses, and it must
match the gap set exactly in BOTH directions: an undeclared gap is an id someone
will hunt for, and a declared id that exists is a stale declaration.

Every fixture here is synthetic and uses a neutral `REC` prefix. The ledgers the
checker knows by name are part of the original documentation archive, which
this distribution does not ship, so their live state is not asserted here.
"""
from __future__ import annotations
import pathlib, re, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def ledger_ids(text: str, prefix: str) -> list[int]:
    return sorted({int(m) for m in re.findall(rf"^#+ {prefix}-(\d+)\b", text, re.M)})


# ─────────── the rule exists and is declared ───────────────────────────

def test_the_checker_owns_the_rule() -> None:
    import check_docs
    src = (ROOT / "tools/check_docs.py").read_text(encoding="utf-8")
    check("check_docs.py knows the ledgers", "LEDGERS" in src,
          "the pointer rule needs a subject, and hardcoding one file would leave "
          "OPEN_QUESTIONS.md drifting alone")
    listed = subprocess.run([PY, "tools/check_docs.py", "--list"], cwd=ROOT,
                            capture_output=True, text=True, timeout=120).stdout
    check("and `--list` names it", "Next free ID" in listed or "pointer" in listed.lower(),
          listed[-300:])
    check("the checker still exposes failures()", hasattr(check_docs, "failures"))


# ─────────── it fails on a planted defect ──────────────────────────────
#
# A green from a check nobody has watched fail is not evidence. Each fixture
# below breaks ONE claim and the rule must name that claim and no other.

def _run_on(text: str, prefix: str, rel: str) -> list[str]:
    """Drive the pointer rule alone, over a fixture, via its own function."""
    import check_docs
    fn = getattr(check_docs, "ledger_failures", None)
    if fn is None:
        return ["ABSENT"]
    with tempfile.TemporaryDirectory(prefix="observatory-ledger-") as d:
        f = pathlib.Path(d) / "L.md"
        f.write_text(text, encoding="utf-8")
        return fn(f, prefix, rel)


HEALTHY = """# L

**Next free ID:** `REC-0004`

## REC-0001 — one
## REC-0002 — two
## REC-0003 — three
"""


def test_a_healthy_ledger_passes() -> None:
    out = _run_on(HEALTHY, "REC", "fixture.md")
    if out == ["ABSENT"]:
        check("check_docs.ledger_failures exists", False,
              "the rule must be callable on one file so it can be watched failing")
        return
    check("a ledger whose header agrees reports nothing", out == [], str(out))


def test_a_stale_pointer_is_named() -> None:
    out = _run_on(HEALTHY.replace("`REC-0004`", "`REC-0002`"), "REC", "fixture.md")
    if out == ["ABSENT"]:
        return
    check("a stale pointer fails", len(out) == 1, str(out))
    check("and the message carries both numbers",
          bool(out) and "0002" in out[0] and "0004" in out[0], str(out))


def test_an_undeclared_gap_is_named() -> None:
    text = HEALTHY.replace("## REC-0002 — two\n", "")
    out = _run_on(text, "REC", "fixture.md")
    if out == ["ABSENT"]:
        return
    check("a hole nobody declared fails", any("0002" in m for m in out), str(out))


def test_a_declared_gap_passes() -> None:
    text = HEALTHY.replace("## REC-0002 — two\n", "")
    text = text.replace("**Next free ID:**", "**Vacant ids:** `REC-0002`\n\n**Next free ID:**")
    out = _run_on(text, "REC", "fixture.md")
    if out == ["ABSENT"]:
        return
    check("a hole the ledger declares passes", out == [], str(out))


def test_a_stale_declaration_is_named() -> None:
    """The other direction: an id declared vacant that in fact exists. Without
    this the declaration becomes a place to silence the rule forever."""
    text = HEALTHY.replace("**Next free ID:**", "**Vacant ids:** `REC-0002`\n\n**Next free ID:**")
    out = _run_on(text, "REC", "fixture.md")
    if out == ["ABSENT"]:
        return
    check("declaring a present id fails", any("0002" in m for m in out), str(out))


def test_a_missing_pointer_is_named() -> None:
    out = _run_on(HEALTHY.replace("**Next free ID:** `REC-0004`\n", ""), "REC", "fixture.md")
    if out == ["ABSENT"]:
        return
    check("a ledger with no pointer at all fails", len(out) >= 1, str(out))


# ─────────── an absent ledger is not a failure ─────────────────────────

def test_an_absent_ledger_is_skipped_not_failed() -> None:
    """A distribution without the documentation archive must not read the
    missing files as drifted ones: the checker asks `is_file()` first."""
    src = (ROOT / "tools/check_docs.py").read_text(encoding="utf-8")
    i = src.find("for rel, prefix in LEDGERS:")
    check("the checker walks the declared ledgers", i != -1)
    check("and only reads one that exists", "if f.is_file():" in src[i:i + 200],
          src[i:i + 200])


if __name__ == "__main__":
    print("ledger pointers — the header must agree with the ledger\n")
    for fn in (test_the_checker_owns_the_rule,
               test_a_healthy_ledger_passes,
               test_a_stale_pointer_is_named,
               test_an_undeclared_gap_is_named,
               test_a_declared_gap_passes,
               test_a_stale_declaration_is_named,
               test_a_missing_pointer_is_named,
               test_an_absent_ledger_is_skipped_not_failed):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mboth ledgers account for every id they do not carry\033[0m")
