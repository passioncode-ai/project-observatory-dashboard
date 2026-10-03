#!/usr/bin/env python3
"""A rule reported its own correct decision as a defect, and a finding prescribed
the wrong cure.

Three findings of one shape: the row's prose against the truth of the case.

**One.** `estate.py` exists to answer "what counts as this estate's OWN work" in
one place, and `tools/record_turn.py` obeys it: a turn inside a third-party clone
is declined, because "a third-party clone is somebody else's history". That
decline lands as `reason = "project ownership is 'external'; only [...] are
recorded"` — and `NOT_A_FAULT` did not list it, so **the rule working correctly
was recorded as a failure of the recorder**. In the original installation
seven of the eight `external` projects had a checkout on the machine, so any
session working in one of them raised `companion.not_recording` — and, once
faults were journalled, also appended a durable line to `companion-faults.jsonl`
on every turn.

**Two.** `model.degraded` carried ONE hardcoded action for every reason: "check
that `gh` is authenticated for the PATH the tick runs with, then
`./observatory.py merge`". That is the remedy for the `transfer:` class, which
cannot re-check whether an address moved. The live degradation was the
`ownership` class — an organisation the GitHub listing returned and `OWNED_ORGS`
(the workspace's `config/ownership.json`) does not declare — whose remedy is a
decision only the operator can make. A
reader following the action would check `gh`, find it healthy, run `merge`, and
get the same row back.

**Three.** That reason states a consequence — "estate.py will refuse to record
sessions for them" — which is true of the class and **not of every instance**.
A repository such as `gamma-labs/partner-tool` can have `local_folders: []`: there is no checkout, so
no session exists to lose and the effect is classification only. The collector
knows which case it is in, and said the alarming one either way.
"""
from __future__ import annotations
import ast
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


#: Literal decline reasons that ARE faults, each with the reason it is one. A
#: reason absent from both this table and `NOT_A_FAULT` fails the enumeration
#: below — which is the point: the next decline site has to be classified by
#: somebody rather than inherit `fault: true` by default.
DELIBERATE_FAULTS = {
    "cwd does not exist":
        "the hook hands over the session's own working directory, so a path "
        "that is not there is a broken assumption rather than an answer about "
        "the work",
}


def literal_decline_reasons(path: pathlib.Path) -> list[str]:
    """Every `out({"recorded": False, "reason": <literal…>})` in the source.

    A reason that BEGINS with an interpolation is a fault by construction — the
    two outer handlers format an exception into it — so those are not part of
    the enumeration and their absence is not a gap.
    """
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "out"
                and node.args and isinstance(node.args[0], ast.Dict)):
            continue
        for k, v in zip(node.args[0].keys, node.args[0].values):
            if getattr(k, "value", None) != "reason":
                continue
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.append(v.value)
            elif isinstance(v, ast.JoinedStr) and v.values and \
                    isinstance(v.values[0], ast.Constant):
                out.append(v.values[0].value)
    return out


# ─────────── one: a decision is not a defect ───────────────────────────

def test_every_decline_reason_is_classified_deliberately() -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    import record_turn as R
    reasons = literal_decline_reasons(ROOT / "tools/record_turn.py")
    check(f"the source has decline reasons to classify ({len(reasons)})",
          len(reasons) >= 6, str(reasons))
    for text in reasons:
        answered = any(k in text for k in R.NOT_A_FAULT)
        declared = any(k in text for k in DELIBERATE_FAULTS)
        check(f"classified: {text[:52]!r}", answered != declared,
              "in neither table, so it is a fault by accident"
              if not (answered or declared) else
              "in BOTH tables, so the classification contradicts itself")
    check("and each deliberate fault carries its reason",
          all(len(v.split()) >= 8 for v in DELIBERATE_FAULTS.values()), "")


