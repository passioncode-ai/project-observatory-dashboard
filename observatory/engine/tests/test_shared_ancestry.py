#!/usr/bin/env python3
"""Two repositories, one commit: who gets it, and does anyone say so.

Two repositories can hold the same commit without either being a fork — creating
one from a copy of another's history does it, and GitHub records no `fork` flag
for that. A real estate had dozens of commits shared by two repositories created
seven weeks apart.

`INSERT OR IGNORE` on `(kind, ref)` records such a commit once and says nothing,
so the younger repository's history simply looks shorter than it is — and that is
indistinguishable from a repository that did less work.

Recording it twice would be worse: it would credit the younger repository with
the elder's history, which is the mistake `estate.py` prevents one level up. So
the commit stays single, the ELDER repository owns it by a stated rule rather
than by `sorted()`, and the sharing is reported.

This file plants the case the live estate cannot show: two repositories whose
alphabetical order is the OPPOSITE of their age. Under the old rule the younger
one would win.
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def git(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_estate() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """An elder repository whose name sorts LAST, and a younger copy of it."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ancestry-"))
    elder = d / "zeta-elder"          # sorts after "alpha-younger"
    elder.mkdir()
    git("init", "-q", "-b", "main", ".", cwd=elder)
    git("config", "user.email", "t@example.com", cwd=elder)
    git("config", "user.name", "T", cwd=elder)
    for i in range(3):
        (elder / "f").write_text(str(i))
        git("add", "f", cwd=elder)
        git("commit", "-q", "-m", f"shared-{i}", cwd=elder)
    younger = d / "alpha-younger"
    subprocess.run(["git", "clone", "-q", str(elder), str(younger)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git("config", "user.email", "t@example.com", cwd=younger)
    git("config", "user.name", "T", cwd=younger)
    (younger / "own").write_text("only here")
    git("add", "own", cwd=younger)
    git("commit", "-q", "-m", "the younger repository's own work", cwd=younger)

    reg = d / "registry"
    reg.mkdir()
    def repo(name: str, path: pathlib.Path, created: str) -> dict:
        return {"id": f"repository:test/{name}", "host": "github.com",
                "name_with_owner": f"test/{name}", "url": "", "visibility": "private",
                "default_branch": "main", "description": "", "archived": False,
                "fork": False, "language": "", "topics": [], "last_pushed_on": "",
                "created_on": created, "discovered_by": "github-api",
                "source_refs": ["SRC-0008"],
                "local": {"path": str(path), "folder": name, "symlink": False,
                          "checked_out_branch": "main", "commits": 3,
                          "last_commit_on": "", "uncommitted_files": 0, "stack": []}}
    (reg / "repositories.json").write_text(json.dumps({"schema_version": 2, "repositories": [
        repo("zeta-elder", elder, "2020-01-01"),        # older, sorts last
        repo("alpha-younger", younger, "2024-01-01"),   # younger, sorts first
    ]}), encoding="utf-8")
    (reg / "projects.json").write_text(json.dumps({"schema_version": 2, "projects": [
        {"id": "project:elder", "name": "elder", "ownership": "owned"},
        {"id": "project:younger", "name": "younger", "ownership": "owned"},
    ]}), encoding="utf-8")
    (reg / "relations.json").write_text(json.dumps({"schema_version": 2, "relations": [
        {"id": "r1", "type": "implemented_by", "from": "project:elder",
         "to": "repository:test/zeta-elder"},
        {"id": "r2", "type": "implemented_by", "from": "project:younger",
         "to": "repository:test/alpha-younger"},
    ]}), encoding="utf-8")
    return d, reg, d / "store.db"


def test_the_elder_repository_owns_shared_ancestry() -> None:
    d, reg, db = build_estate()
    p = subprocess.run([PY, "collectors/scan_events.py"], cwd=ROOT, capture_output=True,
                       text=True, timeout=300,
                       env={**os.environ, "OBSERVATORY_REGISTRY": str(reg),
                            "OBSERVATORY_DB": str(db)})
    out = p.stdout + p.stderr
    check("the collector ran", p.returncode == 0, out[-200:])

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = dict(conn.execute(
        "SELECT repo_id, COUNT(*) FROM events WHERE kind='commit' GROUP BY repo_id").fetchall())
    elder, younger = "repository:test/zeta-elder", "repository:test/alpha-younger"
    check("the ELDER repository owns the three shared commits, though it sorts last",
          rows.get(elder) == 3, str(rows))
    check("the younger repository keeps only its own commit",
          rows.get(younger) == 1, str(rows))
    check("each commit is recorded exactly once",
          sum(rows.values()) == 4, str(rows))

    check("the sharing is reported, not silent", "SHARED ANCESTRY" in out, out[-300:])
    check("and the report names both addresses and the count",
          "zeta-elder" in out and "alpha-younger" in out and "3 commit" in out,
          [l for l in out.splitlines() if "SHARED" in l or "also in" in l])

    scan = conn.execute("SELECT counts_json FROM scans ORDER BY started_at DESC LIMIT 1").fetchone()
    counts = json.loads(scan[0]) if scan and scan[0] else {}
    check("the scan record counts the shared commits", counts.get("commits_shared") == 3,
          str(counts))


def test_the_rule_is_age_not_the_alphabet() -> None:
    src = (ROOT / "collectors/scan_events.py").read_text(encoding="utf-8")
    check("the iteration is ordered by creation date", "key=by_age" in src)
    check("with a stated fallback for a repository of unknown age",
          '"9999-12-31"' in src, "an undated repository must not sort first by accident")
    check("plain alphabetical iteration is gone", "sorted(repos.items()):" not in src)


if __name__ == "__main__":
    print("shared ancestry — one commit, two repositories\n")
    for fn in (test_the_elder_repository_owns_shared_ancestry,
               test_the_rule_is_age_not_the_alphabet):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe elder repository owns what both of them hold\033[0m")
