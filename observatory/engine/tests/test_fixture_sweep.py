#!/usr/bin/env python3
"""The leak that stopped the work, and the disk finding that blamed the wrong thing.

Measured once: `no space left on device` out of the gate, with **thousands of
`observatory-*` directories in `$TMPDIR` holding tens of gigabytes**, most of
them from that day. `tests/tmp.py` does register `shutil.rmtree` with `atexit`, and
it works: a suite run by hand leaks nothing and the whole `check` group leaks
nothing (both measured after the sweep). What leaks is a process that never
reaches its handlers — killed by a timeout, by the out-of-space crash itself, by
a session ending.

**And `host.disk_low` blamed the operator's projects.** It names the largest
holders under the project directory from the `disk-usage` plugin, so a volume filled by this
project's own litter elsewhere reads as somebody's repository being too big. It
produced a wrong attribution out loud before the directories were counted, which
is why the sweep reports its own volume instead of leaving the disk finding to
carry it.

The floor is the whole safety argument: a concurrent gate run's fixtures are
minutes old, so refusing to touch anything younger than six hours is what keeps
a sweeper from deleting the working set of a run in flight.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sys, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def planted(ages_hours: dict[str, float], size: int = 4096) -> pathlib.Path:
    """A temp root holding fixture dirs of given ages, plus one directory that
    is NOT ours — the sweeper must leave it alone."""
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sweeproot-"))
    now = time.time()
    for name, age in ages_hours.items():
        d = root / name
        (d / "nested").mkdir(parents=True)
        (d / "nested" / "f.bin").write_bytes(b"x" * size)
        stamp = now - age * 3600
        os.utime(d, (stamp, stamp))
    other = root / "someone-elses-work"
    other.mkdir()
    (other / "keep.txt").write_text("not ours", encoding="utf-8")
    os.utime(other, (now - 999 * 3600, now - 999 * 3600))
    return root


def module():
    import sweep_fixtures as S
    return importlib.reload(S)


def test_the_age_floor_protects_a_run_in_flight() -> None:
    S = module()
    root = planted({"observatory-old-a": 10, "observatory-old-b": 7,
                    "observatory-live": 0.02})
    stale, fresh = S.survey(root, older_than_h=6)
    check("the two old fixtures are stale", len(stale) == 2,
          str([r["path"].rsplit("/", 1)[-1] for r in stale]))
    check("and the one from a live run is kept", len(fresh) == 1
          and fresh[0]["path"].endswith("observatory-live"), str(fresh))
    check("bytes are measured, not guessed",
          all(r["bytes"] >= 4096 for r in stale), str([r["bytes"] for r in stale]))


def test_it_removes_only_its_own_prefix() -> None:
    S = module()
    root = planted({"observatory-gone": 48})
    rep = S.sweep(root, older_than_h=6)
    check("the stale fixture is removed", rep["removed"] == 1, json.dumps(rep)[:200])
    check("and the space is reported", rep["freed_bytes"] >= 4096, str(rep["freed_bytes"]))
    check("a directory that is not ours survives",
          (root / "someone-elses-work" / "keep.txt").is_file(),
          "the glob is `observatory-*`, and litter with another name is "
          "somebody else's business")
    check("nothing of ours is left behind",
          not list(root.glob("observatory-gone")), "")


def test_a_dry_run_removes_nothing_and_still_measures() -> None:
    S = module()
    root = planted({"observatory-a": 48, "observatory-b": 48})
    rep = S.sweep(root, older_than_h=6, dry_run=True)
    check("it counts what it would remove", rep["stale"] == 2, json.dumps(rep)[:200])
    check("and reports the volume", rep["stale_bytes"] >= 8192, str(rep["stale_bytes"]))
    check("while removing nothing", rep["removed"] == 0 and rep["freed_bytes"] == 0,
          f"removed={rep['removed']} freed={rep['freed_bytes']}")
    check("both fixtures are still on disk",
          len(list(root.glob("observatory-*"))) == 2, "")


def test_one_undeletable_fixture_does_not_stop_the_sweep() -> None:
    """A swallowed failure here would be a sweeper reporting success while the
    disk fills — the shape of hours of silent failure seen before."""
    S = module()
    root = planted({"observatory-x": 48, "observatory-blocked": 48,
                    "observatory-y": 48})
    blocked = root / "observatory-blocked"
    os.chmod(blocked / "nested", 0o500)          # cannot unlink the file inside
    os.chmod(blocked, 0o500)                     # cannot unlink `nested` either
    try:
        rep = S.sweep(root, older_than_h=6)
        check("the other two are still removed", rep["removed"] == 2,
              f"removed={rep['removed']} refused={rep['refused']}")
        check("and the refusal is named with its reason",
              len(rep["refused"]) == 1 and "blocked" in rep["refused"][0]["path"]
              and rep["refused"][0]["reason"], json.dumps(rep["refused"])[:200])
    finally:
        os.chmod(blocked, 0o700)
        os.chmod(blocked / "nested", 0o700)


def test_the_receipt_carries_what_a_later_reader_needs() -> None:
    S = module()
    root = planted({"observatory-big": 48}, size=200_000)
    rep = S.sweep(root, older_than_h=6)
    for key in ("ran_at", "root", "older_than_hours", "removed", "freed_bytes",
                "stale_bytes", "fresh_kept", "refused", "largest"):
        check(f"the report states `{key}`", key in rep, str(sorted(rep)))
    check("and names the largest litter",
          rep["largest"] and rep["largest"][0]["bytes"] >= 200_000,
          json.dumps(rep["largest"])[:200])


# ─────────── the finding, so the disk stops blaming the projects ──────────

def findings_for(receipt: dict | None) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sweepf-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text('{"projects": []}')
    (d / "registry/repositories.json").write_text('{"repositories": []}')
    (d / "registry/relations.json").write_text('{"relations": []}')
    if receipt is not None:
        (d / "scratch/fixtures.json").write_text(json.dumps(receipt))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("fixtures.")]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def receipt(freed: int, refused: list | None = None, **kw) -> dict:
    return {"ran_at": "2026-09-07T13:00:00Z", "root": "/tmp",
            "older_than_hours": 6.0, "dry_run": False, "stale": 1,
            "fresh_kept": 0, "removed": 1, "freed_bytes": freed,
            "stale_bytes": freed, "fresh_bytes": 0,
            "refused": refused or [], "largest": [], **kw}


def test_a_large_sweep_is_reported_so_the_disk_finding_stops_lying() -> None:
    got = findings_for(receipt(9 * 1024 ** 3))
    check("it fires", len(got) == 1, str(got)[:200])
    if not got:
        return
    f = got[0]
    check("as a warning at nine gigabytes", f["severity"] == "warning", f["severity"])
    check("naming the volume", "9.0 GiB" in f["detail"] or "9.00 GiB" in f["detail"],
          f["detail"][:200])
    # The engine names the configured project directory rather than a fixed
    # path, since the estate's location is the workspace's own setting.
    check("and saying the litter is this project's own",
          "own" in f["detail"] and "configured project directory" in f["detail"],
          "the disk finding blames the largest holders under the project "
          "directory, and this is what stops that reading")


def test_a_small_sweep_is_routine_and_says_so() -> None:
    got = findings_for(receipt(3 * 1024 ** 2))
    check("it is info, not a warning", got and got[0]["severity"] == "info",
          str(got)[:200])


def test_nothing_swept_raises_nothing() -> None:
    check("a clean sweep is silence", findings_for(receipt(0, stale=0, removed=0)) == [],
          "a tool that reports every time it found nothing is a tool people stop reading")


def test_an_absent_receipt_is_not_an_answer() -> None:
    check("no receipt raises no finding", findings_for(None) == [],
          "the sweep has not run; that is not the same as nothing to sweep")


def test_a_refusal_is_raised_even_when_little_was_freed() -> None:
    """The sweep can free almost nothing AND be unable to remove what matters."""
    got = findings_for(receipt(1024, refused=[
        {"path": "/tmp/observatory-stuck", "reason": "PermissionError: denied"}]))
    check("the refusal surfaces", len(got) == 1, str(got)[:200])
    if got:
        check("as a warning, since it will recur every run",
              got[0]["severity"] == "warning", got[0]["severity"])
        check("and it names one path so the reader can look",
              "observatory-stuck" in got[0]["detail"], got[0]["detail"][:200])


if __name__ == "__main__":
    print("fixture sweep — 22.3 GiB of this project's own litter\n")
    for fn in (test_the_age_floor_protects_a_run_in_flight,
               test_it_removes_only_its_own_prefix,
               test_a_dry_run_removes_nothing_and_still_measures,
               test_one_undeletable_fixture_does_not_stop_the_sweep,
               test_the_receipt_carries_what_a_later_reader_needs,
               test_a_large_sweep_is_reported_so_the_disk_finding_stops_lying,
               test_a_small_sweep_is_routine_and_says_so,
               test_nothing_swept_raises_nothing,
               test_an_absent_receipt_is_not_an_answer,
               test_a_refusal_is_raised_even_when_little_was_freed):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe project sweeps its own litter and says how much there was\033[0m")
