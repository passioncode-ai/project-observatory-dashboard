#!/usr/bin/env python3
"""Planted fixtures for the traps this repository was actually burned by.

Every case here corresponds to a numbered trap — a recorded failure — and the
marker in each docstring is what `tools/trap_map.py` and
`tools/trap_efficacy.py` attribute guards by. The registry of those failures is
not part of this distribution, so each guard carries its story in its own
docstring.

The registry-wide invariants run over the synthetic estate the portable runner
builds; every trap that depends on particular estate content is driven against
its own frozen fixture (`emitter_fixture`, `merge_fixture`) instead of against
names that exist only on one machine.

    python3 tests/test_traps.py
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
import paths  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def registry(name: str):
    return json.loads((paths.REGISTRY / name).read_text(encoding="utf-8"))


def test_t1_no_org_adoption() -> None:
    """T1 — a project must not hold repositories of an owner it merely mentions.

    Trap: T1
    """
    projects = registry("projects.json")["projects"]
    rel = registry("relations.json")["relations"]
    members: dict[str, list[str]] = {}
    for r in rel:
        if r["type"] == "implemented_by":
            members.setdefault(r["from"], []).append(r["to"])
    worst = max(projects, key=lambda p: len(members.get(p["id"], [])))
    n = len(members.get(worst["id"], []))
    # A project whose anchor IS the organisation holds its repositories by a
    # stated rule, not an inference, so it is exempt from the bound.
    check("T1 no project absorbs an owner's repositories by mention",
          worst["anchor"] == "organisation" or n <= 8,
          f"{worst['name']} holds {n} repositories with anchor {worst['anchor']}")
    # The invariant is per-repository, not a count: a project with no repository
    # still carries a rule saying why (e.g. "not a git repository"), so comparing
    # lengths asserts something the data never promised.
    unruled = []
    for p in projects:
        rules = " \n".join(p.get("membership_rules", []))
        for rid in members.get(p["id"], []):
            nwo = rid.split(":", 1)[1]
            if nwo not in rules:
                unruled.append((p["id"], nwo))
    check("T1 every repository link names itself in a rule", not unruled,
          f"{len(unruled)} unruled, e.g. {unruled[:3]}")


def test_t2_t3_sites() -> None:
    """T2/T3 — a site needs overview or non-fork declaration evidence.

    Trap: T2, T3
    """
    repos = {r["id"]: r for r in registry("repositories.json")["repositories"]}
    bad_ev, fork_sites = [], []
    for p in registry("projects.json")["projects"]:
        for s in p.get("sites", []):
            if not s.get("evidence"):
                bad_ev.append(p["id"])
            for e in s["evidence"]:
                if e.startswith("github:") and e.endswith(":homepageUrl"):
                    rid = "repository:" + e[len("github:"):-len(":homepageUrl")]
                    if repos.get(rid, {}).get("fork"):
                        fork_sites.append((p["id"], s["host"]))
    check("T2 every site carries evidence", not bad_ev, f"{bad_ev[:3]}")
    check("T3 no site is claimed from a fork's homepage", not fork_sites, f"{fork_sites[:3]}")


def test_t4_t5_emit_is_idempotent() -> None:
    """T4/T5 — a second emit from the same measurement changes nothing.

    Trap: T4, T5
    """
    # FROZEN INPUTS AND A SCRATCH OUTPUT, because otherwise this measures the
    # stability of the machine rather than the idempotence of `emit`. The first
    # version read the LIVE registry, ran `emit` against it, and re-read — so
    # the scheduled tick, which rewrites `store/raw/*.json` and commits the
    # registry every thirty minutes, could change the INPUT between the two
    # readings and this check would report `changed: ['projects.json']`,
    # accusing `emit` of non-determinism while two consecutive emits compared by
    # hand were byte-identical. A check that reports the wrong cause is a check
    # people argue with.
    #
    # It also stopped writing the operator's real registry. That it ever passed
    # the gate's `registry/` write ban relied on the property under test.
    import shutil as _sh
    import os as _os
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-t4-"))
    from emitter_fixture import seed
    env = seed(d)

    def _emit():
        return subprocess.run([sys.executable, "collectors/emit_registry.py",
                               str(d / "raw")], cwd=ROOT, env=env,
                              capture_output=True, text=True)

    live_before = {n: (paths.REGISTRY / n).read_text(encoding="utf-8")
                   for n in ("projects.json", "repositories.json", "relations.json")}
    first = _emit()
    if first.returncode != 0:
        check("T4/T5 emit runs against a frozen copy", False,
              first.stderr.strip()[:200])
        return
    before = {n: (d / "registry" / n).read_text(encoding="utf-8") for n in live_before}
    edges_before = len(json.loads((d / "registry/relations.json").read_text(
        encoding="utf-8"))["relations"])
    second = _emit()
    if second.returncode != 0:
        check("T4/T5 emit re-runs", False, second.stderr.strip()[:200])
        return
    after = {n: (d / "registry" / n).read_text(encoding="utf-8") for n in before}
    changed = [n for n in before if before[n] != after[n]]
    edges_after = len(json.loads((d / "registry/relations.json").read_text(
        encoding="utf-8"))["relations"])
    check("T5 a second emit does not duplicate edges", edges_before == edges_after,
          f"{edges_before} -> {edges_after}")
    check("T4 a second emit is byte-identical", not changed, f"changed: {changed}")
    check("and the live registry was not written at all",
          all(live_before[n] == (paths.REGISTRY / n).read_text(encoding="utf-8")
              for n in live_before),
          "a test that writes the operator's registry passes only while the "
          "property it tests holds")


def test_t7_owner_is_required() -> None:
    """T7 — the ledger must reject a write with no declared owner.

    Trap: T7
    """
    db = sqlite3.connect(":memory:")
    # The schema ships with the program; `paths.STORE` is the workspace's store.
    db.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    row = ("m1", 1, "note", None, None, None, None, "episodic", "project",
           "a statement", None, "proposed", 0.5)
    try:
        db.execute(
            "INSERT INTO ledger (memory_id,revision,kind,project_id,agent_id,run_id,session_id,"
            "function,scope,statement,why,state,confidence,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'2026-01-01T00:00:00Z')", row)
        check("T7 an unnamed writer is rejected", False, "the insert succeeded")
    except sqlite3.IntegrityError as exc:
        check("T7 an unnamed writer is rejected", "owner" in str(exc), str(exc))


def test_t9_embedding_contract_matches_estate() -> None:
    """T9 — the store's pinned contract must equal the one the provider embeds with.

    Trap: T9

    Historically the contract was pinned by another project on the same
    machine. The engine ships its own: the embedding block of the default model
    configuration is what `agent/providers.py` embeds with, and `vec_meta` in a
    fresh store is what the indexer refuses a mismatch against. Two numbers that
    must agree, in two files — so they are compared.
    """
    want = json.loads((ROOT / "defaults/models.json").read_text(encoding="utf-8"))["embedding"]
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    provider, model, dims = db.execute(
        "SELECT embedding_provider, embedding_model, dims FROM vec_meta").fetchone()
    check("T9 embedding contract matches the shipped provider configuration",
          (provider, model, dims) == (want.get("provider"), want.get("model"), want.get("dims")),
          f"store={provider}/{model}/{dims} config={want.get('provider')}/{want.get('model')}/{want.get('dims')}")


def test_t11_curated_values_survive_rebuild() -> None:
    """T11 — a curated field must come from its own file, not the artefact.

    Trap: T11

    Driven on frozen inputs rather than read off whatever overrides an estate
    happens to carry: a curated description is written for a real project id,
    the registry is rebuilt from the model, and the value must be there — and
    must be marked as curated so a reader can tell it from a measurement.
    """
    import merge_fixture
    plain = merged()
    if plain is None:
        return
    target = sorted(p["id"] for p in plain[1])[0]
    value = "a curated sentence the model never measured"

    def curate(root):
        (merge_fixture.curation(root) / "project_overrides.json").write_text(json.dumps(
            {"projects": {target: {"description": value, "why": "Synthetic curation"}}}),
            encoding="utf-8")

    got = merged(curate)
    if got is None:
        return
    row = next((p for p in got[1] if p["id"] == target), {})
    check("T11 curated values survive a rebuild", row.get("description") == value,
          str(row.get("description")))
    check("T11 and say they are curated", "description" in (row.get("curated_fields") or []),
          str(row.get("curated_fields")))
    check("T11 the provenance note is not copied into the registry", "why" not in row,
          "`why` belongs to the override, not to the project")


# Three traps live in their own suites because they need a database, a plugin
# tree or an index rather than the registry. This table said "not built" for all
# three long after all three were built — a hand-written status is an assertion,
# and this one advertised gaps that were closed. It now names WHERE each is
# covered, and `test_the_pointers_resolve` fails if a pointer stops resolving,
# so the table cannot quietly go stale again.
COVERED_ELSEWHERE = {
    "T6  retention tombstones proposed rows":
        ("tests/test_retention.py", "tombstone"),
    "T8  the plugin declares no SessionStart hook":
        # T8 guards a BOUNDED SessionStart now, not an absent one.
        ("tests/test_skill.py", "T8 the plugin declares SessionStart and Stop, and nothing else"),
    "T10 conflicting supported records return together":
        ("tests/test_index.py", "def test_search_returns_conflicts_together"),
}


def test_the_pointers_resolve() -> None:
    """A cross-reference nobody checks is how the table above started lying."""
    for trap, (rel, marker) in COVERED_ELSEWHERE.items():
        f = ROOT / rel
        if not f.is_file():
            # The runner copies only registered suites, so a pointer into a
            # suite this tree does not carry is named rather than failed.
            print(f"  SKIP  {trap.split()[0]} pointer — {rel} is not in this tree")
            continue
        check(f"{trap.split()[0]} is covered by {rel}",
              f.is_file() and marker in f.read_text(encoding="utf-8"),
              f"{rel} missing or no longer contains {marker!r}")

def test_t26_a_deletion_prunes_its_edges() -> None:
    """No relation may point at an endpoint the registry does not hold.

    Trap: T26
    """
    # FOUR NAMESPACES, and the fourth arrived with the Heroku subsystem. It was
    # missing for a day: `deployed_to` edges point at
    # `heroku:<app>`, the emitter and the validator both learned the namespace
    # and this trap did not, so forty legitimate edges read as dangling. A trap
    # that cries wolf on correct data is worse than a missing one — it teaches
    # the reader to ignore the number.
    # FIVE NAMESPACES NOW, and the fifth — `credential:` — arrived with the
    # credentials projection and broke this trap exactly as the
    # fourth did: three legitimate `used_by` edges read as dangling. Twice is a
    # class, not an incident, so the sources are DECLARED and the check below
    # refuses a sixth namespace that this table does not know about.
    SOURCES = {"projects.json": ("projects", None),
               "repositories.json": ("repositories", None),
               "domains.json": ("domains", "domain:"),
               "heroku-apps.json": ("apps", None),
               "credentials.json": ("credentials", None),
               "products.json": ("products", None),
               "cloudflare-zones.json": ("zones", None),
               "mcp-servers.json": ("servers", None)}
    ids: set[str] = set()
    for fname, (key, prefix) in SOURCES.items():
        if not (paths.REGISTRY / fname).is_file():
            continue                          # a projection this estate has not grown
        for row in registry(fname)[key]:
            ids.add((prefix + row["name"]) if prefix else row["id"])
    # THE SIXTH NAMESPACE, caught as a NAMED omission rather than as a false
    # dangling report. Written first as "any registry file with id-bearing
    # rows", it failed on findings.json — findings carry ids and are never
    # relation endpoints. The precise rule is the one the trap actually needs:
    # every namespace that APPEARS in a relation must be resolvable from
    # SOURCES.
    used = {s.split(":", 1)[0]
            for r in registry("relations.json")["relations"]
            for s in (r["from"], r["to"]) if ":" in s}
    covered = {i.split(":", 1)[0] for i in ids}
    check("T26 resolves every namespace the relations actually use",
          not (used - covered),
          f"undeclared namespace(s): {sorted(used - covered)} — add the "
          f"projection to SOURCES above, or the edges read as dangling")
    dangling = [(r["id"], s) for r in registry("relations.json")["relations"]
                for s in (r["from"], r["to"]) if s not in ids]
    check("T26 every relation endpoint resolves", not dangling, str(dangling[:3]))
    src = (ROOT / "collectors/emit_registry.py").read_text(encoding="utf-8")
    check("T26 the emitter prunes rather than leaving it to the validator",
          "no longer exists" in src and "endpoints" in src)


def merged(adjust=None):
    """Run merge + emit over `merge_fixture`'s frozen inputs.

    `adjust(root)` may edit the seeded curation before the merge reads it.
    Returns (root, projects, repositories, relations, merge stdout), or None
    when a step failed — the failure is then already reported as a check.
    """
    import merge_fixture
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-merge-")).resolve()
    env = merge_fixture.seed(root)
    if adjust:
        adjust(root)
    out = ""
    for script in ("merge.py", "emit_registry.py"):
        result = subprocess.run([sys.executable, str(ROOT / "collectors" / script), str(root / "raw")],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        out += result.stdout
        if result.returncode:
            check(f"{script} runs on the frozen inputs", False, result.stderr[-300:])
            return None
    reg = root / "registry"

    def load(name, key):
        return json.loads((reg / name).read_text(encoding="utf-8"))[key]
    return (root, load("projects.json", "projects"), load("repositories.json", "repositories"),
            load("relations.json", "relations"), out)


def test_t27_a_transfer_is_followed_not_duplicated() -> None:
    """A redirected remote must not become a second repository.

    Trap: T27

    Driven over two synthetic transfers: the local checkouts still point at the
    old addresses, the listing returns only the new ones, and the stubbed `gh`
    answers the redirect. Recording the OLD address is the phantom.
    """
    import merge_fixture
    got = merged()
    if got is not None:
        repos = {r["name_with_owner"] for r in got[2]}
        for old, new in merge_fixture.TRANSFERS.items():
            check(f"T27 {old} is not recorded as its own repository", old not in repos,
                  str(sorted(repos)))
            check(f"T27 {new} is", new in repos, str(sorted(repos)))
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    check("T27 the merge resolves a name the listing did not return",
          "def canonical(" in src and "full_name" in src)
    check("T27 and reports the transfer rather than silently rewriting it",
          "followed" in src and "transfer" in src)


def test_t28_the_link_checker_does_not_cry_wolf() -> None:
    """Three false-positive classes, each asserted against a crafted input.

    Trap: T28
    """
    import importlib.util, tempfile
    spec = importlib.util.spec_from_file_location("aud", ROOT / "tools/audit_vault_links.py")
    aud = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aud)
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-links-"))
    (root / "real.md").write_text("x", encoding="utf-8")
    # resolve() receives what the LINK regex captured as the PATH, not the whole
    # link body — the label is already split off by then.
    cases = {
        "real#Some Section": True,      # an anchor is not part of the path
        "real\\": True,                 # the escaped pipe left its backslash behind
        "real": True,
        "nope": False,
    }
    for target, want in cases.items():
        ok, why = aud.resolve(root, target)
        check(f"T28 path {target!r} resolves={want}", ok == want, f"got {ok}: {why}")
    # and end to end: the regex must hand resolve() the right path
    m = aud.LINK.search("see [[real\\|a label]] here")
    check("T28 an escaped pipe yields the path, not the label",
          m and aud.resolve(root, m.group(1))[0], m.group(1) if m else "no match")
    (root / "prose.md").write_text("use `[[wikilinks]]` in full-path form\n", encoding="utf-8")
    text = (root / "prose.md").read_text(encoding="utf-8")
    blanked = aud.CODE.sub(lambda m: " " * len(m.group(0)), text)
    check("T28 a wikilink inside backticks is not read as a link",
          not aud.LINK.search(blanked), blanked)


def test_t29_a_mention_is_not_ownership() -> None:
    """The third form of one mistake, and the operator caught this one.

    Trap: T29
    """
    # DRIVEN, not grepped. An earlier form asserted that the source contained
    # the name of the WIDE path set the rule rejected — which proved a string
    # existed and kept a dead field alive for it. A test that reads the source
    # proves nothing about the behaviour (trap T21), so the rule is exercised: a
    # path in the note's opening counts, the same path only in its body does not.
    sys.path.insert(0, str(ROOT / "collectors"))
    import importlib.util as _il
    _spec = _il.spec_from_file_location("scan_vault_probe", ROOT / "collectors/scan_vault.py")
    _mod = _il.module_from_spec(_spec)
    _mod.__dict__["__name__"] = "scan_vault_probe"
    try:
        _spec.loader.exec_module(_mod)
    except SystemExit:
        pass
    note = ("---\ntitle: t\n---\n"
            "It lives at DATA/counts-me and uses the same stack.\n\n"
            "## Notes\n\nA sibling at DATA/mentioned-only shares that stack.\n")
    opening = _mod.opening(note)
    found = _mod.DATA_RX.findall(opening)
    check("T29 a DATA/ path in the opening definition counts",
          "counts-me" in found, str(found))
    check("T29 the same shape mentioned after the first heading does NOT",
          "mentioned-only" not in found, str(found))

    # The operator's denial is CURATED, so it survives a re-merge: the same
    # frozen inputs are merged with and without a denial of a link the rules
    # would otherwise make — and the rule must still fire where it is right.
    import merge_fixture
    link = f"{merge_fixture.OWNER}/fixture-parent"

    def holds(relations):
        out = {}
        for r in relations:
            if r["type"] == "implemented_by":
                out.setdefault(r["from"], set()).add(r["to"].split(":", 1)[1])
        return out

    plain = merged()
    if plain is None:
        return
    before = holds(plain[3])
    owner = next((pid for pid, rs in before.items() if link in rs), None)
    check("T29 the rule still links a folder to its own repository", owner is not None,
          str(before))
    if owner is None:
        return

    def deny(root):
        (merge_fixture.curation(root) / "denied_links.json").write_text(json.dumps(
            {"denied": [{"project": "fixture-parent", "repo": link,
                         "why": "Synthetic operator denial"}]}), encoding="utf-8")

    denied = merged(deny)
    if denied is None:
        return
    after = holds(denied[3])
    check("T29 the operator's denial removes that link on the next merge",
          link not in after.get(owner, set()), str(after.get(owner)))
    check("T29 and the merge says it was denied rather than dropping it silently",
          "denied by the operator" in denied[4], denied[4][-200:])


def test_t30_a_derived_relation_is_rebuilt_not_accumulated() -> None:
    """A link the model stopped making must not survive because it was written once.

    Trap: T30
    """
    # THE BEHAVIOUR, not the name of the constant that implements it. The
    # first two assertions here were once `"DERIVED_TYPES" in src` and
    # `"authored" in src` — and `tools/trap_efficacy.py` set `DERIVED_TYPES` to
    # the empty set, which is the whole defect, and watched this guard pass. A
    # test that greps the source proves nothing about the data: trap T21, in
    # this file, two hundred lines above, committed inside a trap's own guard.
    #
    # So both halves are driven against a frozen copy: an edge of a DERIVED type
    # planted by hand must be gone after one emit, because the emitter rebuilds
    # those from the model; an edge of an AUTHORED type planted the same way
    # must survive, because nothing else can re-derive it.
    import os as _os
    import shutil as _sh
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-t30-"))
    from emitter_fixture import seed
    env = seed(d)
    rels = d / "registry/relations.json"
    doc = json.loads(rels.read_text(encoding="utf-8"))
    doc["relations"] += [
        {"id": "relation:planted:derived", "type": "implemented_by",
         # A RESOLVABLE endpoint, deliberately: an edge pointing at nothing is
         # pruned by T26's rule instead, and the first version of this fixture
         # measured that mechanism while believing it measured this one.
         "from": "project:fixture-a", "to": "repository:fixture/service",
         "source_refs": []},
        {"id": "relation:planted:authored", "type": "consumes_knowledge_from",
         "from": "project:fixture-a", "to": "project:fixture-b",
         "source_refs": []}]
    rels.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    emitted = subprocess.run([sys.executable, "collectors/emit_registry.py", str(d / "raw")],
                             cwd=ROOT, capture_output=True, text=True,
                             env=env)
    if emitted.returncode != 0:
        check("T30 the emitter runs against a frozen copy", False,
              emitted.stderr.strip()[:200])
    else:
        after = {r["id"] for r in json.loads(rels.read_text(encoding="utf-8"))["relations"]}
        check("T30 a derived edge whose reason vanished does NOT survive the rebuild",
              "relation:planted:derived" not in after,
              "the emitter carried it forward, which is how a corrected link comes back")
        check("T30 a hand-authored edge DOES survive it",
              "relation:planted:authored" in after,
              "nothing can re-derive an authored edge, so losing it loses the fact")
    rel = registry("relations.json")["relations"]
    projects = {p["id"] for p in registry("projects.json")["projects"]}
    repos = {r["id"] for r in registry("repositories.json")["repositories"]}
    # Every derived relation must be justified by a membership rule naming it.
    rules = {p["id"]: " \n".join(p.get("membership_rules", []))
             for p in registry("projects.json")["projects"]}
    unjustified = [r["id"] for r in rel if r["type"] == "implemented_by"
                   and r["to"].split(":", 1)[1] not in rules.get(r["from"], "")]
    check("T30 every implemented_by edge is justified by a stated rule",
          not unjustified, f"{len(unjustified)}: {unjustified[:3]}")


def test_t31_a_folder_on_disk_is_never_silently_absent():
    """A remoteless Git checkout and a plain folder survive scan/merge/emit.
    Trap: T31

    The original defect skipped every Git folder from the local-only pass.
    Controlled disk inputs make this independent of other sessions creating
    checkouts between a live scan and the gate. The mutation instrument restores
    that exact source defect and drives this guard.
    """
    import merge_fixture
    root = pathlib.Path(tmpdir.mkdtemp(prefix='observatory-t31-'))
    env = merge_fixture.seed(root)
    data = merge_fixture.git_estate(root)
    (data/'fixture-plain').mkdir()
    for script, argument in [('scan_filesystem.py', str(root/'raw/local.json')),
                             ('merge.py', str(root/'raw')),
                             ('emit_registry.py', str(root/'raw'))]:
        scan_env = {**env, 'PATH': os.environ.get('PATH', '')} if script=='scan_filesystem.py' else env
        result = subprocess.run([sys.executable, str(ROOT/'collectors'/script), argument],
                                cwd=ROOT, env=scan_env, capture_output=True, text=True, timeout=120)
        check('T31 ' + script + ' runs on its own inputs', result.returncode == 0, result.stderr[-300:])
        if result.returncode:
            return
    projects = json.loads((root/'registry/projects.json').read_text())['projects']
    local = {p['local_only']['folder']:p for p in projects if p.get('local_only')}
    check('T31 both observed local folders reach the registry',
          {'fixture-local','fixture-plain'} <= set(local), str(sorted(local)))
    check('T31 the excluded container remains absent', 'fixture-excluded' not in local)
    unpublished = local.get('fixture-local', {})
    check('T31 a remote-less git folder says so in its rule',
          unpublished.get('local_only',{}).get('unpublished') is True and
          any('no remote' in rule for rule in unpublished.get('membership_rules', [])))


def test_t32_findings_are_rebuilt_and_acknowledgements_are_not():
    """A finding disappears when its cause does; a dismissal does not.
    Trap: T32

    Two halves of one rule, and they pull in opposite directions. `findings.json`
    is DERIVED, so it must be rebuilt — a finding that outlived its cause is a
    false alarm, and the emitter's DERIVED_TYPES exist for the same reason.
    `finding_acks.json` is CURATED, so it must survive the rebuild — a dismissal
    kept inside a generated file is lost the first time it is generated (T11),
    and an operator who silences a finding twice stops silencing it at all.
    """
    import shutil, subprocess, tempfile
    root = ROOT
    # The fixture makes its own state. This once drove the REAL registry: it planted a row in `registry/findings.json`, rebuilt it in
    # place and left `built_at` rewritten, so the gate that ran it could never
    # leave a clean tree — and the test depended on whatever the tree happened to
    # hold, which is exactly what trap T16 forbids two hundred lines above.
    sandbox = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-t32-"))
    shutil.copytree(paths.REGISTRY, sandbox / "registry")
    if paths.FINDING_ACKS.is_file():
        shutil.copy(paths.FINDING_ACKS, sandbox / "acks.json")
    else:
        (sandbox / "acks.json").write_text('{"acks": []}', encoding="utf-8")
    env = {**os.environ,
           "OBSERVATORY_REGISTRY": str(sandbox / "registry"),
           "OBSERVATORY_ACKS": str(sandbox / "acks.json")}
    REG = sandbox / "registry"
    ACKS_FILE = sandbox / "acks.json"

    def rebuild():
        return subprocess.run([sys.executable, str(root / "tools" / "build_findings.py")],
                              cwd=root, capture_output=True, env=env)

    doc = json.loads((REG / "findings.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in doc["findings"]}
    check("T32 every finding id is unique", len(ids) == len(doc["findings"]),
          f"{len(doc['findings'])} findings, {len(ids)} ids")
    check("T32 the note says the file is rebuilt",
          "REBUILT" in doc.get("note", "") or "rebuilt" in doc.get("note", ""))

    # Plant a finding that no measurement supports, rebuild, and it must be gone.
    planted = dict(doc["findings"][0], id="planted.trap:subject", type="planted.trap",
                   subject="subject", title="a finding nothing measures")
    tampered = dict(doc, findings=doc["findings"] + [planted])
    (REG / "findings.json").write_text(
        json.dumps(tampered, ensure_ascii=False, indent=1), encoding="utf-8")
    rebuild()
    after = {f["id"] for f in json.loads(
        (REG / "findings.json").read_text(encoding="utf-8"))["findings"]}
    check("T32 a finding with no cause does not survive a rebuild",
          "planted.trap:subject" not in after)

    # An acknowledgement is outside the rebuild, so it must still apply after one.
    target = sorted(after)[0]
    ACKS_FILE.write_text(json.dumps({"note": "trap", "acks": [
        {"id": target, "until": "2099-01-01", "why": "planted by T32", "by": "test"}]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    rebuild()
    rebuilt = json.loads((REG / "findings.json").read_text(encoding="utf-8"))
    acked = [f for f in rebuilt["findings"] if f["id"] == target and f.get("acked")]
    check("T32 an acknowledgement survives the rebuild", bool(acked),
          f"{target} came back unacknowledged")
    shutil.rmtree(sandbox, ignore_errors=True)
    check("T32 the real registry was never touched",
          not (paths.REGISTRY / "findings.json").read_text(encoding="utf-8")
          .count('"planted.trap:subject"'))


def test_t33_an_inactive_repository_is_recorded_but_anchors_nothing():
    """Retiring an address must remove its ROW, not recolour it.
    Trap: T33

    An empty repository that was never pushed to had been given a project of
    its own by the standalone-repository rule — an empty address sitting in the
    project list beside real work, indistinguishable at a glance. A status that
    only changes how a row is drawn leaves the row.

    Both halves are asserted, because each without the other is a defect: drop
    the repository too and the registry forgets an address that still exists and
    that a reader may follow; keep the project and nothing was retired. Driven on
    frozen inputs: the same listing merged with and without the retirement.
    """
    import merge_fixture
    nwo = f"{merge_fixture.OWNER}/fixture-module-a"
    rid = f"repository:{nwo}"

    def anchored(projects):
        return [p["id"] for p in projects
                if p.get("anchor") == "repository" and nwo in json.dumps(p)]

    plain = merged()
    if plain is None:
        return
    check("T33 without a retirement the standalone rule anchors a project",
          bool(anchored(plain[1])), "the fixture no longer exercises the rule")

    def retire(root):
        (merge_fixture.curation(root) / "repo_status.json").write_text(json.dumps(
            {"repositories": {nwo: {"status": "inactive", "evidence": ["fixture:empty-repository"],
                                    "measured_on": "2026-01-01"}}}),
            encoding="utf-8")

    got = merged(retire)
    if got is None:
        return
    _, projects, repositories, rel, _ = got
    check(f"T33 {nwo} is still recorded", rid in {r["id"] for r in repositories},
          "retiring an address must not erase it")
    check(f"T33 {nwo} anchors no project of its own", not anchored(projects),
          str(anchored(projects)))
    edges = [e["from"] for e in rel if e["to"] == rid]
    check(f"T33 nothing claims {nwo}", not edges, str(edges))


def test_t34_the_vector_index_does_not_partition_per_memory():
    """428 MB of pre-allocation for 553 KB of vectors, and it cost the store.
    Trap: T34

    `vec0` reserves a chunk of 1024 vectors per PARTITION. Declaring
    `memory_id TEXT PARTITION KEY` therefore gave every single memory its own
    1024 × 1536 × 4 bytes — 6.3 MB apiece for the one or two revisions it holds.
    Measured once: 68 memories, 90 vectors, and `vec_notes_vector_chunks00`
    occupying 428,380,160 bytes of a 434 MB file.

    It bought nothing — nothing searches within one memory — and it cost more
    than space: the store was found corrupt mid-write on a nearly full volume,
    and the write that must succeed is the one extending a 428 MB allocation.
    """
    src = (ROOT / "store" / "indexer.py").read_text(encoding="utf-8")
    check("T34 the vector table declares no PARTITION KEY",
          "PARTITION KEY" not in src,
          "a partition per memory pre-allocates 1024 vectors for each")

    db = paths.DB
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        total = conn.execute("SELECT sum(pgsize) FROM dbstat").fetchone()[0] or 0
        vec = conn.execute(
            "SELECT coalesce(sum(pgsize), 0) FROM dbstat "
            "WHERE name LIKE 'vec_notes_vector_chunks%'").fetchone()[0] or 0
        rows = conn.execute("SELECT count(*) FROM ledger").fetchone()[0] or 1
    except sqlite3.Error:
        return                     # dbstat is a compile-time option, not a given
    finally:
        conn.close()
    # A megabyte of chunk per ledger revision is the shape of the defect, and
    # well clear of the ~70 KB one chunk shared by everything works out to.
    check("T34 vector storage is not per-memory-sized",
          vec / rows < 1_000_000,
          f"{vec:,} bytes of vector chunk for {rows} revisions")
    check("T34 the store is not dominated by empty pre-allocation",
          total == 0 or vec / total < 0.9,
          f"vector chunks are {vec/max(total,1):.0%} of the store")


def test_t35_the_ledger_exists_outside_the_store():
    """The one table nothing can rebuild, and the sentence that said it was safe.
    Trap: T35

    `store/db.py` claimed the ledger "is exported to the wiki". Nothing exported
    it. The store was once found corrupt with the only copy of every conclusion
    the agent had drawn inside it; it returned because `.recover` happened to
    find the pages intact. A sentence promising a backup that does not exist is
    worse than silence — it is what stops anyone from adding one.

    Driven on a sandbox store: one record is written and exported, and the
    export must exist in the REGISTRY directory, carry the record, and audit as
    consistent. The historical checkout also asserted the export was tracked by
    Git; a workspace is not a Git checkout, so what is asserted instead is that
    the export lives outside the store directory it backs up.
    """
    sandbox = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-t35-")).resolve()
    (sandbox / "registry").mkdir()
    (sandbox / "store").mkdir()
    env = {**os.environ, "OBSERVATORY_DB": str(sandbox / "store/observatory.db"),
           "OBSERVATORY_REGISTRY": str(sandbox / "registry")}
    for name in ("projects.json", "repositories.json", "relations.json"):
        (sandbox / "registry" / name).write_text(
            (paths.REGISTRY / name).read_text(encoding="utf-8"), encoding="utf-8")
    write = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, '.')\n"
                            "from store import db, ledger\n"
                            "c = db.connect()\n"
                            "ledger.append(c, owner='agent:t35', statement='a synthetic conclusion')\n"
                            "c.close()"], cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=120)
    check("T35 a record is written to the sandbox store", write.returncode == 0, write.stderr[-300:])
    exported = subprocess.run([sys.executable, "tools/export_ledger.py"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
    check("T35 the export runs", exported.returncode == 0, exported.stderr[-300:])
    export = sandbox / "registry" / "ledger.jsonl"
    check("T35 the ledger is exported outside the store", export.is_file(),
          "registry/ledger.jsonl is missing — run tools/export_ledger.py")
    if not export.is_file():
        return
    lines = [l for l in export.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("T35 the export carries records, not just its header", len(lines) > 1,
          f"{len(lines)} line(s)")
    check("T35 the export is not inside the store directory it backs up",
          (sandbox / "store") not in export.parents, str(export))
    # ONE implementation of the property, two callers: the gate's own audit,
    # which forgives an export seconds behind a live writer and refuses one that
    # disagrees with the store.
    audit = subprocess.run([sys.executable, "tools/export_ledger.py", "--check"], cwd=ROOT,
                           env=env, capture_output=True, text=True, timeout=120)
    check("T35 the export is consistent with the store and complete beyond the grace",
          audit.returncode == 0, (audit.stdout + audit.stderr)[-300:])


if __name__ == "__main__":
    print("planted fixtures — the recorded traps\n")
    for fn in (test_t1_no_org_adoption, test_t2_t3_sites, test_t4_t5_emit_is_idempotent,
               test_t7_owner_is_required, test_t9_embedding_contract_matches_estate,
               test_t11_curated_values_survive_rebuild, test_t26_a_deletion_prunes_its_edges,
               test_t27_a_transfer_is_followed_not_duplicated,
               test_t28_the_link_checker_does_not_cry_wolf,
               test_t29_a_mention_is_not_ownership,
               test_t30_a_derived_relation_is_rebuilt_not_accumulated,
               test_t31_a_folder_on_disk_is_never_silently_absent,
               test_t32_findings_are_rebuilt_and_acknowledgements_are_not,
               test_t33_an_inactive_repository_is_recorded_but_anchors_nothing,
               test_t34_the_vector_index_does_not_partition_per_memory,
               test_t35_the_ledger_exists_outside_the_store,
               test_the_pointers_resolve):
        fn()
    print("\ncovered in their own suites, verified above:")
    for name, (rel, _) in COVERED_ELSEWHERE.items():
        print(f"  ->    {name} — {rel}")
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mall runnable fixtures pass\033[0m")
