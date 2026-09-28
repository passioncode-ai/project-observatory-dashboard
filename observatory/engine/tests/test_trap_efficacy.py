#!/usr/bin/env python3
"""The efficacy registry's shape — cheap here, because the sweep itself is not.

`tools/trap_efficacy.py` re-introduces each recorded defect and watches the
guard fail. It edits tracked files and copies the store, so it is not a gate
step. What IS cheap, and what rots first, is the registry it runs from: an
anchor that has moved, a guard name that no longer exists, a claim that is
shaped like neither a trap nor a finding rule.

So the gate asserts the registry can still be executed, and the sweep is run by
hand when a trap or a guard changes. That division is stated rather than
implied: **a green run here does not mean the guards catch anything.** It means
the instrument that measures whether they do is not broken.

This distribution ships no trap registry (`docs/knowledge-pack.md`), so the
claims are checked the way the tool itself checks them without one: a named
guard must exist, and a registry-subject anchor — which names rows of an
operator's real registry — is reported as SKIP rather than asserted against a
synthetic estate that cannot contain it.
"""
from __future__ import annotations
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                       # noqa: E402
FAILURES: list[str] = []


def skip(name: str, reason: str) -> None:
    print(f"  SKIP  {name} — {reason}")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_t", ROOT / f"tools/{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_every_claim_is_shaped_as_a_trap_or_a_rule() -> None:
    """Two populations: `T<n>` is a trap, `R-<rule>` is a finding rule.

    With a registry this also asserted that every declared trap carries a
    claim; no registry ships here, so what remains checkable is the shape — a
    typo in a trap id must not slip into the second population and stop being
    checked at all.
    """
    eff = load("trap_efficacy")
    claimed = {m["trap"] for m in eff.MUTATIONS} | set(eff.SELF_DRIVEN)
    traps = {t for t in claimed if t.startswith("T") and t[1:].isdigit()}
    rules = claimed - traps
    check("traps carry efficacy claims", bool(traps), str(sorted(claimed))[:120])
    misshapen = sorted(r for r in rules if not r.startswith("R-"))
    check("and every non-trap claim is named as a rule", not misshapen, str(misshapen))
    check(f"{len(rules)} finding rule(s) carry an efficacy claim", bool(rules))


def test_every_mutation_can_still_be_applied() -> None:
    """A stale anchor turns a sweep into a row of INCONCLUSIVE nobody reads."""
    eff = load("trap_efficacy")
    for mut in eff.MUTATIONS:
        if mut.get("patch") or mut.get("patch_dir") or mut.get("truncate") or mut.get("sql"):
            continue                      # structural or SQL: nothing to anchor
        if mut["subject"] == "registry":
            skip(f"{mut['trap']}'s anchor in registry/{mut['file']}",
                 "it names rows of an operator's real registry; the synthetic estate cannot hold them")
            continue
        target = ROOT / mut["file"]
        if not target.is_file():
            check(f"{mut['trap']} names a file that exists", False, str(target))
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        for find, _ in mut.get("edits") or [(mut["find"], mut["replace"])]:
            n = text.count(find)
            check(f"{mut['trap']}'s anchor in {mut['file']} is unique", n == 1,
                  f"occurs {n} times — the mutation would be reported stale")


def test_every_mutation_names_a_guard_that_exists() -> None:
    """A named guard is checked the way the tool checks it with no registry.

    Without a registry, a mutation that names neither a step nor a guard is
    reported INCONCLUSIVE by the sweep by design, so only named ones carry a
    claim here: the step must be real, the guard's file and function must exist.
    """
    eff = load("trap_efficacy")
    for mut in eff.MUTATIONS:
        if mut.get("step"):
            check(f"{mut['trap']} names a real step", mut["step"] in _steps(), mut["step"])
            continue
        if not mut.get("guard"):
            continue
        rel, fn = mut["guard"].split("::")
        path = ROOT / rel
        if not path.is_file():
            skip(f"{mut['trap']}'s named guard {mut['guard']}",
                 f"{rel} is not in this tree")
            continue
        check(f"{mut['trap']}'s named guard exists",
              f"def {fn}(" in path.read_text(encoding="utf-8"), mut["guard"])


def _steps() -> set[str]:
    spec = importlib.util.spec_from_file_location("obs_eff", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obs)
    return set(obs.STEPS)


def test_every_self_driven_marker_is_still_there() -> None:
    """The weaker claim, and it is checked as the weaker claim.

    A self-driven trap is one whose guard plants the defect itself. This asserts
    the planting line still exists — not that it still plants what it did, which
    only running it can say.
    """
    eff = load("trap_efficacy")
    for trap, (rel, marker) in sorted(eff.SELF_DRIVEN.items(), key=lambda kv: int(kv[0][1:])):
        f = ROOT / rel
        if not f.is_file():
            skip(f"{trap}'s planted defect", f"{rel} is not in this tree")
            continue
        check(f"{trap}'s planted defect is still in {rel}",
              marker in f.read_text(encoding="utf-8"), f"{marker!r} is gone")


def test_the_sweep_refuses_to_run_over_uncommitted_work() -> None:
    """The one safety property, asserted rather than trusted.

    Source mutations are undone with `git checkout`, so a dirty file would be
    lost. The tool refuses in that case; here the refusal is driven with a file
    that is genuinely dirty, in a throwaway Git repository holding a copy of the
    tool, so no checkout this suite runs from is touched.
    """
    import subprocess
    sys.path.insert(0, str(ROOT / "tests"))
    import tmp as tmpdir
    repo = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-efficacy-")).resolve()
    (repo / "tools").mkdir()
    (repo / "tools/trap_efficacy.py").write_text(
        (ROOT / "tools/trap_efficacy.py").read_text(encoding="utf-8"), encoding="utf-8")
    probe = repo / "tracked.md"
    probe.write_text("tracked\n", encoding="utf-8")
    git = dict(capture_output=True, text=True, timeout=60, cwd=repo)
    ident = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.com"]
    subprocess.run(["git", "init", "-q"], **git)
    subprocess.run(["git", *ident, "add", "-A"], **git)
    subprocess.run(["git", *ident, "commit", "-qm", "fixture"], **git)
    spec = importlib.util.spec_from_file_location("trap_efficacy_repo", repo / "tools/trap_efficacy.py")
    eff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eff)
    rel = "tracked.md"
    base = eff.dirty([rel])
    check("a committed file starts clean", base == [], str(base))
    was = probe.read_text(encoding="utf-8")
    probe.write_text(was + "\n<!-- efficacy probe -->\n", encoding="utf-8")
    try:
        check("a modified file is reported as dirty", bool(eff.dirty([rel])),
              "the refusal to mutate over uncommitted work rests on this")
    finally:
        probe.write_text(was, encoding="utf-8")
    check("and undoing the change returns the answer to what it was",
          eff.dirty([rel]) == base, "restore failed")


