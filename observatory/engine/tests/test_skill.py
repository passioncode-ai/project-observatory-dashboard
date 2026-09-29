#!/usr/bin/env python3
"""The companion plugin's contract — including the hook it must NOT ship.

T8 is the reason this file exists. A SessionStart injector from another plugin
once printed hundreds of tokens of doctrine into every session and outcompeted
the operator's own instructions; the only remedy was disabling that plugin. A
promise not to repeat that is worth nothing. This is the assertion.
"""
from __future__ import annotations
import json, os, pathlib, re, subprocess, sys, tempfile
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import tmp as tmpdir  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "skill/plugins/observatory-log"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def test_t8_no_session_start() -> None:
    """The shipped plugin injects no DOCTRINE at session start.

    This once asserted the absence of a SessionStart hook outright — another
    plugin's injector had printed hundreds of tokens and beaten the operator's
    own instructions, and absence was the only guard this file knew. The hook exists now, and the trap
    guards what the doctrine actually forbade: unbounded prose.
    `tools/session_start.py` prints at most two lines, only when actionable,
    and nothing for a directory that is not a repository — watched here by
    running it, not by reading its promise.

    Trap: T8
    """
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text(encoding="utf-8"))
    events = sorted(hooks["hooks"])
    check("T8 the plugin declares SessionStart and Stop, and nothing else",
          events == ["SessionStart", "Stop"], f"declared: {events}")
    check("T8 it declares no UserPromptSubmit hook", "UserPromptSubmit" not in events, f"declared: {events}")
    cmds = [h["command"] for ev in hooks["hooks"].values() for grp in ev for h in grp["hooks"]]
    check("T8 the SessionStart hook is the bounded script, not an inline echo",
          any("session-start.sh" in c for c in cmds), str(cmds))
    # The public manifest states the bound in words ("brief actionable context")
    # rather than citing the private incident that motivated it.
    desc = hooks.get("description", "")
    check("T8 the reason a SessionStart is allowed is written where a reviewer reads it",
          "SessionStart" in desc and "brief" in desc and "actionable" in desc,
          "hooks.json description must say the SessionStart output is bounded")
    # THE CEILING, WATCHED: a non-repository directory yields nothing at all, and
    # the tool's own source caps what a known project yields at two lines.
    import subprocess, tempfile, sys
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run([sys.executable, str(ROOT / "tools/session_start.py"), "--cwd", d],
                           capture_output=True, text=True, timeout=60)
    check("T8 a directory that is not a repository yields no output and exit 0",
          p.returncode == 0 and p.stdout == "", repr(p.stdout[:80]))
    src = (ROOT / "tools/session_start.py").read_text(encoding="utf-8")
    check("T8 the tool prints one line per outcome — state_line or unknown_line, never a loop of prose",
          src.count("print(") == 1 and "state_line(p) if p else unknown_line(" in src, f"{src.count('print(')} print sites")


def test_hook_never_blocks() -> None:
    src = (PLUGIN / "hooks/ask-why.py").read_text(encoding="utf-8")
    check("the hook emits no blocking decision",
          '"decision"' not in src and "'decision'" not in src,
          "a Stop veto costs a turn; the facts are already recorded")
    check("the hook exits 0 on every path",
          "SystemExit(0)" in src and "exit 2" not in src)


def test_hook_is_silent_when_not_its_business() -> None:
    """Four silent paths, driven through the real shell script."""
    hook = PLUGIN / "hooks/record-turn.sh"
    tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-skill-")) / "t.db"
    # THE SCRATCH TOO — see `tests/test_companion_hook.py` for the same fix in
    # the same run. `tools/record_turn.py` gained a receipt, so a
    # suite that redirected only the store began writing a fixture's outcome
    # into the live `store/raw/record-turn.json` that the findings read.
    env = {**os.environ, "OBSERVATORY_DB": str(tmp), "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
           "OBSERVATORY_SCRATCH": str(pathlib.Path(tmp).parent / "scratch")}
    # A real clone of somebody else's repository, which no registry lists.
    clone = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-skill-clone-")) / "tool"
    subprocess.run(["git", "init", "-q", str(clone)], capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(clone), "remote", "add", "origin",
                    "https://github.com/third-party/tool.git"], capture_output=True, timeout=60)
    cases = {
        "a re-entrant Stop": {"session_id": "a", "cwd": str(ROOT), "stop_hook_active": True},
        "a directory that is not a repository": {"session_id": "b", "cwd": "/tmp",
                                                 "stop_hook_active": False},
        "a third-party clone": {"session_id": "c", "cwd": str(clone),
                                "stop_hook_active": False},
    }
    for label, payload in cases.items():
        p = subprocess.run(["bash", str(hook)], input=json.dumps(payload), text=True,
                           capture_output=True, env=env, timeout=60)
        check(f"silent for {label}", p.returncode == 0 and not p.stdout.strip(),
              f"exit={p.returncode} stdout={p.stdout[:120]!r}")

    no_checkout = {**env, "OBSERVATORY_ROOT": "/nonexistent", "CLAUDE_PLUGIN_ROOT": "/nonexistent"}
    p = subprocess.run(["bash", str(hook)], input=json.dumps({"session_id": "d", "cwd": "/tmp"}),
                       text=True, capture_output=True, env=no_checkout, timeout=60)
    check("silent when there is no observatory checkout",
          p.returncode == 0 and not p.stdout.strip(), f"exit={p.returncode}")


