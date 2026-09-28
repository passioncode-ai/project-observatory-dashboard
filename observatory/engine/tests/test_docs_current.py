#!/usr/bin/env python3
""""The docs are in sync" as an exit code, and every rule driven against a plant.

An audit once found fourteen documentation drifts, and every one had been true
once. The README said the LLM layer was "not built yet" while the agent had been
spending for three days. `observatory.py`'s own help documented a `links` step
that did not exist — while `tools/audit_vault_links.py`, the tool behind it, sat
orphaned and reachable only by typing its path. `fabric/FABRIC-CONFORMANCE.md`
certified a revision of the manifest that had moved on.

Fixing fourteen sentences is an afternoon; the class returns the following week,
because nothing measures it. `tools/check_docs.py` is the measurement, and this
file is what stops IT from rotting: each rule is broken on purpose in a copy of
the engine and must be caught.

The engine ships its code but not the documentation archive the checker was
written against, so the copy carries a SYNTHETIC documentation set built to
satisfy every rule — the control below — and each plant then breaks one rule.
"""
from __future__ import annotations
import hashlib, json, os, pathlib, re, shutil, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []

#: The engine's code the checker reads. Documentation is written fresh below.
CODE_DIRS = ("agent", "collectors", "dashboard", "defaults", "fabric", "mcp",
             "plugins", "skill", "store", "tools")
AGENT_SYNC_CONFIG = '{\n  "leaseTtlSeconds": 2700\n}\n'


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def assertions_md(manifest: dict) -> str:
    """The published prose contract: every probe with its assertions, verbatim."""
    lines = ["# Probe assertions", ""]
    for cap in manifest.get("capabilities", []):
        lines.append(f"## {cap['name']}")
        for probe in (cap.get("profile") or {}).get("probes", []):
            lines.append(f"### {probe.get('id') or probe.get('name')}")
            lines += [f"- {a}" for a in probe.get("assertions", [])]
        lines.append("")
    return "\n".join(lines) + "\n"


def architecture_md(root: pathlib.Path) -> str:
    """A design document that names every table, registry document and component."""
    import paths
    tables = sorted(set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)",
                                   (root / "store/schema.sql").read_text(encoding="utf-8"))))
    registry = sorted(f.name for f in paths.REGISTRY.glob("*.json"))
    top = sorted(p.name for p in root.iterdir()
                 if p.is_dir() and not p.name.startswith((".", "_")))
    tree = "\n".join(["project-observatory/"] + [f"  {d}/" for d in top])
    return ("# Architecture\n\n"
            "The store holds these tables: " + ", ".join(tables) + ".\n\n"
            "The registry holds these documents: " + ", ".join(registry) + ".\n\n"
            "A tombstone makes a revision unreadable, not unrecoverable.\n\n"
            "```\n" + tree + "\n```\n\n"
            "The checker is `tools/check_docs.py`.\n")


