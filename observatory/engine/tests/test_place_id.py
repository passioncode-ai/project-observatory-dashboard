#!/usr/bin/env python3
"""OBS-357, part 1: the place id of this host and this OS account, and how a recorded id matches it."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import machine_identity as mi

HOST = "m-" + "a" * 24
OTHER_HOST = "m-" + "b" * 24


def env(account: str = "501", host: str = HOST):
    return patch.dict(os.environ, {"OBSERVATORY_SYSTEM_SETUP": "0", mi.ENV: host, mi.ACCOUNT_ENV: account})


class PlaceId(unittest.TestCase):
    def test_the_place_is_the_host_and_a_hash_of_the_account(self):
        with env("501"):
            here = mi.place_id()
        self.assertRegex(here, r"^m-a{24}\.a-[0-9a-f]{10}$")
        self.assertEqual(len(here), 39)
        self.assertTrue(mi.valid_id(here), "inside ID_SHAPE, so an older reader accepts it")
        self.assertEqual((mi.host_of(here), mi.is_place(here), mi.is_place(HOST)), (HOST, True, False))
        with env("502"):
            other_account = mi.place_id()
        self.assertNotEqual(here, other_account)
        self.assertEqual(mi.host_of(other_account), HOST, "two accounts of one host share the prefix")
        self.assertNotIn("501", here, "the id names no account")

    def test_the_place_is_stable(self):
        with env("501"):
            self.assertEqual(mi.place_id(), mi.place_id())

    def test_match_follows_the_table(self):
        with env("501"):
            here = mi.place_id()
        with env("502"):
            neighbour = mi.place_id()
        with env("501", OTHER_HOST):
            elsewhere = mi.place_id()
        self.assertEqual(mi.match(here, here, accounts=2), "same")
        self.assertEqual(mi.match(neighbour, here, accounts=2), "other", "same host, another account")
        self.assertEqual(mi.match(elsewhere, here, accounts=1), "other")
        self.assertEqual(mi.match(OTHER_HOST, here, accounts=1), "other", "a bare id of another host")
        self.assertEqual(mi.match(HOST, here, accounts=1), "same", "a bare id of this host, one account: as before")
        self.assertEqual(mi.match(HOST, here, accounts=3), "same-host-account-unknown")
        self.assertEqual(mi.match(HOST, here, accounts=None), "same-host-account-unknown", "an unknown count is several")
        self.assertEqual(mi.match("not an id!", here, accounts=1), "other")

    def test_the_account_override_needs_a_sandbox(self):
        with patch.dict(os.environ, {mi.ACCOUNT_ENV: "999"}, clear=False):
            os.environ.pop("OBSERVATORY_SYSTEM_SETUP", None)
            os.environ.pop(mi.TEST_MARKER, None)
            import osprivacy
            self.assertEqual(mi._account(), osprivacy.account())

    def test_doctor_names_the_place(self):
        with env("501"):
            block = mi.place_summary()
        self.assertEqual(block["host"], HOST)
        self.assertTrue(mi.is_place(block["place"]))
        self.assertIn("accountsOnHost", block)


class LocalSettingsPlace(unittest.TestCase):
    """config/local.json records the place id; a record from before (a bare host id) still matches this host."""

    def test_a_bare_host_record_still_matches_and_a_neighbours_does_not(self):
        import configuration as config
        import tempfile
        import workspace
        with tempfile.TemporaryDirectory() as raw, env("501"), \
                patch.dict(os.environ, {"OBSERVATORY_HOME": str(Path(raw).resolve() / "home"),
                                        "HOME": str(Path(raw).resolve() / "u")}):
            home = Path(raw).resolve() / "home"
            workspace.initialize(home)
            with workspace.lock(home):
                config.write_local(home, {"apps": {"location": "user"}}, place=HOST)
            self.assertTrue(config.local(home)["place"]["matches"], "a bare host id written before place ids")
            with workspace.lock(home):
                config.write_local(home, {"apps": {"location": "user"}})
            self.assertTrue(mi.is_place(config.local(home)["place"]["recorded"]))
            with env("502"):
                self.assertFalse(config.local(home)["place"]["matches"], "another account's place is another place")


class RestoreMode(unittest.TestCase):
    """Spec M5 3.3: the restore mode from `match`, for bundles and snapshots."""

    def setUp(self):
        with env("501"):
            self.here = mi.place_id()
        with env("502"):
            self.neighbour = mi.place_id()
        with env("501", OTHER_HOST):
            self.elsewhere = mi.place_id()

    def mode(self, recorded, *, accounts=1, home_path=None, home=Path("/Users/a")):
        with env("501"), patch.dict(os.environ, {"OBSERVATORY_HOST_ACCOUNTS": str(accounts)}):
            got = mi.restore_mode(recorded, here=self.here, home=home, home_path=home_path)
        return got["mode"], got["why"]

    def test_the_table(self):
        self.assertEqual(self.mode(self.here), ("same-machine", "same-place"))
        self.assertEqual(self.mode(self.neighbour, accounts=2), ("second-machine", "another-account"))
        self.assertEqual(self.mode(self.elsewhere), ("second-machine", "another-machine"))
        self.assertEqual(self.mode(OTHER_HOST), ("second-machine", "another-machine"))
        self.assertEqual(self.mode(HOST, accounts=1), ("same-machine", "same-place"), "a bare id, one account: as before")
        self.assertEqual(self.mode(None), ("second-machine", "names-no-machine"))

    def test_an_old_bundle_of_a_shared_host_is_this_accounts_only_by_its_home(self):
        self.assertEqual(self.mode(HOST, accounts=2, home_path="/Users/a"), ("same-machine", "same-host-same-home"))
        self.assertEqual(self.mode(HOST, accounts=2, home_path="/Users/b"),
                         ("second-machine", "same-host-account-unknown"))
        self.assertEqual(self.mode(HOST, accounts=2), ("second-machine", "same-host-account-unknown"),
                         "a snapshot names no home")

    def test_the_answer_names_the_hosts(self):
        with env("501"):
            got = mi.restore_mode(self.neighbour, here=self.here)
        self.assertEqual((got["this_host"], got["taken_on_host"], got["same_host"]), (HOST, HOST, True))

    def test_an_older_reader_restores_a_new_bundle_as_a_second_machine(self):
        def restore_mode_0_33(recorded, here):           # the comparison of the engine before place ids
            same = recorded == here
            return "same-machine" if same else "second-machine"
        self.assertEqual(restore_mode_0_33(self.here, HOST), "second-machine",
                         "a new bundle never reads as the same machine to an older engine: the safe way")


class MoveAcrossAccounts(unittest.TestCase):
    """Spec M5 3.5: `leave --to` another account of this host; `status` reads such a peer as another place."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def leave(self, to, accounts):
        import machine_move as mm
        with env("501"), patch.dict(os.environ, {"OBSERVATORY_HOST_ACCOUNTS": str(accounts)}):
            try:
                mm.leave("com.example.bot", to, self.home, platform="linux", uid=501)
            except mm.MachineMoveError as exc:
                return str(exc)
        return "moved"

    def test_leave_to_another_account_of_this_host_passes_the_checks(self):
        with env("502"):
            neighbour = mi.place_id()
        self.assertIn("no job com.example.bot is defined", self.leave(neighbour, 2),
                      "the --to checks pass; only the missing job stops it")

    def test_leave_to_a_bare_id_of_this_host_is_refused_with_several_accounts(self):
        self.assertIn("to-names-this-host", self.leave(HOST, 2))
        self.assertIn("names this machine", self.leave(HOST, 1), "one account: the bare id is this machine, as before")
        with env("501"):
            here = mi.place_id()
        self.assertIn("names this machine", self.leave(here, 2))

    def test_status_reads_a_same_host_peer_as_another_place(self):
        import machine_move as mm
        with env("502"):
            neighbour = mi.place_id()
        peer = {"machine": neighbour, "generated_at": "2026-11-12T10:00:00Z", "engine_version": "0", "platform": "linux",
                "ssh": [], "device": None, "jobs": [], "elsewhere": [], "restore": None}
        old = dict(peer, machine=HOST)
        with env("501"), patch.dict(os.environ, {"OBSERVATORY_HOST_ACCOUNTS": "2"}):
            view = mm.status(self.home, None, peers=[peer, old], platform="linux")
        machines = {m["machine"]: m["sameHost"] for m in view["other_machines"]}
        self.assertEqual(machines, {neighbour: True, HOST: True}, "both are other places of this host")
        self.assertIn("same-host-account-unknown", [d["code"] for d in view["degraded"]])


if __name__ == "__main__":
    unittest.main()