def test_hook_asks_for_the_why() -> None:
    """The hook must be driven against a KNOWN-dirty tree — one the fixture owns.
    Trap: T16

    The first version of this test used whatever state the repository happened to
    be in, so it passed while there were uncommitted files and failed the moment
    the work was committed — green for the wrong reason either way. It now makes
    its own dirt and removes it.
    """
    hook = PLUGIN / "hooks/record-turn.sh"
    tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-skill-")) / "t.db"
    # THE SCRATCH TOO — see `tests/test_companion_hook.py` for the same fix in
    # the same run. `tools/record_turn.py` gained a receipt, so a
    # suite that redirected only the store began writing a fixture's outcome
    # into the live `store/raw/record-turn.json` that the findings read.
    # AND THE DIRT WAS NEVER SEEN. This planted `.observatory-skill-test-dirt`
    # in THIS repository — a path `.gitignore:36` ignores, so `git status`
    # never listed it and the plant contributed nothing. The case passed for a
    # hundred and fifty iterations on the ambient dirt of work in progress, and
    # went red the moment the tree was clean and pushed (2026-09-09). A fixture
    # that plants an invisible change is trap T16 wearing a fix's clothes.
    import watched_repo
    _work, repo, built = watched_repo.build()
    env = {**os.environ, **built, "OBSERVATORY_DB": str(tmp),
           "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
           "OBSERVATORY_SCRATCH": str(pathlib.Path(tmp).parent / "scratch")}
    payload = {"session_id": "skill-test-ask", "cwd": str(repo), "stop_hook_active": False}
    try:
        p = subprocess.run(["bash", str(hook)], input=json.dumps(payload), text=True,
                           capture_output=True, env=env, timeout=90)
        if not p.stdout.strip():
            check("a changed watched repository produces a request for the why", False,
                  f"no output even with a planted change; stderr={p.stderr[:120]!r}")
            return
        out = json.loads(p.stdout)
        check("a changed watched repository produces a request for the why",
              "why" in out.get("systemMessage", ""), str(out)[:140])
        check("the request names the exact call to make",
              "observatory_record(" in out["systemMessage"]
              and "expected_revision=" in out["systemMessage"])
        check("the request refuses a fabricated reason",
              "invented" in out["systemMessage"] or "fabricat" in out["systemMessage"])
        check("the request is suppressed from the user's transcript",
              out.get("suppressOutput") is True, str(out.get("suppressOutput")))
        check("no blocking decision is returned",
              "decision" not in out and out.get("continue") is not False, str(out.keys()))

        second = subprocess.run(["bash", str(hook)], input=json.dumps(payload), text=True,
                                capture_output=True, env=env, timeout=90)
        check("an unchanged second turn adds nothing and says nothing",
              second.returncode == 0 and not second.stdout.strip(),
              f"stdout={second.stdout[:120]!r}")
    finally:
        # Nothing to clean: the change lives in the fixture's own repository
        # under `tests/tmp`, which registers its own removal. The old cleanup
        # removed a file that had never been visible to git in the first place.
        pass


def test_version_sync() -> None:
    mkt = json.loads((ROOT / "skill/.claude-plugin/marketplace.json").read_text())
    plug = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    # EVERY skill in the plugin, not one of them. This read only
    # `explaining-changes` and went green while `handling-secrets` was bumped to
    # 0.3.0 alone — the plugin then shipped two skills claiming different
    # versions, and `skill_check.py`'s handshake compares against exactly this
    # number, so the drift would have been reported to sessions as staleness
    # (found 2026-09-12 by the gate, after the bump).
    versions = {"marketplace entry": mkt["plugins"][0]["version"],
                "plugin.json": plug["version"]}
    for d in sorted((PLUGIN / "skills").glob("*/SKILL.md")):
        m = re.search(r'^\s+version:\s*["\']?([0-9.]+)', d.read_text(encoding="utf-8"), re.M)
        versions[f"{d.parent.name}/SKILL.md"] = m.group(1) if m else None
    check("every manifest carries the same version", len(set(versions.values())) == 1,
          str(versions))


