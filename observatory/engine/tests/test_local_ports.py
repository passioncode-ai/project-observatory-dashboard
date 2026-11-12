#!/usr/bin/env python3
"""OBS-354: a port per OS account for every Observatory listener.

Everything the host would say is injected: the account count, this account, whether a uid exists, who
listens on a port, whether it binds, and the descriptors' ports. Other accounts' claims are written in a
temporary folder under the real uid's name (so the owner check holds) while this process plays account 900.
"""
from __future__ import annotations
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration as config
import local_ports as lp
import workspace

ME = "900"                       # this process, as the allocator sees it: uid 900 starts at block 900 % 75 = 0
OTHER = str(os.getuid()) if hasattr(os, "getuid") else "501"
FIRST_BLOCK = {"server": 47400, "memory_http": 47402, "relay": 47406, "keyserver": 47407}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.claims = self.base / "claims"
        self.claims.mkdir()
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(self.base / "user"),
                                           "USERPROFILE": str(self.base / "user"), "OBSERVATORY_SYSTEM_SETUP": "0",
                                           "OBSERVATORY_MACHINE_ID": "m-" + "a" * 24,
                                           lp.CLAIMS_ENV: str(self.claims)}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        self.n = 2
        self.listening: dict[int, str] = {}
        self.bound: set[int] = set()
        self.declared: set[int] = set()
        self.server = None
        for name, fake in (("accounts", lambda: self.n), ("account", lambda: ME),
                           ("account_exists", lambda who: who in (OTHER, ME)),
                           ("listener_kind", lambda port: self.listening.get(port, "none")),
                           ("bind_free", lambda port: port not in self.bound),
                           ("declared_ports", lambda: set(self.declared)),
                           ("current_server_port", lambda base=None: self.server)):
            p = patch.object(lp, name, fake)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(lp, "RACE_WAIT", 0)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def other_claim(self, ports, *, who=OTHER, at="2026-11-12T00:00:00Z", name=None):
        path = self.claims / f"{lp.CLAIM_PREFIX}{name or who}.json"
        path.write_text(json.dumps({"schema": lp.CLAIM_SCHEMA, "service": lp.SERVICE, "account": who,
                                    "ports": list(ports), "updatedAt": at}))
        return path

    def my_claim(self) -> dict:
        return json.loads((self.claims / f"{lp.CLAIM_PREFIX}{ME}.json").read_text())