def test_an_empty_plan_reports_no_dirt() -> None:
    """`git status --porcelain --` with no paths reports the whole tree.

    Both of the tool's safety decisions read this function, and the restore
    check ran with an empty list whenever a plan held no source mutation — so
    it printed `TREE NOT RESTORED` and a `git checkout --` line naming every
    file the session had open. A recovery command aimed at work the tool never
    touched is worse than no check at all.
    """
    eff = load("trap_efficacy")
    check("an empty file list yields an empty answer", eff.dirty([]) == [],
          "the restore check would name files the tool never touched")


def test_the_gate_does_not_run_the_sweep() -> None:
    """Saying it in a docstring is not the same as it being true."""
    spec = importlib.util.spec_from_file_location("obs_eff2", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obs)
    running = [s for s, cmd in obs.STEPS.items()
               if any(str(c).endswith("tools/trap_efficacy.py") for c in cmd)]
    check("no step runs tools/trap_efficacy.py", not running, str(running))


if __name__ == "__main__":
    print("trap efficacy — the registry that measures the guards\n")
    for fn in (test_every_claim_is_shaped_as_a_trap_or_a_rule,
               test_every_mutation_can_still_be_applied,
               test_every_mutation_names_a_guard_that_exists,
               test_every_self_driven_marker_is_still_there,
               test_the_sweep_refuses_to_run_over_uncommitted_work,
               test_an_empty_plan_reports_no_dirt,
               test_the_gate_does_not_run_the_sweep):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mtrap efficacy registry ok\033[0m")