def sandbox() -> pathlib.Path:
    """A copy of the engine's code with a complete synthetic documentation set."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-docs-"))
    for name in CODE_DIRS:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, d / name, ignore=shutil.ignore_patterns(
                "*.db", "*.db-*", "__pycache__", "raw", "logs"))
    for f in ROOT.iterdir():
        if f.is_file() and f.suffix in {".py", ".json"}:
            shutil.copy(f, d / f.name)
    (d / "tests").mkdir(exist_ok=True)
    shutil.copy(ROOT / "tests/tmp.py", d / "tests/tmp.py")
    (d / "docs").mkdir(exist_ok=True)
    (d / "README.md").write_text("# Synthetic engine\n\nRun `tools/check_docs.py`.\n",
                                 encoding="utf-8")
    (d / "AGENTS.md").write_text("# Agents\n\nThe coordination snapshot carries a cfg= "
                                 "digest of its configuration.\n", encoding="utf-8")
    manifest = json.loads((d / "fabric-agent.json").read_text(encoding="utf-8"))
    (d / "fabric/probes").mkdir(parents=True, exist_ok=True)
    (d / "fabric/probes/assertions.md").write_text(assertions_md(manifest), encoding="utf-8")
    (d / "docs/ARCHITECTURE.md").write_text(architecture_md(d), encoding="utf-8")
    (d / ".claude").mkdir()
    (d / ".claude/agent-sync.json").write_text(AGENT_SYNC_CONFIG, encoding="utf-8")
    digest = hashlib.sha256(AGENT_SYNC_CONFIG.encode("utf-8")).hexdigest()[:12]
    (d / "docs/AGENT_SYNC.md").write_text(f"<!-- generated cfg={digest} -->\n# Agent sync\n",
                                          encoding="utf-8")
    # Generated by the engine's own tool, from the workspace registry.
    subprocess.run([PY, "tools/registry_shape.py", "--write"], cwd=d, capture_output=True,
                   text=True, timeout=300)
    return d


def run(root: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run([PY, "tools/check_docs.py"], cwd=root, capture_output=True,
                       text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


#: Drifts the engine's OWN shipped documents carry today. Two documents name a
#: file of the private workspace (`store/…`, `docs/dashboard/…`), which the
#: checker resolves against the source tree, and the generated registry-shape
#: document is not shipped. This is why `docs-current` is a source-only step.
ENGINE_DOC_GAPS = (
    "docs/COMPATIBILITY.md names store/scrub-watermark.json",
    "docs/ONBOARDING.md names docs/dashboard/index.html",
    "docs/REGISTRY_SHAPE.md is missing",
)


def test_the_synthetic_documentation_is_current() -> None:
    """The control: without it, every 'caught it' below could be a fixture artefact."""
    code, out = run(sandbox())
    check("the checker passes on a complete synthetic documentation set", code == 0, out[-400:])


def test_the_engines_own_documents() -> None:
    code, out = run(ROOT)
    drifts = [line.strip()[2:] for line in out.splitlines() if line.strip().startswith("- ")]
    unknown = [x for x in drifts if not x.startswith(ENGINE_DOC_GAPS)]
    check("the engine's documents carry no drift beyond the known gaps", not unknown, str(unknown))
    if code == 0:
        check("and the checker passes on them", True)
    elif not unknown:
        print("  SKIP  KNOWN-GAP: the engine's shipped docs name workspace files that "
              "tools/check_docs.py resolves against the source tree, and "
              "docs/REGISTRY_SHAPE.md is not shipped — " + "; ".join(drifts))


def test_rule1_a_help_naming_a_missing_step_fails() -> None:
    d = sandbox()
    src = (d / "observatory.py").read_text(encoding="utf-8")
    anchor = '"""Project Observatory — the deterministic pipeline.'
    check("the help's first line is where the plant goes", anchor in src, src[:120])
    src = src.replace(anchor, anchor + '\n\n    teleport   move the estate to another machine')
    (d / "observatory.py").write_text(src, encoding="utf-8")
    code, out = run(d)
    check("a documented step that does not exist fails", code != 0, out[-200:])
    check("and it is named", "teleport" in out, out[-200:])


def test_rule11_a_duplicate_step_key_fails() -> None:
    """The defect that motivated the rule, planted: a second declaration of a
    name the table already has. Python keeps the last and says nothing, so the
    only witness is the text — which is why the rule reads the source and not
    `obs.STEPS`, where the duplicate has already collapsed to one entry."""
    d = sandbox()
    f = d / "observatory.py"
    src = f.read_text(encoding="utf-8")
    anchor = '    "test-ledger": [PY, "tests/test_ledger.py"],'
    check("the anchor step exists", anchor in src, "the anchor step was renamed; pick another")
    f.write_text(src.replace(anchor, anchor + '\n    "test-ledger": [PY, "tests/test_other.py"],'),
                 encoding="utf-8")
    code, out = run(d)
    check("a step declared twice fails", code != 0, out[-300:])
    check("and the name is in the message", "test-ledger" in out, out[-300:])


def test_rule11_a_group_listing_a_step_twice_fails() -> None:
    d = sandbox()
    f = d / "observatory.py"
    src = f.read_text(encoding="utf-8")
    # THE ANCHOR IS DERIVED, not written down. Hardcoded group members went
    # stale as steps were inserted beside them, and a fixture that cannot find
    # its anchor fails the suite for a reason unrelated to the rule under test.
    gb = re.search(r"GROUPS = \{(.*?)^\}", src, re.S | re.M).group(1)
    body = re.search(r'"check":\s*\[(.*?)\]', gb, re.S).group(1)
    first = re.search(r'"([a-z0-9-]+)"', body).group(1)
    # INSIDE THE GROUP BODY ONLY. A whole-file replace hit the identically named
    # key in `STEPS` first and produced a SyntaxError — the fixture broke the
    # file instead of planting the defect.
    b0 = src.index(body)
    doubled = body.replace(f'"{first}"', f'"{first}", "{first}"', 1)
    f.write_text(src[:b0] + doubled + src[b0 + len(body):], encoding="utf-8")
    code, out = run(d)
    check("a group listing one step twice fails", code != 0, out[-300:])
    check("and it says how many times", "2 times" in out, out[-300:])


