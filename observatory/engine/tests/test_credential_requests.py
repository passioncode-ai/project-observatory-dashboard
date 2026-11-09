#!/usr/bin/env python3
"""Narrow credentials on request: the queue, the decision, delivery through the door (OBS-191 parts 2 and 3).

Nothing here reaches a provider. The doors are either fakes that write through the vault's own door, or the
real OpenRouter and Cloudflare adapters over fakes of the calls those doors make. The vault is a scratch store
under `OBSERVATORY_VAULT_DIR`, the node folder a scratch one under `OBSERVATORY_NODE`. What is asserted:

- a request is the contract's `credential-request/1` (DEC-0111), refused before it is written when it names a
  path or a credential, asks for too long or too short, names a preset no door offers, or doubles an open one;
- the administrator's decision is recorded once, in the contract's `credential-decision/1` shape, with the
  operator's words when an agent relays them; a request decided once is never decided again;
- `issue` delivers through the door into an empty slot only, in a context this space belongs to, and writes the
  provenance (issuer, recipient, scope, limit, end, revoke handle) beside the slot; a credential that ends later
  than asked, or whose end is unconfirmed, is revoked again and the request stays open;
- `revoke` revokes at the provider with proof and deletes the slot's value only when it is still the one issued;
- no value ever reaches the queue, the journal, the metadata or an answer.
"""
from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402  sandboxes HOME first

BASE = pathlib.Path(tmpdir.mkdtemp(prefix="credential-requests-")).resolve()
VAULT_DIR = BASE / "secrets" / "projects"
NODE_DIR = BASE / "node"
os.environ["OBSERVATORY_VAULT_DIR"] = str(VAULT_DIR)
os.environ["OBSERVATORY_NODE"] = str(NODE_DIR)
sys.path.insert(0, str(ROOT / "tools"))
import credential_requests as cr                                    # noqa: E402
import credential_request_findings as crf                           # noqa: E402
import private_io                                                   # noqa: E402
import vault                                                        # noqa: E402

AGENT = "urn:fabric:agent:acme:alpha-web"
VALUE = "synthetic-issued-value-" + "z" * 24
#: The members `credential-request/1` allows (fabric-agent-contract schemas/credential-request.schema.json).
REQUEST_MEMBERS = {"type", "requestId", "agentRef", "context", "provider", "preset", "scope", "limit", "expiresAt",
                   "reason", "requestedAt"}
REQUEST_REQUIRED = {"type", "requestId", "agentRef", "context", "provider", "preset", "expiresAt", "reason",
                    "requestedAt"}
DECISION_MEMBERS = {"type", "requestId", "outcome", "by", "reason", "decidedAt", "credential"}


def pin() -> None:
    """This suite's scratch vault and node, whatever another suite in the same process set."""
    os.environ["OBSERVATORY_VAULT_DIR"] = str(VAULT_DIR)
    os.environ["OBSERVATORY_NODE"] = str(NODE_DIR)
    vault.STORE, vault.LEAKS, vault.MOVES = VAULT_DIR, VAULT_DIR / "leaks.jsonl", VAULT_DIR / "movements.jsonl"


def wipe() -> None:
    import shutil
    shutil.rmtree(BASE / "secrets", ignore_errors=True)
    shutil.rmtree(NODE_DIR, ignore_errors=True)