def test_declared_tools_exist() -> None:
    """The skill names MCP tools; the server must actually serve them."""
    skill = (PLUGIN / "skills/explaining-changes/SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"observatory_[a-z_]+", skill))
    served = set(re.findall(r"^def (observatory_[a-z_]+)", (ROOT / "mcp/server.py").read_text(),
                            re.M))
    missing = named - served
    check("every observatory tool the skill names is served", not missing, f"missing {missing}")
    ask = (PLUGIN / "hooks/ask-why.py").read_text(encoding="utf-8")
    named_hook = set(re.findall(r"observatory_[a-z_]+", ask))
    check("every tool the hook's message names is served", not (named_hook - served),
          f"missing {named_hook - served}")


# --- front matter: strict YAML, checked offline ---------------------------------
# Claude Code parses a SKILL.md front matter with a strict YAML parser, and one
# that fails to parse loads the skill with EMPTY metadata: no name, no
# description, so it never triggers, and nothing says so at runtime. An unquoted
# `: ` inside a plain scalar is the classic way in (a sibling plugin shipped one
# and its own validator missed it). PyYAML is not a dependency of this engine,
# so this reads the subset of YAML the shipped skills use and refuses anything
# outside it: stricter than YAML, never looser. `claude plugin validate --strict`
# in CI is the second, real parser.

KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+):(?P<rest>.*)$")
BLOCK = re.compile(r"^[>|][+-]?$")
DOUBLE = re.compile(r'^"(?:[^"\\]|\\.)*"$')
SINGLE = re.compile(r"^'(?:[^']|'')*'$")
PLAIN_START = set("-?:,[]{}#&*!|>'\"%@`")


def _plain_problem(value: str) -> str | None:
    if value[0] in PLAIN_START:
        return f"a plain scalar may not start with {value[0]!r}; quote it or use a block scalar (>-)"
    if ": " in value or value.endswith(":"):
        return "an unquoted ': ' inside a plain scalar is a mapping to YAML; quote it or use >-"
    if " #" in value:
        return "an unquoted ' #' starts a YAML comment and truncates the value; quote it or use >-"
    return None


