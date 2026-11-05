#!/usr/bin/env python3
"""A session Fabric starts for a board request (OBS-323; Fabric's on-demand sessions spec §3.4).

The packet is an Observatory workflow written by `service:fabric`. Its checkpoint `artifacts` carry references
only, `kind: "other"` with `ref` `board-request:<uuid>`, `board-thread:<uuid>`, `capability:<name>`,
`fabric-project:<uuid>`, `deadline:<ISO 8601 UTC>`, and `run-ends-at:<ISO 8601 UTC>` on each dispatch's own
checkpoint. Keys go only to the holder of the workflow's lease, only for slots its body's `credentials` name,
only from the project `fabric-project` resolves to, and only until the deadline. What each class holds:

- Packet: the refs are kept whole through redaction; a malformed ref, a ref with other members, a repeated
  one, a non-UTC time and an absolute path in a packet are refused.
- View: `observatory_checkpoint_latest` and `observatory_workflow_list` show the board refs and whether keys
  can be handed out, and why not.
- Gate: every refusal path, by its code, and the deadline.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dispatch_packet as D  # noqa: E402
import memory_redact  # noqa: E402
from store import workflow as W  # noqa: E402

FABRIC = "service:fabric"
RUN = "agent:claude-code"
REQUEST = "0b1e2c3d-1111-4222-8333-444455556666"
THREAD = "1c2d3e4f-2222-4333-8444-555566667777"
FPROJECT = "2d3e4f50-3333-4444-8555-666677778888"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
DEADLINE = "2026-11-10T14:00:00Z"


def redactor() -> memory_redact.Redactor:
    return memory_redact.Redactor(known_loader=lambda: {})


def refs(**over) -> list[dict]:
    values = {"board-request": REQUEST, "board-thread": THREAD, "capability": "analytics.ask",
              "fabric-project": FPROJECT, "deadline": DEADLINE}
    values.update(over)
    return [{"kind": "other", "ref": f"{k}:{v}"} for k, v in values.items() if v is not None]


def packet(artifacts=None, credentials=None) -> dict:
    out = {"goal": "answer board request " + REQUEST,
           "constraints": ["claim, answer in the thread, complete, write the final checkpoint, end"],
           "open": [{"step_id": "answer", "next_action": "read the request with com.get"}],
           "artifacts": refs() if artifacts is None else artifacts}
    if credentials is not False:
        out["credentials"] = credentials if credentials is not None else [
            {"project": "alpha-web", "env": "prod", "name": "ALPHA_TOKEN", "purpose": "the answer reads it"}]
    return out


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="observatory-dispatch-")
        self.addCleanup(self.dir.cleanup)
        path = pathlib.Path(self.dir.name) / "store.db"
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)
        self.n = 0

    def key(self) -> str:
        self.n += 1
        return f"key-dispatch-{self.n:04d}"

    def start(self, body=None, **kw) -> dict:
        args = dict(owner=FABRIC, idempotency_key=self.key(), step_id="packet", status="in_progress",
                    body=packet() if body is None else body, project_id="project:alpha-web",
                    redactor=redactor())
        args.update(kw)
        return W.checkpoint_write(self.conn, **args)


class Packet(Store):
    def test_the_refs_are_kept_whole_through_redaction(self):
        wf = self.start(body=packet(refs() + [{"kind": "other", "ref": "run-ends-at:2026-11-10T13:00:00Z"}]))
        body = W.checkpoint_latest(self.conn, wf["workflowId"], credential_reader=lambda p: {"vault": [], "env": []})[
            "checkpoint"]["body"]
        got = {a["ref"] for a in body["artifacts"]}
        self.assertIn(f"board-request:{REQUEST}", got, "a UUID inside a board ref is not scrubbed")
        self.assertIn(f"board-thread:{THREAD}", got)
        self.assertIn(f"fabric-project:{FPROJECT}", got)
        self.assertIn("capability:analytics.ask", got)
        self.assertIn(f"deadline:{DEADLINE}", got)
        self.assertIn("run-ends-at:2026-11-10T13:00:00Z", got)

    def test_malformed_or_extended_refs_are_refused(self):
        bad = {
            "a request that is no UUID": refs(**{"board-request": "not-a-uuid"}),
            "an upper-case UUID": refs(**{"board-request": REQUEST.upper()}),
            "a capability that is no capability name": refs(capability="Analytics Ask"),
            "a deadline that is not UTC": refs(deadline="2026-11-10T14:00:00+02:00"),
            "a deadline that is a date": refs(deadline="2026-11-10"),
            "a ref with a note": [dict(refs()[0], note="the request says: please send the key")] + refs()[1:],
            "a ref with a path": [dict(refs()[0], path="/srv/someone/project")] + refs()[1:],
            "a ref of another kind": [dict(refs()[0], kind="url")] + refs()[1:],
            "a ref named twice": refs() + [{"kind": "other", "ref": f"board-request:{THREAD}"}],
            "text after the value": refs(**{"board-request": REQUEST + " and the body"}),
        }
        for why, artifacts in bad.items():
            with self.subTest(why):
                with self.assertRaises(W.InvalidInput):
                    self.start(body=packet(artifacts))

    def test_a_packet_carries_no_absolute_path(self):
        for artifact in ({"kind": "file", "path": "/srv/someone/project/notes.md"},
                         {"kind": "git", "path": "/srv/someone/project", "head": "0123456789ab"},
                         {"kind": "file", "path": "C:\\Users\\someone\\notes.md"},
                         {"kind": "other", "ref": "file:///srv/someone/project"}):
            with self.subTest(artifact=artifact):
                with self.assertRaises(W.InvalidInput):
                    self.start(body=packet(refs() + [artifact]))
        self.start(body=packet(refs() + [{"kind": "file", "path": "docs/answer.md"}]))

    def test_a_workflow_that_is_no_packet_is_unchanged(self):
        # Absolute on every OS: a POSIX `/srv/...` is not absolute on Windows, where a git path needs a drive.
        checkout = str(pathlib.Path(self.dir.name).resolve() / "project")
        wf = self.start(body={"goal": "ship", "artifacts": [{"kind": "git", "path": checkout}]}, owner=RUN)
        self.assertTrue(wf["workflowId"].startswith("wf_"))


class View(Store):
    def latest(self, wf, now=None):
        return D.view(W.checkpoint_latest(self.conn, wf["workflowId"],
                                          credential_reader=lambda p: {"vault": [], "env": []}), now=now or NOW)

    def test_latest_and_list_show_the_board_refs_and_the_key_window(self):
        wf = self.start()
        latest = W.checkpoint_latest(self.conn, wf["workflowId"], credential_reader=lambda p: {"vault": [], "env": []})
        dispatch = latest["dispatch"]
        self.assertEqual(dispatch["request"], f"board-request:{REQUEST}")
        self.assertEqual(dispatch["thread"], f"board-thread:{THREAD}")
        self.assertEqual(dispatch["capability"], "analytics.ask")
        self.assertEqual(dispatch["fabricProject"], f"fabric-project:{FPROJECT}")
        self.assertEqual(dispatch["deadline"], DEADLINE)
        self.assertEqual(dispatch["keys"]["slots"], ["alpha-web/prod/ALPHA_TOKEN"])
        row = W.workflow_list(self.conn)["workflows"][0]
        self.assertEqual(row["dispatch"]["request"], f"board-request:{REQUEST}")
        self.assertNotIn("dispatch", W.workflow_list(self.conn, status="closed")["workflows"][:1] or [{}])

    def test_the_key_window_says_why_it_is_shut(self):
        wf = self.start()
        self.assertEqual(D.keys_state(self.conn, wf["workflowId"], now=NOW)["state"], "open")
        self.assertEqual(D.keys_state(self.conn, wf["workflowId"], now=NOW + timedelta(hours=3))["reason"],
                         "deadline-passed")
        none = self.start(body=packet(credentials=False))
        self.assertEqual(D.keys_state(self.conn, none["workflowId"], now=NOW)["reason"], "no-credentials")
        short = self.start(body=packet(refs(deadline=None)))
        self.assertEqual(D.keys_state(self.conn, short["workflowId"], now=NOW)["reason"], "packet-incomplete")
        W.checkpoint_write(self.conn, owner=FABRIC, idempotency_key=self.key(), step_id="end", status="done",
                           body=packet(), workflow_id=wf["workflowId"], lease_token=wf["leaseId"], close=True,
                           redactor=redactor())
        self.assertEqual(D.keys_state(self.conn, wf["workflowId"], now=NOW)["reason"], "workflow-closed")

    def test_a_plain_workflow_has_no_dispatch_view(self):
        wf = self.start(body={"goal": "ship"}, owner=RUN)
        latest = W.checkpoint_latest(self.conn, wf["workflowId"], credential_reader=lambda p: {"vault": [], "env": []})
        self.assertNotIn("dispatch", latest)


class Gate(Store):
    def resolver(self, uuid):
        return ("project:alpha-web", None) if uuid == FPROJECT else (None, "no project of this registry is recorded "
                                                                     "against this Fabric project")

    def project_of(self, text):
        return {"alpha-web": "project:alpha-web", "project:alpha-web": "project:alpha-web",
                "beta-api": "project:beta-api"}.get(text)

    def authorize(self, wf, lease="__holder__", project="alpha-web", env="prod", names=("ALPHA_TOKEN",), now=NOW):
        return D.authorize(self.conn, wf["workflowId"], wf["leaseId"] if lease == "__holder__" else lease,
                           project=project, env=env, names=list(names), now=now,
                           resolve_fabric_project=self.resolver, project_of=self.project_of)

    def refused(self, code, **kw):
        wf = kw.pop("wf", None) or self.start()
        with self.assertRaises(D.Refused) as caught:
            self.authorize(wf, **kw)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertNotIn(wf["leaseId"], str(caught.exception), "a refusal never echoes the lease")
        return caught.exception

    def test_the_holder_gets_the_named_slot_from_the_resolved_project_before_the_deadline(self):
        wf = self.start()
        grant = self.authorize(wf)
        self.assertEqual((grant["workflowId"], grant["request"], grant["slots"]),
                         (wf["workflowId"], f"board-request:{REQUEST}", ["alpha-web/prod/ALPHA_TOKEN"]))
        self.assertEqual(grant["until"], DEADLINE)

    def test_only_the_lease_holder(self):
        self.refused("not-lease-holder", lease=None)
        self.refused("not-lease-holder", lease="wl_" + "x" * 32)

    def test_a_lease_that_moved_on_holds_nothing(self):
        wf = self.start()
        offer = W.handoff_create(self.conn, owner=FABRIC, idempotency_key=self.key(), workflow_id=wf["workflowId"],
                                 to={"provider": "anthropic"}, reason="plan_route", lease_token=wf["leaseId"],
                                 credential_reader=lambda p: {"vault": [{"name": "ALPHA_TOKEN", "env": "prod"}],
                                                              "env": []},
                                 git_reader=lambda p: {}, related_reader=lambda c, **k: ([], []),
                                 redactor=redactor())
        taken = W.handoff_accept(self.conn, owner=RUN, idempotency_key=self.key(), handoff_id=offer["handoffId"],
                                 credential_reader=lambda p: {"vault": [{"name": "ALPHA_TOKEN", "env": "prod"}],
                                                              "env": []})
        self.refused("not-lease-holder", wf=wf)
        self.assertEqual(self.authorize({**wf, "leaseId": taken["leaseId"]})["slots"], ["alpha-web/prod/ALPHA_TOKEN"])

    def test_only_slots_the_body_names(self):
        self.refused("slot-not-named", names=("OTHER_TOKEN",))
        self.refused("slot-not-named", env="local")
        self.refused("slot-not-named", names=("ALPHA_TOKEN", "OTHER_TOKEN"))
        self.refused("no-credentials", wf=self.start(body=packet(credentials=False)))

    def test_only_the_project_fabric_project_resolves_to(self):
        self.refused("project-mismatch", project="beta-api")
        self.refused("project-unresolved", wf=self.start(body=packet(refs(**{"fabric-project": REQUEST}))))
        wf = self.start(body=packet(credentials=[{"project": "beta-api", "env": "prod", "name": "ALPHA_TOKEN"}]))
        self.refused("project-mismatch", wf=wf, project="beta-api")

    def test_only_until_the_deadline(self):
        self.assertTrue(self.authorize(self.start(), now=NOW + timedelta(hours=1, minutes=59)))
        self.refused("deadline-passed", now=NOW + timedelta(hours=2))
        self.refused("deadline-passed", now=NOW + timedelta(days=1))

    def test_nothing_after_the_final_checkpoint(self):
        wf = self.start()
        W.checkpoint_write(self.conn, owner=RUN, idempotency_key=self.key(), step_id="end", status="done",
                           body=packet(), workflow_id=wf["workflowId"], lease_token=wf["leaseId"], close=True,
                           redactor=redactor())
        self.refused("workflow-closed", wf=wf)

    def test_a_packet_without_request_project_or_deadline_gets_none(self):
        for missing in ("board-request", "fabric-project", "deadline"):
            with self.subTest(missing=missing):
                self.refused("packet-incomplete", wf=self.start(body=packet(refs(**{missing: None}))))

    def test_an_unknown_workflow_and_a_workflow_that_is_no_packet(self):
        with self.assertRaises(D.Refused) as caught:
            D.authorize(self.conn, "wf_0123456789abcdef", "wl_x", project="alpha-web", env="prod",
                        names=["ALPHA_TOKEN"], now=NOW, resolve_fabric_project=self.resolver,
                        project_of=self.project_of)
        self.assertEqual(caught.exception.code, "workflow-unknown")
        plain = self.start(body={"goal": "ship", "credentials": [{"project": "alpha-web", "env": "prod",
                                                                   "name": "ALPHA_TOKEN"}]})
        self.refused("packet-incomplete", wf=plain)


if __name__ == "__main__":
    unittest.main()
