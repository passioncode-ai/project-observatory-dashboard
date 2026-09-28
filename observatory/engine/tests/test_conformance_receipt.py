#!/usr/bin/env python3
"""The conformance evidence, and the two ways it could not be checked.

`tools/run_probes.py` writes a receipt as proof that the published capabilities
behave as declared. In this distribution the receipt lives in the selected,
explicitly synthetic probe workspace (`store/probe-receipts.json`); no historical
receipt ships with the source.

**Gap one: the receipt could not say what it was measured against.** It carried
`manifestContentHash` and nothing else about its subject — not the contract
version, not the pinned contract commit, not the provider revision. So a host
reading it had to take the document's word for which contract those assertions
were measured under, and `--check` compared the manifest hash ALONE, so a
receipt taken against a different contract revision passed silently.

**Gap two: one assertion could not say why it passed.** The declared text is *"a
write with no owner is refused before the ledger is touched"* and the
implementation was `bool(res.is_error)` — true for a misspelled tool name, a
crashed server, or any unrelated failure, and silent about the ledger. It now
requires the refusal to NAME `owner` and the scratch ledger to be unchanged.

**Kept to ONE assertion deliberately.** Splitting it would make the probe
evaluate one more than declared, changing the manifest, its `contentHash` and
the published assertions — a revision bump and a publish the operator owns.

Every probe here runs against a workspace this suite builds: initialized, with
`features.probe_fixture` on, no integrations and no external sources, holding the
fictional projects the published fixtures name (`project:example-app`, its
sibling under the same owner, and an independent project). Nothing outside it is
read or written.
"""
from __future__ import annotations
import json, os, pathlib, subprocess, sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup                 # noqa: E402
portable_setup()
import tmp as tmpdir                                                  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []

