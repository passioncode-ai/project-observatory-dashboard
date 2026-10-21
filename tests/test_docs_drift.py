"""Documentation that drifted from the code, each instance turned into a rule that fails.

An audit of main at 23a52296 (2026-10-21) found these, every one a statement that had been true
once or a surface nobody was told to update:

- a scenario id defined twice (OSS-62 for the agents table row and for the SSH-key heading), one
  id never used (OSS-59) and a table out of order;
- `full node` and `full scan-digitalocean` named in the docs with no line in `full --help`;
- the DigitalOcean door's presets named in no document a person or an agent reads;
- state the engine writes since 0.26.0 (`node/orgs/`, `store/raw/digitalocean.json`, the
  DigitalOcean and Heroku secret folders, the Fabric alias source) absent from COMPATIBILITY.md;
- the 0.27.0 release record still saying "Next: tag v0.27.0" after the release was published.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "observatory/engine"
SCENARIOS = ENGINE / "docs/ux/portable-scenarios.md"
SKILL = ENGINE / "skill/plugins/observatory-log/skills/handling-secrets/SKILL.md"


def tracked_text_files() -> list[Path]:
    run = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, timeout=60)
    if run.returncode != 0:
        return []
    out = []
    for name in run.stdout.decode("utf-8", "replace").split("\0"):
        path = ROOT / name
        if name and path.suffix in (".md", ".py", ".json", ".html", ".mjs", ".js", ".yml", ".txt") and path.is_file():
            out.append(path)
    return out


# ---- scenario ids ------------------------------------------------------------------------------

ROW_DEF = re.compile(r"^\| (OSS-(\d+)) \|", re.M)
HEADING_DEF = re.compile(r"^#{2,6} (OSS-(\d+))(?::| \u2014)", re.M)
DETAIL = re.compile(r"^#{2,6} (OSS-(\d+)), in detail:", re.M)
NEXT_FREE_OSS = re.compile(r"^\*\*Next free OSS id:\*\* `OSS-(\d+)`$", re.M)
VACANT_OSS_LINE = re.compile(r"^\*\*Vacant OSS ids:\*\*(.*)$", re.M)
VACANT_OSS = re.compile(r"`OSS-(\d+)` \(([^()]+)\)")


def scenario_problems(text: str) -> list[str]:
    problems = []
    defs: dict[int, int] = {}
    for m in list(ROW_DEF.finditer(text)) + list(HEADING_DEF.finditer(text)):
        n = int(m.group(2))
        defs[n] = defs.get(n, 0) + 1
    for n, count in sorted(defs.items()):
        if count > 1:
            problems.append(f"OSS-{n:02d} is defined {count} times; renumber the later one to the next free id")
    for m in DETAIL.finditer(text):
        if int(m.group(2)) not in defs:
            problems.append(f"{m.group(1)}, in detail: expands a scenario that is not defined")
    nxt = NEXT_FREE_OSS.findall(text)
    if len(nxt) != 1:
        problems.append("one `**Next free OSS id:** `OSS-n`` line is required")
        return problems
    next_free = int(nxt[0])
    top = max(defs) if defs else 0
    if next_free != top + 1:
        problems.append(f"`Next free OSS id` says OSS-{next_free}; the highest defined is OSS-{top}, so it is OSS-{top + 1}")
    vacant = {int(n): why for line in VACANT_OSS_LINE.findall(text) for n, why in VACANT_OSS.findall(line)}
    for n in range(1, top + 1):
        if n not in defs and n not in vacant:
            problems.append(f"OSS-{n:02d} is neither defined nor declared on the `**Vacant OSS ids:**` line")
    for n in vacant:
        if n in defs:
            problems.append(f"OSS-{n:02d} is declared vacant but defined")
    # Each table lists its rows in id order.
    for block in re.split(r"\n(?!\| OSS-)", text):
        ids = [int(n) for n in re.findall(r"^\| OSS-(\d+) \|", block, re.M)]
        if ids != sorted(ids):
            problems.append(f"a table lists OSS ids out of order: {ids}")
    return problems


class ScenarioIds(unittest.TestCase):
    def test_every_scenario_id_has_one_definition_and_the_next_free_line_is_true(self):
        self.assertEqual(scenario_problems(SCENARIOS.read_text(encoding="utf-8")), [])

    def test_the_rule_catches_the_23a52296_defects(self):
        text = ("**Next free OSS id:** `OSS-04`\n\n| OSS-01 | a | b | c |\n| OSS-03 | a | b | c |\n"
                "| OSS-02 | a | b | c |\n\n### OSS-02: twice\n")
        found = "\n".join(scenario_problems(text))
        self.assertIn("OSS-02 is defined 2 times", found)
        self.assertIn("out of order", found)
        gap = scenario_problems("**Next free OSS id:** `OSS-03`\n\n| OSS-02 | a | b | c |\n")
        self.assertTrue(any("OSS-01 is neither" in p for p in gap), gap)

    def test_every_reference_names_a_scenario_that_exists(self):
        text = SCENARIOS.read_text(encoding="utf-8")
        known = {int(m.group(2)) for m in ROW_DEF.finditer(text)} | {int(m.group(2)) for m in HEADING_DEF.finditer(text)}
        known |= {int(n) for line in VACANT_OSS_LINE.findall(text) for n, _ in VACANT_OSS.findall(line)}
        known |= {int(n) for n in NEXT_FREE_OSS.findall(text)}
        unknown = sorted({f"{p.relative_to(ROOT)}: OSS-{n}" for p in tracked_text_files()
                          for n in re.findall(r"\bOSS-(\d+)\b", p.read_text(encoding="utf-8", errors="replace"))
                          if int(n) not in known})
        self.assertEqual(unknown, [])


# ---- `full --help` against the commands the docs name ------------------------------------------

USER_DOCS = ("README.md", "docs/ONBOARDING.md", "docs/AGENT-ONBOARDING.md", "CHANGELOG.md")
NAMED = re.compile(r"`(?:project-observatory )?full ([a-z][a-z0-9-]*)")


def full_help() -> str:
    with tempfile.TemporaryDirectory(prefix="observatory-help-") as home:
        env = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVATORY_")}
        env.update(HOME=home, OBSERVATORY_HOME=str(Path(home, "ws")))
        run = subprocess.run([sys.executable, str(ENGINE / "observatory.py"), "--help"], capture_output=True,
                             text=True, timeout=120, env=env, cwd=home)
    if run.returncode != 0:
        raise AssertionError(f"full --help exited {run.returncode}: {run.stderr[-400:]}")
    return run.stdout


class DocumentedCommandsAreInTheHelp(unittest.TestCase):
    def test_every_step_a_document_names_has_a_line_in_the_help(self):
        text = full_help()
        body, _, tail = text.partition("\nsteps:")
        accepted = set()
        for line in ("steps:" + tail).splitlines():
            if line.startswith(("steps:", "groups:")):
                accepted |= set(line.split(":", 1)[1].strip().split(", "))
        docs = [ROOT / d for d in USER_DOCS] + sorted((ROOT / "docs/design").glob("*.md"))
        named = {name for doc in docs for name in NAMED.findall(doc.read_text(encoding="utf-8"))}
        undescribed = sorted(n for n in named & accepted
                             if not re.search(rf"(?m)^ {{2,4}}(?:[a-z-]+ / )*{re.escape(n)}(?=[\s\[]|$)", body))
        self.assertIn("node", named & accepted, "docs/design/NODE.md names `full node`")
        self.assertEqual(undescribed, [], "describe each in PUBLIC_HELP (observatory/engine/observatory.py)")


# ---- every door preset is named where people and agents read ------------------------------------

def door_presets() -> dict[str, list[str]]:
    """{door: preset names}, read from each tool's `PRESETS` literal. A tool that takes `--preset`
    without a `PRESETS` dict is an error: its choices would have no authoritative list."""
    out: dict[str, list[str]] = {}
    for path in sorted((ENGINE / "tools").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if '"--preset"' not in source:
            continue
        keys = None
        for node in ast.parse(source).body:
            target = node.target if isinstance(node, ast.AnnAssign) else (
                node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
            if isinstance(target, ast.Name) and target.id == "PRESETS" and isinstance(node.value, ast.Dict):
                keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
        if keys is None:
            raise AssertionError(f"{path.name} takes --preset but defines no PRESETS dict")
        out[path.stem] = keys
    return out


class DoorPresetsAreDocumented(unittest.TestCase):
    def test_every_preset_is_named_beside_its_door(self):
        """Named in a paragraph of ONBOARDING or the handling-secrets skill that names the door."""
        paragraphs = [p for doc in (ROOT / "docs/ONBOARDING.md", SKILL)
                      for p in re.split(r"\n\s*\n", doc.read_text(encoding="utf-8"))]
        presets = door_presets()
        self.assertTrue({"cloudflare", "digitalocean", "heroku"} <= set(presets), presets)
        missing = []
        for door, names in presets.items():
            beside = [p for p in paragraphs if door in p.lower()]
            for name in names:
                pattern = re.compile(rf"--preset (?:[a-z0-9-]+\|)*{re.escape(name)}(?![a-z0-9-])|`{re.escape(name)}`")
                if not any(pattern.search(p) for p in beside):
                    missing.append(f"{door}: {name}")
        self.assertEqual(missing, [], "name each preset in docs/ONBOARDING.md or the handling-secrets skill")


# ---- persisted state has a compatibility row -----------------------------------------------------

#: What the engine persists under store/, secrets/ and node/, by family, with the release that
#: added it. Families from 0.26.0 on need a row in COMPATIBILITY.md that says what an upgrade and a
#: downgrade do with them; older ones shipped before this rule and are listed so a NEW family
#: cannot appear unclassified. `StateFamilies` derives the families from the code and refuses one
#: missing here.
STATE_FAMILIES = {
    "store/raw/agents.json": "0.25.0", "store/raw/bitbucket.json": "0.2.0",
    "store/raw/cloudflare_zones.json": "0.2.0", "store/raw/companion-faults.jsonl": "0.2.0",
    "store/raw/digitalocean.json": "0.27.0", "store/raw/domains_live.json": "0.2.0", "store/raw/env.json": "0.2.0",
    "store/raw/fabric-attribution.json": "0.26.0", "store/raw/gh": "0.2.0", "store/raw/google.json": "0.2.0",
    "store/raw/harness.json": "0.23.0", "store/raw/heroku.json": "0.2.0", "store/raw/integrity.json": "0.2.0",
    "store/raw/local.json": "0.2.0", "store/raw/mcp.json": "0.2.0", "store/raw/openrouter.json": "0.2.0",
    "store/raw/record-turn.json": "0.2.0", "store/raw/remote-env.json": "0.2.0", "store/raw/remotes.json": "0.2.0",
    "store/raw/serverd.json": "0.2.0", "store/raw/sessions.json": "0.2.0", "store/raw/spend.json": "0.24.0",
    "store/raw/store-faults.jsonl": "0.2.0", "store/raw/tick-lease.json": "0.2.0", "store/raw/vault.json": "0.2.0",
    "store/raw/why-asks.jsonl": "0.25.0", "store/raw/why-asks.jsonl.1": "0.25.0",
    "store/raw/why-asks.jsonl.lock": "0.25.0",
    "secrets/heroku-admin": "0.26.0", "secrets/heroku-issued.json": "0.26.0",
    "secrets/digitalocean-admin": "0.27.0", "secrets/digitalocean-read.d": "0.27.0",
    "secrets/digitalocean-issued": "0.27.0",
    "node/device": "0.25.0", "node/records": "0.27.0", "node/orgs": "0.27.0",
}
ROW_FROM = (0, 26, 0)


def code_families() -> set[str]:
    families = set()
    families |= set(re.findall(r'"(store/raw/[A-Za-z0-9_.-]+)"', (ENGINE / "observatory.py").read_text(encoding="utf-8")))
    secret = re.compile(r'^[A-Z_]+\s*=\s*(?:_STORE|paths\.SECRETS|paths\.source_path\("secret_store", paths\.SECRETS\))'
                        r'\s*/\s*"([A-Za-z0-9_.-]+)"', re.M)
    node = re.compile(r"""(?:\bnode\b|\bNODE\b)\)?(?:\.resolve\(\))?\s*/\s*['"]([a-z][a-z0-9_.-]*)['"]""")
    for path in sorted((ENGINE / "tools").glob("*.py")) + sorted((ENGINE / "collectors").glob("*.py")):
        families |= {f"secrets/{n}" for n in secret.findall(path.read_text(encoding="utf-8"))}
    for path in sorted((ENGINE / "node").glob("*.py")) + [ENGINE / "tools/node.py", ENGINE / "tools/org.py"]:
        families |= {f"node/{n}" for n in node.findall(path.read_text(encoding="utf-8"))}
    return families


