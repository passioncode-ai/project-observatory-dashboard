#!/usr/bin/env python3
"""The wiki's copy of the registry: seven of ten documents, written unatomically.

`tools/project_into_vault.py` keeps a generated copy of the registry in the
narrative wiki, so Obsidian links and the `wiki-*` skills keep resolving. Every
file carries `_generated` and `_do_not_edit`, "because a projection nobody can
tell from a source is the second source of truth this split exists to prevent" —
a careful design with two holes in the carrying-out.

**The mirror was a hardcoded list of seven names, and the registry had grown
to ten.** `domain-liveness.json`, `findings.json` and `stale-remotes.json` were
absent from the wiki and nothing said so.
`findings.json` is the one that matters most — the wiki is where the operator's
narrative memory lives, and "what needs a person" was the document missing from
it. Same class as `purge_projections`' hardcoded pair of indexes, `OWNED_ORGS`,
and `scan_github`'s output directory: the set is now DERIVED from
`registry/*.json`, and excluding one takes a sentence in `NOT_MIRRORED`.

**And the write was not atomic**, into ANOTHER repository that the wiki skills
read. `atomic.write_json` exists in this project for exactly that; a direct
`write_text` that dies mid-write leaves truncated JSON in the wiki.

**Every refusal in the commit half returned 0 and told nobody.** That return is
deliberate — a scheduled job must not fail because the wiki is dirty — but it
made a standing refusal invisible: the projection could go uncommitted for a
week because one stray file sat in the wiki, and the tick's `step` would see a
clean exit. Seven paths now report through one helper, which is the eighth time
this repository has needed that shape.

The live-wiki checks of the original are driven here into a temporary vault
over the synthetic registry, so they run on every machine rather than being
skipped wherever no wiki exists.

**The finding needs BOTH conditions.** A refusal alone is an operator working in
their own wiki, which is the normal case, and firing on it would be noise the
board was deliberately cleared of. `projection.uncommitted` requires a current refusal AND a wiki
whose last commit is older than 24 hours — the difference between "somebody is
working in there" and "the mirror has stopped tracking the registry".
"""
from __future__ import annotations
import json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import paths                                                        # noqa: E402
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── the mirror is complete ────────────────────────────────────

def projection_home(base: pathlib.Path, enabled: bool = True) -> pathlib.Path:
    """A workspace home whose settings opt in to the wiki projection.

    The projection is an explicit feature in the engine — a mirror written into
    somebody's notes is not a default — so the suite opts in on a home of its
    own rather than editing the shared synthetic workspace.
    """
    home = base / "home"
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config/settings.json").write_text(json.dumps({
        "schema_version": 1, "sources": {}, "integrations": {},
        "features": {"wiki_projection": enabled}}), encoding="utf-8")
    return home


def project_into(vault: pathlib.Path, registry: pathlib.Path | None = None,
                 enabled: bool = True) -> subprocess.CompletedProcess:
    """Run the projection over a registry (the workspace's by default) into `vault`."""
    vault.mkdir(parents=True, exist_ok=True)
    scratch = vault.parent / "scratch"
    scratch.mkdir(exist_ok=True)
    return subprocess.run([PY, "tools/project_into_vault.py"], cwd=ROOT,
                          env=dict(os.environ,
                                   OBSERVATORY_HOME=str(projection_home(vault.parent, enabled)),
                                   OBSERVATORY_REGISTRY=str(registry or paths.REGISTRY),
                                   OBSERVATORY_VAULT=str(vault),
                                   OBSERVATORY_SCRATCH=str(scratch)),
                          capture_output=True, text=True, timeout=300)


def test_the_projection_is_opt_in() -> None:
    """A mirror written into a person's notes is a choice, not a default."""
    vault = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-mirror-off-")) / "vault"
    p = project_into(vault, enabled=False)
    check("without the feature the run exits cleanly", p.returncode == 0, p.stderr[-200:])
    check("and says how to turn it on", "features.wiki_projection" in p.stdout, p.stdout[-200:])
    check("and writes nothing into the vault", not any(vault.rglob("*.json")),
          str(sorted(x.name for x in vault.rglob("*.json"))))


