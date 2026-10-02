#!/usr/bin/env python3
"""Eight clones held work that exists nowhere else, and the findings said nothing.

Measured once against a live registry: 61 clones `current`, 11
`behind-or-diverged`, **8 `ahead`**, 5 `local-only-branch`, 1 `diverged`, 1
`behind`. Every state but one produced a finding. `ahead` produced none — for
an application, a notes vault and, closing the circle, the observatory's own
repository.

**`ahead` and `local-only-branch` are one fact seen from two ends.** Both mean
commits exist on this disk and nowhere else. The only difference is whether the
branch has a remote counterpart, and that changes nothing about what a dead disk
costs. One was a `warning` telling the operator to push; the other was silence.

**The cause is structural, and it is why nobody decided this.** Both consumers
look the state up in a closed table and skip a miss without a word —
`SYNC.get(state)` then `continue` in `tools/build_findings.py`, and
`if (SYNC[x.sync])` in the dashboard. So `ahead` was never REJECTED as benign;
it was never LISTED. A state absent from the table is now a finding of its own,
because a state machine whose unhandled branch is silence will grow another one.

**And the fetch that was never needed.** `sync_state` gave up when the remote
commit was absent from the clone — correctly refusing to fetch, since a fetch
writes into the repository this system only watches. But the question that
MATTERS there — does this clone hold work that exists nowhere else — is
answerable from `refs/remotes/origin/<branch>`, which the clone already has: no
network, no write. So `behind-or-diverged` splits into `stale` (nothing at risk)
and `unpushed-and-remote-moved` (a warning), and survives only where there is no
tracking ref to ask — genuinely unanswerable then, and still not guessed.

On that estate all 11 `behind-or-diverged` clones measured as merely stale, so
that half was a latent defect: it filed no work-at-risk as `info` then, and it
would have.
"""
from __future__ import annotations
import importlib, json, os, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def git(args: list[str], cwd: pathlib.Path) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def commit(repo: pathlib.Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    git(["add", name], repo)
    git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", name], repo)
    return git(["rev-parse", "HEAD"], repo)


# ─────────── the tracking ref answers what a fetch was asked for ───────

def planted_clone() -> tuple[pathlib.Path, str]:
    """A clone whose remote has moved on WITHOUT it fetching — the exact state
    `sync_state` used to give up on. Returns (clone, the remote's new sha)."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sync-"))
    bare = d / "remote.git"
    git(["init", "--bare", "--initial-branch=main", str(bare)], d)
    seed = d / "seed"
    seed.mkdir()
    git(["init", "--initial-branch=main"], seed)
    commit(seed, "c1")
    git(["remote", "add", "origin", str(bare)], seed)
    git(["push", "-u", "origin", "main"], seed)

    clone = d / "clone"
    git(["clone", str(bare), str(clone)], d)

    # The remote moves through somebody else's copy, so `clone` never sees it.
    moved = commit(seed, "c2")
    git(["push", "origin", "main"], seed)
    return clone, moved


def test_the_tracking_ref_answers_it_without_a_fetch() -> None:
    import scan_remotes as S
    importlib.reload(S)

    clone, moved = planted_clone()
    local = git(["rev-parse", "HEAD"], clone)
    check("the premise holds: the remote commit is absent from the clone",
          subprocess.run(["git", "-C", str(clone), "cat-file", "-e", moved + "^{commit}"],
                         capture_output=True).returncode != 0,
          "if this fails the fixture is not reproducing the ambiguous state")

    check("a clone with nothing of its own reads as stale",
          S.tracking_state(clone, local, "main") == "stale",
          str(S.tracking_state(clone, local, "main")))
    check("and `sync_state` reports it rather than the old ambiguity",
          S.sync_state(clone, local, moved, "main") == "stale",
          S.sync_state(clone, local, moved, "main"))

    own = commit(clone, "mine")
    check("one local commit on top makes it work at risk",
          S.tracking_state(clone, own, "main") == "unpushed-and-remote-moved",
          str(S.tracking_state(clone, own, "main")))
    check("and `sync_state` agrees",
          S.sync_state(clone, own, moved, "main") == "unpushed-and-remote-moved",
          S.sync_state(clone, own, moved, "main"))


def test_no_tracking_ref_is_still_answered_with_a_refusal() -> None:
    """The third outcome. Without `origin/<branch>` the question genuinely needs
    a fetch, and the old honest answer is the right one — not a guess in either
    direction."""
    import scan_remotes as S
    importlib.reload(S)
    clone, moved = planted_clone()
    local = git(["rev-parse", "HEAD"], clone)
    git(["update-ref", "-d", "refs/remotes/origin/main"], clone)
    check("with the ref gone it declines to guess",
          S.tracking_state(clone, local, "main") is None,
          str(S.tracking_state(clone, local, "main")))
    check("and `sync_state` falls back to the measured ambiguity",
          S.sync_state(clone, local, moved, "main") == "behind-or-diverged",
          S.sync_state(clone, local, moved, "main"))
    check("a detached head has no branch to ask about",
          S.tracking_state(clone, local, "HEAD") is None
          and S.tracking_state(clone, local, "") is None, "")


def test_the_states_that_could_always_be_told_apart_still_are() -> None:
    """Refining the miss branch must not have moved the cases that never needed
    it: equal is `current`, the remote's commit present and behind us is
    `ahead`, no remote branch at all is `local-only-branch`."""
    import scan_remotes as S
    importlib.reload(S)
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sync-basic-"))
    repo = d / "r"
    repo.mkdir()
    git(["init", "--initial-branch=main"], repo)
    a = commit(repo, "a")
    check("equal shas are current", S.sync_state(repo, a, a, "main") == "current", "")
    b = commit(repo, "b")
    check("the remote's commit present and behind us is ahead",
          S.sync_state(repo, b, a, "main") == "ahead", S.sync_state(repo, b, a, "main"))
    check("we behind a present remote commit is behind",
          S.sync_state(repo, a, b, "main") == "behind", S.sync_state(repo, a, b, "main"))
    check("no remote sha at all is a branch that exists nowhere else",
          S.sync_state(repo, a, "", "main") == "local-only-branch", "")


# ─────────── every state is declared by every consumer ─────────────────

def dashboard_chips() -> set[str]:
    """The dashboard's chip map is JS inside a Python file, so it is parsed —
    a substring check would pass on the word appearing in a comment."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    m = re.search(r"const SYNC = \{(.*?)\n  \};", src, re.S)
    return set(re.findall(r'"([a-z-]+)":', m.group(1))) if m else set()


def test_no_state_can_reach_a_surface_and_vanish() -> None:
    """THE STRUCTURAL CAUSE. `ahead` was not judged harmless — it was never
    listed, and both consumers skip an unlisted state without a word. The set
    the collector can emit is now a published object, so this compares two real
    objects rather than grepping for names."""
    import scan_remotes as S
    import build_findings as B
    importlib.reload(S)
    importlib.reload(B)

    check("the collector publishes the states it can emit",
          isinstance(getattr(S, "STATES", None), (set, frozenset)) and len(S.STATES) >= 7,
          str(getattr(S, "STATES", None)))
    declared = set(B.SYNC_FINDINGS) | set(B.SYNC_SILENT)
    missing = sorted(S.STATES - declared)
    check("every state is declared in the findings table or its silent set",
          not missing, f"undeclared: {missing}")
    check("and `current` is the only state that deliberately says nothing",
          set(B.SYNC_SILENT) == {"current"}, str(sorted(B.SYNC_SILENT)))

    chips = dashboard_chips()
    check("the dashboard's chip map was parsed at all", len(chips) >= 6, str(chips))
    # `current` earns no chip by design — the common case says nothing.
    missing_chips = sorted(S.STATES - chips - {"current"})
    check("every state the operator can meet has a chip",
          not missing_chips, f"no chip: {missing_chips}")


# ─────────── the findings, from a planted registry ─────────────────────

def findings_for(sync: str, **local) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-syncf-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text('{"projects": []}')
    (d / "registry/relations.json").write_text('{"relations": []}')
    (d / "registry/repositories.json").write_text(json.dumps({"repositories": [
        {"id": "repository:o/r", "local": {
            "sync": sync, "checked_out_branch": "main",
            "remote_checked_on": "2026-09-07", **local}}]}))
    keys = ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("clone.")]
    finally:
        # Restored rather than popped: the runner's own redirects must survive.
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(paths)


