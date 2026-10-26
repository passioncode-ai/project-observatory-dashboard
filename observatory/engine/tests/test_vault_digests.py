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


class TwoMachines(unittest.TestCase):
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


class VaultDigestsTests(TwoMachines):
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


class ReviewFindings(TwoMachines):
    """The independent review of PR #364: one test per finding."""

    def peer_doc(self) -> dict:
        self.share_key()
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.digests("write")
        return json.loads(self.b.manifest.read_text())

    def place(self, name: str, doc) -> None:
        (self.peers_a / name).write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def good_peer_stays(self, view: dict) -> None:
        """One bad peer file never blanks the others."""
        self.assertEqual(self.cell(view, "alpha-web/prod/API_TOKEN")["state"], "same")

    def bad_file(self, name: str, doc, good: dict | None = None) -> dict:
        self.place("machine-b.json", good or self.peer_doc())
        self.place(name, doc)
        view = self.view_a(check=1)
        self.good_peer_stays(view)
        self.assertFalse(view["all_match"])
        self.assertTrue(any(d["code"] == "unreadable-manifest" for d in view["degraded"]), view["degraded"])
        return view

    def test_m1_a_non_hex_key_id_is_refused_into_degraded(self):
        good = self.peer_doc()
        self.bad_file("machine-c.json", dict(good, machine="machine-c", key_id="clé-non-ascii-é"), good)
        # The credentials answer keeps the good peer.
        code = ("import json, survey; out = survey.credentials('alpha-web'); "
                "print(json.dumps({'vault': out['vault'], 'degraded': out['degraded']}))")
        p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), text=True, capture_output=True,
                           env=dict(self.a.env, PYTHONPATH=str(ROOT)), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(out["vault"][0]["machines"], ["machine-a", "machine-b"])
        self.assertFalse([d for d in out["degraded"] if d.get("code") == "machines-unknown"])

    def test_m2_rotated_at_and_on_purpose_are_validated(self):
        for i, row_patch in enumerate(({"rotated_at": 5}, {"rotated_at": "not a time"},
                                       {"on_purpose": {"why": "\x1b[31mred\x1b[0m text", "at": "x"}},
                                       {"on_purpose": {"why": "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2", "at": "x"}},
                                       {"on_purpose": {"why": "w" * 400, "at": "x"}},
                                       {"on_purpose": "a string"})):
            with self.subTest(row_patch=row_patch):
                doc = json.loads(self.b.manifest.read_text()) if self.b.manifest.is_file() else self.peer_doc()
                doc["machine"] = f"machine-x{i}"
                doc["slots"]["alpha-web/prod/API_TOKEN"].update(row_patch)
                self.place(f"machine-x{i}.json", doc)
                p = self.a.digests("view", check=1)
                self.assertNotIn("\x1b", p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn("a slot row is malformed", p.stdout)
                (self.peers_a / f"machine-x{i}.json").unlink()

    def test_m2_rotation_compares_times_not_strings(self):
        import vault_digests as vd
        at = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc)
        local = {"machine": "here", "generated_at": "2026-10-26T00:00:00Z", "key_id": "0" * 16,
                 "slots": {"p/prod/A": {"digest": "1" * 64, "rotated_at": "2026-10-20T10:00:00+02:00"}}}
        peer = {"machine": "there", "generated_at": "2026-10-25T23:00:00Z", "key_id": "0" * 16,
                "slots": {"p/prod/A": {"digest": "2" * 64, "rotated_at": "2026-10-20T09:00:00Z"}}}
        # 10:00+02:00 is 08:00Z, earlier than 09:00Z: the peer rotated later, so here is stale.
        cell = vd.compare(local, [peer], at=at)["slots"][0]["machines"]["there"]
        self.assertEqual(cell["stale"]["where"], "here")

    def test_m3_a_future_answer_is_unreachable_naming_the_clock(self):
        doc = self.peer_doc()
        doc["generated_at"] = (datetime.datetime.now(datetime.timezone.utc)
                               + datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.place("machine-b.json", doc)
        view = self.view_a(check=1)
        self.assertEqual(self.cell(view, "alpha-web/prod/API_TOKEN")["state"], "unreachable")
        self.assertIn("clock", view["machines"][0]["reason"])

    def test_m4_deeply_nested_json_is_refused(self):
        self.bad_file("deep.json", "[" * 100000 + "]" * 100000)

    def test_l5_export_never_follows_or_replaces(self):
        target = Path(self.temp.name) / "exported"
        os.symlink(Path(self.temp.name) / "elsewhere", target)
        self.a.digests("key", "export", str(target), check=2)
        self.assertFalse((Path(self.temp.name) / "elsewhere").exists())
        fresh = Path(self.temp.name) / "fresh"
        self.a.digests("key", "export", str(fresh))
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o600)

    def test_l7_a_lost_key_is_not_replaced_silently(self):
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.a.digests("write")
        (self.a.vault / ".estate-digest-key").unlink()
        p = self.a.digests("write", check=2)
        self.assertIn("key import", p.stderr)
        self.assertFalse((self.a.vault / ".estate-digest-key").exists())

    def test_l8_the_all_match_line_counts_differences_on_purpose(self):
        self.share_key()
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.b.put("alpha-web", "prod", "API_TOKEN", VALUE_B)
        self.a.digests("mark", "alpha-web", "prod", "API_TOKEN", "--why", "each machine has its own token")
        self.deliver_b()
        line = self.a.digests("view", check=0).stdout.strip()
        self.assertIn("1 differ on purpose", line)

    def test_l9_peer_file_names_are_screened(self):
        name = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2" + ".json"
        view = self.bad_file(name, "{not json")
        text = json.dumps(view)
        self.assertNotIn("A1b2C3d4E5f6G7h8I9j0K1l2", text)
        weird = "bad\x1bname.json"
        (self.peers_a / weird).write_text("{")
        p = self.a.digests("view", check=1)
        self.assertNotIn("\x1b", p.stdout)

    def test_l10_max_age_must_be_finite_and_positive(self):
        for bad in ("nan", "inf", "0", "-5"):
            with self.subTest(bad=bad):
                p = self.a.digests("view", "--max-age-hours", bad, check=2)
                self.assertNotIn("Traceback", p.stderr)

    def test_l12_a_peer_claiming_this_machine_is_named_as_such(self):
        doc = self.peer_doc()
        doc["machine"] = "machine-a"
        self.place("impostor.json", doc)
        view = self.view_a(check=1)
        self.assertTrue(any("names this machine" in d["reason"] for d in view["degraded"]), view["degraded"])

    def test_l13_a_credential_shaped_why_exits_2(self):
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        p = self.a.digests("mark", "alpha-web", "prod", "API_TOKEN", "--why",
                           "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2", check=2)
        self.assertNotIn("A1b2C3d4E5f6G7h8I9j0K1l2", p.stdout + p.stderr)

    def test_l14_the_digest_folder_is_owner_only(self):
        folder = self.a.state / "vault-digests"
        folder.mkdir(parents=True, mode=0o755)
        folder.chmod(0o755)
        self.a.digests("write")
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)

    def test_r2_1_an_extreme_rotation_time_never_crashes_the_view(self):
        doc = self.peer_doc()
        doc["slots"]["alpha-web/prod/API_TOKEN"]["rotated_at"] = "0001-01-01T00:00:00+14:00"
        self.place("machine-b.json", doc)
        p = self.a.digests("view", check=1)
        self.assertIn("a slot row is malformed", p.stdout)
        self.assertNotIn("None", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_r2_2_key_export_refuses_a_lost_key(self):
        self.a.put("alpha-web", "prod", "API_TOKEN", VALUE_A)
        self.a.digests("write")
        (self.a.vault / ".estate-digest-key").unlink()
        target = Path(self.temp.name) / "exported"
        p = self.a.digests("key", "export", str(target), check=2)
        self.assertIn("key import", p.stderr)
        self.assertFalse(target.exists())
        self.assertFalse((self.a.vault / ".estate-digest-key").exists())
        self.a.digests("write", check=2)

    def test_r2_4_a_manifest_is_read_with_a_limit(self):
        import vault_digests as vd
        from unittest.mock import patch
        path = self.peers_a / "big.json"
        path.write_text(json.dumps({"schema": vd.SCHEMA, "pad": "x" * 500}))
        with patch.object(vd, "MAX_MANIFEST_BYTES", 100):
            with self.assertRaises(vd.Refused):
                vd.read_manifest(path)
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.peers_a / "pipe.json")
            p = self.a.digests("view", check=1)
            self.assertIn("pipe.json", p.stdout)


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

    def test_r2_1_a_missing_rotated_at_key_is_never_indexed(self):
        import vault_digests as vd
        at = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc)
        local = {"machine": "here", "generated_at": "2026-10-26T00:00:00Z", "key_id": "0" * 16,
                 "slots": {"p/prod/A": {"digest": "1" * 64}}}
        peer = {"machine": "there", "generated_at": "2026-10-25T23:00:00Z", "key_id": "0" * 16,
                "slots": {"p/prod/A": {"digest": "2" * 64, "rotated_at": "2026-10-20T00:00:00Z"}}}
        cell = vd.compare(local, [peer], at=at)["slots"][0]["machines"]["there"]
        self.assertEqual(cell["stale"], {"where": "here", "rotated_there_at": "2026-10-20T00:00:00Z"})
        cell = vd.compare(peer | {"machine": "here"}, [local | {"machine": "there"}], at=at)["slots"][0]["machines"]["there"]
        self.assertEqual(cell["stale"]["where"], "peer")
        both = vd.compare(local, [peer | {"slots": {"p/prod/A": {"digest": "2" * 64}}}], at=at)
        self.assertNotIn("stale", both["slots"][0]["machines"]["there"])

    def test_r2_3_the_newest_answer_compares_times(self):
        import vault_digests as vd
        at = datetime.datetime(2026, 10, 26, 1, tzinfo=datetime.timezone.utc)
        local = {"machine": "here", "generated_at": "2026-10-26T00:30:00Z", "key_id": "0" * 16,
                 "slots": {"p/prod/A": {"digest": "1" * 64}}}
        # 01:00+02:00 is 23:00Z the day before: older, though its text sorts later.
        peer = {"machine": "there", "generated_at": "2026-10-26T01:00:00+02:00", "key_id": "0" * 16,
                "slots": {"p/prod/A": {"digest": "1" * 64}}}
        view = vd.compare(local, [peer], at=at)
        self.assertTrue(view["all_match"])
        self.assertIn("newest answer 2026-10-26T00:30:00Z", view["summary"])

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