def test_rule12_a_test_step_in_no_group_fails() -> None:
    """The defect that motivated the rule, planted: a `test-*` step declared and
    listed nowhere. One such suite had been red for two iterations while the
    gate was reported green."""
    d = sandbox()
    f = d / "observatory.py"
    src = f.read_text(encoding="utf-8")
    m = re.search(r'^    "[a-z0-9-]+": \[PY, "tests/[a-z_]+\.py"\],$', src, re.M)
    check("a test step line to anchor on", m is not None)
    if m is None:
        return
    f.write_text(src[:m.end()] + '\n    "test-orphan": [PY, "tests/test_traps.py"],'
                 + src[m.end():], encoding="utf-8")
    code, out = run(d)
    check("a test step no group runs fails", code != 0, out[-300:])
    check("and the step is named", "test-orphan" in out, out[-300:])


def test_rule12_ignores_a_non_test_step_outside_every_group() -> None:
    """The boundary. An operator command run on demand is not a check that
    stopped running, so `index-status`, `retention-apply` and `skip-sites` are
    legitimately in no group and must not be reported."""
    d = sandbox()
    f = d / "observatory.py"
    src = f.read_text(encoding="utf-8")
    m = re.search(r'^    "[a-z0-9-]+": \[PY, "tests/[a-z_]+\.py"\],$', src, re.M)
    check("a test step line to anchor on", m is not None)
    if m is None:
        return
    f.write_text(src[:m.end()] + '\n    "show-something": [PY, "tools/skip_sites.py"],'
                 + src[m.end():], encoding="utf-8")
    code, out = run(d)
    check("a non-test step outside every group is not reported",
          "show-something" not in out, out[-300:])


def test_rule2_a_path_that_does_not_resolve_fails() -> None:
    d = sandbox()
    f = d / "docs/ARCHITECTURE.md"
    f.write_text(f.read_text(encoding="utf-8") + "\n\nSee `tools/no_such_tool.py`.\n",
                 encoding="utf-8")
    code, out = run(d)
    check("a repository path that does not exist fails", code != 0, out[-200:])
    check("and it is named", "no_such_tool.py" in out, out[-200:])


def test_rule2_does_not_cry_wolf() -> None:
    """Dozens of 'drifts' of which a handful were real is a checker nobody runs."""
    d = sandbox()
    f = d / "docs/ARCHITECTURE.md"
    f.write_text(f.read_text(encoding="utf-8") +
                 "\n\nA bare name `projects.json`, another repository's "
                 "`beta-api/configs/embedding.yaml`, and a skill's "
                 "`references/lease-protocol.md`.\n", encoding="utf-8")
    code, out = run(d)
    check("a bare filename is not treated as a path", "projects.json, which does not" not in out)
    check("another repository's path is not checked", "embedding.yaml" not in out)
    check("a skill's own reference is not checked", "lease-protocol" not in out)
    check("so the run stays green", code == 0, out[-200:])


def test_rule3_a_stale_conformance_revision_fails() -> None:
    d = sandbox()
    m = json.loads((d / "fabric-agent.json").read_text(encoding="utf-8"))
    m["provider"]["revision"] = 99
    (d / "fabric-agent.json").write_text(json.dumps(m), encoding="utf-8")
    code, out = run(d)
    check("a conformance report behind its manifest fails", code != 0, out[-200:])
    check("and the revision is named", "99" in out, out[-200:])


def test_rule5_a_retired_claim_fails_but_a_quotation_does_not() -> None:
    d = sandbox()
    f = d / "README.md"
    f.write_text(f.read_text(encoding="utf-8") + "\n\nThe LLM layer is not built yet.\n",
                 encoding="utf-8")
    code, out = run(d)
    check("a retired claim, restated, fails", code != 0, out[-200:])

    d2 = sandbox()
    f2 = d2 / "README.md"
    f2.write_text(f2.read_text(encoding="utf-8") +
                  '\n\nThis file once said "not built yet", which was false.\n',
                  encoding="utf-8")
    code2, out2 = run(d2)
    check("the same phrase QUOTED in its own retirement does not",
          code2 == 0, out2[-200:])


