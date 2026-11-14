"""Agent parity is a test, not a promise (OBS-559, CP-18, decision D8).

Every action the key server offers on the dashboard (`keyserver.ACTIONS`) and every
state-changing CLI verb of `observatory.py` either names the MCP tool that does the
same act, or is declared a person's gate with its reason, or is recorded as missing
with the board row that owes its tool. The list is `docs/design/control-plane/parity.json`.

The three surfaces are read from source with `ast`, never imported: importing the
engine resolves a workspace and the MCP SDK, and this list must hold in the plain
`unittest discover -s tests` job on every OS. The served tool names are the
`GOLDEN_TOOLS` list the MCP wire test holds equal to `tools/list`.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "observatory" / "engine"
PARITY = REPO / "docs" / "design" / "control-plane" / "parity.json"
STATUSES = ("covered", "gate", "missing")
KINDS = ("dashboard", "cli")
ROW = re.compile(r"OBS-[0-9]+")


def _tree(relative: str) -> ast.Module:
    return ast.parse((ENGINE / relative).read_text(encoding="utf-8"))


def _assigned(tree: ast.Module, name: str) -> list[ast.AST]:
    """Every value assigned to a module-level NAME (plain or annotated)."""
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            values.append(node.value)
    return values


def _dict_keys(tree: ast.Module, name: str) -> set[str]:
    """The string keys of NAME's dict literals and of every `NAME["key"] = ...` at module level."""
    keys = set()
    for value in _assigned(tree, name):
        if isinstance(value, ast.Dict):
            keys |= {k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == name and isinstance(target.slice, ast.Constant)):
                    keys.add(target.slice.value)
    return keys


def keyserver_actions() -> set[str]:
    return _dict_keys(_tree("tools/keyserver.py"), "ACTIONS")


def help_blocks(text: str) -> dict[str, str]:
    """PUBLIC_HELP split by command: a line indented two spaces opens a block for each
    `/`-separated name in its head; deeper lines continue it. One command's blocks join."""
    blocks: dict[str, list[str]] = {}
    current: list[str] = []
    for line in text.splitlines():
        if re.match(r"  \S", line):
            head = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
            names = [part.split()[0] for part in head.split(" / ") if part.split()]
            current = [blocks.setdefault(name, []) for name in names]
            for block in current:
                block.append(line)
        elif line.startswith("   ") and current:
            for block in current:
                block.append(line)
        else:
            current = []
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def cli_surface() -> dict:
    tree = _tree("observatory.py")
    commands = set()
    for name in ("WORKSPACE_COMMANDS", "OWN_PARSER_COMMANDS"):
        for value in _assigned(tree, name):
            commands |= set(ast.literal_eval(value))
    commands |= _dict_keys(tree, "STEPS") | _dict_keys(tree, "GROUPS")
    text = ast.literal_eval(_assigned(tree, "PUBLIC_HELP")[0])
    return {"commands": commands, "help": help_blocks(text)}


def served_tools() -> set[str]:
    """The tool names the MCP wire test pins as `tools/list` (GOLDEN_TOOLS)."""
    return set(ast.literal_eval(_assigned(_tree("tests/test_mcp_wire.py"), "GOLDEN_TOOLS")[0]))


def server_tool_functions() -> set[str]:
    """The functions `mcp/server.py` registers with `@server.tool(...)`."""
    names = set()
    for node in ast.walk(_tree("mcp/server.py")):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                call = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(call, ast.Attribute) and call.attr == "tool" and \
                        isinstance(call.value, ast.Name) and call.value.id == "server":
                    names.add(node.name)
    return names


def surface() -> dict:
    return {"dashboard": keyserver_actions(), **cli_surface(), "tools": served_tools()}


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[\s|\[\]()]+", text) if w}


def _found_in_source(where: str, word: str) -> bool:
    """WORD as a quoted string literal in the file `where` names (`path: what`)."""
    path = ENGINE / where.split(":", 1)[0].strip()
    if not path.is_file():
        return False
    source = path.read_text(encoding="utf-8")
    return f'"{word}"' in source or f"'{word}'" in source


