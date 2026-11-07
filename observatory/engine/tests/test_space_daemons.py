#!/usr/bin/env python3
"""The administrator's tool for a headless space (OBS-361 part 1): tools/space_daemons.py.

Every rule both ways, the daemon plist it writes, the atomic write and its modes, the refusals (not
root, not the root-owned copy, a daemon it did not write), remove and status, and that it stays a
Python 3.9 program with no engine import. Nothing here is root and nothing reaches the real launchd:
the system root is a temporary folder, `euid` and the owner change are injected, and launchctl is a
fake that answers from a table. The synthetic account is the test's own (its uid, its temporary home).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmp  # noqa: E402,F401  the sandbox defaults for a suite run by hand
import space_daemons as sd  # noqa: E402

TOOL = ROOT / "tools" / "space_daemons.py"
LABEL = "org.example.agent"
POSIX = os.name == "posix" and hasattr(os, "getuid")
ROOT_OWNED_PROGRAM = "/bin/sh"


class Launchctl:
    """A fake launchctl: records each call, answers `print system/LABEL` from `loaded` and every other
    verb from `codes` (0 by default)."""

    def __init__(self, loaded=(), codes=None):
        self.calls, self.loaded, self.codes = [], set(loaded), dict(codes or {})

    def __call__(self, argv, **kw):
        self.calls.append(list(argv[1:]))
        verb = argv[1]
        if verb == "print":
            code = 0 if argv[2][len("system/"):] in self.loaded else 113
        else:
            code = self.codes.get(verb, 0)
            if code == 0 and verb == "bootstrap":
                self.loaded.add(Path(argv[3]).stem)
            if code == 0 and verb == "bootout":
                self.loaded.discard(argv[2][len("system/"):])
        return subprocess.CompletedProcess(argv, code, "", "synthetic refusal" if code else "")


class Pure(unittest.TestCase):
    """What runs on every platform, Windows included: no account database is needed."""

    def test_it_stays_a_python_39_program_with_no_engine_import(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        stdlib = {"argparse", "datetime", "hashlib", "json", "os", "plistlib", "re", "stat", "subprocess", "sys",
                  "tempfile", "fcntl", "grp", "pwd"}
        for node in ast.walk(tree):
            self.assertNotEqual(type(node).__name__, "Match", "no match statement before Python 3.10")
            if isinstance(node, ast.ImportFrom):
                self.fail(f"from-import of {node.module}: standard modules only, imported whole")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, stdlib, "the standard library only, never the engine")
            annotations = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations += [a.annotation for a in node.args.args + node.args.kwonlyargs if a.annotation]
                annotations += [node.returns] if node.returns else []
            if isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
            for annotation in annotations:
                for sub in ast.walk(annotation):
                    self.assertFalse(isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr),
                                     "a PEP 604 union is evaluated at runtime before 3.10")
        self.assertNotIn("VERSION = ", TOOL.read_text(encoding="utf-8").replace("SPACE_DAEMONS_VERSION = ", ""))

    @unittest.skipUnless(Path("/usr/bin/python3").exists() and POSIX, "no /usr/bin/python3 here")
    def test_the_system_python_runs_it(self):
        out = subprocess.run(["/usr/bin/python3", "-I", str(TOOL), "--version"], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        digest = hashlib.sha256(TOOL.read_bytes()).hexdigest()
        self.assertEqual(out.stdout.split()[:4], ["space-daemons", sd.SPACE_DAEMONS_VERSION, "sha256", digest])

    def test_version_names_the_file_and_its_sha256(self):
        line = sd.version_line()
        self.assertIn(hashlib.sha256(TOOL.read_bytes()).hexdigest(), line)
        self.assertIn(sd.SPACE_DAEMONS_VERSION, line)

    def test_a_test_root_is_ignored_without_the_switch(self):
        with mock.patch.dict(os.environ, {sd.TEST_ROOT: "/var/empty/example"}, clear=False):
            os.environ.pop(sd.TEST_SWITCH, None)
            self.assertEqual(sd.system_from_environment().registry_path(),
                             os.path.join("/", sd.REGISTRY_DIR, sd.REGISTRY_NAME))

    @unittest.skipUnless(POSIX, "euid is POSIX")
    def test_a_test_root_is_ignored_for_root(self):
        with mock.patch.dict(os.environ, {sd.TEST_SWITCH: "1", sd.TEST_ROOT: "/var/empty/example"}), \
                mock.patch.object(sd.os, "geteuid", return_value=0):
            self.assertEqual(sd.system_from_environment().root, "/")

    def test_writable_by_others(self):
        def info(mode, uid=0, gid=0):
            return os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))
        d = stat.S_IFDIR
        cases = [(info(d | 0o755), False), (info(d | 0o755, uid=501), False), (info(d | 0o755, uid=502), True),
                 (info(d | 0o777), True), (info(d | 0o1777), False), (info(d | 0o775, gid=20), True),
                 (info(d | 0o775, gid=0), False), (info(d | 0o1775, gid=20), False)]
        for i, (st, expected) in enumerate(cases):
            with self.subTest(i=i):
                self.assertEqual(sd._writable_by_others(st, (0, 501)), expected)

    def test_the_daemon_runs_as_the_account(self):
        account = sd.Account("space", 600, 20, "staff", "/Users/example")
        doc = {"Label": LABEL, "ProgramArguments": ["/bin/sh"], "LimitLoadToSessionType": "Aqua",
               "EnvironmentVariables": {"HOME": "/elsewhere", "PATH": "/usr/bin:/bin", "USER": "root"}}
        daemon = sd.daemon_plist(doc, account)
        self.assertEqual(daemon["Label"], LABEL)
        self.assertEqual((daemon["UserName"], daemon["GroupName"], daemon["InitGroups"]), ("space", "staff", True))
        self.assertEqual(daemon["EnvironmentVariables"], {"HOME": "/Users/example", "USER": "space",
                                                          "LOGNAME": "space", "PATH": "/usr/bin:/bin"})
        self.assertEqual(daemon["WorkingDirectory"], "/Users/example")
        self.assertNotIn("LimitLoadToSessionType", daemon)
        self.assertEqual(sd.daemon_plist(dict(doc, WorkingDirectory="/Users/example/w"), account)["WorkingDirectory"],
                         "/Users/example/w")


@unittest.skipUnless(POSIX, "accounts, owners and launchd are POSIX")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="observatory-space-daemons-")
        self.base = Path(os.path.realpath(self.tmpdir.name))
        self.addCleanup(self.tmpdir.cleanup)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.account = sd.Account("space", os.getuid(), os.getgid(), "staff", str(self.home))
        self.folder = Path(self.account.background_dir())
        self.folder.mkdir(parents=True, mode=0o700)
        self.sysroot = self.base / "system"
        self.sysroot.mkdir()
        self.owner_calls = []
        self.launchctl = Launchctl()

    def system(self, euid=0, self_check=lambda: []):
        return sd.System(root=str(self.sysroot), runner=self.launchctl, euid=euid, set_owner=self.owner_calls.append,
                         self_check=self_check, owner_uid=os.getuid())

    def define(self, doc=None, name=None, mode=0o644, **extra):
        doc = dict(doc or {"Label": LABEL, "ProgramArguments": [ROOT_OWNED_PROGRAM, "-c", "true"]}, **extra)
        path = self.folder / (name or f"{doc.get('Label', LABEL)}.plist")
        path.write_bytes(plistlib.dumps(doc))
        path.chmod(mode)
        return path

    def problems(self, **extra):
        doc = {"Label": LABEL, "ProgramArguments": [ROOT_OWNED_PROGRAM]}
        doc.update(extra)
        found, daemon = sd.validate({k: v for k, v in doc.items() if v is not None}, f"{LABEL}.plist", self.account)
        return sorted(set(p["code"] for p in found)), daemon

    def daemon(self, label=LABEL):
        return self.sysroot / sd.DAEMONS_DIR / f"{label}.plist"

    def registry(self):
        return json.loads((self.sysroot / sd.REGISTRY_DIR / sd.REGISTRY_NAME).read_text())


class Rules(Base):
    def test_a_plain_definition_passes(self):
        self.assertEqual(self.problems(), ([], self.problems()[1]))
        self.assertIsNotNone(self.problems()[1])

    def test_every_allowed_key_passes_with_a_simple_value(self):
        log = str(self.home / "job.log")
        codes, daemon = self.problems(
            Program=ROOT_OWNED_PROGRAM, EnvironmentVariables={"A": "b"}, WorkingDirectory=str(self.home),
            RunAtLoad=True, KeepAlive={"SuccessfulExit": False, "OtherJobEnabled": {"org.example.other": True}},
            StartInterval=300, StartCalendarInterval=[{"Hour": 3, "Minute": 0}], StandardOutPath=log,
            StandardErrorPath=log, ThrottleInterval=10, ProcessType="Background", Nice=5, LowPriorityIO=True,
            ExitTimeOut=30, AbandonProcessGroup=True, LimitLoadToSessionType=["Aqua", "Background"])
        self.assertEqual(codes, [])
        self.assertNotIn("LimitLoadToSessionType", daemon)

    def test_every_key_with_reach_in_the_system_domain_is_refused(self):
        for key, value in (("UserName", "root"), ("GroupName", "wheel"), ("InitGroups", True), ("Umask", 0),
                           ("SessionCreate", True), ("LaunchEvents", {}), ("MachServices", {"x": True}),
                           ("Sockets", {}), ("inetdCompatibility", {}), ("WatchPaths", ["/etc"]),
                           ("QueueDirectories", ["/tmp"]), ("RootDirectory", "/"), ("HardResourceLimits", {}),
                           ("SoftResourceLimits", {}), ("EnableTransactions", True), ("Disabled", False)):
            with self.subTest(key=key):
                self.assertEqual(self.problems(**{key: value})[0], ["key_not_allowed"])

    def test_values_of_the_wrong_shape_are_refused(self):
        for key, value in (("Nice", -1), ("Nice", 21), ("StartInterval", 0), ("StartInterval", True),
                           ("ProcessType", "Root"), ("KeepAlive", {"Foo": True}), ("KeepAlive", "yes"),
                           ("StartCalendarInterval", {"Second": 1}), ("StartCalendarInterval", []),
                           ("EnvironmentVariables", {"A": 1}), ("RunAtLoad", "yes"), ("ExitTimeOut", -1),
                           ("LimitLoadToSessionType", 3)):
            with self.subTest(key=key, value=value):
                self.assertEqual(self.problems(**{key: value})[0], ["bad_value"])

    def test_the_label(self):
        for label, code in (("bad label!", "label_shape"), (".hidden", "label_shape"), ("x" * 129, "label_shape"),
                            ("com.apple.example", "label_reserved"), (None, "label_shape")):
            with self.subTest(label=label):
                doc = {"Label": label, "ProgramArguments": [ROOT_OWNED_PROGRAM]}
                found, _ = sd.validate({k: v for k, v in doc.items() if v is not None}, f"{label}.plist",
                                       self.account)
                self.assertIn(code, [p["code"] for p in found])
        found, _ = sd.validate({"Label": LABEL, "ProgramArguments": [ROOT_OWNED_PROGRAM]}, "other.plist",
                               self.account)
        self.assertEqual([p["code"] for p in found], ["file_name"])

    def test_the_program(self):
        own = self.home / "bin" / "job"
        own.parent.mkdir(mode=0o755)
        own.write_text("#!/bin/sh\n")
        own.chmod(0o755)
        self.assertEqual(self.problems(ProgramArguments=[str(own)])[0], [], "the account's own program")
        for args, code in (([], "program_arguments"), ("sh", "program_arguments"), ([1], "program_arguments"),
                           (["sh"], "program_not_absolute"), ([str(self.home / "absent")], "program_missing"),
                           ([str(self.home)], "program_not_regular_file")):
            with self.subTest(args=args):
                self.assertIn(code, self.problems(ProgramArguments=args)[0])
        self.assertEqual(self.problems(Program="/bin/echo")[0], ["program_mismatch"])
        own.chmod(0o775)
        self.assertEqual(self.problems(ProgramArguments=[str(own)])[0], ["program_writable_by_others"])
        own.chmod(0o755)
        open_dir = self.home / "open"
        open_dir.mkdir()
        open_dir.chmod(0o777)
        (open_dir / "job").write_text("#!/bin/sh\n")
        (open_dir / "job").chmod(0o755)
        self.assertEqual(self.problems(ProgramArguments=[str(open_dir / "job")])[0], ["program_dir_writable"])
        open_dir.chmod(0o1777)
        self.assertEqual(self.problems(ProgramArguments=[str(open_dir / "job")])[0], [], "a sticky folder")
        link = self.home / "link"
        link.symlink_to(open_dir / "job")
        open_dir.chmod(0o777)
        self.assertEqual(self.problems(ProgramArguments=[str(link)])[0], ["program_dir_writable"],
                         "the file a link resolves to is checked too")

    def test_paths_stay_in_the_home(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.home / "logs").symlink_to(outside)
        (self.home / "out.log").symlink_to(outside / "out.log")
        for key, value, code in (("WorkingDirectory", str(self.base), "path_outside_home"),
                                 ("StandardOutPath", "job.log", "path_not_absolute"),
                                 ("StandardOutPath", str(self.home / "logs" / "job.log"), "path_outside_home"),
                                 ("StandardErrorPath", str(self.home / "out.log"), "path_symlink"),
                                 ("StandardErrorPath", str(self.home / ".." / "x.log"), "path_outside_home"),
                                 ("WorkingDirectory", 3, "bad_value")):
            with self.subTest(key=key, value=value):
                self.assertEqual(self.problems(**{key: value})[0], [code])
        self.assertEqual(self.problems(StandardOutPath=str(self.home / "fresh.log"))[0], [])


class Reading(Base):
    def test_plan_reports_each_definition_and_loads_nothing(self):
        self.define()
        self.define({"Label": "org.example.bad", "ProgramArguments": [ROOT_OWNED_PROGRAM], "UserName": "root"})
        result = sd.plan(self.account)
        by = {d["label"]: d for d in result["definitions"]}
        self.assertEqual(by[LABEL]["verdict"], "ok")
        self.assertEqual(by[LABEL]["daemon"]["UserName"], "space")
        self.assertEqual(by["org.example.bad"]["verdict"], ["key_not_allowed"])
        self.assertIsNone(by["org.example.bad"]["daemon"])
        self.assertEqual(self.launchctl.calls, [])
        self.assertEqual(sd.plan(self.account, labels=["org.example.none"])["not_found"], ["org.example.none"])

    def test_plan_reads_the_staged_folder_on_request(self):
        staged = Path(self.account.staged_dir())
        staged.mkdir(parents=True, mode=0o700)
        (staged / f"{LABEL}.plist").write_bytes(plistlib.dumps({"Label": LABEL,
                                                                 "ProgramArguments": [ROOT_OWNED_PROGRAM]}))
        self.assertEqual([d["label"] for d in sd.plan(self.account, "staged")["definitions"]], [LABEL])
        self.assertEqual(sd.plan(self.account)["definitions"], [])

    def test_a_definition_root_must_not_trust_is_refused(self):
        target = self.base / "elsewhere.plist"
        target.write_bytes(plistlib.dumps({"Label": "org.example.link", "ProgramArguments": [ROOT_OWNED_PROGRAM]}))
        (self.folder / "org.example.link.plist").symlink_to(target)
        self.define({"Label": "org.example.open", "ProgramArguments": [ROOT_OWNED_PROGRAM]}, mode=0o664)
        big = self.folder / "org.example.big.plist"
        big.write_bytes(b" " * (sd.MAX_DEFINITION_BYTES + 1))
        big.chmod(0o644)
        (self.folder / "org.example.junk.plist").write_bytes(b"not a plist")
        by = {d["label"]: d["verdict"] for d in sd.plan(self.account)["definitions"]}
        self.assertEqual(by, {"org.example.link": ["symlink"], "org.example.open": ["writable_by_others"],
                              "org.example.big": ["too_large"], "org.example.junk": ["unreadable_plist"]})

    def test_a_definition_another_account_owns_is_refused(self):
        path = self.define()
        other = sd.Account("other", os.getuid() + 1, os.getgid(), "staff", str(self.home))
        data, problems = sd._read_one(str(path), other)
        self.assertIsNone(data)
        self.assertEqual([p["code"] for p in problems], ["not_owned_by_account"])

    def test_a_folder_others_can_write_or_a_linked_folder_is_refused(self):
        self.folder.chmod(0o770)
        with self.assertRaisesRegex(sd.SpaceDaemonsError, "writable by group or others"):
            sd.plan(self.account)
        self.folder.chmod(0o700)
        real = self.base / "real"
        shutil.move(str(self.folder), str(real))
        self.folder.symlink_to(real)
        with self.assertRaisesRegex(sd.SpaceDaemonsError, "not a folder"):
            sd.plan(self.account)

    def test_no_folder_is_no_definition(self):
        self.folder.rmdir()
        self.assertEqual(sd.plan(self.account)["definitions"], [])

    def test_the_paths_are_machine_identitys_without_xdg_data_home(self):
        import machine_identity as MI
        env = {k: v for k, v in os.environ.items() if k != "XDG_DATA_HOME"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.account.background_dir(), str(MI.background_dir(self.home)))
            self.assertEqual(self.account.staged_dir(), str(MI.staging_dir(self.home, "launchd")))
            self.assertEqual(self.account.data_dir(), str(MI.data_dir(self.home)))


class Accounts(unittest.TestCase):
    class Entry:
        def __init__(self, uid, gid=20, home="/Users/example"):
            self.pw_uid, self.pw_gid, self.pw_dir = uid, gid, home

    class Group:
        gr_name = "staff"

    def lookup(self, name, entry):
        def getpwnam(n):
            if entry is None:
                raise KeyError(n)
            return entry
        return sd.lookup_account(name, getpwnam=getpwnam, getgrgid=lambda gid: self.Group())

    def test_root_system_and_unknown_accounts_are_refused(self):
        for name, entry, reason in (("root", self.Entry(0), "root"), ("_www", self.Entry(70), "system account"),
                                    ("nobody", None, "no account")):
            with self.subTest(name=name), self.assertRaisesRegex(sd.SpaceDaemonsError, reason):
                self.lookup(name, entry)

    def test_a_space_account(self):
        account = self.lookup("space", self.Entry(600))
        self.assertEqual((account.name, account.uid, account.group, account.home),
                         ("space", 600, "staff", "/Users/example"))
        self.assertEqual(account.background_dir(),
                         "/Users/example/.local/share/project-observatory/background/LaunchAgents")


class Applying(Base):
    def test_apply_writes_the_daemon_atomically_root_wheel_0644_and_loads_it(self):
        source = self.define()
        result = sd.apply(self.system(), self.account)
        self.assertTrue(result["ok"], result)
        path = self.daemon()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        doc = plistlib.loads(path.read_bytes())
        self.assertEqual((doc["Label"], doc["UserName"], doc["InitGroups"]), (LABEL, "space", True))
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name], "no temporary file is left")
        self.assertTrue(any(isinstance(c, int) for c in self.owner_calls), "the written file is made root's")
        self.assertIn(str(self.sysroot / sd.REGISTRY_DIR), self.owner_calls, "the registry folder is made root's")
        self.assertEqual(stat.S_IMODE((self.sysroot / sd.REGISTRY_DIR).stat().st_mode), 0o755)
        entry = self.registry()["daemons"][0]
        self.assertEqual({k: entry[k] for k in ("label", "user", "uid")}, {"label": LABEL, "user": "space",
                                                                             "uid": os.getuid()})
        self.assertEqual(entry["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(entry["written_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE((self.sysroot / sd.REGISTRY_DIR / sd.REGISTRY_NAME).stat().st_mode), 0o644)
        self.assertIn(["bootstrap", "system", str(path)], self.launchctl.calls)

    def test_a_refused_definition_is_skipped_and_nothing_of_it_is_written(self):
        self.define({"Label": LABEL, "ProgramArguments": [ROOT_OWNED_PROGRAM], "UserName": "root"})
        result = sd.apply(self.system(), self.account)
        self.assertFalse(result["ok"])
        self.assertEqual(result["results"][0]["outcome"], "skipped")
        self.assertFalse(self.daemon().exists())
        self.assertNotIn("bootstrap", [c[0] for c in self.launchctl.calls])

    def test_apply_needs_root_and_the_root_owned_copy(self):
        self.define()
        with self.assertRaisesRegex(sd.SpaceDaemonsError, "needs root: sudo /usr/bin/python3 "
                                    "/usr/local/libexec/observatory-space-daemons apply --user space"):
            sd.apply(self.system(euid=os.getuid() or 501), self.account)
        with self.assertRaisesRegex(sd.SpaceDaemonsError, "root-owned copy: the copy x is not owned by root"):
            sd.apply(self.system(self_check=lambda: ["the copy x is not owned by root"]), self.account)
        self.assertFalse(self.daemon().exists())
        self.assertEqual(self.launchctl.calls, [])

    def test_the_copy_check(self):
        mine = self.base / "copy"
        mine.write_text("x")
        mine.chmod(0o755)
        if os.getuid() != 0:
            self.assertIn(f"the copy {mine} is not owned by root", sd.check_own_copy(str(mine)))
        mine.chmod(0o775)
        self.assertIn(f"the copy {mine} is writable by group or others", sd.check_own_copy(str(mine)))
        link = self.base / "link"
        link.symlink_to(mine)
        self.assertEqual(sd.check_own_copy(str(link)), [f"the copy {link} is a symbolic link"])
        system_file = Path("/usr/bin/true")
        if system_file.is_file() and not system_file.is_symlink() and system_file.stat().st_uid == 0:
            self.assertEqual(sd.check_own_copy(str(system_file)), [], "a root-owned file in root's folders")

    def test_a_daemon_it_did_not_write_is_never_replaced(self):
        self.define()
        foreign = self.daemon()
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"someone else's")
        result = sd.apply(self.system(), self.account)
        self.assertEqual(result["results"][0]["problems"][0]["code"], "not_written_by_this_tool")
        self.assertEqual(foreign.read_bytes(), b"someone else's")

    def test_a_label_loaded_by_someone_else_is_refused(self):
        self.define()
        self.launchctl.loaded.add(LABEL)
        result = sd.apply(self.system(), self.account)
        self.assertEqual(result["results"][0]["problems"][0]["code"], "label_taken")
        self.assertNotIn("bootout", [c[0] for c in self.launchctl.calls])

    def test_another_accounts_daemon_and_a_changed_file_are_refused(self):
        self.define()
        sd.apply(self.system(), self.account)
        entries = self.registry()["daemons"]
        entries[0]["user"] = "other"
        sd.write_registry(self.system(), entries)
        self.assertEqual(sd.apply(self.system(), self.account)["results"][0]["problems"][0]["code"],
                         "owned_by_other_account")
        entries[0]["user"] = "space"
        sd.write_registry(self.system(), entries)
        self.daemon().write_bytes(b"edited by hand")
        self.assertEqual(sd.apply(self.system(), self.account)["results"][0]["problems"][0]["code"],
                         "changed_since_written")
        self.assertEqual(self.daemon().read_bytes(), b"edited by hand")

    def test_applying_again_replaces_its_own_daemon(self):
        self.define()
        sd.apply(self.system(), self.account)
        self.define(RunAtLoad=True)
        result = sd.apply(self.system(), self.account)
        self.assertTrue(result["ok"], result)
        self.assertIn(["bootout", f"system/{LABEL}"], self.launchctl.calls)
        self.assertTrue(plistlib.loads(self.daemon().read_bytes())["RunAtLoad"])

    def test_a_failed_bootstrap_leaves_nothing_behind(self):
        self.define()
        self.launchctl.codes["bootstrap"] = 5
        result = sd.apply(self.system(), self.account)
        self.assertEqual(result["results"][0]["outcome"], "failed")
        self.assertEqual(result["results"][0]["problems"][0]["code"], "bootstrap_failed")
        self.assertFalse(self.daemon().exists())
        self.assertEqual(self.registry()["daemons"], [])

    def test_a_failed_bootstrap_puts_the_previous_daemon_back(self):
        self.define()
        sd.apply(self.system(), self.account)
        before = self.daemon().read_bytes()
        self.define(RunAtLoad=True)
        self.launchctl.codes["bootstrap"] = 5
        result = sd.apply(self.system(), self.account)
        self.assertEqual(result["results"][0]["put_back"], "the previous daemon is back")
        self.assertEqual(self.daemon().read_bytes(), before)
        self.assertEqual(self.registry()["daemons"][0]["written_sha256"], hashlib.sha256(before).hexdigest())

    def test_remove_takes_only_its_own_entries(self):
        self.define()
        sd.apply(self.system(), self.account)
        with self.assertRaisesRegex(sd.SpaceDaemonsError, "needs root"):
            sd.remove(self.system(euid=os.getuid() or 501), self.account, LABEL)
        other = sd.Account("other", os.getuid(), os.getgid(), "staff", str(self.home))
        with self.assertRaisesRegex(sd.SpaceDaemonsError, "names no daemon"):
            sd.remove(self.system(), other, LABEL)
        with self.assertRaisesRegex(sd.SpaceDaemonsError, "names no daemon"):
            sd.remove(self.system(), self.account, "org.example.unknown")
        result = sd.remove(self.system(), self.account, LABEL)
        self.assertEqual(result["outcome"], "removed")
        self.assertIn(["bootout", f"system/{LABEL}"], self.launchctl.calls)
        self.assertFalse(self.daemon().exists())
        self.assertEqual(self.registry()["daemons"], [])

    def test_status_says_what_is_loaded_and_unchanged(self):
        self.define()
        sd.apply(self.system(), self.account)
        rows = sd.status(self.system(euid=501), self.account)["daemons"]
        self.assertEqual([(r["label"], r["loaded"], r["matches"]) for r in rows], [(LABEL, True, True)])
        self.daemon().write_bytes(b"edited")
        self.launchctl.loaded.clear()
        rows = sd.status(self.system(euid=501), self.account)["daemons"]
        self.assertEqual([(r["loaded"], r["present"], r["matches"]) for r in rows], [(False, True, False)])

    def test_a_registry_that_is_not_one_is_refused_not_replaced(self):
        self.define()
        path = self.sysroot / sd.REGISTRY_DIR / sd.REGISTRY_NAME
        path.parent.mkdir(parents=True)
        path.write_text('{"schema": "something-else"}')
        with self.assertRaisesRegex(sd.SpaceDaemonsError, "move it aside"):
            sd.apply(self.system(), self.account)
        self.assertEqual(path.read_text(), '{"schema": "something-else"}')


@unittest.skipUnless(POSIX, "accounts are POSIX")
class CommandLine(Base):
    """The script as an administrator runs it, through the test seams a non-root process may use."""

    def run_tool(self, *args, python=sys.executable):
        try:
            import pwd
            me = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError):
            self.skipTest("this account has no name")
        if os.getuid() < sd.FIRST_PERSON_UID:
            self.skipTest("this account's uid is a system account's")
        fake = self.base / "launchctl"
        fake.write_text("#!/bin/sh\nexit 113\n")
        fake.chmod(0o755)
        env = dict(os.environ, **{sd.TEST_SWITCH: "1", sd.TEST_HOME: str(self.home),
                                  sd.TEST_ROOT: str(self.sysroot), sd.TEST_LAUNCHCTL: str(fake)})
        return subprocess.run([python, "-I", str(TOOL), *args, "--user", me, "--json"], capture_output=True,
                              text=True, timeout=60, env=env)

    def test_plan_and_the_refused_apply(self):
        self.define()
        for python in [sys.executable] + (["/usr/bin/python3"] if Path("/usr/bin/python3").exists() else []):
            with self.subTest(python=python):
                out = self.run_tool("plan", python=python)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(json.loads(out.stdout)["definitions"][0]["verdict"], "ok")
                out = self.run_tool("apply", python=python)
                self.assertEqual(out.returncode, 2)
                self.assertIn("needs root", json.loads(out.stdout)["error"])
                out = self.run_tool("status", python=python)
                self.assertEqual((out.returncode, json.loads(out.stdout)["daemons"]), (0, []))
        self.assertFalse((self.sysroot / "Library").exists(), "nothing was written")


if __name__ == "__main__":
    unittest.main()