#: The project the detail, timeline and admission probes ask about, and two
#: projects under one synthetic owner so name inference has a sibling to find
#: and an independent project it must not confuse with it.
PROJECT = "example-app"
MEMBERSHIPS = {PROJECT: ["fixture/web", "fixture/local-only"],
               "example-sibling": ["example-org/fixture-chat"],
               "fixture-independent": ["example-org/fixture-independent"]}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def build_probe_home() -> pathlib.Path:
    """An initialized synthetic workspace the probe runner will accept."""
    import workspace
    from emitter_fixture import seed
    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe-home-")).resolve() / "ws"
    workspace.initialize(base)
    settings = base / "config/settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc["features"]["probe_fixture"] = True
    settings.write_text(json.dumps(doc), encoding="utf-8")
    # One curated description, because the proposal probe patches a
    # description and a field is appliable only once the curation file has a
    # row that uses it.
    (base / "config/project_overrides.json").write_text(json.dumps(
        {"projects": {PROJECT: {"description": "Synthetic conformance project"}}}),
        encoding="utf-8")
    for name in ("projects", "wiki"):
        (base / name).mkdir()
    # The emitter's synthetic model, rewritten to the published fixture labels
    # and emitted into the workspace's own registry.
    seed_root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe-seed-")).resolve()
    seed(seed_root)
    model = json.loads((seed_root / "raw/model.json").read_text(encoding="utf-8"))
    project = model["projects"]["fixture-a"]
    repository = model["repositories"]["fixture/service"]
    model["projects"] = {key: dict(project, name="Synthetic " + key, repos=repos,
                                   owners=["example-org" if key != PROJECT else "fixture"],
                                   rules=["Synthetic explicit membership: " + r for r in repos])
                         for key, repos in MEMBERSHIPS.items()}
    model["repositories"] = {name: dict(repository, url="https://github.com/" + name)
                             for repos in MEMBERSHIPS.values() for name in repos}
    model["repositories"]["fixture/local-only"].update(
        host="bitbucket", url="https://bitbucket.org/fixture/local-only",
        source="local-remote-only")
    raw = base / "store/raw"
    (raw / "model.json").write_text(json.dumps(model), encoding="utf-8")
    (raw / "bitbucket.json").write_text(json.dumps({"degraded": [
        {"source": "bitbucket:fixture", "reason": "Synthetic unavailable listing"}]}),
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVATORY_")}
    env.update(OBSERVATORY_HOME=str(base), OBSERVATORY_DATA=str(base / "projects"),
               OBSERVATORY_VAULT=str(base / "wiki"),
               OBSERVATORY_VAULT_DIR=str(base / "secrets/projects"))
    p = subprocess.run([PY, "collectors/emit_registry.py", str(raw)], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    if p.returncode:
        raise RuntimeError("probe workspace emit failed: " + (p.stdout + p.stderr)[-300:])
    now = datetime.now(timezone.utc)
    stamps = [(now - timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(3)]
    seed_store = (
        "import sys; sys.path.insert(0, '.')\n"
        "from store import db, ledger\n"
        "c = db.connect()\n"
        "with c:\n"
        "    for i, stamp in enumerate(%r):\n"
        "        c.execute('INSERT INTO events(id,project_id,kind,ref,actor,occurred_at,payload_json)"
        " VALUES (?,?,?,?,?,?,?)', (f'fixture-{i}', 'project:%s', 'commit', f'fixture-{i}',"
        " 'fixture', stamp, '{\"subject\":\"Synthetic work\"}'))\n"
        "ledger.append(c, owner='agent:fixture', statement='Synthetic proposed note',"
        " why='Synthetic evidence', project_id='project:%s', kind='observation')\n"
        "c.close()\n" % (stamps, PROJECT, PROJECT))
    p = subprocess.run([PY, "-c", seed_store], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=300)
    if p.returncode:
        raise RuntimeError("probe workspace store seed failed: " + p.stderr[-300:])
    (base / "registry/findings.json").write_text(json.dumps({"findings": [
        {"id": "finding:fixture", "type": "repo.stale_remote",
         "subject": "repository:fixture/web", "severity": "warning",
         "title": "Synthetic finding", "detail": "Synthetic evidence",
         "action": "Inspect fixture", "evidence": ["SRC-0007"]}]}), encoding="utf-8")
    return base


HOME = build_probe_home()
RECEIPT = HOME / "store/probe-receipts.json"


def probes(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "tools/run_probes.py", "--fixture-home", str(HOME), *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=900)


FIRST = probes()


# ─────────── the receipt names its subject ─────────────────────────────

def test_the_first_run_writes_the_receipt_in_the_workspace() -> None:
    check("the probes pass against the synthetic workspace", FIRST.returncode == 0,
          (FIRST.stdout + FIRST.stderr)[-400:])
    check("and the receipt lands inside that workspace", RECEIPT.is_file(), str(RECEIPT))
    check("never beside the source", not (ROOT / "fabric/probe-receipts.json").exists(),
          "a receipt is a dated artefact of one workspace, not a tracked file")


def test_the_receipt_says_what_it_was_measured_against() -> None:
    doc = json.loads(RECEIPT.read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "fabric-contract.lock.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
    check("the contract version is recorded",
          doc.get("contractVersion") == manifest["contractVersion"],
          f"{doc.get('contractVersion')} vs {manifest['contractVersion']}")
    # The public lock pins a profile and schema release rather than a commit,
    # so the receipt's `contractCommit` mirrors whatever the lock carries.
    check("and the pinned contract commit, as the lock states it",
          "contractCommit" in doc and doc.get("contractCommit") == lock.get("commit"),
          f"{doc.get('contractCommit')} vs {lock.get('commit')}")
    check("and the provider revision",
          doc.get("providerRevision") == manifest["provider"]["revision"],
          f"{doc.get('providerRevision')} vs {manifest['provider']['revision']}")
    check("beside the manifest hash it already had",
          doc.get("manifestContentHash") == manifest["provider"]["contentHash"])
    check("every probe is NAMED, so a reader is not counting positions",
          all(p.get("probe") for p in doc["probes"]),
          str([p.get("probe") for p in doc["probes"]]))
    check("and carries its capability", all(p.get("capability") for p in doc["probes"]))


def test_a_receipt_from_another_contract_is_refused() -> None:
    """Driven against a planted defect, one field at a time: a check that has
    never been watched refusing is a green nobody earned."""
    original = RECEIPT.read_bytes()
    doc = json.loads(original)
    try:
        for field, planted in (("contractVersion", "0.0.9"),
                               ("contractCommit", "deadbeef" * 5),
                               ("providerRevision", 99),
                               ("manifestContentHash", "sha256:planted")):
            RECEIPT.write_text(json.dumps({**doc, field: planted}, indent=2) + "\n",
                               encoding="utf-8")
            p = probes("--check")
            check(f"a receipt with the wrong {field} is refused",
                  p.returncode != 0, f"exit {p.returncode}")
            check(f"…naming {field}", field in p.stderr, p.stderr[-200:])
            check("…and quoting both values",
                  str(planted) in p.stderr, p.stderr[-200:])
    finally:
        RECEIPT.write_bytes(original)
    check("the receipt is restored byte for byte", RECEIPT.read_bytes() == original)
    p = probes("--check")
    # NO GUARD ON THE WORD. A version looked for `RevisionConflict` anywhere in
    # the output and skipped when it found one — and the record probe PRINTS
    # that word in a passing assertion, so the guard fired on every run and
    # this assertion was never made: a condition that matched prose.
    check("and the restored receipt passes", p.returncode == 0,
          (p.stdout + p.stderr)[-300:])


def test_a_drifted_receipt_still_shows_the_run() -> None:
    """The first version raised on drift and printed no verdict at all — a check
    that hides its own measurement to complain about the paperwork."""
    original = RECEIPT.read_bytes()
    doc = json.loads(original)
    try:
        RECEIPT.write_text(json.dumps({**doc, "providerRevision": 99}, indent=2) + "\n",
                           encoding="utf-8")
        p = probes("--check")
        check("the drift is reported", "providerRevision" in p.stderr, p.stderr[-160:])
        check("and every probe verdict is still printed",
              p.stdout.count("assertions evaluated") == len(doc["probes"]),
              f"{p.stdout.count('assertions evaluated')} of {len(doc['probes'])}")
        check("with the exit code still non-zero", p.returncode != 0, str(p.returncode))
    finally:
        RECEIPT.write_bytes(original)


def test_check_writes_nothing() -> None:
    """`--check` repeats the probes and leaves the workspace's inputs and its
    receipt byte-identical; draft-effect probes write into a scratch copy."""
    files = [*(HOME / "registry").rglob("*.json"), HOME / "store/observatory.db", RECEIPT]
    original = {p: p.read_bytes() for p in files if p.is_file()}
    result = probes("--check")
    check("check mode succeeds against the synthetic workspace", result.returncode == 0,
          (result.stdout + result.stderr)[-200:])
    changed = [str(p.relative_to(HOME)) for p, data in original.items()
               if not p.exists() or p.read_bytes() != data]
    check("check mode leaves the registry, the store and the receipt byte-identical",
          not changed, str(changed))


def test_the_runner_refuses_a_workspace_that_is_not_a_probe_fixture() -> None:
    """The feature flag is the guard against pointing draft-effect probes at a
    working installation."""
    import workspace
    other = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe-refuse-")).resolve() / "ws"
    workspace.initialize(other)
    p = subprocess.run([PY, "tools/run_probes.py", "--fixture-home", str(other), "--check"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("an ordinary workspace is refused", p.returncode != 0, str(p.returncode))
    check("before any probe runs", "assertions evaluated" not in p.stdout, p.stdout[-200:])


def test_synthetic_probe_guards() -> None:
    import asyncio
    import importlib
    sys.path.insert(0, str(ROOT / "tools"))
    saved = dict(os.environ)
    import run_probes
    try:
        run_probes.select_fixture_home(HOME)
        importlib.reload(run_probes.paths)
        before = run_probes.registry_fingerprint()
        target = HOME / "registry/projects.json"
        original = target.read_bytes()
        target.write_bytes(original + b"\n")
        check("a write to the selected registry changes the fingerprint",
              run_probes.registry_fingerprint() != before)
        target.write_bytes(original)
        check("restored selected registry has the same fingerprint",
              run_probes.registry_fingerprint() == before)
        cap = next(c for c in run_probes.CAPS if c["name"] == "project.timeline")

        async def timeline():
            async with run_probes.stdio_client(run_probes.params_for(None)) as (read, write):
                async with run_probes.ClientSession(read, write) as session:
                    await session.discover()
                    return await run_probes.call_tool_for(session, cap,
                        run_probes.local_fixture(cap["profile"]["probes"][0]["inputFixture"]))
        data = asyncio.run(timeline())
        schema = run_probes.schema_for(cap)
        check("actual synthetic timeline has three distinct events",
              len(data.get("events", [])) == 3, str(len(data.get("events", []))))
        check("actual wire timeline passes every declared shape assertion",
              all(a["verdict"] == "PASS" for a in run_probes.assess_timeline(data, schema)))
        for defect in ("empty", "snake_case", "string_payload", "oldest_first"):
            broken = json.loads(json.dumps(data))
            if defect == "empty":
                broken["events"] = []
            elif defect == "snake_case":
                for event in broken["events"]:
                    event["occurred_at"] = event.pop("occurredAt")
            elif defect == "string_payload":
                for event in broken["events"]:
                    event["payload"] = json.dumps(event["payload"])
            else:
                broken["events"].reverse()
            check("timeline assessor refuses " + defect,
                  any(a["verdict"] == "FAIL" for a in run_probes.assess_timeline(broken, schema)))
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(run_probes.paths)


# ─────────── the assertion says why it passed ──────────────────────────

def test_the_ownerless_refusal_is_asserted_precisely() -> None:
    src = (ROOT / "tools/run_probes.py").read_text(encoding="utf-8")
    check("the old any-error form is gone",
          'bool(res.is_error), "expected a schema-level rejection"' not in src,
          "true for a misspelled tool name or a crashed server")
    check("the refusal must name the field", '"owner" in text.lower()' in src,
          "otherwise the assertion cannot say WHY it passed")
    check("and the ledger must be unchanged", "after_rows == before_rows" in src,
          "`is_error` says nothing about what was written")
    doc = json.loads(RECEIPT.read_text(encoding="utf-8"))
    rec = next(p for p in doc["probes"] if p["probe"] == "record-proposes-and-refuses")
    a = next(x for x in rec["assertions"] if "no owner" in x["assertion"])
    check("the receipt records the verdict", a["verdict"] == "PASS", str(a))
    check("with what it actually saw", "rejected arguments" in a["note"].lower()
          or "owner" in a["note"].lower(), a["note"][:160])
    check("and the row counts it compared", "rows" in a["note"], a["note"][:160])


def test_the_declared_wording_is_used_verbatim() -> None:
    """The receipt must quote the manifest's sentence, not a paraphrase: a host
    matches assertions by their declared text."""
    manifest = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
    declared = {}
    for cap in manifest["capabilities"]:
        for probe in cap["profile"].get("probes", []):
            declared[probe["id"]] = list(probe.get("assertions", []))
    doc = json.loads(RECEIPT.read_text(encoding="utf-8"))
    for p in doc["probes"]:
        want = declared.get(p["probe"], [])
        got = [a["assertion"] for a in p["assertions"]]
        check(f"{p['probe']} quotes its declared assertions in order", got == want,
              f"{got} vs {want}")
        check(f"{p['probe']} evaluated exactly what it declared",
              p["evaluatedAssertions"] == p["declaredAssertions"] == len(want),
              f"{p['evaluatedAssertions']}/{p['declaredAssertions']} of {len(want)}")


def test_the_published_surface_did_not_change() -> None:
    """The point of keeping it to one assertion. A revision bump and a publish
    are the operator's, and this change earned neither."""
    manifest = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
    check("the provider is still revision 4",
          manifest["provider"]["revision"] == 4,
          str(manifest["provider"]["revision"]))
    # THROUGH THE HASH, not through `git status`: what matters is that the
    # surface still matches the stamp the manifest carries, which is exactly
    # `tools/fabric_hash.py --check` — delegated to here rather than
    # reimplemented.
    p = subprocess.run([PY, "tools/fabric_hash.py", "--check"], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    check("and the surface still matches the manifest's own contentHash",
          p.returncode == 0, (p.stdout + p.stderr)[-200:])
    check("which is the invariant a revision bump exists to record",
          "contentHash OK" in p.stdout, p.stdout[-120:])


# ─────────── the conformance document stays true ───────────────────────

def test_the_conformance_document_matches_the_distribution() -> None:
    """The private original checked the document's quoted pass count and dates
    against a tracked receipt. This distribution ships no receipt, so the
    document must say where one is produced and that none is included."""
    md = (ROOT / "fabric/FABRIC-CONFORMANCE.md").read_text(encoding="utf-8")
    check("the document names the runner and its synthetic workspace",
          "tools/run_probes.py --fixture-home" in md, "")
    check("and where the receipt is stored",
          "store/probe-receipts.json" in md and RECEIPT.name in md, "")
    check("and that no historical receipt is included",
          "No historical" in md and "receipts" in md, "")
    check("and that check mode does not rewrite it",
          "without rewriting" in md, "")


if __name__ == "__main__":
    print("the conformance receipt — what it was measured against, and why it passed\n")
    for fn in (test_the_first_run_writes_the_receipt_in_the_workspace,
               test_the_receipt_says_what_it_was_measured_against,
               test_a_receipt_from_another_contract_is_refused,
               test_a_drifted_receipt_still_shows_the_run,
               test_check_writes_nothing,
               test_the_runner_refuses_a_workspace_that_is_not_a_probe_fixture,
               test_synthetic_probe_guards,
               test_the_ownerless_refusal_is_asserted_precisely,
               test_the_declared_wording_is_used_verbatim,
               test_the_published_surface_did_not_change,
               test_the_conformance_document_matches_the_distribution):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe evidence now names its subject, and the assertion names its reason\033[0m")
