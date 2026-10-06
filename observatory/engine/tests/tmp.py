#!/usr/bin/env python3
"""A temp directory that goes away, because 98 of them did not.

Every suite here builds its fixtures in `tempfile.mkdtemp()`, and `mkdtemp` is
the one that does NOT clean up — that is its whole difference from
`TemporaryDirectory`. Thirteen of the suites copy something substantial into it:
the live store is 22 MB and `test_erasure_bytes` copies it twice, the registry
and `store/raw` go in whole elsewhere.

**Measured 2026-09-07, and it is not theoretical.** One `./observatory.py check`
run took the data volume from 607 MiB free to 317 MiB — about 290 MB per run,
never returned. The volume reads 100% full, and the run before that one died with
`no space left on device` before the shell could write its own working file. A
gate that cannot run twice is not a gate.

`atexit` rather than a context manager, deliberately: the suites hold their
directories across several assertions inside one function and sometimes across
functions, so a `with` block would force every call site to be restructured, a
bigger change than the defect itself. `atexit` runs on a normal exit AND on
`SystemExit`, which is how every suite here ends, including the failing path. That
path matters most, because a failing suite is exactly the one somebody runs
again and again.

It does NOT run on SIGKILL, and that is stated rather than hidden: a killed run
leaves its directory, and `tools/check_paths.py` cannot help with that. The
remedy there is the operating system's periodic cleaner.
"""
from __future__ import annotations
import atexit
import os
import shutil
import tempfile

# A SUITE RUN BY HAND IS SANDBOXED TOO (audit A07/A08). `run_portable.py` hands every suite
# a fresh home; a suite started directly (`python tests/test_x.py`) used to fall through to
# the operator's real workspace — `test_backup_store.py` run that way wrote three copies of
# its fixture into the real backups root and pushed the real daily copies out (OBS-39).
# 130 suites import this module, so this is where the defaults are set: a temporary
# workspace, a temporary backups root, and no launchd, systemd or Keychain.
#
# AN INHERITED REAL WORKSPACE IS NOT A SANDBOX EITHER (audit A08 review). A shell
# started by the agent plugin or by `project-observatory full …` carries
# OBSERVATORY_HOME, OBSERVATORY_ROOT and OBSERVATORY_BACKUPS pointing at the
# operator's live workspace; `setdefault` kept them, and a suite run from that shell
# resolved the real store. Only a path inside a temporary directory is kept — that is
# what `run_portable.py` and the suites' own fixtures hand over.
_TEMP_ROOTS = {os.path.realpath(p) for p in (tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/folders",
                                              "/private/var/folders")}


def _inside_temp(path: str | None) -> bool:
    if not path:
        return False
    real = os.path.realpath(path)
    return any(real == root or real.startswith(root + os.sep) for root in _TEMP_ROOTS)


_replaced = [name for name in ("OBSERVATORY_HOME", "OBSERVATORY_ROOT", "OBSERVATORY_BACKUPS")
             if os.environ.get(name) and not _inside_temp(os.environ[name])]
for _name in _replaced:
    del os.environ[_name]
if _replaced:
    import sys
    print(f"tests/tmp.py: {', '.join(_replaced)} pointed outside a temporary directory; "
          "a suite never runs against a real workspace, so a sandbox is used instead", file=sys.stderr)
if not os.environ.get("OBSERVATORY_HOME"):
    _sandbox = tempfile.mkdtemp(prefix="observatory-suite-home-")
    atexit.register(shutil.rmtree, _sandbox, ignore_errors=True)
    os.environ["OBSERVATORY_HOME"] = os.path.join(_sandbox, "ws")
os.environ.setdefault("OBSERVATORY_BACKUPS", tempfile.mkdtemp(prefix="observatory-suite-backups-"))
os.environ.setdefault("OBSERVATORY_SYSTEM_SETUP", "0")


def mkdtemp(prefix: str = "observatory-") -> str:
    """`tempfile.mkdtemp`, registered for removal when the process ends."""
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path
