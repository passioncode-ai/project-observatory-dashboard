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


if __name__ == "__main__":
    unittest.main()
