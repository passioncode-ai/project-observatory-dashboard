#!/usr/bin/env python3
"""OBS-595: every place that asks "is this shaped like a credential" asks `credential_shape`.

One table per call site pins the answers that site gave before the detectors were folded into the one rule.
Where the one rule answers differently, the difference is a defect of the old detector, named in `CHANGED`
beside the input with the answer it used to give. Every key-shaped value is composed at runtime from obviously
fake pieces, so no literal here looks like a real provider's key to a scanner.
"""
from __future__ import annotations

import io
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
for where in (ROOT, ROOT / "collectors", ROOT / "tools", ROOT / "agent"):
    sys.path.insert(0, str(where))

import credential_shape as cs  # noqa: E402

OR = "sk-" + "or-" + "v1-"          # the OpenRouter issuer, assembled
BODY = "0a1b2c3d" * 8               # 64 hex characters, obviously fake
MIXED = "Fk9" * 8                   # 24 characters, three classes


# ── collectors/scan_env.py: is a variable NAME itself credential-shaped? ──────────────────────────────────────
ENV_NAMES = {
    "OPENROUTER_API_KEY": False,
    "DATABASE_URL": False,
    "NEXT_PUBLIC_API_BASE_URL": False,
    "APP_ENV": False,
    "sk_" + "LIVE" + "FAKE" * 4: True,
    "pk_" + "test" + MIXED: True,
    "AK" + "IA" + "FAKE" * 3 + "0000": True,
    "AI" + "za" + MIXED: True,
    "gh" + "p_" + MIXED: True,
    "xo" + "xb" + MIXED: True,
    # The old pattern read `sk`, `pk` and `rk` inside a word: ordinary lower-case names were redacted.
    "task_runner_concurrency_limit": False,
    "risk_assessment_threshold": False,
    "desk_reservations_enabled": False,
    "work_queue": False,
    # Shapes the old pattern did not know: a GitHub fine-grained token, a Hugging Face token, 32 hex.
    "github" + "_pat_" + MIXED: True,
    "hf" + "_" + MIXED: True,
    "X_" + "0123456789abcdef" * 2: True,
}
#: input: (the old answer, why the one rule's answer is right)
CHANGED_ENV = {
    "task_runner_concurrency_limit": (True, "`sk` inside a word is no issuer prefix"),
    "risk_assessment_threshold": (True, "`sk` inside a word is no issuer prefix"),
    "desk_reservations_enabled": (True, "`sk` inside a word is no issuer prefix"),
    "github" + "_pat_" + MIXED: (False, "a GitHub fine-grained token went unredacted"),
    "hf" + "_" + MIXED: (False, "a Hugging Face token went unredacted"),
    "X_" + "0123456789abcdef" * 2: (False, "32 hexadecimal characters went unredacted"),
}


# ── service_connections.py: could a declared string be read as a credential? ─────────────────────────────────
DECLARED = {
    "github": False,
    "repositories of the workspaces you configured": False,
    "zones, Workers, Pages and storage of the accounts you configured": False,
    "task-runner": False,
    "https://example.com": True,
    "user@example.com": True,
    "a s" + "k-x": True,
    "gh" + "p_x": True,
    "xo" + "xb-1": True,
    "AK" + "IA": True,
    "glp" + "at-": True,
    "e" + "yJ": True,
    "A" * 40: True,
    "a/b" * 14: True,
    "mask-off": False,
    "risk-free": False,
    # The contract's reader refuses these; the old check, which models that reader, did not.
    "Bearer abc": True,
    "x_" + "s" + "k-Test0Example1Key2Value3": True,
}
CHANGED_DECLARED = {
    "Bearer abc": (False, "the contract refuses a value that starts with `Bearer `"),
    "x_" + "s" + "k-Test0Example1Key2Value3": (False, "the contract reads a key after `_`; `\\b` did not"),
}


# ── the OpenRouter issuer: scan_openrouter.label_of, revoke_key, providers._shape_ok ──────────────────────────
OR_KEY = OR + BODY
OR_INPUTS = {
    "or": OR_KEY,
    "or-padded": "  " + OR_KEY + "\n",
    "or-short": OR + "abc",
    "or-v2": "sk-" + "or-" + "v2-" + BODY,
    "or-upper": OR.upper() + BODY,
    "proj": "sk-" + "proj-" + BODY,
    "plain-sk": "sk-" + BODY,
    "empty": "",
}
LABELS = {
    "or": OR + BODY[:3] + "..." + BODY[-3:],
    "or-padded": OR + BODY[:3] + "..." + BODY[-3:],
    "or-short": "",
    "or-v2": "",
    "or-upper": "",
    "proj": "",
    "plain-sk": "",
    "empty": "",
}
#: (OPENROUTER_API_KEY verdict, OPENAI_API_KEY verdict): None is accepted, a word names the refusal.
SHAPES = {
    "or": (None, "starts"),
    "or-padded": ("not", "not"),
    "or-short": (None, "starts"),
    "or-v2": ("not", "starts"),
    "or-upper": ("not", "not"),
    "proj": ("not", None),
    "plain-sk": ("not", None),
}
#: Does revoke_key store the value it was given on stdin?
STORES = {"or": True, "or-padded": True, "or-short": True, "or-v2": False, "or-upper": False,
          "proj": False, "plain-sk": False}


