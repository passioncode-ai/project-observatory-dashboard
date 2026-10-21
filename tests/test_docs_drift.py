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
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Every test's git environment comes from the engine tests' helper (no background gc or maintenance).
_git_env_spec = importlib.util.spec_from_file_location("observatory_test_git_env",
                                                       ROOT / "observatory/engine/tests/git_env.py")
git_env = importlib.util.module_from_spec(_git_env_spec)
_git_env_spec.loader.exec_module(git_env)
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

#: A release named as a step still to take: `tag v0.28.0`, `tag 0.28.0`, `the 0.28.0 tag`, `release v0.28.0`.
TAG_NAMED = re.compile(r"\b(?:tag(?:ging)?|release) `?v?(\d+\.\d+\.\d+)`?|`?v?(\d+\.\d+\.\d+)`? tag\b", re.I)
#: Headings whose whole body is a next step, and headings under which only the text after a `Next:`
#: marker is one. Measured on 99 run records (2026-10-21): `## Next`, `## Next task`, `## Exact next task`,
#: `## Open work and exact next task`, … carry the step whole; `## State now`, `## Left`, `## Open`
#: carry it after `Next task:` or `Next:`.
NEXT_HEADING = re.compile(r"^#{2,3} .*\bnext\b", re.I)
NEXT_MARKER = re.compile(r"\bnext(?: task| step)?:", re.I)


def next_lines(path: Path, only_section: str | None = None) -> list[tuple[int, str]]:
    """(line number, the text that is a next step on that line) in one document."""
    out: list[tuple[int, str]] = []
    whole, inside, marked = False, only_section is None, False
    for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if line.startswith("## "):
            inside = only_section is None or line.startswith(only_section)
            whole = bool(NEXT_HEADING.match(line))
            marked = False
            continue
        if not inside:
            continue
        if not line.strip():
            marked = False
            continue
        if whole:
            out.append((n, line))
            continue
        m = NEXT_MARKER.search(line)
        if m:
            marked = True
            out.append((n, line[m.end():]))
        elif marked:
            out.append((n, line))
    return out


def release_tags() -> dict[str, str] | None:
    """{tag: the commit it names} for the release tags in this checkout; None without Git or tags."""
    run = subprocess.run(["git", "-C", str(ROOT), "for-each-ref", "refs/tags/v*",
                          "--format=%(refname:short) %(objectname) %(*objectname)"],
                         capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        return None
    out = {}
    for line in run.stdout.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"v\d+\.\d+\.\d+", parts[0]):
            out[parts[0]] = parts[-1]
    return out or None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)


def base_ref(repo: Path = ROOT, env: dict | None = None) -> str | None:
    """The branch a change lands on, as a ref `repo` resolves: the merge checkout's first parent in a
    pull_request run, else `origin/<GITHUB_BASE_REF>`, else `origin/main`; None when none resolves."""
    env = os.environ if env is None else env
    candidates = []
    if env.get("GITHUB_EVENT_NAME") == "pull_request":
        candidates.append("HEAD^1")
    if env.get("GITHUB_BASE_REF"):
        candidates.append(f"origin/{env['GITHUB_BASE_REF']}")
    candidates.append("origin/main")
    for ref in candidates:
        if _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0:
            return ref
    return None


def written_after(path: Path, line: int, commit: str, repo: Path = ROOT, base: str | None = None) -> bool:
    """Whether `line` of `path` was last written after `commit`: by a strict descendant of it, not yet
    committed, or by a commit the base branch does not hold yet (this change's own, which the squash
    merge will rewrite as a descendant of every tag on the base). A line the tagged commit itself wrote
    (the release's own record) is not."""
    blame = _git(repo, "blame", "--porcelain", "-L", f"{line},{line}", "--", str(path.relative_to(repo)))
    if blame.returncode != 0:
        return False
    author = blame.stdout.split(" ", 1)[0]
    if set(author) == {"0"}:
        return True
    if author == commit:
        return False
    if base and _git(repo, "merge-base", "--is-ancestor", author, base).returncode != 0:
        return True
    return _git(repo, "merge-base", "--is-ancestor", commit, author).returncode == 0


def stale_next_steps(known: dict[str, str], repo: Path = ROOT, base: str | None = None,
                     docs: list[tuple[Path, str | None]] | None = None) -> list[str]:
    if docs is None:
        docs = [(p, None) for p in sorted((repo / "docs/runs").glob("*/README.md"))]
        docs.append((repo / "docs/HANDOFF.md", "## Latest run"))
    stale = []
    for path, section in docs:
        for n, text in next_lines(path, section):
            for m in TAG_NAMED.finditer(text):
                tag = "v" + (m.group(1) or m.group(2))
                if tag in known and written_after(path, n, known[tag], repo, base):
                    stale.append(f"{path.relative_to(repo)}:{n}: a next step names {tag}, which was tagged before "
                                 "this line was written; record the release and the real next step")
    return stale