def test_a_declined_ownership_is_not_a_fault() -> None:
    """DRIVEN against a fixture registry, not the live estate: the property is
    about the classification, and a suite that needs a particular third-party
    checkout to exist goes red when somebody tidies up."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-remedy-"))
    repo = d / "somebody-elses-clone"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, timeout=300)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    (repo / "README.md").write_text("first\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "first")
    (repo / "README.md").write_text("changed, so the turn has something to record\n",
                                    encoding="utf-8")
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo,
                         capture_output=True, text=True, timeout=300).stdout.strip()

    reg = d / "registry"
    reg.mkdir()
    (reg / "repositories.json").write_text(json.dumps({"repositories": [
        {"id": "repository:x", "name_with_owner": "somebodyelse/thing",
         "local": {"path": top}}]}), encoding="utf-8")
    (reg / "relations.json").write_text(json.dumps({"relations": [
        {"type": "implemented_by", "from": "project:x", "to": "repository:x"}]}),
        encoding="utf-8")
    (reg / "projects.json").write_text(json.dumps({"projects": [
        {"id": "project:x", "ownership": "external"}]}), encoding="utf-8")

    scratch = d / "scratch"
    scratch.mkdir()
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(reg),
               OBSERVATORY_SCRATCH=str(scratch), OBSERVATORY_DB=str(d / "obs.db"))
    p = subprocess.run([PY, "tools/record_turn.py", "--cwd", str(repo),
                        "--session-id", "s-ext"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    said = json.loads(p.stdout or "{}")
    check("the turn is declined", said.get("recorded") is False, p.stdout[:200])
    check("because of the ownership rule",
          "ownership" in (said.get("reason") or ""), str(said)[:200])
    doc = json.loads((scratch / "record-turn.json").read_text(encoding="utf-8"))
    check("and the decline is NOT a fault", doc.get("fault") is False,
          "the rule working as designed was reported as the recorder failing")
    check("so nothing is added to the durable fault log",
          not (scratch / "companion-faults.jsonl").exists(),
          "seven external checkouts would each append one line per turn")


# ─────────── two: the remedy matches the reason ─────────────────────────

def merge_degradation_classes() -> set[str]:
    """The `source` prefixes `collectors/merge.py` can actually emit.

    Read from the collector rather than listed here, so a new degradation site
    cannot quietly inherit another class's cure. `merge.py` runs at module
    level, so this reads the file instead of importing it.
    """
    import re
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    return set(re.findall(r'degraded\.append\(\{\s*"source"\s*:\s*f?"([a-z_]+)', src))


def test_every_degradation_class_has_its_own_remedy() -> None:
    import build_findings as B
    classes = merge_degradation_classes()
    check(f"the collector's classes are readable ({sorted(classes)})",
          len(classes) >= 2, str(classes))
    for c in sorted(classes):
        check(f"`{c}` has a remedy of its own", c in B.MERGE_REMEDY,
              f"it would inherit another class's cure — {sorted(B.MERGE_REMEDY)}")
    check("and every remedy says what to run or what to decide",
          all(len(v.split()) >= 8 for v in B.MERGE_REMEDY.values()), "")


def test_the_ownership_reason_does_not_get_the_gh_cure() -> None:
    import build_findings as B
    rows = B.merge_findings([{"source": "ownership",
                              "reason": "the GitHub listing returned repositories "
                                        "under gamma-labs, which OWNED_ORGS does "
                                        "not declare"}])
    check("one row", len(rows) == 1, str(rows))
    if not rows:
        return
    act = rows[0]["action"]
    check("the action names the decision", "OWNED_ORGS" in act, act)
    check("and does not send the reader to check `gh`",
          "authenticated" not in act,
          "gh answered fine; the estate gained an owner nobody declared")


def test_a_transfer_reason_still_gets_the_gh_cure() -> None:
    import build_findings as B
    rows = B.merge_findings([{"source": "transfer:example/alpha-web",
                              "reason": "could not check whether it has moved"}])
    check("the transfer class keeps its own remedy",
          rows and "authenticated" in rows[0]["action"], str(rows)[:200])


def test_two_classes_at_once_carry_both_remedies() -> None:
    import build_findings as B
    rows = B.merge_findings([
        {"source": "ownership", "reason": "an undeclared organisation"},
        {"source": "transfer:a/b", "reason": "could not check whether a/b moved"}])
    act = rows[0]["action"] if rows else ""
    check("both cures are offered", "OWNED_ORGS" in act and "authenticated" in act, act)
    check("and neither is repeated", act.count("OWNED_ORGS") == 1, act)


def test_an_unprobed_remote_scan_is_told_which_command_runs_it() -> None:
    """With `git_remotes` on and the probe not yet run, the merge's optional
    input `remotes.json` is missing; the row's action read "this degradation's
    class has no recorded remedy … nothing here can tell you what to run",
    though `project-observatory full remotes` produces it. And its detail said
    "170-odd repositories are unaffected" on a two-project workspace: a number
    from one estate, printed on every other."""
    import build_findings as B
    rows = B.merge_findings([{"source": "remotes.json",
                              "reason": "optional collector output unavailable; not measured"}],
                            integrations={"git_remotes": True})
    act = rows[0]["action"] if rows else ""
    check("the action names the command that produces it",
          "project-observatory full remotes" in act and "no recorded remedy" not in act, act)
    detail = rows[0]["detail"] if rows else ""
    check("and the detail states no number measured elsewhere", "170" not in detail, detail)
    for source in ("sessions.json", "bitbucket.json"):
        rows = B.merge_findings([{"source": source, "reason": "not measured"}])
        act = rows[0]["action"] if rows else ""
        check(f"`{source}` has a remedy of its own", "no recorded remedy" not in act, act)


def test_an_unknown_class_is_given_no_invented_cure() -> None:
    """The third outcome. A degradation source nobody has written a remedy for
    must not inherit one: a wrong remedy is followed, and the reader's evidence
    that it was wrong is that nothing changed."""
    import build_findings as B
    rows = B.merge_findings([{"source": "weather", "reason": "it was raining"}])
    act = rows[0]["action"] if rows else ""
    check("no prescription is invented",
          "authenticated" not in act and "OWNED_ORGS" not in act, act)
    check("and the reader is sent to the reason itself",
          "reason" in act.lower(), act)


# ─────────── three: the consequence, only where it applies ─────────────

def test_the_session_loss_is_claimed_only_when_a_checkout_exists() -> None:
    import estate
    fn = getattr(estate, "undeclared_owner_reason", None)
    if fn is None:
        check("estate owns the sentence about undeclared owners", False,
              "the vocabulary of ownership lives in estate.py, and merge.py "
              "runs at module level so nothing can test a function defined there")
        return
    with_checkout = fn(["acme"], repos=3, checked_out=2)
    check("a checkout means work is being dropped",
          "2" in with_checkout and "session" in with_checkout, with_checkout)
    none_here = fn(["gamma-labs"], repos=1, checked_out=0)
    check("no checkout means no session is being lost",
          "no session" in none_here, none_here)
    check("and it says the effect is classification",
          "classification" in none_here, none_here)
    check("both name the owner", "acme" in with_checkout and "gamma-labs" in none_here,
          f"{with_checkout[:60]} / {none_here[:60]}")
    check("both name the remedy as the operator's decision",
          "OWNED_ORGS" in with_checkout and "OWNED_ORGS" in none_here, "")


def test_the_collector_uses_that_one_sentence() -> None:
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    check("merge.py calls it rather than composing its own",
          "undeclared_owner_reason" in src,
          "two writers of one sentence is how the alarming half survives a fix")


if __name__ == "__main__":
    print("the remedy against the case — a decision reported as a defect\n")
    for fn in (test_every_decline_reason_is_classified_deliberately,
               test_a_declined_ownership_is_not_a_fault,
               test_every_degradation_class_has_its_own_remedy,
               test_the_ownership_reason_does_not_get_the_gh_cure,
               test_a_transfer_reason_still_gets_the_gh_cure,
               test_an_unprobed_remote_scan_is_told_which_command_runs_it,
               test_two_classes_at_once_carry_both_remedies,
               test_an_unknown_class_is_given_no_invented_cure,
               test_the_session_loss_is_claimed_only_when_a_checkout_exists,
               test_the_collector_uses_that_one_sentence):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma cure that does not fit its cause is followed, and nothing changes\033[0m")
