#!/usr/bin/env python3
"""The one credential-shape heuristic, held in BOTH directions.

A heuristic that refuses too little lets a pasted key become a project name
that five dashboard pages print; one that refuses too much makes the vault
refuse `alpha-web`. Both lists below are the contract. Every key-shaped value
is composed at runtime from obviously fake parts, so no literal here looks like
a real provider's key to a scanner.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import credential_shape as cs  # noqa: E402

FAKE = "FAKE" * 4


def fakes() -> dict[str, str]:
    """Credential-shaped synthetic values, one per kind the module names."""
    mixed = "Ab3" * 9                                  # 27 chars, three classes
    return {
        "openrouter": "sk-or-v1-" + FAKE * 4,
        "openai-like": "sk-" + "Zq7" * 12,
        "github": "ghp_" + "Fk9" * 12,
        "github-fine": "github_pat_" + "Fk9" * 10,
        "slack": "xoxb-" + "1234-" + "Fk9" * 8,
        "gitlab": "glpat-" + "Fk9x" * 5,
        "google": "AIza" + "Fk9" * 12,
        "aws": "AKIA" + "FAKE" * 3 + "0000",
        "jwt": "eyJ" + "hbGci" * 3 + "." + "eyJzdWIi" * 2 + "." + "c2ln" * 4,
        "pem": "-----" + "BEGIN PRIVATE KEY-----",
        "url-password": "postgres://user:" + "s3cret" + "@db.example.invalid/app",
        "uuid": "-".join(("0f" * 4, "1a2b", "3c4d", "5e6f", "7a" * 6)),
        "hex32": "0123456789abcdef" * 2,
        "token-as-project": "Fake" + "Proj" + "_" + mixed,
        "token-in-sentence": "pasted " + "fake" + "_key_" + mixed + " here",
        "base64-40": ("Ab3+" * 10) + "==",
        "single-case-run": "abcd1234" * 3 + "ef",
        "long-unbroken-run": "xab" * 16,
    }


ORDINARY = (
    "alpha-web", "beta-api", "local-alpha-web", "project:local-alpha-web",
    "OPENROUTER_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "CF_API_TOKEN",
    "credential:vault/alpha-web/prod/CF_API_TOKEN", "credential:openrouter/project-alpha-tick",
    "2026-10-03", "2026-10-03T02:50:40Z", "2026-10-03T02:50:40.550185+00:00",
    "/srv/example-ws/alpha-web/config/settings.py", "/srv/example-ws/Alpha2Web/docs/Notes.md",
    "https://ci.example.invalid/jobs/12345/logs", "commit abc123def456 in alpha-web",
    "sk-or-v1-abc...def", "sk-or-v1-999...zzz", "PRODUCTION_user_9", "h-observatory",
    "agent:claude-code", "events-2026-10-03T02:37:42Z-2f16b5", "mem:95f4ea31621c4e84",
    "npm_config_cache_directory", "task-1234567890-follow-up", "observatory-alpha-web",
    "a traceback in a transcript of session 2026-10-03", "DNS edits for alpha-web",
    "rotated in the provider dashboard, deployment 2026-09-14T01:00Z",
    "GOOGLEAPPLICATIONCREDENTIALSFILE",
)


class CredentialShapeTests(unittest.TestCase):
    def test_every_fake_credential_shape_is_found(self):
        for label, value in fakes().items():
            with self.subTest(label=label):
                self.assertIsNotNone(cs.find(value), label)
                self.assertTrue(cs.shaped(value), label)

    def test_ordinary_identifiers_dates_and_paths_are_not(self):
        for value in ORDINARY:
            with self.subTest(value=value):
                self.assertIsNone(cs.find(value), f"{value!r} read as {cs.find(value)}")

    def test_a_uuid_may_be_allowed_where_it_is_the_documented_shape(self):
        uuid = fakes()["uuid"]
        self.assertEqual(cs.find(uuid), "uuid")
        self.assertIsNone(cs.find(uuid, uuids=False))

    def test_redact_removes_the_value_and_keeps_the_sentence(self):
        for label, value in fakes().items():
            with self.subTest(label=label):
                text = f"seen at the CI log: {value} (job 12)"
                out = cs.redact(text)
                self.assertNotIn(value, out)
                self.assertIn("[redacted]", out)
                self.assertTrue(out.startswith("seen at the CI log: "))
        for value in ORDINARY:
            self.assertEqual(cs.redact(value), value)

    def test_echo_quotes_identifiers_and_withholds_everything_else(self):
        self.assertEqual(cs.echo("project:nosuch"), "'project:nosuch'")
        self.assertEqual(cs.echo("alpha-web"), "'alpha-web'")
        for label, value in fakes().items():
            with self.subTest(label=label):
                said = cs.echo(value)
                self.assertNotIn(value, said)
                self.assertIn(f"{len(value)} characters", said)
        self.assertIn("characters, not shown", cs.echo("../../secrets"))
        self.assertIn("characters, not shown", cs.echo("two words"))
        self.assertIn("not shown", cs.echo(5))
        import re
        self.assertIn("not shown", cs.echo("project:x", pattern=re.compile(r"chat-[0-9a-f]{32}")))

    def test_redaction_is_linear_in_its_input(self):
        """A 2 MB statement to `observatory_record` took about an hour: from every
        position, an unbounded URL scheme ran to the end of the input looking for
        `://`, and a run of `eyJ` did the same looking for a JWT's dot. Quadratic in
        the input is a denial of service on the one tool every agent may call."""
        import time
        for text in ("x" * 1_000_000, "eyJ" * 330_000, "see a://u:" + "y" * 1_000_000,
                     "Ab3-" * 250_000):
            started = time.monotonic()
            cs.redact(text, marker="[r]")
            took = time.monotonic() - started
            self.assertLess(took, 10, f"{len(text)} characters of {text[:6]!r}… took {took:.1f} s")

    def test_bounded_shapes_still_catch_what_they_caught(self):
        secret = "Zq7" * 12
        for text in (f"clone https://user:{secret}@example.test/repo",
                     "x" * 100 + f"https://user:{secret}@example.test",
                     f"git+ssh://user:{secret}@example.test"):
            self.assertNotIn(secret, cs.redact(text, marker="[r]"), text[:40])
        jwt = "eyJ" + "hb3" * 6 + "." + "eyJ" + "zd2" * 6 + "." + "sig" * 6
        for text in (f"Bearer {jwt}", f"token={jwt}", f'{{"t":"{jwt}"}}'):
            self.assertNotIn(jwt, cs.redact(text, marker="[r]"), text[:20])

    def test_refuse_names_the_field_and_the_shape_never_the_value(self):
        value = fakes()["token-as-project"]
        with self.assertRaises(ValueError) as caught:
            cs.refuse("project", value)
        self.assertIn("project", str(caught.exception))
        self.assertNotIn(value, str(caught.exception))
        cs.refuse("project", "alpha-web")


if __name__ == "__main__":
    unittest.main()