class StateFamilies(unittest.TestCase):
    def test_every_family_the_code_writes_is_classified(self):
        found = code_families()
        self.assertTrue({"store/raw/digitalocean.json", "secrets/digitalocean-admin", "node/orgs"} <= found, found)
        self.assertEqual(sorted(found - set(STATE_FAMILIES)), [],
                         "add it to STATE_FAMILIES with its release, and a COMPATIBILITY.md row from 0.26.0 on")

    def test_every_family_from_0_26_has_a_compatibility_row(self):
        for doc in (ROOT / "docs/COMPATIBILITY.md", ENGINE / "docs/COMPATIBILITY.md"):
            text = doc.read_text(encoding="utf-8")
            missing = sorted(f for f, since in STATE_FAMILIES.items()
                             if tuple(int(x) for x in since.split(".")) >= ROW_FROM and f"`{f}" not in text)
            with self.subTest(str(doc.relative_to(ROOT))):
                self.assertEqual(missing, [])


# ---- a run record's next step is not already done ------------------------------------------------

TAG_NAMED = re.compile(r"\btag(?:ging)? `?v(\d+\.\d+\.\d+)`?|`?v?(\d+\.\d+\.\d+)`? tag\b")


def tags() -> dict[str, str] | None:
    """{tag: commit} for the release tags in this checkout; None without Git or tags."""
    run = subprocess.run(["git", "-C", str(ROOT), "tag", "--list", "v*", "--format=%(refname:short) %(objectname)"
                          " %(*objectname)"], capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        return None
    out = {}
    for line in run.stdout.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"v\d+\.\d+\.\d+", parts[0]):
            out[parts[0]] = parts[-1]
    return out or None