def test_every_registry_document_is_mirrored() -> None:
    registry = {p.name for p in paths.REGISTRY.glob("*.json")}
    vault = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-mirror-all-")) / "vault"
    p = project_into(vault)
    check("the projection runs", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    dest = vault / "inventory"
    mirrored = {p.name for p in dest.glob("*.json")}
    sys.path.insert(0, str(ROOT / "tools"))
    import project_into_vault as pv
    missing = sorted(registry - mirrored - set(pv.NOT_MIRRORED))
    check("nothing in the registry is missing from the wiki", not missing,
          f"{missing} — the wiki is what the wiki-* skills read")
    check("and the set is DERIVED rather than listed",
          "paths.REGISTRY.glob" in (ROOT / "tools/project_into_vault.py").read_text(
              encoding="utf-8"),
          "a literal of seven names went stale the moment the registry grew")
    check("the exclusion list is empty today, with the reason it exists",
          pv.NOT_MIRRORED == {}, str(pv.NOT_MIRRORED))
    check("the three that were once absent are there",
          {"domain-liveness.json", "findings.json", "stale-remotes.json"} <= mirrored,
          str(sorted({"domain-liveness.json", "findings.json",
                      "stale-remotes.json"} - mirrored)))


def test_every_mirrored_file_says_it_is_generated() -> None:
    vault = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-mirror-stamp-")) / "vault"
    project_into(vault)
    files = sorted((vault / "inventory").glob("*.json"))
    check("there are mirrored files to inspect", bool(files), str(vault))
    # The canonical copy is this workspace's registry, named by its own URI:
    # the engine has no fixed repository name to point a reader at.
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8"))
        check(f"{f.name} carries `_generated`", doc.get("_generated") is True,
              str(sorted(doc)[:5]))
        check(f"{f.name} says where the canonical copy is",
              doc.get("_canonical_source") == paths.REGISTRY.as_uri(),
              str(doc.get("_canonical_source")))


def test_the_write_is_atomic() -> None:
    src = (ROOT / "tools/project_into_vault.py").read_text(encoding="utf-8")
    check("the projection writes through `atomic`", "atomic.write_json(dest / name" in src,
          "the destination is another repository the wiki skills read")
    check("and a direct write_text is gone",
          "(dest / name).write_text(" not in src,
          "a crash mid-write leaves truncated JSON in the wiki")


def test_the_run_reports_what_it_mirrored() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-mirror-"))
    (d / "registry").mkdir()
    (d / "registry/projects.json").write_text('{"projects": []}', encoding="utf-8")
    (d / "registry/domains.json").write_text('{"domains": []}', encoding="utf-8")
    (d / "registry/sources.json").write_text('{"sources": []}', encoding="utf-8")
    p = project_into(d / "vault", registry=d / "registry")
    f = d / "scratch/projection.json"
    check("a report is written", f.is_file(), (p.stdout + p.stderr)[-200:])
    if not f.is_file():
        return
    doc = json.loads(f.read_text(encoding="utf-8"))
    check("counting what it wrote", doc.get("written") == 3, str(doc.get("written")))
    check("naming nothing as skipped when nothing was", doc.get("skipped") == [],
          str(doc.get("skipped")))
    check("and the report says how many documents the registry has",
          doc.get("registry_documents") == 3, str(doc.get("registry_documents")))
    # THE STAMPS, on the file this run actually wrote. Once the only assertion
    # that a mirrored document says it is generated read the LIVE wiki, so on a
    # machine without one — a fresh clone, CI — nothing checked that the tool
    # stamps at all. This drives the same property with no machine in it.
    wrote = d / "vault/inventory/projects.json"
    check("the mirrored document exists where the wiki reads it", wrote.is_file(),
          str(sorted(x.name for x in (d / "vault").rglob("*.json"))))
    if wrote.is_file():
        got = json.loads(wrote.read_text(encoding="utf-8"))
        check("it carries `_generated`", got.get("_generated") is True, str(sorted(got)))
        check("and says where the canonical copy is",
              got.get("_canonical_source") == (d / "registry").as_uri(),
              str(got.get("_canonical_source")))
        check("and tells an editor their edit will be overwritten",
              "overwritten" in (got.get("_do_not_edit") or ""),
              str(got.get("_do_not_edit")))


def test_a_document_the_mirror_cannot_carry_stops_the_run_by_name() -> None:
    """An unreadable or non-object registry document is refused, and named.

    The engine validates every registry document when `paths` is imported, so a
    broken one stops the tool before any mirroring starts rather than leaving a
    hole the report has to describe. Either way the reader must learn WHICH file.
    """
    for name, body in (("broken.json", "{not json"), ("alist.json", "[1,2,3]")):
        d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-mirror-bad-"))
        (d / "registry").mkdir()
        (d / "registry/projects.json").write_text('{"projects": []}', encoding="utf-8")
        (d / "registry" / name).write_text(body, encoding="utf-8")
        p = project_into(d / "vault", registry=d / "registry")
        check(f"{name} stops the run", p.returncode != 0, str(p.returncode))
        check(f"and the refusal names {name}", name in p.stderr, p.stderr[-200:])
        check(f"and nothing half-mirrored is left behind for {name}",
              not any((d / "vault").rglob("*.json")), "")


# ─────────── the commit half reports every refusal ─────────────────────

def test_the_commit_reports_on_every_path() -> None:
    src = (ROOT / "tools/commit_projection.py").read_text(encoding="utf-8")
    check("there is one reporter", "def report(outcome: str" in src)
    for outcome in ("no-git", "status-failed", "clean", "refused-foreign-changes",
                    "lease-held-elsewhere", "committed"):
        check(f"`{outcome}` is reported", f'"{outcome}"' in src or
              f"'{outcome}'" in src, outcome)
    check("a dry run reports NOTHING, deliberately",
          "A dry run reports nothing" in src,
          "answering a question must not overwrite the record of the last real run")
    check("and the report carries the wiki's last commit",
          '"wiki_last_commit"' in src,
          "a reader must tell a refusal that happened once from one standing for days")


def test_a_refusal_is_recorded_rather_than_only_printed() -> None:
    """Driven against a wiki with a foreign change: the refusal is the right
    behaviour, and the report is what makes it visible."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-commitproj-"))
    wiki = d / "vault"
    (wiki / "inventory").mkdir(parents=True)
    (d / "scratch").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wiki, capture_output=True, timeout=120)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=wiki, capture_output=True)
    (wiki / "inventory/projects.json").write_text('{"_generated": true}', encoding="utf-8")
    (wiki / "note.md").write_text("an operator's own note\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wiki, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=wiki, capture_output=True)
    # Now a change OUTSIDE the projection, which must stop the commit.
    (wiki / "note.md").write_text("edited by hand, mid-thought\n", encoding="utf-8")
    (wiki / "inventory/projects.json").write_text('{"_generated": true, "x": 1}',
                                                  encoding="utf-8")
    env = dict(os.environ, OBSERVATORY_VAULT=str(wiki / "projects-vault"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    p = subprocess.run([PY, "tools/commit_projection.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    check("it exits 0, because a dirty wiki is not a failure", p.returncode == 0,
          str(p.returncode))
    f = d / "scratch/commit-projection.json"
    check("and the outcome is recorded", f.is_file(), (p.stdout + p.stderr)[-200:])
    if f.is_file():
        doc = json.loads(f.read_text(encoding="utf-8"))
        check("naming an outcome rather than nothing", bool(doc.get("outcome")),
              str(doc))


# ─────────── the finding needs both conditions ─────────────────────────

def findings_for(doc: dict) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-projfind-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "scratch/commit-projection.json").write_text(json.dumps(doc), encoding="utf-8")
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    try:
        return [f for f in json.loads(p.stdout)["findings"]
                if f["type"] == "projection.uncommitted"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return []


def test_only_a_standing_refusal_is_a_finding() -> None:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=2)).isoformat()
    stale = (now - timedelta(hours=100)).isoformat()
    check("a committed run raises nothing",
          not findings_for({"outcome": "committed", "detail": "ok",
                            "wiki_last_commit": fresh}))
    check("a clean wiki raises nothing",
          not findings_for({"outcome": "clean", "detail": "nothing to commit",
                            "wiki_last_commit": stale}),
          "a clean wiki that has not changed for days is not a fault")
    check("a FRESH refusal raises nothing",
          not findings_for({"outcome": "refused-foreign-changes",
                            "detail": "1 change outside",
                            "wiki_last_commit": fresh}),
          "an operator working in their own wiki is the normal case")
    got = findings_for({"outcome": "refused-foreign-changes",
                        "detail": "1 change outside the projection",
                        "wiki_last_commit": stale})
    check("a STANDING refusal does", len(got) == 1, str(len(got)))
    if got:
        check("counting the hours", "100h" in got[0]["title"], got[0]["title"])
        check("naming the refusal", "refused-foreign-changes" in got[0]["detail"],
              got[0]["detail"][:120])
        check("saying what the wiki is for",
              "wiki-* skills read" in got[0]["detail"], got[0]["detail"][-90:])
        check("and giving both steps of the remedy",
              "by hand" in got[0]["action"] and "commit-projection" in got[0]["action"],
              got[0]["action"])


if __name__ == "__main__":
    print("the wiki's mirror — every document, written atomically, and a refusal that speaks\n")
    for fn in (test_the_projection_is_opt_in,
               test_every_registry_document_is_mirrored,
               test_every_mirrored_file_says_it_is_generated,
               test_the_write_is_atomic,
               test_the_run_reports_what_it_mirrored,
               test_a_document_the_mirror_cannot_carry_stops_the_run_by_name,
               test_the_commit_reports_on_every_path,
               test_a_refusal_is_recorded_rather_than_only_printed,
               test_only_a_standing_refusal_is_a_finding):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe mirror carries every document, and a refusal that stands is visible\033[0m")