# ── tools/check_secrets.py: the commit gate's issuer grammar ──────────────────────────────────────────────────
GATE_TOKENS = {
    OR + BODY: (OR, "refused"),
    "sk-" + "proj-" + BODY: ("sk-" + "proj-", "refused"),
    "sk-" + "ant-" + "api03-" + BODY: ("sk-" + "ant-" + "api03-", "refused"),
    "sk-" + BODY: ("sk-", "refused"),
    "gl" + "pat-" + MIXED[:20]: ("gl" + "pat-", "refused"),
    "gl" + "pat-" + MIXED[:19]: ("gl" + "pat-", None),
    "gh" + "p_" + "your_key_goes_here_please": ("gh" + "p_", "fixture"),
    "gh" + "p_" + "your_key_goes_here": ("gh" + "p_", None),
    "sk_" + "some_long_snake_case_name_here": ("sk_", "fixture"),
    "sk_" + "configurationvalue_settings": ("sk_", None),
    "alpha-web": (None, None),
}
GATE_TEXT = "\n".join((
    'key = "' + OR + BODY + '"',
    "id: " + "AK" + "IA" + "FAKE" * 3 + "0000",
    "id: " + "AK" + "IA" + "FAKE" * 3 + "0000x",
    "-----" + "BEGIN RSA PRIVATE KEY-----",
    "-----" + "BEGIN CERTIFICATE-----",
    'api_key = "' + MIXED + MIXED + '"',
    "plain words only",
))
GATE_HITS = [
    ("issuer prefix", OR, True),
    ("aws access key", "AKIA", True),
    ("private key block", None, None),
    ("named secret", None, False),
]


# ── the contract's `secretValue` (FAC-SEM-167, DEC-0151): ops_digest and folder_attribution ───────────────────
KEY = "s" + "k-" + "Test0Example1Key2Value3"
CONTRACT = {
    KEY: True,
    "x." + KEY: True,
    "x%22" + KEY: True,
    "x_" + KEY: True,
    "x" + KEY: False,
    "S" + "K-Test0Example1Key2Value3": False,
    "s" + "k-learn-experiments-2026": False,
    "e" + "yJabc.def.ghi": True,
    "Bearer x": True,
    "BEARER x": True,
    "Bearer": False,
    "bearer \n": False,
    "A" * 20 + "b" * 19 + "1": True,
    "a" * 64: False,
    "AK" + "IA" + "ABCDEFGHIJKLMNOP": True,
    "board.tick": False,
    # JavaScript reads `\s` and `.` with its own sets: U+FEFF is a space, U+001C and U+0085 are not, and
    # U+2028 is a line terminator.
    "Bearer﻿x": True,
    "Bearer\x1cx": False,
    "Bearer\x85x": False,
    "Bearer  ": False,
}
#: folder_attribution read `\s` and `.` the way Python does; the contract is JavaScript.
CHANGED_FOLDER = {
    "Bearer﻿x": (False, "U+FEFF is a space in JavaScript: a bearer credential was missed"),
    "Bearer\x1cx": (True, "U+001C is no space in JavaScript"),
    "Bearer\x85x": (True, "U+0085 is no space in JavaScript"),
    "Bearer  ": (True, "JavaScript's `.` does not match a line terminator"),
}


class EnvNames(unittest.TestCase):
    def test_every_name_answers_as_pinned(self):
        import scan_env
        for name, want in ENV_NAMES.items():
            self.assertEqual(scan_env.secret_shaped_name(name), want, name)

    def test_a_changed_answer_is_a_named_defect_of_the_old_pattern(self):
        import scan_env
        for name, (old, why) in CHANGED_ENV.items():
            self.assertNotEqual(ENV_NAMES[name], old, why)
            if hasattr(scan_env, "SECRET_SHAPED"):
                self.fail("the old pattern is still defined beside the one rule")

    def test_the_rule_is_credential_shape(self):
        import scan_env
        with mock.patch.object(cs, "shaped", return_value=True) as asked:
            self.assertTrue(scan_env.secret_shaped_name("APP_ENV"))
        asked.assert_called_once()


class DeclaredStrings(unittest.TestCase):
    def test_every_string_answers_as_pinned(self):
        import service_connections
        for text, want in DECLARED.items():
            self.assertEqual(service_connections.credentialish(text), want, text)

    def test_a_changed_answer_is_a_named_defect(self):
        for text, (old, why) in CHANGED_DECLARED.items():
            self.assertNotEqual(DECLARED[text], old, why)

    def test_the_rule_is_credential_shape(self):
        import service_connections
        self.assertFalse(hasattr(service_connections, "_CREDENTIALISH"))
        with mock.patch.object(cs, "shaped", return_value=True) as asked:
            self.assertTrue(service_connections.credentialish("github"))
        self.assertEqual(asked.call_args.kwargs.get("reading"), cs.STRICT)