def test_ahead_is_reported_as_work_that_exists_nowhere_else() -> None:
    """THE DEFECT as it was measured: eight repositories, the observatory's own among them."""
    got = findings_for("ahead")
    check("it fires at all", len(got) == 1, str(got)[:200])
    if not got:
        return
    f = got[0]
    check("as a warning, like the branch that exists on no remote",
          f["severity"] == "warning",
          "both mean the disk is the only copy; severity must not depend on "
          "whether the branch has a remote counterpart")
    check("the title says the commits are unpushed",
          "не" not in f["title"] and "push" in f["action"], f"{f['title']} / {f['action']}")
    check("and the detail says what is at stake",
          "only this disk" in f["detail"], f["detail"])


def test_stale_and_at_risk_do_not_share_a_severity() -> None:
    stale = findings_for("stale")
    risk = findings_for("unpushed-and-remote-moved")
    check("a merely stale clone is info", stale and stale[0]["severity"] == "info",
          str(stale)[:160])
    check("and says nothing here is at risk",
          stale and "nothing here" in stale[0]["detail"], str(stale)[:200])
    check("unpushed work beside a moved remote is a warning",
          risk and risk[0]["severity"] == "warning", str(risk)[:160])
    check("and it is honest that the comparison is against the last fetch",
          risk and "last fetch" in risk[0]["detail"], str(risk)[:220])
    check("neither of them tells the operator to go and measure it",
          all("fetch and look" not in f["action"] for f in stale + risk),
          str([f["action"] for f in stale + risk]))


