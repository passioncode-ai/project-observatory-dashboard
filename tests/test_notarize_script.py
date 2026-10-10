"""`macos/scripts/notarize.sh` turns a Developer ID-signed app into a notarized, stapled
download, and refuses everything else before Apple is asked.

Apple's tools are replaced by fakes on PATH, so the contract is checked on any
machine: which tools run, in what order, with which arguments, and what the script
prints. A real submission is a release act with the maintainer's credentials; the
release record says when one ran.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "macos" / "scripts" / "notarize.sh"
KEY_ID, ISSUER = "FAKEKEYID9", "00000000-0000-4000-8000-00000000abcd"

DEVELOPER_ID = textwrap.dedent("""\
    Executable=/x
    CodeDirectory v=20500 size=1 flags=0x10000(runtime) hashes=1+1 location=embedded
    Authority=Developer ID Application: Example Maintainer (EXAMPLE123)
    Authority=Developer ID Certification Authority
    Authority=Apple Root CA
    TeamIdentifier=EXAMPLE123
    """)
AD_HOC = "Executable=/x\nCodeDirectory v=20500 size=1 flags=0x2(adhoc) hashes=1+1\nSignature=adhoc\n"
NO_RUNTIME = DEVELOPER_ID.replace("flags=0x10000(runtime)", "flags=0x0(none)")

FAKE = {
    # Each fake records its argv and answers from the scenario in $FAKE_DIR.
    "codesign": 'echo "codesign $*" >> "$FAKE_DIR/calls"; cat "$FAKE_DIR/codesign.out" >&2',
    "xcrun": textwrap.dedent("""\
        echo "xcrun $*" >> "$FAKE_DIR/calls"
        case "$1 $2" in
          "notarytool submit") cat "$FAKE_DIR/submit.json" ;;
          "notarytool log") echo '{"issues":[{"message":"The binary is not signed."}]}' ;;
          "stapler staple"|"stapler validate") echo "The $2 action worked!" ;;
        esac"""),
    "spctl": 'echo "spctl $*" >> "$FAKE_DIR/calls"; echo "accepted" >&2; echo "source=Notarized Developer ID" >&2',
    "ditto": 'echo "ditto $*" >> "$FAKE_DIR/calls"; : > "${@: -1}"',
    "plutil": 'echo "0.13.0"',
}


class Scenario:
    def __init__(self, tmp: Path, signature: str = DEVELOPER_ID, status: str = "Accepted"):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, body in FAKE.items():
            path = self.bin / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        (self.bin / "python3").symlink_to(sys.executable)
        (tmp / "codesign.out").write_text(signature)
        (tmp / "submit.json").write_text(json.dumps(
            {"id": "11111111-2222-4333-8444-555555555555", "status": status, "message": "Processing complete"}))
        self.app = tmp / "Example.app"
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents" / "Info.plist").write_text("<plist/>")
        self.out = tmp / "dist"
        self.key = tmp / "AuthKey.p8"
        self.key.write_text("placeholder: the script passes the path and never reads the key\n")

    def run(self, **creds: str) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_DIR": str(self.tmp), "HOME": str(self.tmp), **creds}
        return subprocess.run(["bash", str(SCRIPT), str(self.app), str(self.out)], env=env,
                              capture_output=True, text=True)

    def key_creds(self) -> dict[str, str]:
        return {"OBSERVATORY_NOTARY_KEY": str(self.key), "OBSERVATORY_NOTARY_KEY_ID": KEY_ID,
                "OBSERVATORY_NOTARY_ISSUER": ISSUER}

    def calls(self) -> list[str]:
        path = self.tmp / "calls"
        return path.read_text().splitlines() if path.exists() else []


@unittest.skipIf(os.name == "nt", "notarization is macOS release tooling: the script runs on the macOS release runner")
class NotarizeRefusesBeforeAppleIsAsked(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_credentials_is_refused_with_both_ways_named(self):
        s = Scenario(self.tmp)
        result = s.run()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("OBSERVATORY_NOTARY_PROFILE", result.stderr)
        self.assertIn("OBSERVATORY_NOTARY_KEY", result.stderr)
        self.assertFalse(any(c.startswith("xcrun") for c in s.calls()))

    def test_a_partial_api_key_is_refused(self):
        s = Scenario(self.tmp)
        creds = s.key_creds()
        del creds["OBSERVATORY_NOTARY_ISSUER"]
        result = s.run(**creds)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(any(c.startswith("xcrun") for c in s.calls()))

    def test_a_missing_key_file_is_refused(self):
        s = Scenario(self.tmp)
        creds = {**s.key_creds(), "OBSERVATORY_NOTARY_KEY": str(self.tmp / "absent.p8")}
        result = s.run(**creds)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(any(c.startswith("xcrun") for c in s.calls()))

    def test_an_ad_hoc_signature_is_refused(self):
        s = Scenario(self.tmp, signature=AD_HOC)
        result = s.run(**s.key_creds())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Developer ID", result.stderr)
        self.assertFalse(any(c.startswith("xcrun") for c in s.calls()))

    def test_a_signature_without_the_hardened_runtime_is_refused(self):
        s = Scenario(self.tmp, signature=NO_RUNTIME)
        result = s.run(**s.key_creds())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("hardened runtime", result.stderr)
        self.assertFalse(any(c.startswith("xcrun") for c in s.calls()))

    def test_a_missing_app_is_refused(self):
        s = Scenario(self.tmp)
        env = {"PATH": f"{s.bin}:/usr/bin:/bin", "FAKE_DIR": str(self.tmp), **s.key_creds()}
        result = subprocess.run(["bash", str(SCRIPT), str(self.tmp / "Absent.app")], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)


@unittest.skipIf(os.name == "nt", "notarization is macOS release tooling: the script runs on the macOS release runner")
class NotarizeRunsAppleInOrder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_accepted_is_stapled_assessed_and_packaged(self):
        s = Scenario(self.tmp)
        result = s.run(**s.key_creds())
        self.assertEqual(result.returncode, 0, result.stderr)
        steps = [c.split()[0] + " " + c.split()[1] for c in s.calls()]
        submit = steps.index("xcrun notarytool")
        self.assertLess(submit, steps.index("xcrun stapler"))
        self.assertEqual([c for c in s.calls() if c.startswith("xcrun stapler")],
                         [f"xcrun stapler staple {s.app}", f"xcrun stapler validate {s.app}"])
        self.assertTrue(any(c.startswith("spctl --assess --type execute") for c in s.calls()))
        archive = s.out / "ProjectObservatory-0.13.0-macos.zip"
        self.assertTrue(archive.exists())
        self.assertIn(str(archive), result.stdout)
        # The archive is made from the stapled app: the last ditto runs after the staple.
        dittos = [i for i, c in enumerate(s.calls()) if c.startswith("ditto")]
        staple = next(i for i, c in enumerate(s.calls()) if c.startswith("xcrun stapler staple"))
        self.assertGreater(dittos[-1], staple)

    def test_the_api_key_goes_to_notarytool_and_never_to_the_output(self):
        s = Scenario(self.tmp)
        result = s.run(**s.key_creds())
        submit = next(c for c in s.calls() if c.startswith("xcrun notarytool submit"))
        self.assertIn(f"--key {s.key}", submit)
        self.assertIn(f"--key-id {KEY_ID}", submit)
        self.assertIn(f"--issuer {ISSUER}", submit)
        self.assertIn("--wait", submit)
        for value in (KEY_ID, ISSUER, str(s.key)):
            self.assertNotIn(value, result.stdout)
            self.assertNotIn(value, result.stderr)

    def test_a_keychain_profile_is_used_when_named(self):
        s = Scenario(self.tmp)
        result = s.run(OBSERVATORY_NOTARY_PROFILE="observatory-notary")
        self.assertEqual(result.returncode, 0, result.stderr)
        submit = next(c for c in s.calls() if c.startswith("xcrun notarytool submit"))
        self.assertIn("--keychain-profile observatory-notary", submit)
        self.assertNotIn("--key ", submit)

    def test_invalid_shows_apples_log_and_staples_nothing(self):
        s = Scenario(self.tmp, status="Invalid")
        result = s.run(**s.key_creds())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Invalid", result.stderr)
        self.assertIn("The binary is not signed.", result.stderr)
        self.assertTrue(any(c.startswith("xcrun notarytool log 11111111-") for c in s.calls()))
        self.assertFalse(any(c.startswith("xcrun stapler") for c in s.calls()))
        self.assertFalse((s.out / "ProjectObservatory-0.13.0-macos.zip").exists())


if __name__ == "__main__":
    unittest.main()