class Ports(Fixture):
    def test_one_account_records_legacy_and_changes_nothing(self):
        self.n, self.server = 1, 47311
        receipt = __import__("paths").SCRATCH / "serverd.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text('{"port": 47311}')
        before = receipt.read_bytes()
        out = lp.allocate()
        self.assertEqual((out["scheme"], out["ports"]), ("legacy", lp.LEGACY))
        self.assertEqual(lp.record()["server"], 47311)
        self.assertEqual(receipt.read_bytes(), before, "the receipt is left as it was")
        self.assertEqual(self.my_claim()["ports"], sorted(lp.LEGACY.values()), "the claim is written for the next account")

    def test_one_account_keeps_a_custom_port_as_chosen(self):
        self.n, self.server = 1, 48000
        out = lp.allocate()
        self.assertEqual((out["scheme"], out["ports"]["server"], out["ports"]["relay"]), ("chosen", 48000, 47317))

    def test_fresh_install_on_a_multi_account_host_never_takes_legacy(self):
        out = lp.allocate()
        self.assertEqual((out["scheme"], out["ports"]), ("allocated", FIRST_BLOCK))
        self.assertNotIn(47311, out["ports"].values())

    def test_existing_install_keeps_legacy_when_free_or_own(self):
        self.server = 47311
        self.listening = {47311: "this-account"}
        out = lp.allocate()
        self.assertEqual((out["scheme"], out["ports"]), ("legacy", lp.LEGACY))

    def test_existing_install_moves_when_legacy_is_held_by_another_account(self):
        self.server = 47311
        self.listening = {47311: "another-account"}
        with patch("subprocess.run") as run:
            run.return_value.returncode, run.return_value.stderr, run.return_value.stdout = 0, "", ""
            out = lp.allocate(reinstall=True)
        self.assertEqual((out["scheme"], out["ports"]["server"]), ("allocated", 47400))
        argv = run.call_args.args[0]
        self.assertEqual(argv[-3:], ["--install", "--port", "47400"])
        self.assertEqual(out["reinstall"]["status"], "reinstalled")

    def test_block_skips_claimed_listening_bound_and_declared_ports(self):
        self.other_claim([47402])                         # block 0: claimed
        self.listening = {47414: "system"}                # block 1: listening
        self.bound = {47423}                              # block 2: bound by something not listed
        self.declared = {47424}                           # block 3: declared by a descriptor
        self.assertEqual(lp.allocate()["ports"]["server"], 47432)

    def test_two_accounts_racing_end_on_distinct_blocks(self):
        planted = []

        def other_writes_first(_seconds):
            if not planted:
                planted.append(self.other_claim(FIRST_BLOCK.values(), at="2000-01-01T00:00:00Z"))
        with patch.object(lp.time, "sleep", other_writes_first):
            out = lp.allocate()
        self.assertEqual(out["ports"]["server"], 47408, "the later writer moves to the next block")
        self.assertTrue(set(out["ports"].values()).isdisjoint(FIRST_BLOCK.values()))

    def test_claim_owner_mismatch_link_size_and_unknown_uid_are_ignored(self):
        self.other_claim([47400], who="12345")            # owner is not 12345: ignored
        big = self.other_claim([47408], name="777")
        big.write_text(" " * (lp.CLAIM_MAX_BYTES + 1))
        real = self.base / "real.json"
        real.write_text(json.dumps({"schema": lp.CLAIM_SCHEMA, "service": lp.SERVICE, "account": OTHER,
                                    "ports": [47400], "updatedAt": "x"}))
        (self.claims / f"{lp.CLAIM_PREFIX}{OTHER}.json").symlink_to(real)
        claims, readable = lp.read_claims()
        self.assertTrue(readable)
        self.assertEqual(claims, [])
        with patch.object(lp, "account_exists", lambda who: False):
            (self.claims / f"{lp.CLAIM_PREFIX}{OTHER}.json").unlink()
            self.other_claim([47400])
            self.assertEqual(lp.read_claims()[0], [], "a uid that no longer exists claims nothing")

    def test_unknown_account_count_is_treated_as_several(self):
        self.n = None
        self.assertIn("port-unallocated", [n["code"] for n in lp.show()["notes"]])
        self.assertEqual(lp.allocate()["scheme"], "allocated")
        self.assertNotIn("port-unallocated", [n["code"] for n in lp.show()["notes"]])

    def test_range_exhausted_writes_nothing(self):
        self.listening = {p: "another-account" for p in range(lp.FIRST, lp.LAST + 1)}
        with self.assertRaises(lp.PortError) as caught:
            lp.allocate()
        self.assertEqual(caught.exception.code, "port-range-exhausted")
        self.assertIsNone(lp.record())
        self.assertFalse((self.claims / f"{lp.CLAIM_PREFIX}{ME}.json").exists())

    def test_clients_read_env_then_record_then_receipt_and_never_a_default_on_a_shared_host(self):
        with self.assertRaises(lp.PortError) as caught:
            lp.port("server")
        self.assertEqual(caught.exception.code, "port-unallocated")
        receipt = __import__("paths").SCRATCH / "serverd.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text('{"port": 47999}')
        self.assertEqual(lp.port("server"), 47999)
        with self.assertRaises(lp.PortError):
            lp.port("relay")                              # only the server has a receipt
        lp.allocate()
        self.assertEqual((lp.port("server"), lp.port("relay")), (47400, 47406))
        with patch.dict(os.environ, {"OBSERVATORY_SERVER_PORT": "50001"}):
            self.assertEqual(lp.port("server"), 50001)
            shown = lp.show()
            self.assertEqual(shown["listeners"]["server"]["source"], "env")
            self.assertIn("port-env-overrides-record", [n["code"] for n in shown["notes"]])
        self.n = 1
        lp.release()
        receipt.unlink()
        self.assertEqual(lp.port("memory_http"), 47313, "one account: the historical default")

    def test_stale_place_is_reallocated(self):
        lp.allocate()
        self.assertIsNotNone(lp.record())
        with patch.dict(os.environ, {"OBSERVATORY_MACHINE_ID": "m-" + "b" * 24}):
            self.assertIsNone(lp.record(), "a record from another place is not read")
            self.assertIn("port-record-stale", [n["code"] for n in lp.show()["notes"]])
            self.assertEqual(lp.allocate()["scheme"], "allocated")
            import machine_identity                      # OBS-357: the record names the place of host and account
            self.assertEqual(machine_identity.host_of(json.loads(config.local_path(self.home).read_text())["place"]),
                             "m-" + "b" * 24)

    def test_set_refuses_another_accounts_port_and_records_a_choice(self):
        self.other_claim([47500])
        with self.assertRaises(lp.PortError) as caught:
            lp.set_port("server", 47500)
        self.assertEqual(caught.exception.code, "port-claimed-by-another-account")
        out = lp.set_port("server", 47600)
        self.assertEqual((out["scheme"], out["ports"]["server"], out["ports"]["relay"]), ("chosen", 47600, 47317))

    def test_the_command_answers_json(self):
        with patch("sys.stdout") as out:
            self.assertEqual(lp.main(["allocate", "--json"]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertEqual(json.loads(printed)["ports"], FIRST_BLOCK)
        self.assertIn("ports", workspace.doctor(self.home))


class Clients(Fixture):
    """Each client of the record (spec 3.5) and the server's bind message (3.6)."""

    def test_every_client_reads_the_record_and_refuses_on_a_shared_host_without_one(self):
        import secret_client
        from node.repair import LifecycleExecutor
        sys.path.insert(0, str(ROOT / "tools"))
        import dashboard_open
        sys.path.insert(0, str(ROOT / "agent"))
        import assistant
        with self.assertRaises(secret_client.ClientRefused) as refused:
            secret_client.node_url()
        self.assertEqual(refused.exception.args[0], "port-unallocated")
        with self.assertRaises(lp.PortError):
            LifecycleExecutor(lambda: "")
        with patch("sys.stderr"):
            self.assertIsNone(dashboard_open.account_port(False))
        with self.assertRaises(assistant.AssistantError):
            assistant._port()
        lp.allocate()
        self.assertEqual(secret_client.node_url(), "http://127.0.0.1:47400/mcp")
        self.assertEqual(LifecycleExecutor(lambda: "").port, 47400)
        self.assertEqual(dashboard_open.account_port(False), 47400)
        self.assertEqual(assistant._port(), 47400)

    def test_the_server_names_who_holds_the_port(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import serverd
        for kind, code in (("another-account", "port-held-by-another-account"),
                           ("unknown", "port-held-by-unknown-owner")):
            self.listening = {47311: kind}
            self.assertIn(code, serverd._holder_sentence(47311))
        self.listening = {47311: "this-account"}
        self.assertIn("this account", serverd._holder_sentence(47311))

    def test_the_apps_see_another_accounts_server_and_never_probe_it(self):
        sys.path.insert(0, str(ROOT / "agent"))
        import assistant
        lp.allocate()
        self.listening = {47400: "another-account"}
        with patch("tools.dashboard_open.served_workspace") as probe:
            state = assistant.dashboard()
        probe.assert_not_called()
        self.assertEqual((state["server"], state.get("url")), ("other-account", None))
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant.serve()
        self.assertEqual(caught.exception.args[0], "dashboard-port-foreign")

    def test_an_install_with_a_port_another_account_claims_is_refused(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import serverd
        self.other_claim([47500])
        args = type("A", (), {"port": 47500, "install": True, "run": False})()
        with self.assertRaises(lp.PortError) as caught:
            serverd._port_for(args)
        self.assertEqual(caught.exception.code, "port-claimed-by-another-account")
        args.port = 47600
        self.assertEqual(serverd._port_for(args), 47600)
        self.assertEqual(lp.record()["scheme"], "chosen")


class Pass(Fixture):
    """The maintenance pass records an installed server's ports once and refreshes the claim (spec 3.4, 3.11)."""

    def test_the_pass_records_an_install_once_then_keeps_it(self):
        import maintenance
        self.assertEqual(maintenance.step_ports(self.home)["result"], "no-server")
        self.n, self.server = 1, 47311
        first = maintenance.step_ports(self.home)
        self.assertEqual((first["result"], first["scheme"], first["server"]), ("recorded", "legacy", 47311))
        again = maintenance.step_ports(self.home)
        self.assertEqual((again["result"], again["claim"]), ("kept", "written"))

    def test_the_pass_moves_a_server_off_another_accounts_port(self):
        import maintenance
        self.server = 47311
        self.listening = {47311: "another-account"}
        with patch("subprocess.run") as run:
            run.return_value.returncode, run.return_value.stderr, run.return_value.stdout = 0, "", ""
            out = maintenance.step_ports(self.home)
        self.assertEqual((out["result"], out["server"]), ("moved", 47400))

    def test_the_machine_tool_answers_the_accounts_section(self):
        sys.path.insert(0, str(ROOT / "mcp"))
        try:
            import server
        except ImportError as exc:                      # the MCP runtime is not installed in this interpreter
            self.skipTest(f"mcp runtime: {exc}")
        fn = getattr(server.observatory_machine, "fn", server.observatory_machine)
        out = fn(section="accounts")
        self.assertEqual(out["accounts"]["accountsOnHost"], 2)
        self.assertIn("server", out["accounts"]["listeners"])


if __name__ == "__main__":
    unittest.main()
