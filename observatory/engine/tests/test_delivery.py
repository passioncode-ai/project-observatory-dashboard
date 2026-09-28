#!/usr/bin/env python3
"""The two surfaces that tell a person something, and what each could not say.

`tools/notify_findings.py` is the only path from a finding to a human.
`tools/audit_vault_links.py` is the only check on the wiki this system writes
into on every tick. Both had a hole of the same shape: a true statement they
were structurally unable to make.

**Once-only was once-FOR-EVER.** The dedup key was `<id>@<severity>` and the
event store keeps it, so a finding that closed and came back was announced the
first time and never again. Not hypothetical: a `clone.local-only-branch`
finding closed within a day of the channel going live. Had that branch
reappeared, the tool whose entire purpose is telling a human would have been
silent by design. The key now carries an episode, `<id>@<severity>#<k>`, where
k counts recorded endings; `UNIQUE(kind, ref)` still does the enforcing.

**The link audit's result had no reader.** It is not in the gate and was not in
the tick, while `commit_projection.py` writes into that same wiki every tick — so
the one thing that can break a wikilink ran constantly and the checker for it ran
only when a person typed it. It now leaves a receipt and the tick runs it.

**And it reported a working link as suspect.** Obsidian resolves `[[deep-note]]`
to `projects/x/deep-note.md` wherever it lives; this checker only tried
`root/deep-note.md` and filed the link under "may be prose about wikilink
syntax". A false positive of exactly the kind the tool's own docstring was
written about, and it made the ambiguous bucket useless.

**A missing wiki exited 1** — the same code as broken links — so on any machine
without a wiki the tool was red for a reason that had nothing to do with a link.

Every case runs in the synthetic workspace with its own fixture stores.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── notify: an episode ends, and the next one speaks ──────────

def notifier(d: pathlib.Path):
    """The module, pointed at a fixture store, with the channel stubbed.

    `notify()` shells out to `osascript`, which on a desktop puts a real
    notification on the operator's screen. A test suite must not do that, so the
    channel is replaced and the RECORDING path — the half that decides what is
    said next time — is what gets exercised.
    """
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    os.environ["OBSERVATORY_REGISTRY"] = str(d / "registry")
    os.environ["OBSERVATORY_SCRATCH"] = str(d / "scratch")
    import paths
    importlib.reload(paths)
    from store import db as store_db
    importlib.reload(store_db)
    store_db.connect().close()                      # the schema, once
    sys.path.insert(0, str(ROOT / "tools"))
    import notify_findings as N
    importlib.reload(N)
    N.notify = lambda title, body: (True, "stubbed channel")
    return N


def findings_file(d: pathlib.Path, rows: list[dict]) -> None:
    (d / "registry").mkdir(exist_ok=True)
    (d / "registry/findings.json").write_text(json.dumps({"findings": rows}))


def workspace() -> pathlib.Path:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-delivery-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    return d


def refs(d: pathlib.Path, kind: str) -> list[str]:
    conn = sqlite3.connect(d / "observatory.db")
    try:
        return [r[0] for r in conn.execute(
            "select ref from events where kind = ? order by rowid", (kind,))]
    finally:
        conn.close()


F = {"id": "clone.local-only-branch:repository:o/r", "severity": "warning",
     "title": "a branch exists only locally"}


def test_a_finding_that_returns_is_announced_again() -> None:
    d = workspace()
    N = notifier(d)
    findings_file(d, [F])
    N.main([])
    check("the first occurrence is notified",
          refs(d, "finding.notified") == [f"{F['id']}@warning#0"],
          str(refs(d, "finding.notified")))

    findings_file(d, [F])
    N.main([])
    check("a steady finding stays quiet",
          len(refs(d, "finding.notified")) == 1, str(refs(d, "finding.notified")))

    findings_file(d, [])                            # it closed
    N.main([])
    check("its ending is recorded", refs(d, "finding.cleared") == [f"{F['id']}#0"],
          str(refs(d, "finding.cleared")))

    findings_file(d, [F])                           # and it came back
    N.main([])
    check("THE RECURRENCE IS ANNOUNCED",
          refs(d, "finding.notified") == [f"{F['id']}@warning#0",
                                          f"{F['id']}@warning#1"],
          str(refs(d, "finding.notified")))

    findings_file(d, [])
    N.main([])
    findings_file(d, [])
    N.main([])
    check("recording the same ending twice does nothing",
          refs(d, "finding.cleared") == [f"{F['id']}#0", f"{F['id']}#1"],
          str(refs(d, "finding.cleared")))


def test_a_legacy_ref_counts_as_the_first_episode() -> None:
    """Rows written before the episode existed carry no `#k`. Read
    any other way they would all re-notify at once, which is the noise this
    tool exists to prevent."""
    d = workspace()
    N = notifier(d)
    conn = sqlite3.connect(d / "observatory.db")
    with conn:
        conn.execute("insert into events (id, kind, ref, actor, occurred_at,"
                     " payload_json) values ('ev:legacy','finding.notified',?,"
                     "'tool:notify_findings','2026-09-06T18:19:53Z','{}')",
                     (f"{F['id']}@warning",))
    conn.close()
    findings_file(d, [F])
    N.main([])
    check("a legacy notification is not repeated",
          refs(d, "finding.notified") == [f"{F['id']}@warning"],
          str(refs(d, "finding.notified")))


def test_a_dry_run_writes_nothing() -> None:
    """Caught for real: `close_episodes` WRITES, and it ran before the dry-run
    branch — so `--dry-run` advanced the counter that decides what the next real
    run says."""
    d = workspace()
    N = notifier(d)
    findings_file(d, [F])
    N.main([])
    findings_file(d, [])
    N.main(["--dry-run"])
    check("no ending is recorded by a dry run", refs(d, "finding.cleared") == [],
          str(refs(d, "finding.cleared")))
    N.main([])
    check("and a real run afterwards records it",
          refs(d, "finding.cleared") == [f"{F['id']}#0"],
          str(refs(d, "finding.cleared")))


def test_an_unreadable_findings_file_leaves_a_receipt() -> None:
    d = workspace()
    N = notifier(d)
    (d / "registry/findings.json").write_text('{"findings": [')
    rc = N.main([])
    check("it refuses", rc == 1, str(rc))
    rep = json.loads((d / "scratch/notify.json").read_text(encoding="utf-8"))
    check("and says the channel did not deliver", rep["delivered"] is False, str(rep))
    check("naming the cause", "unreadable" in rep["detail"], str(rep))


# ─────────── the link audit ────────────────────────────────────────────

def vault(broken: bool = False, bare: bool = True, twin: bool = False) -> pathlib.Path:
    v = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-vault-"))
    (v / "projects/x").mkdir(parents=True)
    (v / "projects/x/deep-note.md").write_text("# deep\n")
    if twin:
        (v / "projects/y").mkdir(parents=True)
        (v / "projects/y/deep-note.md").write_text("# also deep\n")
    body = ["A full-path link [[projects/x/deep-note]].",
            "An anchor [[projects/x/deep-note#Section]].",
            "A table link [[projects/x/deep-note\\|label]]."]
    if bare:
        body.append("A bare name [[deep-note]].")
    if broken:
        body.append("A broken one [[projects/x/nowhere]].")
    (v / "index.md").write_text("\n".join(body) + "\n")
    return v


def links(v: pathlib.Path | str, *args: str, scratch: pathlib.Path | None = None):
    """Run the audit against a workspace of its own.

    The engine validates the selected registry whenever `paths` is imported,
    and an earlier case here deliberately leaves an unreadable `findings.json`
    behind in its registry. Inheriting that selection would make the receipt
    write fail for a reason that has nothing to do with links.
    """
    env = dict(os.environ)
    scratch = scratch or workspace() / "scratch"
    env["OBSERVATORY_SCRATCH"] = str(scratch)
    env["OBSERVATORY_REGISTRY"] = str(scratch.parent / "registry")
    return subprocess.run([PY, "tools/audit_vault_links.py", "--vault", str(v), *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def test_a_bare_name_resolves_the_way_obsidian_resolves_it() -> None:
    p = links(vault())
    check("a vault of working links is clean", p.returncode == 0, p.stdout[-200:])
    check("and nothing is filed as ambiguous", "ambiguous:     0" in p.stdout,
          p.stdout[-260:])
    sys.path.insert(0, str(ROOT / "tools"))
    import audit_vault_links as A
    importlib.reload(A)
    v = vault()
    ok, why = A.resolve(v, "deep-note", A.basenames(v))
    check("the bare name resolves by basename", ok, why)
    check("and says how it resolved", "basename" in why, why)


def test_two_notes_with_one_basename_are_reported_as_proximity_dependent() -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    import audit_vault_links as A
    importlib.reload(A)
    v = vault(twin=True)
    ok, why = A.resolve(v, "deep-note", A.basenames(v))
    check("a doubled basename is not silently resolved", not ok, why)
    check("and the reason names proximity", "proximity" in why, why)


def test_a_broken_link_is_red() -> None:
    """Watched failing. A checker never seen red is a checker nobody has
    reason to trust."""
    d = workspace()
    p = links(vault(broken=True), scratch=d / "scratch")
    check("the audit exits non-zero", p.returncode == 1, str(p.returncode))
    check("and names the target", "projects/x/nowhere" in p.stdout, p.stdout[-300:])
    rep = json.loads((d / "scratch/vault-links.json").read_text(encoding="utf-8"))
    check("the receipt says broken-links", rep["outcome"] == "broken-links", str(rep))
    check("with the target in it", "projects/x/nowhere" in rep["targets"], str(rep))


def test_a_missing_wiki_is_not_a_broken_link() -> None:
    d = workspace()
    p = links("/nonexistent-wiki-path", scratch=d / "scratch")
    check("a machine without the wiki is not red", p.returncode == 0,
          f"exit {p.returncode}: {p.stderr[-160:]}")
    check("and it says nothing was checked", "nothing was checked" in p.stderr,
          p.stderr[-160:])
    rep = json.loads((d / "scratch/vault-links.json").read_text(encoding="utf-8"))
    check("the receipt distinguishes it from clean",
          rep["outcome"] == "vault-absent", str(rep))
    check("and reports no count rather than zero",
          rep["broken"] is None, str(rep))


def test_archives_are_skipped_by_path_component() -> None:
    v = vault()
    (v / "_archives").mkdir()
    (v / "_archives/snapshot.md").write_text("[[projects/x/long-gone]]\n")
    (v / "notes").mkdir()
    (v / "notes/my_archives-plan.md").write_text("[[projects/x/also-gone]]\n")
    p = links(v)
    check("a dated snapshot's stale links are skipped",
          "long-gone" not in p.stdout, p.stdout[-300:])
    check("but a note whose NAME merely contains the word is checked",
          "also-gone" in p.stdout,
          "`\"_archives\" in str(f)` skipped it — a silent exclusion from a count")
    p2 = links(v, "--include-archives")
    check("--include-archives says so and checks them",
          "long-gone" in p2.stdout, p2.stdout[-300:])


# ─────────── the findings that read the receipts ───────────────────────

def collect_with(d: pathlib.Path, free_mib: int | None = None) -> list[dict]:
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    os.environ["OBSERVATORY_REGISTRY"] = str(d / "registry")
    os.environ["OBSERVATORY_SCRATCH"] = str(d / "scratch")
    import paths
    importlib.reload(paths)
    sys.path.insert(0, str(ROOT / "tools"))
    import build_findings as B
    importlib.reload(B)
    if free_mib is not None:
        import collections as C
        usage = C.namedtuple("usage", "total used free")
        B.shutil.disk_usage = lambda p: usage(0, 0, free_mib * 1024 * 1024)
    return B.collect()


def test_broken_links_become_a_finding_and_an_absent_wiki_does_not() -> None:
    d = workspace()
    (d / "registry/projects.json").write_text('{"projects": []}')
    (d / "scratch/vault-links.json").write_text(json.dumps({
        "ran_at": "2099-01-01T00:00:00Z", "outcome": "broken-links", "broken": 2,
        "ambiguous": 0, "notes": 700, "links": 4000,
        "targets": ["projects/x/nowhere", "concepts/gone"]}))
    got = [f for f in collect_with(d) if f["type"] == "wiki.broken_link"]
    check("a broken link is a finding", len(got) == 1, str(got)[:200])
    if got:
        check("it names the targets", "projects/x/nowhere" in got[0]["detail"],
              got[0]["detail"][:160])
    (d / "scratch/vault-links.json").write_text(json.dumps({
        "ran_at": "2099-01-01T00:00:00Z", "outcome": "vault-absent", "broken": None,
        "ambiguous": None, "notes": 0, "links": 0, "detail": "no directory"}))
    got = [f for f in collect_with(d) if f["type"].startswith("wiki.")]
    check("a wiki that is elsewhere raises nothing", got == [], str(got)[:200])


def test_a_full_disk_is_one_finding_not_four_symptoms() -> None:
    """The cause of a real four-step tick failure. Free space is patched rather
    than filled, because filling a volume to test a threshold is a way to lose
    a database."""
    d = workspace()
    (d / "registry/projects.json").write_text('{"projects": []}')
    got = [f for f in collect_with(d, free_mib=200) if f["type"] == "host.disk_low"]
    check("a nearly full volume is critical", len(got) == 1 and
          got[0]["severity"] == "critical", str(got)[:200])
    if got:
        # The engine words this as a possibility ("may be unable"), since a
        # threshold cannot know which writer is first to fail.
        check("and names what stops working",
              "unable to write its output" in got[0]["detail"], got[0]["detail"][:120])
    got = [f for f in collect_with(d, free_mib=3000) if f["type"] == "host.disk_low"]
    check("a tight volume is a warning", len(got) == 1 and
          got[0]["severity"] == "warning", str(got)[:200])
    got = [f for f in collect_with(d, free_mib=200_000) if f["type"] == "host.disk_low"]
    check("a healthy volume raises nothing", got == [], str(got)[:200])


def test_the_tick_runs_the_link_audit() -> None:
    """The receipt exists so a finding can read it; the finding matters only if
    something refreshes it. `commit_projection` writes into the wiki on every
    tick, so the check belongs immediately after it."""
    src = (ROOT / "tools/tick.sh").read_text(encoding="utf-8")
    check("the tick runs the audit", "audit_vault_links.py" in src,
          "the tick writes into the wiki, and nothing else checks it")
    i, j = src.index("commit_projection.py"), src.index("audit_vault_links.py")
    check("after the step that writes the wiki", i < j, "order matters for the receipt")


if __name__ == "__main__":
    print("delivery — the finding that reaches a person, and the wiki that is checked\n")
    for fn in (test_a_finding_that_returns_is_announced_again,
               test_a_legacy_ref_counts_as_the_first_episode,
               test_a_dry_run_writes_nothing,
               test_an_unreadable_findings_file_leaves_a_receipt,
               test_a_bare_name_resolves_the_way_obsidian_resolves_it,
               test_two_notes_with_one_basename_are_reported_as_proximity_dependent,
               test_a_broken_link_is_red,
               test_a_missing_wiki_is_not_a_broken_link,
               test_archives_are_skipped_by_path_component,
               test_broken_links_become_a_finding_and_an_absent_wiki_does_not,
               test_a_full_disk_is_one_finding_not_four_symptoms,
               test_the_tick_runs_the_link_audit):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma recurrence speaks again, and the wiki is checked by whatever "
          "writes it\033[0m")