def problems(parity: dict, found: dict) -> list[str]:
    """Every way PARITY disagrees with the code's surfaces FOUND; empty when they agree."""
    out: list[str] = []
    entries = parity.get("actions")
    if not isinstance(entries, list):
        return ["parity.json has no actions list"]
    seen: set[tuple[str, str]] = set()
    named_commands: set[str] = set()
    for entry in entries:
        action, kind = entry.get("action"), entry.get("kind")
        label = f"{kind} {action!r}"
        if kind not in KINDS or not isinstance(action, str) or not action:
            out.append(f"{label}: kind must be one of {KINDS} and action a non-empty string")
            continue
        if (kind, action) in seen:
            out.append(f"{label}: listed twice")
        seen.add((kind, action))
        where = entry.get("where")
        if not isinstance(where, str) or not (ENGINE / where.split(":", 1)[0].strip()).is_file():
            out.append(f"{label}: where {where!r} names no engine file")
        if kind == "dashboard" and action not in found["dashboard"]:
            out.append(f"{label}: not an action of keyserver.ACTIONS")
        if kind == "cli":
            command, *rest = action.split()
            named_commands.add(command)
            if command not in found["commands"]:
                out.append(f"{label}: {command!r} is not a command of observatory.py")
            vocabulary = _words(found["help"].get(command, ""))
            for word in rest:
                if word not in vocabulary and not _found_in_source(where or "", word):
                    out.append(f"{label}: {word!r} is in neither the help of {command!r} nor {where!r}")
        status, tool, gate, row = entry.get("status"), entry.get("tool"), entry.get("gate"), entry.get("row")
        tools = [tool] if isinstance(tool, str) else list(tool or [])
        for name in tools:
            if name not in found["tools"]:
                out.append(f"{label}: tool {name!r} is not a registered MCP tool")
        if status not in STATUSES:
            out.append(f"{label}: status {status!r} is not one of {STATUSES}")
        elif status == "covered":
            if not tools:
                out.append(f"{label}: covered but names no tool")
            if gate is not None:
                out.append(f"{label}: covered and gated at once")
        elif status == "gate":
            if not isinstance(gate, dict) or not str(gate.get("reason") or "").strip():
                out.append(f"{label}: a gate without its reason")
            if tools:
                out.append(f"{label}: a gate that names a tool is covered, not gated")
        elif status == "missing":
            if not isinstance(row, str) or not ROW.fullmatch(row):
                out.append(f"{label}: missing without the board row that owes its tool")
            if tools or gate is not None:
                out.append(f"{label}: missing but names a tool or a gate")
    for action in sorted(found["dashboard"] - {a for k, a in seen if k == "dashboard"}):
        out.append(f"dashboard {action!r}: a keyserver action with no parity entry")
    excluded: set[str] = set()
    for group in parity.get("cli_excluded", []):
        if not str(group.get("reason") or "").strip():
            out.append(f"cli_excluded {group.get('commands')}: an exclusion without its reason")
        for command in group.get("commands", []):
            if command not in found["commands"]:
                out.append(f"cli_excluded {command!r}: not a command of observatory.py any more")
            if command in named_commands:
                out.append(f"cli_excluded {command!r}: excluded and listed as an action at once")
            excluded.add(command)
    prefixes = []
    for group in parity.get("cli_excluded_prefixes", []):
        if not str(group.get("reason") or "").strip() or not group.get("prefix"):
            out.append(f"cli_excluded_prefixes {group.get('prefix')!r}: a prefix without its reason")
        prefixes.append(group.get("prefix") or "\0")
    for command in sorted(found["commands"] - named_commands - excluded):
        if not any(command.startswith(p) for p in prefixes):
            out.append(f"cli {command!r}: a command that is neither an action nor excluded with its reason")
    return out


def load() -> dict:
    return json.loads(PARITY.read_text(encoding="utf-8"))


def counts(parity: dict) -> dict[str, int]:
    tally = dict.fromkeys(STATUSES, 0)
    for entry in parity["actions"]:
        tally[entry["status"]] += 1
    return tally


class SurfaceReadTests(unittest.TestCase):
    """The readers find the surfaces they claim to read."""

    def test_keyserver_actions_are_read(self):
        self.assertIn("mint", keyserver_actions())
        self.assertIn("reveal", keyserver_actions())

    def test_cli_commands_and_help_are_read(self):
        found = cli_surface()
        for command in ("workflow", "cleanup", "update", "scan-mcp", "check-portable", "local"):
            self.assertIn(command, found["commands"])
        self.assertIn("handoff", _words(found["help"]["workflow"]))
        self.assertIn("--apply", _words(found["help"]["cleanup"]))
        self.assertIn("configure", found["help"])          # a head of the form "init / onboard / configure"

    def test_the_tool_list_read_is_the_served_list(self):
        tools = served_tools()
        self.assertIn("machine.mcp.refresh", tools)
        missing = server_tool_functions() - tools
        self.assertEqual(missing, set(), "a @server.tool function is absent from GOLDEN_TOOLS: the list read is stale")