def quiet(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return rc, out.getvalue(), err.getvalue()


def stamp(days: float = 0) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def meta_of(slot: str) -> dict:
    project, env, name = slot.split("/")
    return json.loads((VAULT_DIR / project / env / f"{name}.meta.json").read_text())


def everything_written() -> str:
    """Every file of the scratch vault but the value files themselves: what a reader of names may see."""
    texts = []
    for path in sorted(VAULT_DIR.rglob("*")):
        if path.is_file() and (path.suffix in (".json", ".jsonl")):
            texts.append(path.read_text(encoding="utf-8"))
    return "\n".join(texts)


class FakeDoor(cr.Door):
    """A door that mints into the slot through the vault's own door and records an end, as the real ones do."""
    provider, door = "openrouter", "openrouter.py"

    def __init__(self, end_days: float | None = None, answer_end: str | None = "asked", fail: str | None = None,
                 revoke_fails: bool = False):
        self.end_days, self.answer_end, self.fail, self.revoke_fails = end_days, answer_end, fail, revoke_fails
        self.issued: list[tuple] = []
        self.revoked: list[dict] = []

    def presets(self):
        return {"model-key": {"what": "a key", "limit": "required", "scope_required": (), "scope_optional": ()}}

    def issue(self, request, slot, account, expire_days):
        self.issued.append((request["requestId"], slot, account, expire_days))
        if self.fail:
            raise cr.IssueFailed(self.fail)
        project, env, name = slot
        vault.store_value(project, env, name, VALUE, replace=False, tool="fake")
        if self.answer_end == "asked":
            at = stamp(expire_days)
        elif self.answer_end == "later":
            at = stamp(400)
        else:
            at = None
        vault.record_expiry(project, env, name, at, None if at else "unconfirmed", tool="fake")
        return {"issuer": {"door": self.door, "account": account or "example"},
                "revoke": {"door": self.door, "handle": f"{project}-{env}", "account": account or "example",
                           "command": None},
                "identity": {"hash": f"h-{len(self.issued)}"}}

    def revoke(self, provenance, identity, replaced):
        if self.revoke_fails:
            raise RuntimeError("the provider did not answer")
        self.revoked.append({**provenance, "identity": identity, "replaced": replaced})
        return {"provider": "revoked", "proved": True}


class Suite(unittest.TestCase):
    def setUp(self):
        wipe()
        pin()
        self.enroll("acme")                     # this space's enrollment in the organization most cases use
        self.door = FakeDoor()
        self.saved = dict(cr.DOORS)
        cr.DOORS["openrouter"] = self.door

    def tearDown(self):
        cr.DOORS.clear()
        cr.DOORS.update(self.saved)

    def file(self, slot="alpha-web/prod/OPENROUTER_API_KEY", **kw):
        args = dict(agent=AGENT, provider="openrouter", preset="model-key", slot=slot, reason="release notes",
                    limit="5", reset="monthly")
        args.update(kw)
        return cr.file_request(**args)

    def enroll(self, org: str = "acme", removed: bool = False) -> None:
        """A minimal enrollment state as `node.org` reads it: enrolled, or removed by the server."""
        folder = NODE_DIR / "orgs" / org
        folder.mkdir(parents=True, exist_ok=True)
        doc = {"schema": "org-enrollment/1", "org": org}
        if removed:
            doc["removed"] = {"code": "certificate-revoked", "at": stamp()}
        private_io.write(folder / "enrollment.json", json.dumps(doc))


class FilingARequest(Suite):

    def test_a_request_is_the_contracts_document_and_waits_open(self):
        r = self.file(context="org:acme", expires_in_days=10)
        doc = r["request"]
        self.assertTrue(REQUEST_REQUIRED <= set(doc) <= REQUEST_MEMBERS, doc)
        self.assertEqual((doc["type"], doc["context"], doc["limit"]), ("credential-request/1", "org:acme",
                                                                       {"usd": "5", "reset": "monthly"}))
        self.assertRegex(doc["requestId"], cr.UUID)
        self.assertRegex(doc["expiresAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        known, degraded = cr.requests()
        self.assertEqual((known[doc["requestId"]]["state"], degraded), ("open", []))
        shown = cr.show(doc["requestId"][:8])
        self.assertEqual(shown["requestDocument"], doc, "the document that would leave carries no local member")
        self.assertNotIn("slot", shown["requestDocument"])

    def test_a_personal_request_never_leaves_this_space(self):
        r = self.file()
        self.assertEqual(r["request"]["context"], "personal")
        self.assertIsNone(cr.show(r["requestId"])["requestDocument"])

    def test_what_a_request_may_not_carry_is_refused_before_it_is_written(self):
        cases = [
            (dict(agent="alpha-web"), "invalid"),
            (dict(reason="see /srv/alpha/keys for the old one"), "invalid"),
            (dict(reason="like ~/projects/alpha/.env had"), "invalid"),
            (dict(reason="the old one was sk-or-v1-" + "a1b2c3d4" * 6), "invalid"),
            (dict(reason="two\nlines"), "invalid"),
            (dict(context="org:acme", expires_in_days=31), "expiry-too-long"),
            (dict(expires_in_days=400), "expiry-too-long"),
            (dict(expires_in_days=1), "invalid"),
            (dict(expires_at=stamp(1)), "expiry-too-short"),
            (dict(context="org"), "invalid"),
            (dict(provider="heroku", preset="deploy"), "provider-unavailable"),
            (dict(preset="admin-key"), "preset-unknown"),
            (dict(limit=None, reset=None), "limit-missing"),
            (dict(limit="0"), "invalid"),
            (dict(scope=["zone=example.com"]), "scope-too-wide"),
            (dict(scope=["zone=/etc/x"]), "invalid"),
            (dict(slot="alpha-web/qa/KEY"), "invalid"),
            (dict(slot="alpha-web/prod/_KEY"), "invalid"),
        ]
        for kw, code in cases:
            with self.subTest(kw=kw):
                with self.assertRaises(cr.Refused) as caught:
                    self.file(**kw)
                self.assertEqual(caught.exception.code, code, str(caught.exception))
                self.assertNotIn("a1b2c3d4", str(caught.exception))
        self.assertFalse(cr.queue_path().exists(), "nothing refused is written")

    def test_a_url_is_not_a_path(self):
        r = self.file(reason="the dashboard at https://example.com/a/b reads it")
        self.assertEqual(r["state"], "open")

    def test_one_open_request_per_slot_and_context(self):
        first = self.file()
        with self.assertRaises(cr.Refused) as caught:
            self.file(reason="again")
        self.assertEqual(caught.exception.code, "duplicate")
        self.assertIn(first["requestId"][:8], str(caught.exception))
        self.file(context="org:acme", expires_in_days=5)       # another context is another request
        cr.withdraw(first["requestId"])
        self.file(reason="after the withdrawal")


class TheDecision(Suite):

    def test_a_refusal_is_recorded_once_in_the_contracts_shape(self):
        rid = self.file()["requestId"]
        r = cr.refuse(rid[:8], "not-allowed", note="ask the team lead first")
        doc = r["decision"]
        self.assertTrue(set(doc) <= DECISION_MEMBERS)
        self.assertEqual((doc["outcome"], doc["by"], doc["reason"]), ("refused", "administrator", "not-allowed"))
        self.assertNotIn("credential", doc)
        for again in (lambda: cr.refuse(rid, "unanswered"), lambda: cr.pending(rid), lambda: cr.withdraw(rid),
                      lambda: cr.issue(rid)):
            with self.assertRaises(cr.Refused) as caught:
                again()
            self.assertEqual(caught.exception.code, "decided")
        self.assertEqual(self.door.issued, [], "a refused request is never minted")

    def test_pending_keeps_it_open_and_withdrawn_is_the_contracts_refusal_by_policy(self):
        rid = self.file()["requestId"]
        doc = cr.pending(rid)["decision"]
        self.assertEqual(doc["outcome"], "pending")
        self.assertNotIn("by", doc, "a pending decision names nobody yet")
        self.assertEqual(cr.requests()[0][rid]["state"], "open")
        out = cr.withdraw(rid)
        self.assertEqual((out["state"], out["decision"]["reason"], out["decision"]["by"]),
                         ("withdrawn", "withdrawn", "policy"))
        with self.assertRaises(cr.Refused):
            cr.refuse(rid, "withdrawn")

    def test_an_agent_relays_the_operators_words_or_nothing(self):
        rid = self.file()["requestId"]
        with self.assertRaises(cr.Refused):
            cr.refuse(rid, "not-allowed", instruction="no", source=None)
        with self.assertRaises(cr.Refused):
            cr.refuse(rid, "not-allowed", instruction="token sk-or-v1-" + "f0e1" * 12, source="session 1")
        cr.refuse(rid, "not-allowed", instruction="refuse the alpha key request", source="session 7f3a")
        rows, _ = cr.read_rows()
        relayed = rows[-1]["relayed"]
        self.assertEqual({k: relayed[k] for k in ("by", "via", "action", "target", "instruction", "source")},
                         {"by": "operator", "via": "relayed-instruction", "action": "credential.refuse",
                          "target": rid[:8], "instruction": "refuse the alpha key request", "source": "session 7f3a"})


class Issuing(Suite):

    def test_issue_delivers_into_the_slot_with_its_provenance_and_records_the_decision(self):
        slot = "alpha-web/prod/OPENROUTER_API_KEY"
        r0 = self.file(slot=slot, scope=None)
        rid = r0["requestId"]
        r = cr.issue(rid[:8], account="example", instruction="issue the alpha key", source="session 7f3a")
        self.assertEqual((r["state"], r["slot"]), ("issued", slot))
        self.assertEqual(len(self.door.issued), 1)
        _rid, _slot, account, days = self.door.issued[0]
        self.assertEqual(account, "example")
        self.assertTrue(1 <= days <= 90)
        # the value is in the slot and nowhere else
        self.assertEqual((VAULT_DIR / slot).read_text().strip(), VALUE)
        self.assertNotIn(VALUE, everything_written() + json.dumps(r))
        prov = meta_of(slot)["provenance"]
        self.assertEqual(prov["requestId"], rid)
        self.assertEqual(prov["recipient"], {"agentRef": AGENT, "context": "personal", "slot": slot})
        self.assertEqual(prov["issuer"], {"door": "openrouter.py", "account": "example"})
        self.assertEqual((prov["provider"], prov["preset"], prov["limit"]),
                         ("openrouter", "model-key", {"usd": "5", "reset": "monthly"}))
        self.assertEqual(prov["expiresAt"], meta_of(slot)["expires_at"])
        self.assertIn("handle", prov["revoke"])
        doc = r["decision"]
        self.assertTrue(set(doc) <= DECISION_MEMBERS)
        self.assertEqual((doc["outcome"], doc["by"]), ("issued", "administrator"))
        self.assertEqual(doc["credential"], {"slot": "OPENROUTER_API_KEY", "provider": "openrouter",
                                             "preset": "model-key", "expiresAt": meta_of(slot)["expires_at"]})
        self.assertLessEqual(doc["credential"]["expiresAt"], r0["request"]["expiresAt"],
                             "an issued credential ends no later than asked (FAC-SEM-138)")
        moves = [json.loads(line) for line in vault.MOVES.read_text().splitlines()]
        self.assertIn("provenance", [m["event"] for m in moves])
        _rc, listed, _err = quiet(vault.cmd_list, type("A", (), {"project": None, "env": None})())
        self.assertIn(f"issued on request {rid[:8]} for personal by openrouter.py/example", listed)
        self.assertEqual(cr.requests()[0][rid]["state"], "issued")
        # The readers agents and people use name the request, never the value.
        import use_secret
        self.assertIn(f"issued on request {rid[:8]}", use_secret._expiry_note(VAULT_DIR / slot))
        import survey
        self.assertEqual(survey._slot_provenance(VAULT_DIR / slot)["issuedOnRequest"],
                         {"requestId": rid, "context": "personal", "agentRef": AGENT, "door": "openrouter.py",
                          "account": "example"})
        self.assertEqual(survey._slot_provenance(VAULT_DIR / "alpha-web/prod/ABSENT"), {})

    def test_a_name_already_present_is_never_overwritten(self):
        slot = "alpha-web/prod/OPENROUTER_API_KEY"
        rid = self.file(slot=slot)["requestId"]
        vault.store_value("alpha-web", "prod", "OPENROUTER_API_KEY", "an-older-value-" + "k" * 20,
                          replace=False, tool="test")
        with self.assertRaises(cr.Refused) as caught:
            cr.issue(rid)
        self.assertEqual(caught.exception.code, "slot-filled")
        self.assertEqual(self.door.issued, [], "nothing is minted for a slot that is taken")
        self.assertEqual(cr.requests()[0][rid]["state"], "open")

    def test_an_organizations_credential_lands_only_in_a_space_enrolled_in_it(self):
        with self.assertRaises(cr.Refused) as caught:
            self.file(context="org:other", expires_in_days=7)
        self.assertEqual(caught.exception.code, "wrong-context", "a request names its own enrollment's organization")
        rid = self.file(context="org:acme", expires_in_days=7)["requestId"]
        self.enroll("acme", removed=True)
        with self.assertRaises(cr.Refused) as caught:
            cr.issue(rid)
        self.assertIn("removed", str(caught.exception))
        self.assertEqual(self.door.issued, [])
        self.enroll("acme")
        r = cr.issue(rid)
        self.assertEqual(r["state"], "issued")
        self.assertEqual(meta_of("alpha-web/prod/OPENROUTER_API_KEY")["provenance"]["recipient"]["context"],
                         "org:acme")
        self.assertLessEqual(self.door.issued[0][3], 7)

    def test_a_door_that_delivers_nothing_leaves_the_request_open_with_the_attempt_recorded(self):
        cr.DOORS["openrouter"] = FakeDoor(fail="openrouter.py refused: the provider answered HTTP 402")
        rid = self.file()["requestId"]
        with self.assertRaises(cr.IssueFailed):
            cr.issue(rid)
        entry = cr.requests()[0][rid]
        self.assertEqual(entry["state"], "open")
        self.assertEqual(entry["attempts"][-1]["stage"], "issue")
        self.assertIn("402", entry["attempts"][-1]["reason"])
        rows = crf.findings(cr.queue_path(), stamp()[:10])
        self.assertEqual([f["type"] for f in rows], ["credential.request_waiting"])
        self.assertIn("402", rows[0]["detail"])

    def test_an_end_later_than_asked_or_unconfirmed_is_revoked_again(self):
        for answer in ("later", None):
            with self.subTest(answer=answer):
                wipe()
                pin()
                door = FakeDoor(answer_end=answer)
                cr.DOORS["openrouter"] = door
                rid = self.file()["requestId"]
                with self.assertRaises(cr.IssueFailed):
                    cr.issue(rid)
                self.assertEqual(len(door.revoked), 1, "the credential the request cannot keep is revoked")
                self.assertFalse((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").exists())
                self.assertEqual(cr.requests()[0][rid]["state"], "open")

    def test_the_end_is_counted_in_whole_days_no_later_than_asked(self):
        end = stamp(12)
        rid = self.file(expires_at=end)["requestId"]
        cr.issue(rid)
        days = self.door.issued[0][3]
        self.assertEqual(days, 11, "a request issued at once is minted a day short, never past its end")


class HardCases(Suite):

    def test_one_request_is_never_issued_twice_at_once(self):
        import threading
        rid = self.file()["requestId"]
        seen: dict = {}
        door_issue = self.door.issue

        def issue_while_another_waits(request, slot, account, days):
            def second():
                try:
                    cr.issue(rid)
                    seen["second"] = "issued"
                except cr.Refused as exc:
                    seen["second"] = exc.code
            other = threading.Thread(target=second)
            other.start()
            other.join(0.5)
            seen["blocked"] = other.is_alive()
            seen["thread"] = other
            return door_issue(request, slot, account, days)
        self.door.issue = issue_while_another_waits
        cr.issue(rid)
        seen["thread"].join(10)
        self.assertTrue(seen["blocked"], "the second issue waited for the queue's lock")
        self.assertEqual(seen["second"], "decided")
        self.assertEqual(len(self.door.issued), 1)

    def test_an_interrupted_issue_is_named_not_called_a_taken_slot(self):
        rid = self.file()["requestId"]
        cr._append({"event": "issuing", "requestId": rid, "slot": "alpha-web/prod/OPENROUTER_API_KEY"})
        vault.store_value("alpha-web", "prod", "OPENROUTER_API_KEY", "minted-before-a-crash-" + "c" * 20,
                          replace=False, tool="test")
        with self.assertRaises(cr.Refused) as caught:
            cr.issue(rid)
        self.assertEqual(caught.exception.code, "interrupted")
        rows = crf.findings(cr.queue_path(), stamp()[:10])
        self.assertIn("interrupted", rows[0]["detail"])

    def test_a_pending_decision_does_not_hide_an_interrupted_issue_and_it_cannot_be_closed(self):
        rid = self.file()["requestId"]
        cr._append({"event": "issuing", "requestId": rid, "slot": "alpha-web/prod/OPENROUTER_API_KEY"})
        vault.store_value("alpha-web", "prod", "OPENROUTER_API_KEY", "minted-before-a-crash-" + "c" * 20,
                          replace=False, tool="test")
        cr.pending(rid)
        for act in (lambda: cr.issue(rid), lambda: cr.refuse(rid, "not-allowed"), lambda: cr.withdraw(rid)):
            with self.assertRaises(cr.Refused) as caught:
                act()
            self.assertEqual(caught.exception.code, "interrupted")

    def test_a_filled_slot_names_the_request_that_filled_it(self):
        first = self.file()["requestId"]
        cr.issue(first)
        second = self.file(reason="a renewal")["requestId"]
        with self.assertRaises(cr.Refused) as caught:
            cr.issue(second)
        self.assertIn(f"revoke {first[:8]}", str(caught.exception))

    def test_two_requests_for_one_slot_are_two_board_rows(self):
        self.file()
        self.file(context="org:acme", expires_in_days=5)
        rows = crf.findings(cr.queue_path(), stamp()[:10])
        self.assertEqual(len({(r["type"], r["subject"]) for r in rows}), 2)
        self.assertTrue(all(r["subject"].startswith("credential:vault/alpha-web/prod/") for r in rows))

    def test_a_request_record_without_its_members_is_named_and_breaks_nothing(self):
        rid = self.file()["requestId"]
        cr._append({"event": "requested", "request": {"requestId": "not-a-uuid"}, "slot": "x"})
        known, degraded = cr.requests()
        self.assertEqual(set(known), {rid})
        self.assertEqual(len(degraded), 1)
        rows = crf.findings(cr.queue_path(), stamp()[:10])
        self.assertEqual(sorted(r["type"] for r in rows),
                         ["credential.request_queue_unreadable", "credential.request_waiting"],
                         "a damaged record is said on the board, never hidden")
        self.assertEqual(len(cr.listing("all")["requests"]), 1)

    def test_the_operators_words_may_cite_an_id_a_reason_may_not(self):
        rid = self.file()["requestId"]
        cr.pending(rid, note="seen in session 7b0d2c1e-4a5b-4c6d-8e9f-0a1b2c3d4e5f")
        cr.refuse(rid, "not-allowed", instruction="refuse request 7b0d2c1e-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
                  source="session 7b0d2c1e-4a5b-4c6d-8e9f-0a1b2c3d4e5f")
        with self.assertRaises(cr.Refused):
            self.file(slot="beta-api/prod/KEY", reason="like 7b0d2c1e-4a5b-4c6d-8e9f-0a1b2c3d4e5f had")

    def test_a_provenance_that_cannot_be_written_undoes_the_issue(self):
        rid = self.file()["requestId"]
        original = vault.record_provenance

        def refuse(*a, **k):
            raise vault.VaultBoundaryError("the metadata is not owner-only")
        vault.record_provenance = refuse
        try:
            with self.assertRaises(cr.IssueFailed):
                cr.issue(rid)
        finally:
            vault.record_provenance = original
        self.assertEqual(len(self.door.revoked), 1)
        self.assertFalse((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").exists())
        self.assertEqual(cr.requests()[0][rid]["state"], "open")

    def test_a_credential_a_failed_issue_could_not_revoke_stays_on_the_board_until_settled(self):
        cr.DOORS["openrouter"] = FakeDoor(answer_end="later", revoke_fails=True)
        rid = self.file()["requestId"]
        with self.assertRaises(cr.IssueFailed):
            cr.issue(rid)
        cr.refuse(rid, "unanswered")
        rows = crf.findings(cr.queue_path(), stamp()[:10])
        self.assertEqual([r["type"] for r in rows], ["credential.revocation_refused"])
        self.assertIn("could not be revoked again", rows[0]["detail"])
        cr.revoke(rid, settle="deleted at the provider by hand, session 4e2f")
        self.assertEqual(crf.findings(cr.queue_path(), stamp()[:10]), [])


class Revoking(Suite):

    def issued(self) -> str:
        rid = self.file()["requestId"]
        cr.issue(rid)
        return rid

    def test_revoke_proves_it_at_the_provider_and_deletes_the_value_it_issued(self):
        rid = self.issued()
        r = cr.revoke(rid[:8])
        self.assertEqual(r["revocation"]["slot"], "removed")
        self.assertTrue(r["revocation"]["proved"])
        self.assertFalse((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").exists())
        self.assertEqual(self.door.revoked[0]["requestId"], rid)
        self.assertEqual(cr.requests()[0][rid]["state"], "revoked")
        with self.assertRaises(cr.Refused):
            cr.revoke(rid)

    def test_a_revocation_never_deletes_a_value_the_slot_holds_since(self):
        rid = self.issued()
        vault.store_value("alpha-web", "prod", "OPENROUTER_API_KEY", "rotated-by-hand-" + "r" * 20, replace=True,
                          tool="test")
        self.assertNotIn("provenance", meta_of("alpha-web/prod/OPENROUTER_API_KEY"),
                         "a new value drops the old one's provenance")
        r = cr.revoke(rid)
        self.assertTrue(r["revocation"]["slot"].startswith("kept"))
        self.assertTrue((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").exists())

    def test_a_refused_revocation_stays_listed_until_settled(self):
        rid = self.issued()
        self.door.revoke_fails = True
        with self.assertRaises(RuntimeError):
            cr.revoke(rid)
        self.assertEqual(cr.requests()[0][rid]["state"], "issued")
        rows = crf.findings(cr.queue_path(), stamp()[:10])
        self.assertEqual([f["type"] for f in rows], ["credential.revocation_refused"])
        self.door.revoke_fails = False
        cr.revoke(rid)
        self.assertEqual(crf.findings(cr.queue_path(), stamp()[:10]), [])

    def test_a_revocation_the_provider_does_not_show_is_not_called_done(self):
        rid = self.issued()
        self.door.revoke = lambda provenance, identity, replaced: {"provider": "revoked", "proved": False}
        rc, out, err = quiet(cr.main, ["revoke", rid[:8]])
        self.assertEqual(rc, 1, "an unproved revocation is not a success")
        self.assertIn("NOT proved", out)
        rows = crf.findings(cr.queue_path(), stamp()[:10])
        self.assertEqual([(f["type"], f["title_id"]) for f in rows],
                         [("credential.revocation_refused", "Revoking {slot} at {provider} was not proved")])
        self.assertTrue((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").exists(),
                        "the value stays while its credential may still work")
        cr.revoke(rid, settle="deleted at the provider by hand")
        self.assertFalse((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").exists())
        self.assertEqual(crf.findings(cr.queue_path(), stamp()[:10]), [])

    def test_a_value_put_while_the_provider_answered_is_never_deleted(self):
        rid = self.issued()

        def rotate_meanwhile(provenance, identity, replaced):
            vault.store_value("alpha-web", "prod", "OPENROUTER_API_KEY", "put-meanwhile-" + "w" * 20,
                              replace=True, tool="test")
            return {"provider": "revoked", "proved": True}
        self.door.revoke = rotate_meanwhile
        r = cr.revoke(rid)
        self.assertEqual(r["revocation"]["slot"], "kept: it holds another value now")
        self.assertEqual((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").read_text().strip(),
                         "put-meanwhile-" + "w" * 20)

    def test_a_revocation_the_door_cannot_carry_out_can_be_settled(self):
        rid = self.issued()
        self.door.revoke_fails = True
        with self.assertRaises(RuntimeError):
            cr.revoke(rid)
        with self.assertRaises(cr.Refused):
            cr.revoke(self.file(slot="beta-api/prod/KEY")["requestId"], settle="nothing to settle here")
        r = cr.revoke(rid, settle="deleted in the provider's dashboard, the admin key is gone")
        self.assertTrue(r["revocation"]["proved"])
        self.assertEqual(cr.requests()[0][rid]["state"], "revoked")
        self.assertFalse((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").exists())


class TheQueueFile(Suite):

    def test_a_damaged_line_is_named_and_hides_nothing(self):
        rid = self.file()["requestId"]
        with open(cr.queue_path(), "a", encoding="utf-8") as stream:
            stream.write("{not json\n")
        rid2 = self.file(slot="beta-api/prod/OPENROUTER_API_KEY")["requestId"]
        known, degraded = cr.requests()
        self.assertEqual(set(known), {rid, rid2})
        self.assertEqual([d["line"] for d in degraded], [2])
        listed = cr.listing("all")
        self.assertEqual(len(listed["requests"]), 2)
        self.assertEqual(listed["degraded"][0]["line"], 2)

    def test_an_expired_credential_reads_expired(self):
        rid = self.file()["requestId"]
        cr.issue(rid)
        rows, _ = cr.read_rows()
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=400)
        self.assertEqual(cr.fold(rows, later)[rid]["state"], "expired")


class ProvenanceInTheVault(Suite):

    def test_only_names_and_dates_are_recorded_beside_a_slot(self):
        vault.store_value("alpha-web", "prod", "KEY", "plain-value-" + "p" * 20, replace=False, tool="test")
        for bad in ({"issuer": {"door": "x", "account": "sk-or-v1-" + "a1b2c3d4" * 6}}, {"value": "x"}, {},
                    {"issuer": object()}):
            with self.subTest(bad=str(bad)[:30]):
                with self.assertRaises(vault.VaultBoundaryError):
                    vault.record_provenance("alpha-web", "prod", "KEY", bad)
        with self.assertRaises(vault.VaultBoundaryError):
            vault.record_provenance("alpha-web", "prod", "ABSENT", {"requestId": "x"})
        vault.record_provenance("alpha-web", "prod", "KEY", {"requestId": "abc", "provider": "openrouter"})
        self.assertEqual(vault.provenance_of("alpha-web", "prod", "KEY")["requestId"], "abc")


class TheRealDoors(Suite):
    """The two adapters over fakes of the provider calls each door makes."""

    def load(self, rel: str, name: str):
        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def openrouter(self):
        m = self.load("tools/openrouter.py", "or_requests")
        store = BASE / "or-store"
        import shutil
        shutil.rmtree(store, ignore_errors=True)
        m.ADMIN_STORE = store / "openrouter-admin"
        m.ADMIN_STORE.mkdir(parents=True, exist_ok=True)
        private_io.write(m.ADMIN_STORE / "example", "prov\n")
        m.LEDGER = store / "ledger.json"
        m._journal = lambda *a, **k: None
        keys: dict[str, dict] = {}
        listed: list[str] = []

        def fake(path, key, payload=None, method=None):
            if path.startswith("/keys?"):
                listed.append(path)
                return {"data": list(keys.values())}
            if path == "/keys" and payload:
                h = f"h{len(keys) + 1}x{len(listed)}"
                keys[h] = {"hash": h, "name": payload["name"], "expires_at": payload.get("expires_at"),
                           "limit": payload["limit"]}
                return {"data": keys[h], "key": VALUE + h}
            if path.startswith("/keys/") and method == "DELETE":
                keys.pop(path.split("/")[2], None)
                return {}
            if path.startswith("/keys/") and method == "PATCH":
                keys[path.split("/")[2]].update(payload)
                return {"data": keys[path.split("/")[2]]}
            if path.startswith("/keys/"):
                return {"data": keys.get(path.split("/")[2])}
            return {"data": []}
        m._request = fake
        door = cr.OpenRouterDoor()
        door._module = staticmethod(lambda: m)
        cr.DOORS["openrouter"] = door
        return m, keys, listed

    def test_openrouter_mints_into_the_slot_and_revokes_its_own_key_by_hash(self):
        m, keys, listed = self.openrouter()
        rid = self.file(limit="7.5", reset="weekly")["requestId"]
        r = cr.issue(rid, account="example")
        slot = "alpha-web/prod/OPENROUTER_API_KEY"
        self.assertTrue((VAULT_DIR / slot).read_text().strip().startswith(VALUE))
        prov = meta_of(slot)["provenance"]
        self.assertEqual(prov["revoke"]["handle"], "alpha-web-prod-openrouter-api-key")
        self.assertIn('openrouter.py" revoke alpha-web-prod-openrouter-api-key', prov["revoke"]["command"])
        self.assertNotIn("identity", prov, "a key hash stays in the queue, never beside the slot")
        row = list(keys.values())[0]
        self.assertEqual((row["limit"], row["expires_at"]), (7.5, r["expiresAt"]))
        self.assertNotIn(VALUE, everything_written() + m.LEDGER.read_text())
        before = len(listed)
        rev = cr.revoke(rid)
        self.assertEqual((rev["revocation"]["proved"], keys), (True, {}))
        self.assertEqual(len(listed), before, "the revocation reads the key by its hash, never the listing")

    def test_a_rotation_by_the_door_is_never_revoked_by_the_request(self):
        m, keys, _listed = self.openrouter()
        rid = self.file()["requestId"]
        cr.issue(rid, account="example")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            m.rotate_one("alpha-web-prod-openrouter-api-key")
        self.assertEqual(len(keys), 1, "the rotation deleted the key the request issued")
        successor = list(keys)[0]
        rev = cr.revoke(rid)
        self.assertEqual((rev["revocation"]["provider"], rev["revocation"]["slot"]),
                         ("gone", "kept: it holds another value now"))
        self.assertIn(successor, keys, "the successor the consumer reads is untouched")

    def cloudflare(self, pages_before: int = 0):
        """The door's own `cmd_issue_zone` over a fake Cloudflare API: zones, permission groups, the token
        create, the probe, the token read and delete by id. Only the stash is faked (`find_account`)."""
        m = self.load("tools/cloudflare.py", "cf_requests")
        tokens: dict[str, dict] = {f"old-{i}": {"id": f"old-{i}", "name": f"another token {i}"}
                                   for i in range(pages_before)}
        calls: list = []

        def request(path, token, payload=None, method=None):
            calls.append((method or ("POST" if payload is not None else "GET"), path.split("?")[0]))
            if path.startswith("/zones?"):
                return {"result": [{"id": "zone-1", "name": "example.com"}]}
            if "/tokens/permission_groups" in path:
                return {"result": [{"id": "g-zone-read", "name": "Zone Read", "scopes": ["com.cloudflare.api.account.zone"]},
                                   {"id": "g-rules", "name": "Email Routing Rules Write",
                                    "scopes": ["com.cloudflare.api.account.zone"]}]}
            if path.startswith("/zones/zone-1/"):
                return {"result": []}                      # the probe, with the new token's rights
            if path.endswith("/tokens") and payload is not None:
                tid = f"tid-{len(tokens)}"
                tokens[tid] = {"id": tid, "name": payload["name"], "expires_on": payload.get("expires_on")}
                return {"result": {**tokens[tid], "value": VALUE + tid}}
            tid = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                tokens.pop(tid, None)
                return {}
            if tid in tokens:
                return {"result": tokens[tid]}
            raise RuntimeError("cloudflare GET /accounts/<id>/tokens/<id>: HTTP 404")
        m._request = request
        m.find_account = lambda wanted: ("stash", "admin", {"id": "acct-id", "name": "Example Team"})
        m.existing_token = lambda admin, account_id, name: next(
            (t["id"] for t in list(tokens.values())[:50] if t["name"] == name), None)
        m._journal = lambda *a, **k: None
        door = cr.CloudflareDoor()
        door._module = staticmethod(lambda: m)
        cr.DOORS["cloudflare"] = door
        return m, tokens, calls

    def file_cf(self, **kw):
        args = dict(agent=AGENT, provider="cloudflare", preset="email-routing", slot="alpha-web/prod/CF_TOKEN",
                    reason="mail rules", scope=["zone=example.com"], expires_in_days=20)
        args.update(kw)
        return cr.file_request(**args)["requestId"]

    def test_cloudflare_issues_a_token_named_by_its_slot_and_deletes_it_by_id_past_the_first_page(self):
        m, tokens, calls = self.cloudflare(pages_before=60)
        for preset, why in (("dns-edit", "named by zone"), ("analytics", "not one per vault slot")):
            with self.assertRaises(cr.Refused) as caught:
                self.file_cf(preset=preset)
            self.assertIn(why, str(caught.exception))
        with self.assertRaises(cr.Refused):
            self.file_cf(limit="5")
        rid = self.file_cf()
        r = cr.issue(rid)
        self.assertIn(("POST", "/accounts/acct-id/tokens"), calls)
        self.assertTrue((VAULT_DIR / "alpha-web/prod/CF_TOKEN").read_text().strip().startswith(VALUE))
        prov = meta_of("alpha-web/prod/CF_TOKEN")["provenance"]
        self.assertEqual(prov["issuer"], {"door": "cloudflare.py", "account": "example-team"})
        self.assertIn("alpha-web/prod/CF_TOKEN", prov["revoke"]["handle"])
        self.assertEqual(r["decision"]["credential"]["slot"], "CF_TOKEN")
        rev = cr.revoke(rid)
        self.assertEqual((rev["revocation"]["provider"], rev["revocation"]["proved"]), ("revoked", True))
        self.assertEqual(len(tokens), 60, "the token past the first page was found and deleted; no other")
        self.assertNotIn(VALUE, everything_written())

    def test_a_value_put_while_the_door_waits_is_never_overwritten(self):
        m, tokens, _calls = self.cloudflare()
        rid = self.file_cf()
        door_issue = m.cmd_issue_zone

        def racing(*args, **kwargs):
            vault.store_value("alpha-web", "prod", "CF_TOKEN", "put-meanwhile-" + "m" * 20, replace=False,
                              tool="test")
            return door_issue(*args, **kwargs)
        m.cmd_issue_zone = racing
        with self.assertRaises(cr.IssueFailed):
            cr.issue(rid)
        self.assertEqual((VAULT_DIR / "alpha-web/prod/CF_TOKEN").read_text().strip(), "put-meanwhile-" + "m" * 20)
        self.assertEqual(tokens, {}, "the minted token was discarded")
        self.assertEqual(cr.requests()[0][rid]["state"], "open")

    def test_a_failure_after_the_door_delivered_revokes_what_it_minted(self):
        m, tokens, _calls = self.cloudflare()
        rid = self.file_cf()

        def answer_unreadable(*a, **k):
            raise RuntimeError("the provider's answer could not be read")
        m.answered = answer_unreadable                # a step of the door's after its delivery
        with self.assertRaises(cr.IssueFailed) as caught:
            cr.issue(rid)
        self.assertIn("revoked at the provider", str(caught.exception))
        self.assertNotIn("NOT revoked", str(caught.exception))
        self.assertFalse(cr.requests()[0][rid]["undoUnsettled"])
        self.assertEqual(tokens, {}, "the live token was revoked by its id")
        self.assertFalse((VAULT_DIR / "alpha-web/prod/CF_TOKEN").exists(), "and its value deleted")
        entry = cr.requests()[0][rid]
        self.assertEqual((entry["state"], entry["inFlight"]), ("open", None))

    def test_an_openrouter_ledger_that_fails_after_delivery_leaves_no_live_key(self):
        m, keys, _listed = self.openrouter()
        saves = {"n": 0}
        door_save = m.save_ledger

        def third_save_fails(doc):
            saves["n"] += 1
            if saves["n"] == 3:                       # the door's final row, written after its delivery
                raise OSError("disk full")
            return door_save(doc)
        m.save_ledger = third_save_fails
        rid = self.file()["requestId"]
        with self.assertRaises(cr.IssueFailed):
            cr.issue(rid, account="example")
        self.assertEqual(keys, {}, "the key the door delivered was revoked by its hash")
        self.assertFalse((VAULT_DIR / "alpha-web/prod/OPENROUTER_API_KEY").exists())

    def test_a_slot_that_cannot_be_written_leaves_no_live_token(self):
        m, tokens, _calls = self.cloudflare()
        rid = self.file_cf()
        original = vault.store_value

        def disk_full(*a, **k):
            raise OSError("disk full")
        vault.store_value = disk_full
        try:
            with self.assertRaises(cr.IssueFailed):
                cr.issue(rid)
        finally:
            vault.store_value = original
        self.assertEqual(tokens, {}, "the door discarded what it minted")
        self.assertFalse(cr.requests()[0][rid]["undoUnsettled"])

    def test_a_cloudflare_token_rolled_since_is_not_deleted(self):
        m, tokens, _calls = self.cloudflare()
        rid = self.file_cf()
        cr.issue(rid)
        vault.store_value("alpha-web", "prod", "CF_TOKEN", "rolled-in-place-" + "q" * 20, replace=True, tool="test")
        rev = cr.revoke(rid)
        self.assertEqual((rev["revocation"]["provider"], rev["revocation"]["proved"]), ("not-touched", False))
        self.assertEqual(len(tokens), 1, "the token the slot's new value may be is left alone")
        rows = crf.findings(cr.queue_path(), stamp()[:10])
        self.assertEqual([f["title_id"] for f in rows], ["Revoking {slot} at {provider} was not proved"])
        settled = cr.revoke(rid, settle="checked at Cloudflare: the token was rolled for session 0f3c")
        self.assertTrue(settled["revocation"]["proved"])
        self.assertEqual(crf.findings(cr.queue_path(), stamp()[:10]), [])


class TheCommandLine(Suite):

    def test_a_refusal_is_one_json_document_and_exit_2(self):
        rc, out, err = quiet(cr.main, ["request", "--agent", "nobody", "--provider", "openrouter", "--preset",
                                       "model-key", "--slot", "alpha-web/prod/KEY", "--reason", "x", "--json"])
        self.assertEqual(rc, 2, err)
        self.assertEqual(json.loads(out)["error"], "invalid")

    def test_the_whole_round_from_the_command_line(self):
        rc, out, err = quiet(cr.main, ["request", "--agent", AGENT, "--provider", "openrouter", "--preset",
                                       "model-key", "--limit", "5", "--slot", "alpha-web/prod/OPENROUTER_API_KEY",
                                       "--reason", "release notes", "--json"])
        self.assertEqual(rc, 0, err)
        rid = json.loads(out)["requestId"]
        rc, out, err = quiet(cr.main, ["list"])
        self.assertIn(rid[:8], out)
        rc, out, err = quiet(cr.main, ["issue", rid[:8], "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["state"], "issued")
        rc, out, err = quiet(cr.main, ["show", rid[:8], "--json"])
        self.assertEqual(json.loads(out)["decisions"][-1]["decision"]["outcome"], "issued")
        rc, out, err = quiet(cr.main, ["revoke", rid[:8]])
        self.assertEqual(rc, 0, err)
        self.assertIn("proved", out)
        self.assertNotIn(VALUE, out + err)


if __name__ == "__main__":
    unittest.main()
