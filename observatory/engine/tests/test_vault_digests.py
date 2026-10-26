#!/usr/bin/env python3
"""Vault slots across two machines, compared by keyed digests (OBS-194, step 1).

Two temporary workspaces stand for two machines: each has its own vault, state folder and machine
name. The second one's manifest is placed in the first one's peers folder by hand, which is what
step 1 asks the operator to do until step 2 carries manifests over the network."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

VALUE_A = "synthetic-value-alpha-0001"
VALUE_B = "synthetic-value-bravo-0002"
VALUE_C = "synthetic-value-charlie-0003"


class Machine:
    def __init__(self, base: Path, name: str, peers: Path):
        self.name = name
        self.root = base / name
        self.vault = self.root / "secrets/projects"
        self.state = self.root / "state"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVATORY_")}
        self.env.update({"OBSERVATORY_HOME": str(self.root / "home"), "OBSERVATORY_VAULT_DIR": str(self.vault),
                         "OBSERVATORY_STATE": str(self.state), "OBSERVATORY_DATA": str(self.root / "projects"),
                         "OBSERVATORY_REGISTRY": str(self.root / "registry"),
                         "OBSERVATORY_MACHINE_NAME": name, "OBSERVATORY_VAULT_PEERS": str(peers)})
        self.outputs: list[str] = []

    def run(self, tool: str, *args: str, value: str | None = None, check: int | None = 0):
        p = subprocess.run([sys.executable, str(TOOLS / tool), *args], input=value, text=True,
                           capture_output=True, env=self.env, timeout=60)
        self.outputs.append(p.stdout + p.stderr)
        if check is not None:
            assert p.returncode == check, (tool, args, p.returncode, p.stdout, p.stderr)
        return p

    def put(self, project: str, env: str, name: str, value: str):
        self.run("vault.py", "put", project, env, name, value=value)

    def digests(self, *args: str, check: int | None = 0):
        return self.run("vault_digests.py", *args, check=check)

    @property
    def manifest(self) -> Path:
        return self.state / "vault-digests/local.json"


class VaultDigestsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="observatory-vault-digests-")
        base = Path(self.temp.name).resolve()
        self.a = Machine(base, "machine-a", base / "machine-a-peers")
        self.b = Machine(base, "machine-b", base / "machine-b-peers")
        (base / "machine-a-peers").mkdir()
        (base / "machine-b-peers").mkdir()
        self.peers_a = base / "machine-a-peers"

    def tearDown(self):
        for machine in (self.a, self.b):
            text = "\n".join(machine.outputs)
            for value in (VALUE_A, VALUE_B, VALUE_C):
                self.assertNotIn(value, text, "a value reached a command's output")
            key = machine.vault / ".estate-digest-key"
            if key.is_file():
                self.assertNotIn(key.read_text().strip(), text, "the estate key reached a command's output")
        self.temp.cleanup()

    def share_key(self):
        """Machine B takes machine A's estate key: export to a new file, import on stdin."""
        exported = Path(self.temp.name) / "estate-key"
        self.a.digests("key", "export", str(exported))
        self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
        self.b.run("vault_digests.py", "key", "import", value=exported.read_text())
        exported.unlink()

    def deliver_b(self):
        self.b.digests("write")
        (self.peers_a / "machine-b.json").write_text(self.b.manifest.read_text())

    def view_a(self, *args: str, check: int | None = None) -> dict:
        p = self.a.digests("view", "--json", *args, check=check)
        return json.loads(p.stdout)

    def cell(self, view: dict, slot: str, machine: str = "machine-b") -> dict:
        row = next(r for r in view["slots"] if r["slot"] == slot)
        return row["machines"][machine]

    def test_write_creates_the_key_in_the_vault_and_a_manifest_without_values(self):
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        p = self.a.digests("write")
        key = self.a.vault / ".estate-digest-key"
        self.assertTrue(key.is_file())
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.a.manifest.stat().st_mode & 0o777, 0o600)
        text = self.a.manifest.read_text()
        self.assertNotIn(VALUE_A, text)
        self.assertNotIn(hashlib.sha256(VALUE_A.encode()).hexdigest(), text, "an unkeyed hash is in the manifest")
        self.assertNotIn(key.read_text().strip(), text + p.stdout)
        doc = json.loads(text)
        self.assertEqual(doc["machine"], "machine-a")
        self.assertEqual(doc["algorithm"], "HMAC-SHA-256")
        self.assertEqual(list(doc["slots"]), ["alpha-web/prod/API_TOKEN"])
        import hmac
        expected = hmac.new(bytes.fromhex(key.read_text().strip()), VALUE_A.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(doc["slots"]["alpha-web/prod/API_TOKEN"]["digest"], expected)
        # The key is not a slot: no listing names it.
        listing = self.a.run("vault.py", "list").stdout
        self.assertNotIn("estate-digest-key", listing)
        # A second write keeps the key.
        before = key.read_text()
        self.a.digests("write")
        self.assertEqual(key.read_text(), before)

    def test_all_matching_is_one_line(self):
        self.share_key()
        for m in (self.a, self.b):
            m.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
            m.put("beta-api", "local", "DB_PASSWORD", VALUE_B)
        self.deliver_b()
        p = self.a.digests("view", check=0)
        self.assertEqual(len(p.stdout.strip().splitlines()), 1, p.stdout)
        self.assertIn("All 2 slot(s) match on 2 machines", p.stdout)
        self.assertTrue(self.view_a(check=0)["all_match"])

    def test_same_differs_only_here_and_missing(self):
        self.share_key()
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.a.put("alpha-web", "prod", "SESSION_SALT", VALUE_B)
        self.b.put("alpha-web", "prod", "SESSION_SALT", VALUE_C)
        self.a.put("alpha-web", "stage", "ONLY_A", VALUE_B)
        self.b.put("beta-api", "local", "ONLY_B", VALUE_C)
        self.deliver_b()
        view = self.view_a(check=1)
        self.assertFalse(view["all_match"])
        self.assertEqual(self.cell(view, "alpha-web/prod/API_TOKEN")["state"], "same")
        self.assertEqual(self.cell(view, "alpha-web/prod/SESSION_SALT")["state"], "differs")
        self.assertEqual(self.cell(view, "alpha-web/stage/ONLY_A")["state"], "only here")
        missing = next(r for r in view["slots"] if r["slot"] == "beta-api/local/ONLY_B")
        self.assertFalse(missing["here"])
        self.assertEqual(missing["machines"]["machine-b"]["state"], "missing")
        self.assertEqual(missing["holders"], ["machine-b"])
        self.assertIn("1 same, 1 differs, 1 only here, 1 missing, 0 unreachable", view["summary"])
        self.assertIsNone(re.search(r"[0-9a-f]{64}", json.dumps(view)), "the view never prints a digest")

    def test_an_old_answer_is_unreachable_with_its_time(self):
        self.share_key()
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.digests("write")
        doc = json.loads(self.b.manifest.read_text())
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc["generated_at"] = old
        (self.peers_a / "machine-b.json").write_text(json.dumps(doc))
        view = self.view_a(check=1)
        cell = self.cell(view, "alpha-web/prod/API_TOKEN")
        self.assertEqual(cell, {"state": "unreachable", "last_answer": old})
        self.assertIn("older than 48 hours", view["machines"][0]["reason"])
        text = self.a.digests("view", check=1).stdout
        self.assertIn(f"machine-b: unreachable", text)
        self.assertIn(old, text)
        # A wider window takes the same answer as current.
        self.assertTrue(self.view_a("--max-age-hours", "240", check=0)["all_match"])

    def test_another_estate_key_is_unreachable_not_differs(self):
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.deliver_b()      # B made its own key: the same value digests differently
        view = self.view_a(check=1)
        self.assertEqual(self.cell(view, "alpha-web/prod/API_TOKEN")["state"], "unreachable")
        self.assertIn("another estate key", view["machines"][0]["reason"])

    def test_an_unreadable_manifest_is_named_and_never_reads_as_all_matching(self):
        self.share_key()
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.deliver_b()
        (self.peers_a / "broken.json").write_text("{not json")
        view = self.view_a(check=1)
        self.assertFalse(view["all_match"])
        self.assertTrue(any("broken.json" in d["reason"] for d in view["degraded"]))

    def test_no_peer_is_said_not_read_as_matching(self):
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        view = self.view_a(check=1)
        self.assertFalse(view["all_match"])
        self.assertIn("no peer manifest", view["summary"])

    def test_a_slot_marked_different_on_purpose(self):
        self.share_key()
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_B)
        self.deliver_b()
        self.a.digests("mark", "alpha-web", "prod", "API_TOKEN", "--why", "each machine has its own token")
        view = self.view_a(check=0)
        cell = self.cell(view, "alpha-web/prod/API_TOKEN")
        self.assertEqual(cell["state"], "differs")
        self.assertEqual(cell["on_purpose"], {"why": "each machine has its own token", "by": "here"})
        self.assertTrue(view["all_match"], "a difference on purpose is not a mismatch")
        manifest = json.loads(self.a.manifest.read_text())
        self.assertIn("on_purpose", manifest["slots"]["alpha-web/prod/API_TOKEN"])
        movements = self.a.run("vault.py", "movements").stdout
        self.assertIn("mark-differs", movements)
        self.a.digests("unmark", "alpha-web", "prod", "API_TOKEN")
        self.assertNotIn("on_purpose", self.cell(self.view_a(check=1), "alpha-web/prod/API_TOKEN"))
        # A slot this machine does not hold cannot be marked.
        p = self.a.digests("mark", "alpha-web", "prod", "ABSENT", "--why", "never stored here", check=2)
        self.assertIn("nothing at", p.stderr)

    def test_a_rotation_marks_the_other_copies_stale(self):
        self.share_key()
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.deliver_b()
        self.assertTrue(self.view_a(check=0)["all_match"])
        self.a.run("vault.py", "rotate", "alpha-web", "prod", "API_TOKEN", value=VALUE_C)
        cell = self.cell(self.view_a(check=1), "alpha-web/prod/API_TOKEN")
        self.assertEqual(cell["state"], "differs")
        self.assertEqual(cell["stale"]["where"], "peer")
        self.assertIn("stale on machine-b", self.a.digests("view", check=1).stdout)
        # Seen from B, its own copy is the stale one.
        (Path(self.b.env["OBSERVATORY_VAULT_PEERS"]) / "machine-a.json").write_text(self.a.manifest.read_text())
        view_b = json.loads(self.b.digests("view", "--json", check=1).stdout)
        b_cell = next(r for r in view_b["slots"] if r["slot"] == "alpha-web/prod/API_TOKEN")["machines"]["machine-a"]
        self.assertEqual(b_cell["stale"]["where"], "here")
        # Once B holds the new value too, the copies are the same again.
        self.b.run("vault.py", "rotate", "alpha-web", "prod", "API_TOKEN", value=VALUE_C)
        self.deliver_b()
        self.assertTrue(self.view_a(check=0)["all_match"])

    def test_key_import_and_export_refusals(self):
        bad = self.b.run("vault_digests.py", "key", "import", value="not-a-key", check=2)
        self.assertIn("64 lowercase hexadecimal", bad.stderr)
        # From the file `key export` wrote, as the documentation hands it.
        exported = Path(self.temp.name) / "estate-key"
        self.a.digests("key", "export", str(exported))
        self.b.digests("key", "import", str(exported))
        self.assertEqual(self.b.digests("key", "status").stdout.split()[2],
                         self.a.digests("key", "status").stdout.split()[2])
        exported.unlink()
        other = "ab" * 32
        p = self.b.run("vault_digests.py", "key", "import", value=other, check=2)
        self.assertIn("already holds another estate key", p.stderr)
        self.assertNotIn(other, p.stdout + p.stderr)
        self.b.run("vault_digests.py", "key", "import", "--replace", value=other)
        status = self.b.digests("key", "status").stdout
        self.assertIn("value hidden", status)
        self.assertNotIn(other, status)
        target = Path(self.temp.name) / "exists"
        target.write_text("keep")
        self.b.digests("key", "export", str(target), check=2)
        self.assertEqual(target.read_text(), "keep")

    def test_credentials_name_the_machines_holding_a_slot(self):
        self.share_key()
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.a.put("alpha-web", "prod", "ONLY_A", VALUE_B)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.a.digests("write")
        self.deliver_b()
        code = ("import json, survey; out = survey.credentials('alpha-web'); "
                "print(json.dumps({'vault': out['vault'], 'degraded': out['degraded']}))")
        p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), text=True, capture_output=True,
                           env=dict(self.a.env, PYTHONPATH=str(ROOT)), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.a.outputs.append(p.stdout + p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        rows = {r["name"]: r for r in out["vault"]}
        self.assertEqual(rows["API_TOKEN"]["machines"], ["machine-a", "machine-b"])
        self.assertEqual(rows["ONLY_A"]["machines"], ["machine-a"])
        self.assertFalse([d for d in out["degraded"] if d.get("source") == "vault-digests"])


class CompareIsPure(unittest.TestCase):
    """The comparison reads no file, no key and no value."""

    def test_rotation_and_marks_from_the_peer_side(self):
        import vault_digests as vd
        at = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc)
        local = {"machine": "here", "generated_at": "2026-10-26T00:00:00Z", "key_id": "k",
                 "slots": {"p/prod/A": {"digest": "1" * 64, "rotated_at": None},
                           "p/prod/B": {"digest": "3" * 64, "rotated_at": None}}}
        peer = {"machine": "there", "generated_at": "2026-10-25T23:00:00Z", "key_id": "k",
                "slots": {"p/prod/A": {"digest": "2" * 64, "rotated_at": "2026-10-20T00:00:00Z"},
                          "p/prod/B": {"digest": "4" * 64, "rotated_at": None,
                                       "on_purpose": {"why": "per-machine", "at": "x"}}}}
        view = vd.compare(local, [peer], at=at)
        a = view["slots"][0]["machines"]["there"]
        b = view["slots"][1]["machines"]["there"]
        self.assertEqual(a["stale"], {"where": "here", "rotated_there_at": "2026-10-20T00:00:00Z"})
        self.assertEqual(b["on_purpose"]["by"], "peer")
        self.assertFalse(view["all_match"])

    def test_two_manifests_naming_one_machine_are_degraded(self):
        import vault_digests as vd
        at = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc)
        local = {"machine": "here", "generated_at": "2026-10-26T00:00:00Z", "key_id": "k", "slots": {}}
        peer = {"machine": "there", "generated_at": "2026-10-26T00:00:00Z", "key_id": "k", "slots": {}}
        view = vd.compare(local, [peer, dict(peer)], at=at)
        self.assertEqual(len(view["machines"]), 1)
        self.assertEqual(view["degraded"][0]["code"], "duplicate-machine")


if __name__ == "__main__":
    unittest.main()