class ParityListTests(unittest.TestCase):
    def test_the_list_agrees_with_the_code(self):
        self.assertEqual(problems(load(), surface()), [])

    def test_the_design_note_states_the_counts(self):
        tally = counts(load())
        note = (PARITY.parent / "parity.md").read_text(encoding="utf-8")
        for status, number in tally.items():
            self.assertIn(f"| {status} | {number} |", note, f"parity.md must state {number} {status}")


class ParityFailureModeTests(unittest.TestCase):
    """Each failure mode is proved both ways: the list passes, the modified copy fails."""

    @classmethod
    def setUpClass(cls):
        cls.found = surface()
        cls.parity = load()

    def mutated(self, change) -> list[str]:
        self.assertEqual(problems(self.parity, self.found), [])
        twin = copy.deepcopy(self.parity)
        change(twin)
        return problems(twin, self.found)

    def first(self, twin: dict, **match) -> dict:
        return next(e for e in twin["actions"] if all(e.get(k) == v for k, v in match.items()))

    def test_a_keyserver_action_without_an_entry_fails(self):
        def drop(twin):
            twin["actions"] = [e for e in twin["actions"] if (e["kind"], e["action"]) != ("dashboard", "mint")]
        self.assertTrue(any("'mint': a keyserver action with no parity entry" in p for p in self.mutated(drop)))

    def test_a_new_keyserver_action_fails_until_listed(self):
        found = dict(self.found, dashboard=self.found["dashboard"] | {"purge"})
        self.assertTrue(any("'purge'" in p for p in problems(self.parity, found)))

    def test_a_cli_command_without_an_entry_fails(self):
        found = dict(self.found, commands=self.found["commands"] | {"wipe-everything"})
        self.assertIn("cli 'wipe-everything': a command that is neither an action nor excluded with its reason",
                      problems(self.parity, found))

    def test_a_dropped_cli_action_fails(self):
        def drop(twin):
            twin["actions"] = [e for e in twin["actions"] if not e["action"].startswith("cleanup")]
        self.assertTrue(any("cli 'cleanup'" in p for p in self.mutated(drop)))

    def test_a_verb_the_cli_does_not_have_fails(self):
        def rename(twin):
            twin["actions"].append({"action": "cleanup --obliterate", "kind": "cli", "where": "tools/workspace.py: cleanup",
                                    "tool": None, "gate": None, "status": "missing", "row": "OBS-559"})
        self.assertTrue(any("'--obliterate'" in p for p in self.mutated(rename)))

    def test_an_unregistered_tool_fails(self):
        def rename(twin):
            self.first(twin, status="covered")["tool"] = "observatory_no_such_tool"
        self.assertTrue(any("'observatory_no_such_tool' is not a registered MCP tool" in p for p in self.mutated(rename)))

    def test_a_covered_entry_without_a_tool_fails(self):
        def strip(twin):
            self.first(twin, status="covered")["tool"] = None
        self.assertTrue(any("covered but names no tool" in p for p in self.mutated(strip)))

    def test_a_gate_without_a_reason_fails(self):
        def strip(twin):
            self.first(twin, status="gate")["gate"] = {"reason": " "}
        self.assertTrue(any("a gate without its reason" in p for p in self.mutated(strip)))

    def test_a_missing_entry_without_a_row_fails(self):
        def strip(twin):
            self.first(twin, status="missing")["row"] = None
        self.assertTrue(any("missing without the board row" in p for p in self.mutated(strip)))

    def test_an_exclusion_without_a_reason_fails(self):
        def strip(twin):
            twin["cli_excluded"][0]["reason"] = ""
        self.assertTrue(any("an exclusion without its reason" in p for p in self.mutated(strip)))

    def test_an_exclusion_of_a_removed_command_fails(self):
        def stale(twin):
            twin["cli_excluded"][0]["commands"].append("long-gone")
        self.assertTrue(any("'long-gone': not a command of observatory.py" in p for p in self.mutated(stale)))


if __name__ == "__main__":
    unittest.main()
