#!/usr/bin/env python3
"""OBS-602: every place that redacts text for a record, a journal or an answer calls one redactor.

`memory_redact.redact` is that redactor (over `credential_shape`); `memory_redact.scrub` walks a JSON value with
it. One table per call site pins what the site writes for the same fake inputs. Where the one redactor writes
something else than the site did before, the input is named in the site's `CHANGED` table with the old output
and the reason the old one was a defect. Every key-shaped value is composed at runtime from obviously fake
pieces, so no literal here looks like a real provider's key to a scanner.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
for where in (ROOT, ROOT / "collectors", ROOT / "tools"):
    sys.path.insert(0, str(where))

import memory_redact  # noqa: E402

MIXED = "Fk9" * 8                                    # 24 characters, three classes
GH = "gh" + "p_" + MIXED                             # a GitHub-shaped token, assembled
HEX = "0a1b2c3d" * 8                                 # 64 hex characters, obviously fake
UUID = "123e4567-e89b-12d3-a456-426614174000"
WHO, PW = "alice", "pw" + "1"
M = "[redacted]"

PLAIN = "plain line"
URL_PW = "https://" + WHO + ":" + PW + "@idx.example.com/simple"
URL_USER = "https://" + WHO + "@idx.example.com/simple"
SSH_PW = "ssh://" + "git" + ":" + PW + "@h/x"
TOKEN = "token " + GH
A_UUID = "uuid " + UUID
A_HEX = "sha " + HEX
SESSION = "session:" + UUID
LONG = "x" * 400

# ── maintenance.redact_line: one output line of an automatic apply, bounded ─────────────────────────────────
LINE = {
    PLAIN: PLAIN,
    URL_PW: "https://" + M + "@idx.example.com/simple",
    URL_USER: "https://" + M + "@idx.example.com/simple",
    SSH_PW: M + "h/x",
    TOKEN: "token " + M,
    A_UUID: A_UUID,
    A_HEX: "sha " + M,
    SESSION: SESSION,
    LONG: M,
}
CHANGED_LINE = {
    URL_PW: ("https://***@idx.example.com/simple", "the URL's user is replaced by the one marker"),
    URL_USER: ("https://***@idx.example.com/simple", "the URL's user is replaced by the one marker"),
}

# ── engine_update.redact_tail: the end of an installer's output ─────────────────────────────────────────────
TAIL = {
    PLAIN: PLAIN,
    URL_PW: "https://" + M + "@idx.example.com/simple",
    URL_USER: "https://" + M + "@idx.example.com/simple",
    SSH_PW: M + "h/x",
    TOKEN: "token " + M,
    A_UUID: A_UUID,
    A_HEX: "sha " + M,
    SESSION: SESSION,
    LONG: M,
}
CHANGED_TAIL = {
    URL_PW: ("https://***@idx.example.com/simple", "the URL's user is replaced by the one marker"),
    URL_USER: ("https://***@idx.example.com/simple", "the URL's user is replaced by the one marker"),
    SSH_PW: (SSH_PW, "a password in a URL of another scheme reached the report"),
    TOKEN: (TOKEN, "a provider token in an installer's output reached the report"),
    A_HEX: (A_HEX, "a 64-character hexadecimal run reached the report"),
    LONG: (LONG[-1200:], "a long random-looking run reached the report"),
}

# ── ops_acts journal and result (`_scrub`): every string of a JSON value ────────────────────────────────────
ACTS = {
    PLAIN: PLAIN,
    URL_PW: "https://" + M + "@idx.example.com/simple",
    URL_USER: "https://" + M + "@idx.example.com/simple",
    SSH_PW: M + "h/x",
    TOKEN: "token " + M,
    A_UUID: "uuid " + M,
    A_HEX: "sha " + M,
    SESSION: SESSION,
    LONG: M,
}
CHANGED_ACTS = {
    URL_PW: (M + "idx.example.com/simple", "the scheme and the host stay readable, the user goes"),
    URL_USER: (URL_USER, "the user of a URL (a token, in git's form for one) reached the journal"),
    SESSION: ("session:" + M, "a session reference carries no value and is kept, as memory keeps it"),
}

# ── retrieval_audit.scrub_text: text leaving agent memory (known values and shapes) ─────────────────────────
MEMORY = {
    PLAIN: PLAIN,
    URL_PW: "https://" + M + "@idx.example.com/simple",
    URL_USER: "https://" + M + "@idx.example.com/simple",
    SSH_PW: M + "h/x",
    TOKEN: "token " + M,
    A_UUID: "uuid " + M,
    A_HEX: "sha " + M,
    SESSION: SESSION,
    LONG: M,
}
CHANGED_MEMORY = {
    URL_PW: (M + "idx.example.com/simple", "the scheme and the host stay readable, the user goes"),
    URL_USER: (URL_USER, "the user of a URL (a token, in git's form for one) was served back"),
}

# ── collectors/scan_lifecycle.redact: (executable, script) of a command line ─────────────────────────────────
COMMANDS = {
    "/usr/bin/python3 /opt/x/tool.py --api-key=abc123 more": ("/usr/bin/python3", "/opt/x/tool.py"),
    "/usr/local/bin/node --token sekrit /opt/y/server.js": ("/usr/local/bin/node", ""),
    "/usr/bin/python3 -m alpha_web.server": ("/usr/bin/python3", "alpha_web.server"),
    "/usr/bin/python3 /opt/keys/" + GH + "/run.py": ("/usr/bin/python3", "/opt/keys/" + M + "/run.py"),
    "/opt/" + HEX + "/bin/node server.js": ("/opt/" + M + "/bin/node", "server.js"),
    "/opt/sim/Devices/" + UUID + "/bin/app run.sh": ("/opt/sim/Devices/" + UUID + "/bin/app", "run.sh"),
    "": ("", ""),
}
CHANGED_COMMANDS = {
    "/usr/bin/python3 /opt/keys/" + GH + "/run.py": (("/usr/bin/python3", "/opt/keys/" + GH + "/run.py"),
                                                    "a token in a script path reached the process document"),
    "/opt/" + HEX + "/bin/node server.js": (("/opt/" + HEX + "/bin/node", "server.js"),
                                            "a hexadecimal run in an executable path reached the document"),
}


def _memory_scrub(text: str) -> str:
    """`retrieval_audit.scrub_text` with no known values (the vault is not read in a test)."""
    return memory_redact.Redactor(known_loader=dict).scrub(text)[0]


class Sites(unittest.TestCase):
    def check(self, table: dict, changed: dict, fn) -> None:
        for given, want in table.items():
            self.assertEqual(fn(given), want, repr(given[:60]))
        for given, (old, why) in changed.items():
            self.assertNotEqual(fn(given), old, f"{given[:60]!r}: {why}")
            self.assertTrue(why)

    def test_maintenance_lines(self):
        import maintenance
        self.check(LINE, CHANGED_LINE, maintenance.redact_line)
        self.assertLessEqual(len(maintenance.redact_line("word " * 200)), maintenance.OUTPUT_LINE_CHARS)

    def test_installer_tails(self):
        import engine_update
        self.check(TAIL, CHANGED_TAIL, engine_update.redact_tail)
        self.assertEqual(engine_update.redact_tail("a" + " b" * 100, 10), "b b b b b")

    def test_ops_acts_values(self):
        import ops_acts
        self.check(ACTS, CHANGED_ACTS, ops_acts._scrub)
        nested = {"k": [URL_PW, {"t": TOKEN}], "n": 3, "b": None}
        self.assertEqual(ops_acts._scrub(nested), {"k": [ACTS[URL_PW], {"t": ACTS[TOKEN]}], "n": 3, "b": None})

    def test_memory_text(self):
        self.check(MEMORY, CHANGED_MEMORY, _memory_scrub)

    def test_lifecycle_command_lines(self):
        import scan_lifecycle
        self.check(COMMANDS, CHANGED_COMMANDS, scan_lifecycle.redact)


class OneRedactor(unittest.TestCase):
    """Each site asks `memory_redact.redact`; none keeps a pattern of its own."""

    def asks(self, call) -> None:
        with mock.patch.object(memory_redact, "redact", wraps=memory_redact.redact) as one:
            call()
        self.assertTrue(one.called, "the site did not ask memory_redact.redact")

    def test_every_site_asks_the_one_redactor(self):
        import engine_update
        import maintenance
        import ops_acts
        import scan_lifecycle
        self.asks(lambda: maintenance.redact_line(TOKEN))
        self.asks(lambda: engine_update.redact_tail(TOKEN))
        self.asks(lambda: ops_acts._scrub({"t": TOKEN}))
        self.asks(lambda: _memory_scrub(TOKEN))
        self.asks(lambda: scan_lifecycle.redact("/usr/bin/python3 /opt/x/tool.py"))

    def test_no_site_keeps_its_own_url_pattern(self):
        for name in ("maintenance.py", "engine_update.py", "ops_acts.py", "retrieval_audit.py",
                     "collectors/scan_lifecycle.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("(https?://)", text, name)
            self.assertNotIn("credential_shape.redact(", text, name)

    def test_credential_like_is_the_shape_rule(self):
        import credential_shape
        with mock.patch.object(credential_shape, "shaped", return_value=True) as shaped:
            self.assertTrue(memory_redact.credential_like("anything"))
        shaped.assert_called_once_with("anything")

    def test_redact_is_idempotent(self):
        for given in (URL_PW, URL_USER, SSH_PW, TOKEN, A_UUID, A_HEX, SESSION, LONG):
            once = memory_redact.redact(given)
            self.assertEqual(memory_redact.redact(once), once, repr(given[:60]))

    def test_scrub_walks_json_and_keeps_other_values(self):
        value = {"a": [TOKEN, 1, True, None], "b": {"c": URL_USER}}
        self.assertEqual(memory_redact.scrub(value),
                         {"a": ["token " + M, 1, True, None], "b": {"c": "https://" + M + "@idx.example.com/simple"}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
