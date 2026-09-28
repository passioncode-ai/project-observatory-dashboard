#!/usr/bin/env python3
"""`full profile`: the functional configuration travels, the machine does not.

Two synthetic workspaces stand in for two operators' machines. The first is
filled with everything a workspace holds that must NOT leave it — source paths,
a backups root, secrets, credential annotations, account ids, project names, a
planted key and a planted path inside an otherwise exportable policy file — and
the export is then searched for any of it by checks written independently of
the exporter's own detector.
"""
from __future__ import annotations
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration as config
import workspace

# Built at run time so the repository's own scanners never see a literal of either shape.
PLANTED_KEY = "sk" + "-" + "or" + "-" + "v1" + "-" + "Z9" * 24
PLANTED_TOKEN = "gh" + "p_" + "A1b2" * 10


def canonical(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def strings(value, pointer=""):
    """Every key and every string value in a JSON document: (where, text, is_key)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{pointer}/{k}", k, True
            yield from strings(v, f"{pointer}/{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from strings(v, f"{pointer}/{i}")
    elif isinstance(value, str):
        yield pointer, value, False


# Independent of workspace_profile's detector, deliberately broader where cheap.
ABSOLUTE = re.compile(r"(?:^|[\s\"'=(,])/[A-Za-z0-9._-]+/")
HOME_RELATIVE = re.compile(r"(?:^|[\s\"'=(,])~(?:/|$)|\$HOME|%USERPROFILE%")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PREFIXED = re.compile(r"(?:sk-|sk_|ghp_|gho_|github_pat_|xox[abpr]-|AKIA|AIza|glpat-|lin_api_|eyJ)[A-Za-z0-9_-]{8,}")


class _Tokenish:
    """A long run of token characters holding both letters and digits, or a known prefix."""

    @staticmethod
    def search(value: str):
        if PREFIXED.search(value):
            return True
        for run in re.findall(r"[A-Za-z0-9_+=-]{32,}", value):
            if re.search(r"\d", run) and re.search(r"[A-Za-z]", run):
                return True
        return None


TOKENISH = _Tokenish()
DENIED_KEYS = {"sources", "storage", "secret", "secrets", "secret_store", "token", "password", "passphrase",
               "api_key", "apikey", "key_file", "credentials", "credential", "annotations", "account",
               "account_id", "accounts", "email", "instance_id", "projects", "repositories", "ga4_account",
               "overrides", "home", "path", "paths"}


class Profile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.a, self.b = self.base / "machine-a", self.base / "machine-b"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.a), "HOME": str(self.base / "user")}, clear=True)
        self.env.start()
        for home in (self.a, self.b):
            workspace.initialize(home)
        self.fill_machine_a()
        self.use(self.a)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def use(self, home: Path):
        os.environ["OBSERVATORY_HOME"] = str(home)
        import paths
        importlib.reload(paths)
        import atomic
        importlib.reload(atomic)
        import workspace_profile
        self.mod = importlib.reload(workspace_profile)

    def settings(self, home: Path) -> dict:
        return json.loads((home / "config/settings.json").read_text())

    def write(self, home: Path, name: str, doc: dict):
        (home / "config" / name).write_text(json.dumps(doc, indent=2))

    def fill_machine_a(self):
        s = self.settings(self.a)
        user = self.base / "user"
        s["sources"] = {"projects": str(user / "projects"), "sessions": str(user / "agent-sessions"),
                        "secret_store": str(user / "vault")}
        s["storage"] = {"backups": str(user / "Backups")}
        s["integrations"] = {"github": True, "sessions": True, "openrouter": True, "heroku": False}
        s["features"] = {"scheduler": False, "agent": True, "embeddings": False, "machine_watch": True}
        s["interface"] = {"locale": "ru"}
        self.write(self.a, "settings.json", s)
        models = json.loads((ROOT / "defaults/models.json").read_text())
        models["chain"] = [{"model": "example-provider/example-model-large", "role": "primary"}]
        models["wallet"]["daily_ceiling"] = 1.5
        self.write(self.a, "models.json", models)
        retention = json.loads((ROOT / "defaults/retention.json").read_text())
        retention["events_days"] = 400
        self.write(self.a, "retention.json", retention)
        self.write(self.a, "organizations.json", {"organizations": {"acme": {"ga4_account": "accounts/123456"}},
                                                   "projects": {"project:alpha-web": {"organization": "acme"}}})
        self.write(self.a, "credential_annotations.json", {"schema_version": 1, "annotations": {
            "alpha-web/API_TOKEN": {"owner": "ops@example.com"}}})
        self.write(self.a, "identity_overrides.json", {"overrides": {"beta-api": "project:beta-api"}})
        # A policy file that would be exported, but carries a planted credential and path.
        cleanup = json.loads((ROOT / "defaults/cleanup.json").read_text())
        cleanup["protect"].append(PLANTED_KEY)
        self.write(self.a, "cleanup.json", cleanup)
        tiers = json.loads((ROOT / "defaults/activity_tiers.json").read_text())
        tiers["note"] = "see " + str(user / "notes.md")
        self.write(self.a, "activity_tiers.json", tiers)
        (self.a / "secrets/demo-slot").write_text(PLANTED_TOKEN)
        (self.a / "config/private-notes.json").write_text(json.dumps({"x": PLANTED_TOKEN}))

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.mod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def export(self) -> dict:
        code, out, err = self.cli("export")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def files(self, home: Path) -> dict:
        return {str(p.relative_to(home)): p.read_bytes() for p in sorted(home.rglob("*"))
                if p.is_file() and not p.name.endswith(".lock")}

    # --- export -----------------------------------------------------------------

    def test_export_never_carries_machine_state(self):
        doc = self.export()
        text = json.dumps(doc)
        for planted in (PLANTED_KEY, PLANTED_TOKEN, str(self.base), "accounts/123456", "ops@example.com",
                        "alpha-web", "beta-api", "agent-sessions", "notes.md"):
            self.assertNotIn(planted, text)
        for pointer, value, is_key in strings(doc):
            if pointer == "/sha256" and not is_key:
                self.assertRegex(value, r"^[0-9a-f]{64}$")
                continue
            if is_key:
                self.assertNotIn(value.lower(), DENIED_KEYS, pointer)
            for rx, kind in ((ABSOLUTE, "absolute path"), (HOME_RELATIVE, "home path"), (EMAIL, "email"),
                             (TOKENISH, "token")):
                self.assertIsNone(rx.search(value), f"{kind} at {pointer}")
        self.assertNotIn("sources", doc["settings"])
        self.assertNotIn("storage", doc["settings"])
        self.assertNotIn("scheduler", doc["settings"]["features"], "scheduling is decided per machine")
        excluded = {row["item"]: row["reason"] for row in doc["excluded"]}
        for name in ("config/settings.json#sources", "config/settings.json#storage", "config/organizations.json",
                     "config/credential_annotations.json", "config/identity_overrides.json",
                     "config/cleanup.json", "config/activity_tiers.json", "secrets/", "registry/", "store/"):
            self.assertIn(name, excluded)
            self.assertTrue(excluded[name])
        self.assertIn("token", excluded["config/cleanup.json"])
        self.assertIn("path", excluded["config/activity_tiers.json"])
        self.assertIn("unclassified", json.dumps(doc["excluded"]))
        self.assertNotIn("private-notes", text, "an unknown file's name is not published either")
        included = {row["item"] for row in doc["included"]}
        self.assertIn("config/models.json", included)
        self.assertIn("config/retention.json", included)

    def test_export_is_versioned_and_hashed(self):
        doc = self.export()
        self.assertEqual((doc["kind"], doc["format_version"], doc["engine_version"]),
                         ("project-observatory-profile", 1, config.VERSION))
        self.assertTrue(doc["created_at"].endswith("Z"))
        self.assertEqual(doc["sha256"], canonical(doc))

    def test_export_to_file_is_private_and_never_overwrites(self):
        target = self.base / "profile.json"
        code, out, err = self.cli("export", str(target))
        self.assertEqual(code, 0, err)
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(target.read_text())["kind"], "project-observatory-profile")
        code, out, err = self.cli("export", str(target))
        self.assertEqual(code, 2)
        self.assertIn("exists", err)

    def test_detector_catches_each_shape(self):
        for value, kind in (("/opt/tool/bin", "absolute-path"), ("~/x", "home-path"), ("a@example.com", "email"),
                            (PLANTED_KEY, "token"), ("C:\\tools", "absolute-path"), ("file:///x", "absolute-path")):
            self.assertIn(kind, [k for _, k in self.mod.leaks({"v": value})], value)
        self.assertIn("denied-key", [k for _, k in self.mod.leaks({"account_id": 1})])
        self.assertEqual(self.mod.leaks({"base_url": "https://example.com/api/v1", "model": "vendor/model-3.5-small"}), [])

    # --- import -----------------------------------------------------------------

    def save(self, doc: dict, name="p.json") -> Path:
        path = self.base / name
        path.write_text(json.dumps(doc))
        return path

    def test_round_trip(self):
        profile = self.save(self.export())
        self.use(self.b)
        code, out, err = self.cli("import", str(profile), "--apply")
        self.assertEqual(code, 0, err)
        b = self.settings(self.b)
        a = self.settings(self.a)
        self.assertEqual(b["integrations"], a["integrations"])
        self.assertEqual(b["interface"], {"locale": "ru"})
        self.assertEqual({k: v for k, v in b["features"].items() if k != "scheduler"},
                         {k: v for k, v in a["features"].items() if k != "scheduler"})
        self.assertEqual(json.loads((self.b / "config/models.json").read_text()),
                         json.loads((self.a / "config/models.json").read_text()))
        again = self.export()
        first = json.loads(profile.read_text())
        self.assertEqual(again["settings"], first["settings"])
        for name, content in first["files"].items():
            self.assertEqual(again["files"][name], content, name)

    def test_preview_writes_nothing(self):
        profile = self.save(self.export())
        self.use(self.b)
        before = self.files(self.b)
        code, out, err = self.cli("import", str(profile))
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["status"], "preview")
        self.assertTrue(doc["changes"])
        self.assertIn({"file": "config/settings.json", "pointer": "/interface/locale", "change": "add",
                       "to": "ru"}, doc["changes"])
        self.assertEqual(self.files(self.b), before)

    def test_sources_and_machine_local_settings_are_never_touched(self):
        s = self.settings(self.b)
        s["sources"] = {"projects": str(self.base / "b-projects")}
        s["features"] = {"scheduler": True}
        self.write(self.b, "settings.json", s)
        doc = self.export()
        profile = self.save(doc)
        smuggled = dict(doc, settings=dict(doc["settings"], sources={"projects": "relative-looking-value"}))
        smuggled["sha256"] = canonical(smuggled)
        smuggled = self.save(smuggled, "smuggled.json")
        self.use(self.b)
        before = self.files(self.b)
        code, _, err = self.cli("import", str(smuggled), "--apply")
        self.assertEqual(code, 2, "a profile that carries sources is refused, not half-applied")
        self.assertIn("sources", err)
        self.assertEqual(self.files(self.b), before)
        code, out, err = self.cli("import", str(profile), "--apply")
        self.assertEqual(code, 0, err)
        after = self.settings(self.b)
        self.assertEqual(after["sources"], {"projects": str(self.base / "b-projects")})
        self.assertTrue(after["features"]["scheduler"])
        self.assertFalse(json.loads(out)["sources_touched"])

    def test_unknown_profile_sections_are_ignored_and_named(self):
        doc = self.export()
        doc["settings"]["x_future_section"] = {"flag": True}
        doc["files"]["x_future_policy.json"] = {"threshold": 3}
        doc["sha256"] = canonical(doc)
        profile = self.save(doc)
        self.use(self.b)
        code, out, err = self.cli("import", str(profile), "--apply")
        self.assertEqual(code, 0, err)
        ignored = json.loads(out)["ignored"]
        self.assertIn("settings.x_future_section", ignored)
        self.assertIn("files.x_future_policy.json", ignored)
        self.assertNotIn("x_future_section", self.settings(self.b))
        self.assertFalse((self.b / "config/x_future_policy.json").exists())

    def test_unknown_fields_are_preserved(self):
        s = self.settings(self.b)
        s["x_local_extension"] = {"kept": True}
        self.write(self.b, "settings.json", s)
        models = json.loads((self.b / "config/models.json").read_text())
        models["x_local_note"] = "kept"
        self.write(self.b, "models.json", models)
        doc = self.export()
        doc["x_future_optional"] = {"ignored": True}
        doc["sha256"] = canonical(doc)
        profile = self.save(doc)
        self.use(self.b)
        code, out, err = self.cli("import", str(profile), "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.settings(self.b)["x_local_extension"], {"kept": True})
        after = json.loads((self.b / "config/models.json").read_text())
        self.assertEqual(after["x_local_note"], "kept")
        self.assertEqual(after["wallet"]["daily_ceiling"], 1.5)

    def test_newer_engine_profile_is_refused(self):
        doc = self.export()
        doc["engine_version"] = "99.0.0"
        doc["sha256"] = canonical(doc)
        profile = self.save(doc)
        self.use(self.b)
        before = self.files(self.b)
        code, out, err = self.cli("import", str(profile), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("full update", err)
        self.assertEqual(self.files(self.b), before)

    def test_unknown_major_format_and_requirements_are_refused(self):
        for field, value in (("format_version", 2), ("must_understand", ["future-capability"]), ("kind", "other")):
            doc = self.export()
            doc[field] = value
            doc["sha256"] = canonical(doc)
            self.use(self.b)
            code, _, err = self.cli("import", str(self.save(doc)))
            self.assertEqual(code, 2, field)
            self.use(self.a)

    def test_tampered_or_leaking_profile_is_refused(self):
        doc = self.export()
        doc["settings"]["integrations"]["github"] = False
        tampered = self.save(doc, "tampered.json")
        doc = self.export()
        doc["files"]["models.json"]["base_url"] = "file:///synthetic/models"
        doc["sha256"] = canonical(doc)
        leaking = self.save(doc, "leaking.json")
        self.use(self.b)
        before = self.files(self.b)
        code, _, err = self.cli("import", str(tampered), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("sha256", err)
        code, _, err = self.cli("import", str(leaking), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("path", err)
        self.assertEqual(self.files(self.b), before)

    def test_import_reports_what_this_machine_must_connect(self):
        profile = self.save(self.export())
        self.use(self.b)
        code, out, err = self.cli("import", str(profile), "--apply")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        missing = {(w.get("integration"), w["source"]) for w in doc["connect_on_this_machine"]["coverage_warnings"]}
        self.assertIn(("sessions", "sessions"), missing)
        self.assertIn(("openrouter", "secret_store"), missing)
        credentials = {row["integration"] for row in doc["connect_on_this_machine"]["credentials"]}
        self.assertIn("github", credentials)
        self.assertNotIn("heroku", credentials, "a disabled integration needs nothing")
        self.assertIn("full open --rebuild", json.dumps(doc["next"]))
        log = (self.b / "store/logs/profile.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(log[-1])["event"], "imported")

    def test_a_failed_write_restores_every_file(self):
        profile = self.save(self.export())
        self.use(self.b)
        before = self.files(self.b)
        real, calls = self.mod._write, []
        def flaky(path, doc):
            calls.append(path.name)
            if len(calls) == 2:
                raise OSError("synthetic full disk")
            real(path, doc)
        with patch.object(self.mod, "_write", flaky):
            code, _, err = self.cli("import", str(profile), "--apply")
        self.assertEqual(code, 2)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.files(self.b), before)

    def test_settings_the_engine_would_refuse_are_rolled_back(self):
        doc = self.export()
        doc["settings"]["interface"] = {"locale": "xx"}
        doc["sha256"] = canonical(doc)
        profile = self.save(doc)
        self.use(self.b)
        before = self.files(self.b)
        code, _, err = self.cli("import", str(profile), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("locale", err)
        self.assertEqual(self.files(self.b), before)

class CliRoute(unittest.TestCase):
    def test_profile_is_a_workspace_command(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp,
                   "OBSERVATORY_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
            with patch.dict(os.environ, env, clear=True):
                workspace.initialize(home)
            p = subprocess.run([sys.executable, str(ROOT / "observatory.py"), "profile", "export"],
                               capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout)["kind"], "project-observatory-profile")
            p = subprocess.run([sys.executable, str(ROOT / "observatory.py"), "profile", "import", str(home / "absent.json")],
                               capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
            self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
