#!/usr/bin/env python3
"""Where a product's daily operations digests are read (DEC-0153 OD-9, OBS-533): `ops_digest_folders.py` over the
contract's `ops-digest-folders-*` fixtures (`FAC-SEM-168`), the fold's examples, and the collector over a product's
folders on disk."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ops_digest_folders as F  # noqa: E402

SEMANTIC = ROOT / "fabric" / "ops-digest" / "semantic"
DAILY = json.loads((ROOT / "fabric" / "ops-digest" / "positive" / "ops-digest-daily.json").read_text(encoding="utf-8"))


def fixture(name: str) -> dict:
    return json.loads((SEMANTIC / name).read_text(encoding="utf-8"))


class Fixtures(unittest.TestCase):
    def test_each_fixture_reports_the_contracts_codes(self):
        paths = sorted(SEMANTIC.glob("ops-digest-folders-*.json"))
        self.assertEqual(len(paths), 7)
        for path in paths:
            doc = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path.name):
                self.assertEqual(sorted({f["code"] for f in F.rules(doc["input"])}), sorted(set(doc["expect"])))
                self.assertEqual(F.view(doc["input"])["usable"], not doc["expect"])

    def test_an_undeclared_descriptor_has_nothing(self):
        plain = copy.deepcopy(fixture("ops-digest-folders-ok.json")["input"])
        plain.pop("extensions")
        self.assertEqual(F.view(plain), {"declared": False})
        self.assertEqual(F.rules(plain), [])


class Fold(unittest.TestCase):
    def test_the_specifications_examples(self):
        # docs/specification/ops-digest.md and src/ops-digest.ts at the vendored tag
        self.assertEqual(F.fold_dir("Straße"), F.fold_dir("STRASSE"))
        self.assertEqual(F.fold_dir("Straße"), F.fold_dir("Straẞe"))
        self.assertEqual(F.fold_dir("σασ"), F.fold_dir("ΣΑΣ"))
        self.assertEqual(F.fold_dir("digests. "), "digests")
        self.assertEqual(F.fold_dir("a‌b"), "ab")
        self.assertEqual(F.fold_dir("ⴀ"), "ა")

    def test_a_dir_named_twice_under_another_case_is_one_folder(self):
        doc = copy.deepcopy(fixture("ops-digest-folders-ok.json")["input"])
        doc["extensions"][F.EXTENSION_KEY] = [{"dir": "digests", "source": "app"}, {"dir": "DIGESTS.", "source": "web"}]
        self.assertEqual([f["instancePath"].rsplit("/", 2)[-2:] for f in F.rules(doc)], [["1", "dir"]])

    def test_detectors_on_the_specifications_examples(self):
        doc = copy.deepcopy(fixture("ops-digest-folders-ok.json")["input"])
        for dir_, refused in (("users/digests", True), ("my_users/digests", True), ("my-users/digests", True),
                              ("a/~b", True), ("user-digests", False), ("a/~1/x", False),
                              ("Application+Support/MyProduct/Digests2026x", True), ("x/⁠/y", True)):
            doc["extensions"][F.EXTENSION_KEY] = [{"dir": dir_, "source": "app"}]
            with self.subTest(dir_):
                self.assertEqual(bool(F.rules(doc)), refused)


class Collector(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="od9-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        (self.data / "digests").mkdir(parents=True)
        self.descriptor = copy.deepcopy(fixture("ops-digest-folders-ok.json")["input"])
        self.descriptor["paths"]["data"] = str(self.data)
        self.descriptor["extensions"][F.EXTENSION_KEY] = [{"dir": "digests", "source": "app"}]
        self.kept: list = []

    def record(self, project, service, source, digest, via):
        key = (project, service["id"], source, digest["day"])
        repeated = key in {k for k, _ in self.kept}
        if not repeated:
            self.kept.append((key, digest))
        return {"accepted": True, "repeated": repeated}

    def write(self, *lines: str):
        month = DAILY["day"][:7]
        with open(self.data / "digests" / f"daily-{month}.jsonl", "a", encoding="utf-8") as stream:
            stream.write("".join(lines))

    def collect(self, owners=("project:demo",)):
        return F.collect([(Path("x.json"), self.descriptor)], record=self.record,
                         projects=lambda service: set(owners))

    def test_a_day_is_recorded_once_as_written_and_the_rest_is_skipped(self):
        later = dict(DAILY, total=DAILY["total"])
        self.write(json.dumps(DAILY) + "\n", "not json\n", json.dumps({"schema": "ops.digest.daily@2"}) + "\n",
                   json.dumps(later) + "\n", json.dumps(DAILY)[:30])
        receipt = self.collect()
        self.assertEqual(receipt["recorded"], 1)
        self.assertEqual(receipt["skipped"], {"not-json": 1, "other-schema": 1})
        (key, digest), = self.kept
        self.assertEqual(key, ("project:demo", self.descriptor["id"], "app", DAILY["day"]))
        self.assertEqual(digest, DAILY)
        again = self.collect()
        self.assertEqual((again["recorded"], again["repeated"]), (0, 1))

    def test_a_refused_declaration_is_reported_and_not_read(self):
        self.write(json.dumps(DAILY) + "\n")
        self.descriptor["extensions"][F.EXTENSION_KEY] = [{"dir": "users/digests", "source": "app"}]
        receipt = self.collect()
        self.assertEqual(self.kept, [])
        self.assertEqual(len(receipt["notDeclared"]), 1)

    def test_a_service_of_no_project_or_two_sends_nothing(self):
        self.write(json.dumps(DAILY) + "\n")
        for owners in ((), ("project:a", "project:b")):
            with self.subTest(owners):
                receipt = self.collect(owners)
                self.assertEqual(self.kept, [])
                self.assertEqual(receipt["unattributed"][0]["projects"], len(owners))

    @unittest.skipIf(os.name == "nt", "a symbolic link needs a privilege there")
    def test_a_folder_that_leads_outside_the_data_folder_is_not_read(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / f"daily-{DAILY['day'][:7]}.jsonl").write_text(json.dumps(DAILY) + "\n", encoding="utf-8")
        shutil.rmtree(self.data / "digests")
        os.symlink(outside, self.data / "digests")
        self.assertEqual(self.collect()["folders"], 0)
        self.assertEqual(self.kept, [])



class MaintenanceStep(unittest.TestCase):
    def test_the_pass_reports_what_it_read_and_never_raises(self):
        import maintenance
        base = Path(tempfile.mkdtemp(prefix="od9-pass-"))
        self.addCleanup(shutil.rmtree, base, True)
        self.assertEqual(maintenance.step_ops_digests(base, lambda: {"services": 0, "notDeclared": [],
                                                                    "unattributed": []}), {"result": "none-declared"})
        ran = maintenance.step_ops_digests(base, lambda: {"services": 1, "folders": 1, "recorded": 2, "repeated": 0,
                                                          "skipped": {"not-json": 1}, "refused": {},
                                                          "notDeclared": [], "unattributed": []})
        self.assertEqual(ran, {"result": "ran", "services": 1, "folders": 1, "recorded": 2, "repeated": 0,
                               "skipped": {"not-json": 1}})
        def boom():
            raise OSError("disk")
        self.assertEqual(maintenance.step_ops_digests(base, boom)["result"], "error")

if __name__ == "__main__":
    unittest.main()