def test_rule4_reads_the_probes_where_they_actually_live() -> None:
    """The rule passed because it found nothing — the shape it exists to catch."""
    m = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
    at_top = sum(len(c.get("probes") or []) for c in m["capabilities"])
    in_profile = sum(len((c.get("profile") or {}).get("probes") or [])
                     for c in m["capabilities"])
    # SIX: `estate.survey`'s three required tools were split into three
    # capabilities because two of them could not satisfy one output schema.
    # The literal is deliberate — a probe added without being published fails.
    check("the manifest keeps probes under profile, not at the top",
          at_top == 0 and in_profile == 6, f"top={at_top} profile={in_profile}")
    src = (ROOT / "tools/check_docs.py").read_text(encoding="utf-8")
    check("the rule reads them there", '(cap.get("profile") or {}).get("probes"' in src)
    check("and refuses an empty set rather than calling it agreement",
          "declares no probes at capabilities" in src,
          "a rule that cannot fail is not a rule")


def test_rule4_catches_a_swapped_assertion() -> None:
    """A count cannot see a swap, and a swap is how this document drifted."""
    d = sandbox()
    f = d / "fabric/probes/assertions.md"
    text = f.read_text(encoding="utf-8")
    check("the planted assertion is in the published prose",
          "degraded is present, even when empty" in text)
    f.write_text(text.replace("degraded is present, even when empty",
                              "degraded is present when there is something to say"),
                 encoding="utf-8")
    code, out = run(d)
    check("a swapped assertion fails", code != 0, out[-200:])
    check("and the probe that owns it is named", "survey-single-project" in out, out[-200:])


def test_the_published_contract_covers_both_capabilities() -> None:
    """Read from the manifest a host admits, since the prose is derived from it."""
    text = (ROOT / "fabric-agent.json").read_text(encoding="utf-8")
    for cap in ("estate.survey", "project.record"):
        check(f"{cap} appears in the published manifest", cap in text)
    check("the write capability's probe is described", "record-proposes-and-refuses" in text)
    check("and its first assertion is the one about what it must not touch",
          "no row reaches the operator's ledger" in text)


def test_rule9_catches_a_snapshot_describing_an_older_config() -> None:
    """The generated file CAN drift; the digest is what makes it detectable."""
    d = sandbox()
    code, out = run(d)
    check("a snapshot matching its config passes", code == 0, out[-200:])
    cfg = d / ".claude" / "agent-sync.json"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(
        '"leaseTtlSeconds": 2700', '"leaseTtlSeconds": 3600'), encoding="utf-8")
    code, out = run(d)
    check("changing the config fails the snapshot", code != 0, out[-200:])
    check("and the message says to regenerate", "agent_sync.py setup" in out, out[-200:])


def test_rule14_a_stale_registry_shape_fails() -> None:
    """The generated shape document is stamped; a changed stamp is a drift."""
    d = sandbox()
    doc = d / "docs/REGISTRY_SHAPE.md"
    check("the shape document was generated", doc.is_file())
    if not doc.is_file():
        return
    doc.write_text(re.sub(r"data=\w+", "data=000000000000", doc.read_text(encoding="utf-8"), count=1),
                   encoding="utf-8")
    code, out = run(d)
    check("a shape document describing another registry fails", code != 0, out[-200:])
    check("and it says how to regenerate", "registry_shape.py --write" in out, out[-200:])


def test_the_orphaned_tool_is_reachable_now() -> None:
    """The help named `links`; the tool behind it was reachable only by path."""
    import observatory as obs
    check("`links` is a step", "links" in obs.STEPS)
    check("and it runs the link auditor",
          "audit_vault_links.py" in " ".join(obs.STEPS.get("links", [])))
    check("`docs-current` is a step too", "docs-current" in obs.STEPS)
    check("and it is in the gate", "docs-current" in obs.GROUPS["check"],
          "a checker outside the gate is a checker nobody runs")


if __name__ == "__main__":
    print("documentation — true, or the gate is red\n")
    for fn in (test_the_synthetic_documentation_is_current,
               test_the_engines_own_documents,
               test_rule1_a_help_naming_a_missing_step_fails,
               test_rule12_a_test_step_in_no_group_fails,
               test_rule12_ignores_a_non_test_step_outside_every_group,
               test_rule11_a_duplicate_step_key_fails,
               test_rule11_a_group_listing_a_step_twice_fails,
               test_rule2_a_path_that_does_not_resolve_fails,
               test_rule2_does_not_cry_wolf,
               test_rule3_a_stale_conformance_revision_fails,
               test_rule5_a_retired_claim_fails_but_a_quotation_does_not,
               test_rule4_reads_the_probes_where_they_actually_live,
               test_rule4_catches_a_swapped_assertion,
               test_the_published_contract_covers_both_capabilities,
               test_rule9_catches_a_snapshot_describing_an_older_config,
               test_rule14_a_stale_registry_shape_fails,
               test_the_orphaned_tool_is_reachable_now):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe documentation is measured, not asserted\033[0m")
