#!/usr/bin/env python3
"""The header door serves a vault value to the one MCP server it is bound to.

`use_secret.py header` exists for Claude Code's `headersHelper`: a command
Claude Code runs on each new connection to an HTTP MCP server, whose stdout (a
JSON object of headers) goes into the request and not into the model's context.
That makes it the one door here that PRINTS a value, so everything below is
about the ways the printed value could end up somewhere else:

  * a person or an agent running it in a terminal (stdout is a TTY);
  * an agent piping it to `cat` (no `CLAUDE_CODE_MCP_SERVER_URL`, or one that
    names a different server than the slot is bound to);
  * a slot nobody bound, a value from an env file or the environment, a header
    name or scheme that would split the header, a value carrying CR or LF.

Every case is driven through the real command in a subprocess with synthetic
names (`alpha-web`, `*.example.invalid`) and a composed fake value, and asserts
on the bytes that came back, the audit journal and the vault's own files.
"""
from __future__ import annotations
import json
import os
import pathlib
import pty
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
USE = ROOT / "tools" / "use_secret.py"
VAULT = ROOT / "tools" / "vault.py"

#: Composed, so no literal in this file has a credential's shape.
VALUE = "obs" + "fake" + "Q7m2Xz9R" + "4tLbK8wN" + "1vPc5jD6"
ENV_FILE_VALUE = "env" + "file" + "H5jD6sAu" + "9f3Qm2Xz" + "R7tLbK4w"
SHELL_VALUE = "shell" + "only" + "N8vPc1Ye" + "H5jD6sAu"
SERVER = "https://mcp.example.invalid/v1/mcp"


class HeaderDoorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="observatory-header-door-")
        self.root = pathlib.Path(self.temp.name).resolve()
        data = self.root / "estate"
        (data / "alpha-web").mkdir(parents=True)
        # The same NAME in the project's env file, with a different value: the
        # door must never serve it.
        (data / "alpha-web" / ".env.production").write_text(
            f"ALPHA_TOKEN={ENV_FILE_VALUE}\n", encoding="utf-8")
        scratch = self.root / "scratch"
        scratch.mkdir()
        self.store = self.root / "vault"
        self.state = self.root / "state"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("OBSERVATORY_", "CLAUDE_CODE_MCP_"))}
        self.env.update({"OBSERVATORY_HOME": str(self.root / "home"),
                         "OBSERVATORY_DATA": str(data),
                         "OBSERVATORY_SCRATCH": str(scratch),
                         "OBSERVATORY_STATE": str(self.state),
                         "OBSERVATORY_VAULT_DIR": str(self.store)})
        subprocess.run([sys.executable, str(ROOT / "tools/runtime_identity.py"), "init",
                        "env-fingerprint-salt"], env=self.env, capture_output=True, text=True,
                       check=True, timeout=30)
        subprocess.run([sys.executable, str(ROOT / "collectors/scan_env.py"),
                        str(scratch / "env.json")], env=self.env, capture_output=True,
                       text=True, check=True, timeout=60)

    def tearDown(self):
        self.temp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────

    def vault(self, *args, value=None):
        return subprocess.run([sys.executable, str(VAULT), *args], input=value, text=True,
                              capture_output=True, env=self.env, timeout=60)

    def put(self, value=VALUE, env="prod", name="ALPHA_TOKEN"):
        p = self.vault("put", "alpha-web", env, name, value=value)
        self.assertEqual(p.returncode, 0, p.stderr)

    def bind(self, target="https://mcp.example.invalid", env="prod", name="ALPHA_TOKEN"):
        return self.vault("bind", "alpha-web", env, name, "--header-for", target)

    def header(self, *args, url=SERVER, server_name="example-mcp", extra_env=None):
        env = dict(self.env)
        if url is not None:
            env["CLAUDE_CODE_MCP_SERVER_URL"] = url
        if server_name is not None:
            env["CLAUDE_CODE_MCP_SERVER_NAME"] = server_name
        env.update(extra_env or {})
        argv = list(args) or ["--env", "prod", "alpha-web", "ALPHA_TOKEN"]
        return subprocess.run([sys.executable, str(USE), "header", *argv], text=True,
                              capture_output=True, env=env, timeout=60)

    def ready(self):
        self.put()
        b = self.bind()
        self.assertEqual(b.returncode, 0, b.stderr)

    def audit_rows(self):
        path = self.state / "logs" / "secret-use.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def meta(self, env="prod", name="ALPHA_TOKEN"):
        return json.loads((self.store / "alpha-web" / env / f"{name}.meta.json")
                          .read_text(encoding="utf-8"))

    def assertRefused(self, p, *phrases):
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout, "", "a refusal prints nothing on stdout")
        for leaked in (VALUE, ENV_FILE_VALUE, SHELL_VALUE):
            self.assertNotIn(leaked, p.stderr)
        for phrase in phrases:
            self.assertIn(phrase, p.stderr)

    # ── the served case ──────────────────────────────────────────────────

    def test_success_prints_exactly_one_json_object(self):
        self.ready()
        p = self.header()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, json.dumps({"Authorization": f"Bearer {VALUE}"}) + "\n")
        self.assertEqual(json.loads(p.stdout), {"Authorization": f"Bearer {VALUE}"})
        self.assertNotIn(VALUE, p.stderr)

    def test_name_and_scheme_are_chosen_and_an_empty_scheme_is_the_bare_value(self):
        self.ready()
        p = self.header("--env", "prod", "--name", "X-Api-Key", "--scheme", "",
                        "alpha-web", "ALPHA_TOKEN")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, json.dumps({"X-Api-Key": VALUE}) + "\n")
        p = self.header("--env", "prod", "--scheme", "Token", "alpha-web", "ALPHA_TOKEN")
        self.assertEqual(json.loads(p.stdout), {"Authorization": f"Token {VALUE}"})

    def test_the_vault_value_is_served_not_the_env_file_copy(self):
        self.ready()
        p = self.header()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(ENV_FILE_VALUE, p.stdout + p.stderr)

    # ── the refusals ─────────────────────────────────────────────────────

    def test_a_terminal_is_refused(self):
        self.ready()
        env = dict(self.env, CLAUDE_CODE_MCP_SERVER_URL=SERVER)
        leader, follower = pty.openpty()
        try:
            p = subprocess.run([sys.executable, str(USE), "header", "--env", "prod",
                                "alpha-web", "ALPHA_TOKEN"], stdout=follower,
                               stderr=subprocess.PIPE, text=True, env=env, timeout=60)
            os.close(follower)
            follower = -1
            printed = b""
            while True:
                try:
                    chunk = os.read(leader, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                printed += chunk
        finally:
            if follower != -1:
                os.close(follower)
            os.close(leader)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("the reader must be a program", p.stderr)
        self.assertNotIn(VALUE.encode(), printed)
        self.assertNotIn(VALUE, p.stderr)

    def test_no_server_url_is_refused(self):
        self.ready()
        self.assertRefused(self.header(url=None), "CLAUDE_CODE_MCP_SERVER_URL")
        self.assertRefused(self.header(url=""), "CLAUDE_CODE_MCP_SERVER_URL")

    def test_a_server_other_than_the_binding_is_refused_and_named(self):
        self.ready()
        p = self.header(url="https://other.example.invalid/v1/mcp")
        self.assertRefused(p, "other.example.invalid", "mcp.example.invalid")
        # The scheme is part of the binding: plain http to the same host is
        # another server as far as a bearer is concerned.
        self.assertRefused(self.header(url="http://mcp.example.invalid/v1/mcp"),
                           "http://mcp.example.invalid")
        # So is the port.
        self.assertRefused(self.header(url="https://mcp.example.invalid:8443/v1/mcp"),
                           "mcp.example.invalid:8443")
        # The default port spelled out is the same server.
        ok = self.header(url="https://MCP.example.invalid:443/other/path")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_an_unbound_slot_is_refused_with_the_bind_command(self):
        self.put()
        self.assertRefused(self.header(), "vault.py\" bind", "--header-for")

    def test_an_env_file_value_is_not_served(self):
        # No vault slot at all: the env file holds the name, and the door
        # serves the vault only.
        p = self.header()
        self.assertRefused(p, "vault")
        self.assertNotIn(ENV_FILE_VALUE, p.stdout)

    def test_a_value_in_the_environment_is_not_served(self):
        p = self.header(extra_env={"ALPHA_TOKEN": SHELL_VALUE})
        self.assertRefused(p, "vault")
        self.assertNotIn(SHELL_VALUE, p.stdout)

    def test_a_header_name_or_scheme_that_is_not_a_token_is_refused(self):
        self.ready()
        for args in (["--name", "Author ization"], ["--name", "X-Key:"], ["--name", ""],
                     ["--scheme", "Bearer\r\nX-Evil: 1"], ["--scheme", "Be arer"],
                     ["--name", "X-Ключ"]):
            with self.subTest(args=args):
                self.assertRefused(self.header("--env", "prod", *args, "alpha-web",
                                               "ALPHA_TOKEN"), "RFC 7230 token")

    def test_a_value_with_a_control_character_is_refused(self):
        self.ready()
        slot = self.store / "alpha-web" / "prod" / "ALPHA_TOKEN"
        for bad in (VALUE[:10] + "\r" + VALUE[10:], VALUE[:10] + "\n" + VALUE[10:],
                    VALUE[:10] + "\x1b" + VALUE[10:], VALUE[:10] + "\t" + VALUE[10:]):
            with self.subTest(char=repr(bad[10])):
                # Written past `vault.py put`, which already refuses a newline: the
                # door must not rely on every writer of the slot having done so.
                slot.write_bytes(bad.encode("utf-8"))
                slot.chmod(0o600)
                p = self.header()
                self.assertRefused(p, "control character")
                self.assertNotIn(VALUE[:10], p.stderr)

    # ── what is recorded ─────────────────────────────────────────────────

    def test_every_call_is_audited_and_the_audit_never_holds_the_value(self):
        self.ready()
        self.assertEqual(self.header().returncode, 0)
        self.header(url="https://other.example.invalid/mcp")
        rows = [r for r in self.audit_rows() if r.get("action") == "header"]
        self.assertEqual(len(rows), 2, rows)
        served, refused = rows
        self.assertEqual(served["subject"], "alpha-web:ALPHA_TOKEN")
        self.assertEqual(served["server"], "example-mcp")
        self.assertEqual(served["host"], "mcp.example.invalid")
        self.assertTrue(served["parent"])
        self.assertEqual(served["verdict"], "served")
        self.assertEqual(refused["host"], "other.example.invalid")
        self.assertEqual(refused["verdict"], "refused")
        self.assertEqual(refused["reason"], "server-mismatch")
        journal = (self.state / "logs" / "secret-use.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(VALUE, journal)

    def test_the_value_never_enters_a_child_argv(self):
        """The door asks `ps` for its parent's name; the shim records what it was
        handed, and the value must not be in it."""
        self.ready()
        shim_dir = self.root / "shim"
        shim_dir.mkdir()
        log = self.root / "ps-argv.log"
        shim = shim_dir / "ps"
        shim.write_text("#!/bin/sh\n"
                        f"printf '%s\\n' \"$@\" >> '{log}'\n"
                        "echo synthetic-parent\n", encoding="utf-8")
        shim.chmod(0o700)
        p = self.header(extra_env={"PATH": f"{shim_dir}{os.pathsep}{self.env.get('PATH', '')}"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(log.is_file(), "the parent lookup ran")
        self.assertNotIn(VALUE, log.read_text(encoding="utf-8"))
        rows = [r for r in self.audit_rows() if r.get("action") == "header"]
        self.assertEqual(rows[-1]["parent"], "synthetic-parent")
        self.assertNotIn(VALUE, p.stderr)

    # ── the binding verb ─────────────────────────────────────────────────

    def test_bind_and_clear(self):
        self.put()
        b = self.bind("https://MCP.Example.Invalid:443/v1/mcp")
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertEqual(self.meta()["header_for"], "https://mcp.example.invalid")
        self.assertNotIn(VALUE, b.stdout + b.stderr)
        listing = self.vault("list", "alpha-web")
        self.assertIn("header for https://mcp.example.invalid", listing.stdout)
        moves = (self.store / "movements.jsonl").read_text(encoding="utf-8")
        events = [json.loads(line) for line in moves.splitlines()]
        self.assertEqual(events[-1]["event"], "bind")
        self.assertEqual(events[-1]["header_for"], "https://mcp.example.invalid")
        self.assertNotIn(VALUE, moves)
        # A rotation keeps the binding: it belongs to the slot, not the value.
        r = self.vault("rotate", "alpha-web", "prod", "ALPHA_TOKEN", value=VALUE + "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()["header_for"], "https://mcp.example.invalid")
        c = self.vault("bind", "alpha-web", "prod", "ALPHA_TOKEN", "--clear")
        self.assertEqual(c.returncode, 0, c.stderr)
        self.assertNotIn("header_for", self.meta())
        events = [json.loads(line) for line in
                  (self.store / "movements.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["event"], "unbind")
        self.assertRefused(self.header(), "--header-for")

    def test_a_bare_host_means_https_on_its_port(self):
        self.put()
        self.assertEqual(self.bind("mcp.example.invalid").returncode, 0)
        self.assertEqual(self.meta()["header_for"], "https://mcp.example.invalid")
        self.assertEqual(self.bind("mcp.example.invalid:8443").returncode, 0)
        self.assertEqual(self.meta()["header_for"], "https://mcp.example.invalid:8443")

    def test_a_binding_that_is_not_https_is_refused(self):
        self.put()
        for target in ("http://mcp.example.invalid", "ftp://mcp.example.invalid",
                       "https://" + "user:pw" + "@mcp.example.invalid", "https://mcp.example.invalid/?k=v",
                       "https://", "https://mcp example.invalid", "https://mcp.example.invalid:99999"):
            with self.subTest(target=target):
                p = self.bind(target)
                self.assertNotEqual(p.returncode, 0, p.stdout)
                self.assertNotIn("header_for", self.meta())

    def test_binding_a_slot_that_holds_nothing_is_refused(self):
        p = self.bind()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("put", p.stderr)
        self.assertFalse((self.store / "alpha-web" / "prod" / "ALPHA_TOKEN.meta.json").exists())

    def test_bind_needs_a_target_or_clear(self):
        self.put()
        p = self.vault("bind", "alpha-web", "prod", "ALPHA_TOKEN")
        self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