class OpenRouterIssuer(unittest.TestCase):
    def test_the_label_the_provider_shows(self):
        import scan_openrouter
        for name, value in OR_INPUTS.items():
            self.assertEqual(scan_openrouter.label_of(value), LABELS[name], name)

    def test_a_key_in_the_wrong_variable(self):
        import providers
        for name, (openrouter, openai) in SHAPES.items():
            value = OR_INPUTS[name]
            for env, want in (("OPENROUTER_API_KEY", openrouter), ("OPENAI_API_KEY", openai)):
                said = providers._shape_ok(env, value)
                if want is None:
                    self.assertIsNone(said, (name, env))
                else:
                    self.assertIsNotNone(said, (name, env))
                    self.assertIn(want, said, (name, env))
                    self.assertNotIn(BODY, said)

    def test_the_revoke_tool_stores_only_an_openrouter_key(self):
        import revoke_key
        for name, stores in STORES.items():
            written = []
            with mock.patch.object(revoke_key.osprivacy, "is_terminal", return_value=False), \
                    mock.patch.object(revoke_key.private_io, "write", lambda path, value: written.append(value)), \
                    mock.patch.object(revoke_key, "die", side_effect=SystemExit), \
                    mock.patch.object(sys, "stdin", io.StringIO(OR_INPUTS[name])), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                try:
                    revoke_key.load_or_store()
                except SystemExit:
                    pass
            self.assertEqual(bool(written), stores, name)
            self.assertNotIn(BODY, out.getvalue())

    def test_every_site_names_the_issuer_from_credential_shape(self):
        for rel in ("collectors/scan_openrouter.py", "tools/revoke_key.py", "agent/providers.py",
                    "tools/check_secrets.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertEqual(text.count('"sk-or-v1-"'), 0, rel)
            self.assertIn("credential_shape", text, rel)


class CommitGate(unittest.TestCase):
    def test_the_issuer_and_the_verdict_of_each_token(self):
        import check_secrets
        for raw, (issuer, verdict) in GATE_TOKENS.items():
            self.assertEqual(check_secrets.issuer_of(raw), issuer, raw[:12])
            judged = check_secrets.judge_token(raw)
            self.assertEqual(judged[0] if judged else None, verdict, raw[:12])

    def test_the_body_without_its_prefix(self):
        import check_secrets
        self.assertEqual(check_secrets.body_of(OR + BODY), BODY)
        self.assertEqual(check_secrets.body_of("AK" + "IA" + "FAKE" * 4), "FAKE" * 4)
        self.assertEqual(check_secrets.body_of("plain"), "plain")
        self.assertTrue(check_secrets.is_hex_secret(BODY[:32]))
        self.assertFalse(check_secrets.is_hex_secret(BODY[:31]))

    def test_the_hits_of_a_text(self):
        import check_secrets
        hits = check_secrets.scan_text(GATE_TEXT, "f")
        got = sorted((h["kind"], h.get("issuer"), h.get("credential")) for h in hits)
        self.assertEqual(got, sorted(GATE_HITS, key=lambda t: (t[0], t[1] or "", t[2] or False)))
        self.assertNotIn(BODY, repr(hits))

    def test_the_issuers_and_shapes_are_credential_shapes(self):
        import check_secrets
        self.assertIs(check_secrets.ISSUERS, cs.ISSUERS)
        self.assertIn(OR, cs.ISSUERS)
        self.assertEqual(cs.ISSUERS, tuple(sorted(cs.ISSUERS, key=len, reverse=True)))
        self.assertIs(check_secrets.PATTERNS[0][1], cs.PRIVATE_KEY_BLOCK)


class ContractReading(unittest.TestCase):
    def test_ops_digest_answers_as_pinned(self):
        import ops_digest
        for value, want in CONTRACT.items():
            self.assertEqual(ops_digest.secret_value(value), want, repr(value))

    def test_folder_attribution_answers_as_the_contract(self):
        import folder_attribution
        for value, want in CONTRACT.items():
            self.assertEqual(folder_attribution.secret_value(value), want, repr(value))
        for value, (old, why) in CHANGED_FOLDER.items():
            self.assertNotEqual(CONTRACT[value], old, why)

    def test_both_ports_are_the_one_rule_in_its_contract_reading(self):
        import folder_attribution
        import ops_digest
        for value, want in CONTRACT.items():
            self.assertEqual(cs.shaped(value, reading=cs.CONTRACT), want, repr(value))
        self.assertIs(ops_digest.SECRET_VALUE, cs.CONTRACT_SECRET_VALUE)
        with mock.patch.object(cs, "contract_secret", return_value=True) as asked:
            self.assertTrue(ops_digest.secret_value("board.tick"))
            self.assertTrue(folder_attribution.secret_value("board.tick"))
        self.assertEqual(asked.call_count, 2)


if __name__ == "__main__":
    unittest.main()
