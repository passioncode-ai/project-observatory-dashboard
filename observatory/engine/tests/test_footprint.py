#!/usr/bin/env python3
"""Three questions about disk, and one metric was answering all of them wrong.

`host.disk_low` is the critical finding that says the volume is nearly full, and
it reads the footprint metric to name who is responsible. Measured on a real
estate with **2 GB free**:

    projects root on disk (du)       55.27 GB
    disk.bytes, all projects          8.76 GB   -> 16.1 % of it
    reclaimable (node_modules etc.)  36.70 GB   -> measured by nothing

So the finding asked *why is my disk full* while reading the answer to *what
does this project cost*. Both numbers are right about their own question; one
was standing in for the other.

**And a whole class of checkout was invisible.** `collectors/merge.py` refuses to
let a git worktree displace a real checkout and records the demoted ones in
`repositories[].local.extra_clones` — so the registry had known about ten
worktrees of one project for days, and `plugins/disk_usage.py` read
`projects[].local_folders`, the primary checkout only.

Three metrics now, three meanings, one plugin:

    disk.bytes              the working trees, UNCHANGED so its series stays
                            comparable with its own past
    disk.worktree_bytes     the extra checkouts merge already recorded
    disk.reclaimable_bytes  what a reinstall would restore — the number that
                            answers a full volume

**A correction, recorded because the size of a claim matters as much as its
direction.** The previous iteration reported "~1.5 GB unmeasured" and then
"4.02 GB", both measured with `du` — which counts everything, including the
reinstallables the footprint metric deliberately prunes. By the metric's own
definition the worktrees hold 0.12 GB, not 4.02. The gap was real; the figure
was a comparison between two different questions, which is the mistake this
whole file is about.
"""
from __future__ import annotations
import importlib, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402
# A private synthetic estate backs the findings half; the plugin half builds its
# own projects root and registry below. Nothing on the machine is read.
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
MANIFEST = json.loads((ROOT / "plugins/disk-usage.json").read_text(encoding="utf-8"))
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def estate(worktree: bool = True, node_modules_bytes: int = 4096) -> tuple[pathlib.Path, dict]:
    """A projects root with one project, one worktree of it, and a pruned subtree.

    The pruned bytes are placed DEEP — `node_modules/pkg/dist/big.bin` — because
    the first version of the reclaimable walk counted only the files directly
    inside a pruned directory and reported 0.17 GB where 1.68 GB sat.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-footprint-"))
    data, reg = d / "projects", d / "registry"
    for sub in ("proj", "proj-wt"):
        (data / sub).mkdir(parents=True)
        (data / sub / "src.txt").write_bytes(b"x" * 1000)
    deep = data / "proj/node_modules/pkg/dist"
    deep.mkdir(parents=True)
    (deep / "big.bin").write_bytes(b"y" * node_modules_bytes)
    reg.mkdir()
    (reg / "projects.json").write_text(json.dumps({"projects": [
        {"id": "project:p", "name": "p", "lifecycle": "active",
         "local_folders": ["proj"]}]}))
    (reg / "repositories.json").write_text(json.dumps({"repositories": [
        {"id": "repository:o/p", "name_with_owner": "o/p",
         "local": {"folder": "proj", "path": str(data / "proj"),
                   **({"extra_clones": ["proj-wt"]} if worktree else {})}}]}))
    (reg / "relations.json").write_text(json.dumps({"relations": [
        {"id": "rel:1", "type": "implemented_by", "from": "project:p",
         "to": "repository:o/p"}]}))
    env = dict(os.environ, OBSERVATORY_DATA=str(data), OBSERVATORY_REGISTRY=str(reg))
    return d, env


def measure(env: dict) -> dict[str, float]:
    p = subprocess.run([PY, "plugins/disk_usage.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise AssertionError(f"the plugin failed: {(p.stdout + p.stderr)[-300:]}")
    out = {}
    for line in p.stdout.splitlines():
        row = json.loads(line)
        out[row["metric"]] = row["value"]
    return out


# ─────────── three metrics, three meanings ─────────────────────────────

def test_the_manifest_declares_all_three_with_their_roles() -> None:
    names = {m["name"]: m for m in MANIFEST["metrics"]}
    for want in ("disk.bytes", "disk.worktree_bytes", "disk.reclaimable_bytes"):
        check(f"`{want}` is declared", want in names, str(sorted(names)))
    roles = {m.get("role") for m in MANIFEST["metrics"]}
    check("the footprint role is claimed", "footprint.bytes" in roles, str(roles))
    check("and the reclaimable role, so a core file can resolve it without "
          "naming a metric", "footprint.reclaimable.bytes" in roles, str(roles))
    for m in MANIFEST["metrics"]:
        check(f"`{m['name']}` says what it means", len(m.get("means", "")) > 60,
              m.get("means", "")[:40])


def test_the_three_measure_three_different_things() -> None:
    d, env = estate(node_modules_bytes=50_000)
    got = measure(env)
    check("the working tree is measured", got.get("disk.bytes", 0) >= 1000,
          json.dumps(got))
    check("and excludes the pruned subtree", got.get("disk.bytes", 0) < 50_000,
          json.dumps(got))
    check("the extra checkout is its own number",
          got.get("disk.worktree_bytes", 0) >= 1000, json.dumps(got))
    check("the reclaimable bytes are found DEEP inside the pruned tree",
          got.get("disk.reclaimable_bytes", 0) >= 50_000, json.dumps(got))


def test_a_worktree_does_not_change_what_disk_bytes_has_always_measured() -> None:
    """The comparability rule. Folding the extra checkouts into `disk.bytes`
    would mean yesterday's value measured one thing and today's another, and a
    trend across that boundary is a fiction."""
    with_wt = measure(estate(worktree=True)[1])
    without = measure(estate(worktree=False)[1])
    check("the footprint is identical either way",
          with_wt["disk.bytes"] == without["disk.bytes"],
          f"{with_wt.get('disk.bytes')} vs {without.get('disk.bytes')}")
    check("and only the worktree metric appears when there is one",
          "disk.worktree_bytes" in with_wt and "disk.worktree_bytes" not in without,
          f"{sorted(with_wt)} vs {sorted(without)}")


def test_a_folder_counted_twice_is_counted_once() -> None:
    d, env = estate()
    reg = pathlib.Path(env["OBSERVATORY_REGISTRY"])
    doc = json.loads((reg / "repositories.json").read_text())
    # The same folder as a declared home AND a recorded extra clone.
    doc["repositories"][0]["local"]["extra_clones"] = ["proj", "proj-wt"]
    (reg / "repositories.json").write_text(json.dumps(doc))
    got = measure(env)
    alone = measure(estate(worktree=True)[1])
    check("the duplicate does not inflate the extra checkouts",
          got.get("disk.worktree_bytes") == alone.get("disk.worktree_bytes"),
          f"{got.get('disk.worktree_bytes')} vs {alone.get('disk.worktree_bytes')}")


def test_a_vanished_worktree_is_not_an_error() -> None:
    d, env = estate()
    reg = pathlib.Path(env["OBSERVATORY_REGISTRY"])
    doc = json.loads((reg / "repositories.json").read_text())
    doc["repositories"][0]["local"]["extra_clones"] = ["deleted-by-its-agent"]
    (reg / "repositories.json").write_text(json.dumps(doc))
    got = measure(env)
    check("the plugin still succeeds", "disk.bytes" in got, json.dumps(got))
    check("and emits no row for what is gone",
          "disk.worktree_bytes" not in got, json.dumps(got))


def test_a_project_with_no_folder_emits_nothing() -> None:
    d, env = estate()
    reg = pathlib.Path(env["OBSERVATORY_REGISTRY"])
    (reg / "projects.json").write_text(json.dumps({"projects": [
        {"id": "project:nowhere", "name": "n", "lifecycle": "active"}]}))
    got = measure(env)
    check("no row at all, rather than a zero", got == {}, json.dumps(got))


# ─────────── the finding answers the question it asks ──────────────────

def test_the_disk_finding_names_what_would_free_space() -> None:
    """Driven, not read off a live board. The original read the machine's own
    findings and could only NOTE when the volume had room; here the synthetic
    store carries footprint and planted reclaimable measurements and the
    volume's free space is patched below the warning level, so the wording is
    asserted on every run.
    """
    import collections
    import build_findings as B
    importlib.reload(B)
    from store import db
    metric = B.metric_for_role(B.RECLAIMABLE_ROLE)
    check("an installed plugin claims the reclaimable role", bool(metric), str(metric))
    if not metric:
        return
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO metrics(project_id,metric,at,value,unit,source,"
                         "payload_json,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                         ("project:example-sample-1", metric, "2026-01-02T00:00:00Z", 4.0e9,
                          "bytes", "disk-usage", "{}", "2026-01-02T00:00:00Z"))
    finally:
        conn.close()
    freeable = B.reclaimable_holders()
    check("the reclaimable holders are computable", isinstance(freeable, list), str(freeable))
    check("and the planted holder is among them",
          any("example-sample-1" in h for h in freeable), str(freeable))
    usage = collections.namedtuple("usage", "total used free")
    low = (B.DISK_WARNING_MIB - 100) * 1024 * 1024
    real = B.shutil.disk_usage
    try:
        B.shutil.disk_usage = lambda p: usage(low * 20, low * 19, low)
        rows = [f for f in B.collect() if f["type"] == "host.disk_low"]
    finally:
        B.shutil.disk_usage = real
    check("a nearly full volume raises host.disk_low", len(rows) == 1, str(len(rows)))
    if not rows:
        return
    detail = rows[0]["detail"]
    check("the finding distinguishes cost from reclaimable",
          "Largest working trees" in detail and "reclaimable" in detail,
          detail[-200:])
    check("and says a reinstall restores them",
          "reinstall restores" in detail, detail[-160:])


def test_the_roles_are_resolved_rather_than_named() -> None:
    import source_reader
    src = source_reader.code_keeping_strings(
        (ROOT / "tools/build_findings.py").read_text(encoding="utf-8"))
    for metric in ("disk.bytes", "disk.worktree_bytes", "disk.reclaimable_bytes"):
        check(f"no core file names `{metric}`", metric not in src,
              "a core file naming a plugin's metric is the six-file problem")
    check("the reclaimable role is a constant", "RECLAIMABLE_ROLE" in src, "")
    check("and it is resolved through the manifest",
          "metric_for_role(RECLAIMABLE_ROLE)" in src, "")


if __name__ == "__main__":
    print("the footprint — three questions about one disk\n")
    for fn in (test_the_manifest_declares_all_three_with_their_roles,
               test_the_three_measure_three_different_things,
               test_a_worktree_does_not_change_what_disk_bytes_has_always_measured,
               test_a_folder_counted_twice_is_counted_once,
               test_a_vanished_worktree_is_not_an_error,
               test_a_project_with_no_folder_emits_nothing,
               test_the_disk_finding_names_what_would_free_space,
               test_the_roles_are_resolved_rather_than_named):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mwhat a project costs, what its extra checkouts cost, and what "
          "would free the disk\033[0m")