def test_an_unknown_state_is_raised_rather_than_dropped() -> None:
    """A state absent from the table is a fault in this system, and a fault the
    code detects belongs in the findings, not in a log nobody reads."""
    got = findings_for("something-new")
    check("silence is no longer an option", len(got) == 1, str(got)[:200])
    if got:
        check("it names the unknown state", "something-new" in got[0]["detail"],
              got[0]["detail"][:160])
        check("and it is the system's own fault, not the operator's",
              got[0]["type"] == "clone.unknown-state", got[0]["type"])


def test_behind_or_diverged_still_reports_when_it_is_the_true_answer() -> None:
    got = findings_for("behind-or-diverged")
    check("the honest ambiguity survives where nothing local can answer it",
          len(got) == 1 and got[0]["severity"] == "info", str(got)[:200])
    if got:
        check("and it says why the question is open",
              "no tracking ref" in got[0]["detail"], got[0]["detail"][:200])


# ─────────── no probe reaches a credential store ───────────────────────

def test_a_remote_asking_for_a_password_never_reaches_a_credential_helper() -> None:
    """`git ls-remote` against an HTTPS remote that answers 401 asks every
    configured credential helper — on macOS `osxkeychain`, which can put a
    Keychain dialog in front of the operator from an unattended tick. This
    collector is credential-free by contract: the helper list is emptied for
    the probe, and a remote that wants a password is simply unreachable."""
    import http.server, threading
    import scan_remotes as S
    seen: list[str] = []

    class Asks(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                          # noqa: N802
            seen.append(self.path)
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="example"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):                              # quiet
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Asks)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-helper-"))
    marker = d / "helper-was-asked"
    helper = d / "helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    helper.chmod(0o700)
    config = d / "gitconfig"
    config.write_text(f"[credential]\n\thelper = {helper}\n", encoding="utf-8")
    saved = S.ENV
    S.ENV = {**saved, "GIT_CONFIG_GLOBAL": str(config)}
    try:
        folder, got = S.probe({"folder": "alpha-web", "path": str(d),
                               "remote": f"http://127.0.0.1:{server.server_port}/alpha-web.git",
                               "branch": "main"})
    finally:
        S.ENV = saved
        server.shutdown()
    check("the remote was asked, and answered with a password demand", bool(seen), str(seen))
    check("a remote that wants a password is reported unreachable",
          got.get("reachable") is False, str(got)[:200])
    check("no credential helper ran — no keychain can be consulted",
          not marker.exists(), "the helper configured for the user was invoked")


if __name__ == "__main__":
    print("clone sync — eight clones whose work existed nowhere else\n")
    for fn in (test_the_tracking_ref_answers_it_without_a_fetch,
               test_no_tracking_ref_is_still_answered_with_a_refusal,
               test_the_states_that_could_always_be_told_apart_still_are,
               test_no_state_can_reach_a_surface_and_vanish,
               test_ahead_is_reported_as_work_that_exists_nowhere_else,
               test_stale_and_at_risk_do_not_share_a_severity,
               test_an_unknown_state_is_raised_rather_than_dropped,
               test_behind_or_diverged_still_reports_when_it_is_the_true_answer,
               test_a_remote_asking_for_a_password_never_reaches_a_credential_helper):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mno clone's unpushed work reaches a surface and vanishes\033[0m")