def front_matter(text: str) -> tuple[dict, list[str]]:
    """(top-level fields, problems) for a SKILL.md, under the strict subset above."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return {}, ["the file does not open with a --- front matter line"]
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].rstrip() == "---")
    except StopIteration:
        return {}, ["the front matter is never closed with ---"]
    body, fields, problems, seen = lines[1:end], {}, [], {}
    i, parent = 0, None
    while i < len(body):
        line, n = body[i], i + 2
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if "\t" in line:
            problems.append(f"line {n}: a tab; YAML indentation is spaces only")
            i += 1
            continue
        m = KEY.match(line)
        if not m:
            problems.append(f"line {n}: not `key: value` — {line.strip()[:60]!r}")
            i += 1
            continue
        indent, key, rest = len(m.group("indent")), m.group("key"), m.group("rest")
        if indent == 0:
            parent = key
        elif parent is None or indent != 2:
            problems.append(f"line {n}: unexpected indentation of {key!r}")
        scope = seen.setdefault(parent if indent else None, set())
        if key in scope:
            problems.append(f"line {n}: duplicate key {key!r}")
        scope.add(key)
        if rest and not rest.startswith(" "):
            problems.append(f"line {n}: no space after the colon of {key!r}")
        value = rest.strip()
        i += 1
        if not value:
            if indent == 0:
                fields[key] = {}
            continue
        if BLOCK.match(value):
            parts = []
            while i < len(body) and (not body[i].strip() or len(body[i]) - len(body[i].lstrip()) > indent):
                parts.append(body[i].strip())
                i += 1
            if not any(parts):
                problems.append(f"line {n}: the block scalar of {key!r} is empty")
            joiner = " " if value.startswith(">") else "\n"
            folded = joiner.join(p for p in parts if p) if joiner == " " else "\n".join(parts)
            result = folded.strip()
        elif value.startswith('"'):
            if not DOUBLE.match(value):
                problems.append(f"line {n}: {key!r} opens a double-quoted string it does not close on the line")
            result = value[1:-1]
        elif value.startswith("'"):
            if not SINGLE.match(value):
                problems.append(f"line {n}: {key!r} opens a single-quoted string it does not close on the line")
            result = value[1:-1].replace("''", "'")
        else:
            why = _plain_problem(value)
            if why:
                problems.append(f"line {n}: {key!r}: {why}")
            result = value
        if indent == 0:
            fields[key] = result
        elif isinstance(fields.get(parent), dict):
            fields[parent][key] = result
    return fields, problems


def skill_problems(path: pathlib.Path) -> list[str]:
    fields, problems = front_matter(path.read_text(encoding="utf-8"))
    for required in ("name", "description"):
        if not isinstance(fields.get(required), str) or not fields.get(required):
            problems.append(f"{required} is missing or not a string")
    if fields.get("name") != path.parent.name:
        problems.append(f"name {fields.get('name')!r} differs from the directory {path.parent.name!r}")
    if len(str(fields.get("description", ""))) > 1024:
        problems.append(f"description is {len(fields['description'])} characters; the limit is 1024")
    if not isinstance(fields.get("metadata"), dict) or not fields["metadata"].get("version"):
        problems.append("metadata.version is missing")
    return problems


PLANTED = {
    "an unquoted ': ' in a plain description":
        "---\nname: alpha\ndescription: Use when: the build breaks\n---\n",
    "an unquoted ' #' in a plain description":
        "---\nname: alpha\ndescription: Use for issue #12 and #13\n---\n",
    "a block scalar line that falls back to column 0":
        "---\nname: alpha\ndescription: >-\n  Use when the\nbuild breaks\n---\n",
    "an unclosed double quote":
        '---\nname: alpha\ndescription: "Use when the build breaks\n---\n',
    "a duplicate key":
        "---\nname: alpha\nname: beta\ndescription: x\n---\n",
    "a tab for indentation":
        "---\nname: alpha\nmetadata:\n\tversion: \"1\"\n---\n",
    "a front matter never closed":
        "---\nname: alpha\ndescription: x\n",
}


def test_front_matter_is_strict_yaml() -> None:
    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    check("the plugin ships skills to check", len(skills) >= 3, str(skills))
    for skill in skills:
        problems = skill_problems(skill)
        check(f"{skill.parent.name}: front matter parses under the strict subset", not problems,
              "; ".join(problems))
    for label, text in PLANTED.items():
        _, problems = front_matter(text)
        check(f"a planted defect is refused: {label}", bool(problems), text)
    fields, problems = front_matter('---\nname: alpha\ndescription: "Use when: quoted"\n'
                                    "metadata:\n  version: \"1.0.0\"\n---\n")
    check("the same colon inside quotes is fine",
          not problems and fields["description"] == "Use when: quoted"
          and fields["metadata"] == {"version": "1.0.0"}, f"{fields} {problems}")
    fields, problems = front_matter("---\nname: alpha\ndescription: >-\n  Use when: the build\n"
                                    "  breaks - fix it\n---\n")
    check("and inside a folded block scalar",
          not problems and fields["description"] == "Use when: the build breaks - fix it", f"{fields} {problems}")


def test_tracking_resources_records_and_hands_creation_to_its_owners() -> None:
    """tracking-resources records a resource; it does not create one. Its old
    triggers ("new Figma file", «новый файл в Figma», "add analytics") were the
    creation requests themselves, so it competed with the skills that create the
    file or wire the tracking and could win the turn without creating anything."""
    fields, problems = front_matter((PLUGIN / "skills/tracking-resources/SKILL.md").read_text(encoding="utf-8"))
    desc = str(fields.get("description", ""))
    check("the description parses", not problems, "; ".join(problems))
    for trigger in ('"new Figma file"', "«новый файл в Figma»", '"add analytics"', "«подключи аналитику»"):
        check(f"a creation request is no longer a trigger: {trigger}", trigger not in desc, desc)
    check("it says it is the step AFTER creating", "after" in desc.lower(), desc)
    check("and names the skills that create or wire instead",
          "NOT for creating the Figma file itself (figma-create-new-file" in desc and "ad-tracking" in desc, desc)
    check("and stays within the 1024-character limit", len(desc) <= 1024, str(len(desc)))


if __name__ == "__main__":
    print("companion plugin — observatory-log\n")
    for fn in (test_t8_no_session_start, test_hook_never_blocks,
               test_hook_is_silent_when_not_its_business, test_hook_asks_for_the_why,
               test_version_sync, test_declared_tools_exist, test_front_matter_is_strict_yaml,
               test_tracking_resources_records_and_hands_creation_to_its_owners):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mskill ok\033[0m")