class NextIsNotAlreadyDone(unittest.TestCase):
    """A next step written after a release that still names the release as to come.

    Judged per line by `git blame`, so a record the release itself wrote ("Next: merge, tag vX.Y.Z") does
    not turn every later pull request red: AGENTS.md's release steps rewrite it once the release is out.
    Not checked: a stale line nobody edited since the tag; the release step owns that."""

    def test_no_next_step_written_after_a_tag_names_it(self):
        known = release_tags()
        if known is None:
            self.skipTest("no release tags in this checkout: next steps NOT compared with releases")
        self.assertEqual(stale_next_steps(known, ROOT, base_ref()), [])

    def test_a_line_this_change_writes_is_judged_as_the_squash_will_be(self):
        """Confirming round at 8efc149a: A on main; a branch from A writes "Next task: tag v0.2.0"; B on main,
        tagged v0.2.0. Blamed in the pull request's merge checkout the line belongs to the branch's commit,
        not a descendant of the tag, and passed; after the squash it belongs to the squash commit, a
        descendant, and main went red. The branch's own commits count as written now."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env = git_env.git_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

            def git(*args):
                return subprocess.run(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-C", tmp, *args],
                                      check=True, capture_output=True, text=True, env=env).stdout.strip()

            record = repo / "docs/runs/2026-01-01-x/README.md"
            record.parent.mkdir(parents=True)
            git("init", "-q", "-b", "main")
            record.write_text("# Run\n\n## State now\n\nNothing yet.\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "A")
            git("checkout", "-q", "-b", "topic")
            record.write_text("# Run\n\n## State now\n\nDone. Next task: tag v0.2.0\n", encoding="utf-8")
            git("commit", "-q", "-am", "P1")
            git("checkout", "-q", "main")
            (repo / "other.txt").write_text("b\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "B")
            git("tag", "v0.2.0")
            known = {"v0.2.0": git("rev-parse", "v0.2.0")}
            docs = [(record, None)]
            # The pull request's merge checkout, base = its first parent.
            git("merge", "-q", "--no-ff", "-m", "Merge topic", "topic")
            pr_env = {"GITHUB_EVENT_NAME": "pull_request"}
            self.assertEqual(base_ref(repo, pr_env), "HEAD^1")
            self.assertEqual(len(stale_next_steps(known, repo, base_ref(repo, pr_env), docs)), 1,
                             "the pull request's own line passed, so the squash would turn main red")
            # The squash on main: the same line, now a descendant of the tag.
            git("reset", "-q", "--hard", "HEAD^1")
            git("merge", "-q", "--squash", "topic")
            git("commit", "-q", "-m", "P1 (#1)")
            self.assertEqual(len(stale_next_steps(known, repo, None, docs)), 1)

    def test_the_pattern_reads_every_wording(self):
        for text, version in (("Merge, tag `v0.27.0`, verify", "0.27.0"), ("then tag 0.28.0", "0.28.0"),
                              ("then the 0.25.0 tag and installed acceptance", "0.25.0"),
                              ("Release v0.28.0 once CI is green", "0.28.0")):
            m = TAG_NAMED.search(text)
            self.assertEqual(m and (m.group(1) or m.group(2)), version, text)
        self.assertIsNone(TAG_NAMED.search("0.28.0 tagged and published"))

    def test_sections_and_markers_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp, "README.md")
            doc.write_text("# Run\n\n## What shipped\n\nrelease v0.1.0 was cut\n\n## State now\n\nDone. Next task: "
                           "tag v0.2.0\nand verify.\n\nOther text, tag v0.3.0.\n\n## Exact next task\n\ntag v0.4.0\n",
                           encoding="utf-8")
            found = [m.group(1) for _, t in next_lines(doc) for m in TAG_NAMED.finditer(t)]
        self.assertEqual(found, ["0.2.0", "0.4.0"])

    def test_blame_separates_the_release_commit_from_a_later_edit(self):
        """The reviewer's reproduction: the tagged commit passes, a later edit of the line fails."""
        known = release_tags()
        if known is None or "v0.27.0" not in known:
            self.skipTest("v0.27.0 is not in this checkout")
        record = ROOT / "docs/runs/2026-10-20-release-0.27.0/README.md"
        line = next(n for n, t in enumerate(record.read_text(encoding="utf-8").split("\n"), 1)
                    if t.startswith("# Run"))
        self.assertFalse(written_after(record, line, known["v0.27.0"]), "the tagged commit wrote this line")
        older = known.get("v0.26.0")
        if older:
            self.assertTrue(written_after(record, line, older), "written after v0.26.0")


if __name__ == "__main__":
    unittest.main()