def head_is(commit: str) -> bool:
    run = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=60)
    return run.returncode == 0 and run.stdout.strip() == commit


def next_steps() -> list[tuple[str, str]]:
    """(where, text) for every run record's `## Next` section and the handoff's `Next:` sentences under
    `## Latest run`."""
    out = []
    for record in sorted((ROOT / "docs/runs").glob("*/README.md")):
        text = record.read_text(encoding="utf-8")
        m = re.search(r"^## Next\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
        if m:
            out.append((str(record.relative_to(ROOT)), m.group(1)))
    handoff = (ROOT / "docs/HANDOFF.md").read_text(encoding="utf-8")
    latest = re.search(r"^## Latest run\b(.*?)(?=^## |\Z)", handoff, re.M | re.S)
    if latest:
        for paragraph in re.split(r"\n\s*\n", latest.group(1)):
            if "Next:" in paragraph:
                out.append(("docs/HANDOFF.md (Latest run)", paragraph.split("Next:", 1)[1]))
    return out


class NextIsNotAlreadyDone(unittest.TestCase):
    def test_no_next_step_names_a_tag_that_exists(self):
        """A tag that exists on an earlier commit is a release that happened: the record says so instead.

        The tagged commit itself is exempt: check.yml runs on the tag before the release workflow
        publishes it, and the record in that commit is the one being released."""
        known = tags()
        if known is None:
            self.skipTest("no release tags in this checkout: next steps NOT compared with releases")
        stale = []
        for where, text in next_steps():
            for m in TAG_NAMED.finditer(text):
                tag = "v" + (m.group(1) or m.group(2))
                if tag in known and not head_is(known[tag]):
                    stale.append(f"{where}: Next names {tag}, which is tagged; record the release and the real next step")
        self.assertEqual(stale, [])

    def test_the_pattern_reads_both_wordings(self):
        self.assertEqual(TAG_NAMED.search("Merge, tag `v0.27.0`, verify").group(1), "0.27.0")
        self.assertEqual(TAG_NAMED.search("then the 0.25.0 tag and installed acceptance").group(2), "0.25.0")


if __name__ == "__main__":
    unittest.main()
