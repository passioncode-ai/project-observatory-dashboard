#!/usr/bin/env python3
"""OBS-559 part 2: the key page's acts as MCP tools (`observatory_key_act`, `observatory_credential_mark`).

The key server's eight acts (mint, limit, disable, enable, rotate-key, revoke, leak, annotate) reach an agent through
`key_acts.py`, which calls the key server's own `act_*` functions, never a second implementation. Each test drives an
act through a stubbed provider door, a stubbed vault program or the real signer over a temporary file, and checks:
the same function ran with the same body; acts that spend or cannot be undone (mint, a raised ceiling, rotate, revoke)
need the operator's words in contract DEC-0121's shape; refusals are typed; the journal row names the agent and the
relayed decision; and a fake key value never appears in an answer, a refusal, the journal or the signer's file.
"""
from __future__ import annotations
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

FAKE_KEY = "sk-or-v1-" + "FAKE" * 16


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def words(action: str, target: str, **extra) -> dict:
    return {"by": "operator", "via": "relayed-instruction", "action": action, "target": target,
            "instruction": "do it", "at": now(), **extra}


class Door:
    """The OpenRouter door as the key server sees it: it holds a value and never returns it."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.value = FAKE_KEY
        self.rotate_error: Exception | None = None
        self.issued = {"alpha-web-key": {"limit_usd": 10, "account": "personal"}}

    def ledger(self):
        return {"issued": self.issued}

    def issue_key(self, name, limit, account, dest, project, reset):
        self.calls.append(("issue_key", name, limit, account, dest, project, reset))
        return {"name": name, "account": "personal", "label": "sk-or-v1-abc...xyz", "limit": limit,
                "limit_reset": reset, "delivered_to": dest, "degraded": []}

    def set_limit(self, label, limit, reset):
        self.calls.append(("set_limit", label, limit, reset))
        return {"name": label, "label": None, "limit": limit, "limit_reset": reset or "monthly"}

    def toggle_key(self, name, disabled):
        self.calls.append(("toggle_key", name, disabled))
        if name not in self.issued:
            raise LookupError(f"no issued key called {name!r}")
        return {"name": name, "disabled": disabled}

    def rotate_one(self, name):
        self.calls.append(("rotate_one", name))
        if self.rotate_error is not None:
            raise self.rotate_error
        return {"name": name, "replaced": True, "resumed": False}

    def revoke_key(self, label):
        self.calls.append(("revoke_key", label))
        return {"name": label, "label": None, "revoked": True}


class KeyActs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        import keyserver
        import key_acts
        self.ks, self.acts = keyserver, key_acts
        self.door = Door()
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.patches = [patch.object(keyserver, "AUDIT", self.base / "logs" / "keyserver.jsonl"),
                        patch.object(keyserver, "_door", lambda: self.door),
                        patch.object(keyserver.paths, "SCRATCH", self.scratch)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def journal(self) -> list[dict]:
        if not self.ks.AUDIT.exists():
            return []
        return [json.loads(line) for line in self.ks.AUDIT.read_text(encoding="utf-8").splitlines() if line.strip()]

    def clean(self, *answers):
        """No answer and no journal line carries the fake value."""
        blob = json.dumps(answers) + (self.ks.AUDIT.read_text(encoding="utf-8") if self.ks.AUDIT.exists() else "")
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn("FAKEFAKE", blob)

    def code(self, answer: dict) -> str:
        self.assertEqual(answer.get("degraded"), [], answer)
        return (answer.get("error") or {}).get("code")

    # --- the gate on acts that spend or cannot be undone ------------------------------------------------------

    def test_mint_without_the_operators_words_is_refused_before_the_door(self):
        out = self.acts.key_act("mint", destination="observatory", limit=5)
        self.assertEqual(self.code(out), "operator-decision-required")
        self.assertEqual(out["error"]["action"], "keys.mint")
        self.assertEqual(out["error"]["target"], "observatory")
        self.assertEqual(self.door.calls, [])
        self.assertEqual(self.journal(), [])

    def test_mint_with_the_words_runs_the_key_servers_act_and_journals_the_decision(self):
        out = self.acts.key_act("mint", destination="observatory", limit=5, name="alpha-web-key",
                                decision=words("keys.mint", "observatory"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["action"], "mint")
        self.assertEqual(out["recordedAs"]["via"], "relayed-instruction")
        self.assertEqual(self.door.calls, [("issue_key", "alpha-web-key", 5.0, None, "observatory", None, "monthly")])
        row = self.journal()[-1]
        self.assertEqual((row["action"], row["caller"], row["caller_verified"]), ("mint", "agent:mcp", False))
        self.assertEqual(row["decision"]["by"], "operator")
        self.assertEqual(row["decision"]["target"], "observatory")
        self.clean(out)

    def test_a_decision_for_another_action_another_target_or_an_old_one_is_refused(self):
        for bad in (words("keys.revoke", "observatory"), words("keys.mint", "claude-mem"),
                    {**words("keys.mint", "observatory"), "at": "2020-01-01T00:00:00Z"}, "mint it", {}):
            out = self.acts.key_act("mint", destination="observatory", limit=5, decision=bad)
            self.assertEqual(self.code(out), "decision-invalid", bad)
        self.assertEqual(self.door.calls, [])

    def test_a_decision_carrying_a_credential_is_refused_and_not_echoed(self):
        out = self.acts.key_act("mint", destination="observatory", limit=5,
                                decision=words("keys.mint", "observatory", instruction=f"use {FAKE_KEY}"))
        self.assertEqual(self.code(out), "decision-invalid")
        self.assertEqual(self.door.calls, [])
        self.clean(out)

    def test_lowering_a_ceiling_needs_no_words_and_raising_one_does(self):
        lower = self.acts.key_act("limit", name="alpha-web-key", limit=5)
        self.assertTrue(lower["ok"], lower)
        self.assertNotIn("recordedAs", lower)
        raise_ = self.acts.key_act("limit", name="alpha-web-key", limit=20)
        self.assertEqual(self.code(raise_), "operator-decision-required")
        self.assertEqual(raise_["error"]["action"], "keys.limit")
        unknown = self.acts.key_act("limit", name="beta-key", limit=1)
        self.assertEqual(self.code(unknown), "operator-decision-required", "a ceiling the ledger cannot compare")
        done = self.acts.key_act("limit", name="alpha-web-key", limit=20, limitReset="daily",
                                 decision=words("keys.limit", "alpha-web-key"))
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.door.calls, [("set_limit", "alpha-web-key", 5.0, None),
                                           ("set_limit", "alpha-web-key", 20.0, "daily")])

    def test_rotate_and_revoke_need_the_words_and_run_the_same_acts(self):
        for action, door_call in (("rotate", "rotate_one"), ("revoke", "revoke_key")):
            refused = self.acts.key_act(action, name="alpha-web-key")
            self.assertEqual(self.code(refused), "operator-decision-required")
            done = self.acts.key_act(action, name="alpha-web-key",
                                     decision=words(f"keys.{action}", "alpha-web-key"))
            self.assertTrue(done["ok"], done)
            self.assertEqual(self.door.calls[-1], (door_call, "alpha-web-key"))
        self.assertEqual([r["action"] for r in self.journal()], ["rotate-key", "revoke"])

    def test_a_rotation_held_for_a_consumer_is_its_own_code(self):
        held = RuntimeError("1 copy of the old value was not delivered")
        held.lines = ["heroku app alpha-web: API_KEY"]
        self.door.rotate_error = held
        out = self.acts.key_act("rotate", name="alpha-web-key", decision=words("keys.rotate", "alpha-web-key"))
        self.assertEqual(self.code(out), "held")
        self.assertIn("alpha-web", out["error"]["message"])

    def test_revoke_of_a_key_in_service_is_refused_by_the_key_servers_own_check(self):
        (self.scratch / "openrouter.json").write_text(json.dumps(
            {"keys": [{"label": "alpha-web-key", "serves": "alpha-web"}]}), encoding="utf-8")
        out = self.acts.key_act("revoke", name="alpha-web-key", decision=words("keys.revoke", "alpha-web-key"))
        self.assertEqual(self.code(out), "invalid-argument")
        self.assertNotIn(("revoke_key", "alpha-web-key"), self.door.calls)

    # --- reversible acts ----------------------------------------------------------------------------------------

    def test_disable_and_enable_need_no_words(self):
        for action in ("disable", "enable"):
            out = self.acts.key_act(action, name="alpha-web-key")
            self.assertTrue(out["ok"], out)
        self.assertEqual(self.door.calls, [("toggle_key", "alpha-web-key", True), ("toggle_key", "alpha-web-key", False)])
        self.assertEqual([r["caller"] for r in self.journal()], ["agent:mcp", "agent:mcp"])
        self.assertNotIn("decision", self.journal()[-1])

    def test_an_unknown_key_is_not_found(self):
        out = self.acts.key_act("disable", name="beta-key")
        self.assertEqual(self.code(out), "not-found")

    # --- arguments ----------------------------------------------------------------------------------------------

    def test_a_key_pasted_as_an_argument_is_refused_and_never_echoed(self):
        for kwargs in ({"name": FAKE_KEY}, {"destination": FAKE_KEY, "limit": 5}):
            action = "mint" if "destination" in kwargs else "disable"
            out = self.acts.key_act(action, **kwargs)
            self.assertEqual(self.code(out), "invalid-argument", kwargs)
            self.clean(out)
        self.assertEqual(self.door.calls, [])
        self.assertEqual(self.journal(), [])

    def test_an_argument_another_action_takes_is_refused(self):
        out = self.acts.key_act("disable", name="alpha-web-key", limit=5)
        self.assertEqual(self.code(out), "invalid-argument")
        self.assertIn("limit", out["error"]["message"])
        self.assertEqual(self.door.calls, [])

    def test_a_mint_without_a_ceiling_or_with_a_bad_destination_is_the_key_servers_refusal(self):
        none = self.acts.key_act("mint", destination="observatory", decision=words("keys.mint", "observatory"))
        self.assertEqual(self.code(none), "invalid-argument")
        bad = self.acts.key_act("mint", destination="/etc/passwd", limit=5)
        self.assertEqual(self.code(bad), "invalid-argument")
        self.assertEqual(self.door.calls, [])

    def test_an_unknown_action_is_refused(self):
        self.assertEqual(self.code(self.acts.key_act("reveal", name="alpha-web-key")), "invalid-argument")
        self.assertEqual(self.code(self.acts.credential_mark("reveal")), "invalid-argument")


class CredentialMarks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        import keyserver
        import key_acts
        import sign_credential
        self.ks, self.acts, self.signer = keyserver, key_acts, sign_credential
        self.argv = self.base / "argv.json"
        vault = self.base / "fake_vault.py"
        vault.write_text(
            "import json, sys\n"
            f"open({str(self.argv)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "print('recorded: leak:2026-11-17T00:00:00Z:' + '/'.join(sys.argv[-3:]))\n", encoding="utf-8")
        self.patches = [patch.object(keyserver, "AUDIT", self.base / "logs" / "keyserver.jsonl"),
                        patch.object(keyserver, "VAULT", vault),
                        patch.object(sign_credential, "FILE", self.base / "credential_annotations.json"),
                        patch.object(sign_credential, "board_ids",
                                     lambda: {"vault/alpha-web/prod/API_KEY": {}})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def journal(self) -> list[dict]:
        return [json.loads(line) for line in self.ks.AUDIT.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_a_leak_is_marked_through_the_vault_with_its_receipt(self):
        out = self.acts.credential_mark("leak", project="alpha-web", env="prod", name="API_KEY",
                                        where="a session transcript")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["marked"], "alpha-web/prod/API_KEY")
        self.assertEqual(json.loads(self.argv.read_text()),
                         ["leak", "--where=a session transcript", "--", "alpha-web", "prod", "API_KEY"])
        row = self.journal()[-1]
        self.assertEqual((row["action"], row["caller"]), ("leak", "agent:mcp"))

    def test_a_leak_without_where_or_with_a_value_is_refused(self):
        none = self.acts.credential_mark("leak", project="alpha-web", env="prod", name="API_KEY")
        self.assertEqual(none["error"]["code"], "invalid-argument")
        pasted = self.acts.credential_mark("leak", project="alpha-web", env="prod", name="API_KEY",
                                           where=f"seen as {FAKE_KEY}")
        self.assertEqual(pasted["error"]["code"], "invalid-argument")
        self.assertNotIn(FAKE_KEY, json.dumps([none, pasted]))
        self.assertFalse(self.argv.exists())

    def test_annotate_signs_through_the_one_writer_as_the_agent(self):
        out = self.acts.credential_mark("annotate", id="vault/alpha-web/prod/API_KEY", purpose="billing calls",
                                        evidence="src/billing.py:12", owner="alpha team", rotationDays=90,
                                        tags=["billing"])
        self.assertTrue(out["ok"], out)
        row = json.loads(self.signer.FILE.read_text(encoding="utf-8"))["annotations"]["vault/alpha-web/prod/API_KEY"]
        self.assertEqual((row["purpose"], row["rotation_days"], row["signed_by"]), ("billing calls", 90, "agent:mcp"))
        self.assertEqual(self.journal()[-1]["fields"], ["evidence", "owner", "purpose", "rotation_days", "tags"])

    def test_annotate_refusals_are_typed_and_carry_no_value(self):
        unknown = self.acts.credential_mark("annotate", id="vault/beta/prod/X", purpose="p", evidence="e")
        self.assertEqual(unknown["error"]["code"], "invalid-argument")
        pasted = self.acts.credential_mark("annotate", id="vault/alpha-web/prod/API_KEY", purpose=FAKE_KEY,
                                           evidence="e")
        self.assertEqual(pasted["error"]["code"], "invalid-argument")
        self.assertNotIn(FAKE_KEY, json.dumps([unknown, pasted]))
        self.assertFalse(self.signer.FILE.exists())

    def test_an_argument_another_action_takes_is_refused(self):
        out = self.acts.credential_mark("leak", project="alpha-web", env="prod", name="API_KEY", where="x",
                                        purpose="p")
        self.assertEqual(out["error"]["code"], "invalid-argument")
        self.assertFalse(self.argv.exists())


class Surface(unittest.TestCase):
    """The tools are declared in the server and call this module; no second implementation of an act."""

    def test_the_server_declares_both_tools_over_key_acts(self):
        src = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        for tool, fn in (("observatory_key_act", "key_act"), ("observatory_credential_mark", "credential_mark")):
            self.assertIn(f"def {tool}(", src)
            body = src.split(f"def {tool}(", 1)[1].split("\n@server", 1)[0]
            self.assertIn(f"key_acts.{fn}(", body)

    def test_key_acts_calls_the_key_servers_acts(self):
        src = (ROOT / "key_acts.py").read_text(encoding="utf-8")
        self.assertIn("ACTIONS[", src)
        for door_verb in ("issue_key", "set_limit", "revoke_key", "toggle_key", "rotate_one", "sign_credential.write"):
            self.assertNotIn(door_verb + "(", src, "an act is the key server's, never a second implementation")


if __name__ == "__main__":
    unittest.main()
