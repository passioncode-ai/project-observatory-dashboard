"""The Mac app follows the engine (decision D4, docs/runs/2026-10-05-auto-update).

When the installed `Project Observatory.app` is older than the engine running this pass,
the release of the engine's version is fetched and the app zip is accepted only when
every one of these holds:

- its bytes match BOTH the GitHub asset digest and the line in that release's SHA256SUMS
  (the same double check `full update` gives the wheel);
- the bundle inside names `ai.passioncode.observatory` and exactly the engine's version;
- `codesign --verify --strict --deep` passes, and the signing team is the one that signed
  the installed app — a bundle signed by anyone else never replaces it;
- Gatekeeper (`spctl --assess --type execute`) accepts it.

The swap happens only while the app is not running: a running app keeps working, the
verified bundle waits under `<home>/store/app-update/`, and the next hourly pass installs
it once the app has quit. The replaced bundle is kept under `<home>/store/app-previous/`
(one copy). An app that is not installed, or lives somewhere this user cannot write, is
named in the answer and left alone. Nothing here asks for a password or a person.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

import configuration as config

BUNDLE_ID = "ai.passioncode.observatory"
APP_NAME = "Project Observatory.app"
ZIP = "ProjectObservatory-{version}-macos.zip"
PROCESS = "ProjectObservatory"
MAX_ZIP = 200 * 1024 * 1024
RETRY_REFUSED = datetime.timedelta(hours=24)
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


class AppUpdateError(RuntimeError):
    """Why a downloaded bundle was refused. Never carries a value."""


def bundle_info(app: Path) -> dict | None:
    info = app / "Contents" / "Info.plist"
    if not info.is_file():
        return None
    try:
        doc = plistlib.loads(info.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return {"id": doc.get("CFBundleIdentifier"), "version": doc.get("CFBundleShortVersionString")}


class System:
    """The commands an app swap needs, injectable so no test runs codesign or pgrep."""

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def run(self, *argv: str, timeout: int = 120) -> tuple[int, str]:
        try:
            p = self.runner(list(argv), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 125, type(exc).__name__
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def team(self, app: Path) -> str | None:
        code, out = self.run("codesign", "-dv", "--verbose=2", str(app))
        for line in out.splitlines():
            if line.startswith("TeamIdentifier="):
                value = line.split("=", 1)[1].strip()
                return None if value in ("", "not set") else value
        return None

    def verify(self, app: Path) -> str:
        code, out = self.run("codesign", "--verify", "--strict", "--deep", str(app))
        if code != 0:
            return f"codesign refused the bundle: {out.strip()[:200]}"
        code, out = self.run("spctl", "--assess", "--type", "execute", str(app))
        if code != 0:
            return f"Gatekeeper refused the bundle: {out.strip()[:200]}"
        return ""

    def running(self) -> bool:
        return self.run("pgrep", "-x", PROCESS)[0] == 0

    def extract(self, zipped: Path, into: Path) -> None:
        code, out = self.run("ditto", "-x", "-k", str(zipped), str(into), timeout=600)
        if code != 0:
            raise AppUpdateError(f"the app zip could not be unpacked: {out.strip()[:200]}")

    def copy(self, source: Path, dest: Path) -> None:
        code, out = self.run("ditto", str(source), str(dest), timeout=600)
        if code != 0:
            raise AppUpdateError(f"the bundle could not be copied: {out.strip()[:200]}")

    def register(self, app: Path) -> None:
        if os.path.exists(LSREGISTER):
            self.run(LSREGISTER, "-f", str(app))


def installed_app(home: Path | None = None) -> Path | None:
    """The installed bundle install-app.sh puts in /Applications, else ~/Applications."""
    for folder in (Path("/Applications"), (home or Path.home()) / "Applications"):
        app = folder / APP_NAME
        info = bundle_info(app)
        if info and info["id"] == BUNDLE_ID:
            return app
    return None


class AppUpdater:
    def __init__(self, base: Path, *, system: System | None = None, fetcher=None, app: Path | None = ...,
                 version: str | None = None):
        self.base = base
        self.system = system or System()
        self.fetcher = fetcher
        self.app = installed_app() if app is ... else app
        self.version = version or config.VERSION
        self.staged = base / "store" / "app-update"
        self.previous = base / "store" / "app-previous"

    # --- the pass ------------------------------------------------------------------

    def step(self, state: dict, at: datetime.datetime) -> dict:
        last = state.get("app") or {}
        try:
            last_at = datetime.datetime.strptime(str(last.get("at")), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            last_at = None
        if (last.get("result") == "refused" and last.get("target") == self.version and last_at is not None
                and at - last_at < RETRY_REFUSED):
            # A refused download is not repeated every hour: once a day per version keeps
            # the network budget of decision D2 (review F9).
            return {"result": "not-due", "detail": "refused earlier today"}
        record = self._step()
        record["target"] = self.version
        record["at"] = at.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["app"] = record
        return {k: v for k, v in record.items() if k != "at"}

    def _step(self) -> dict:
        if self.app is None:
            return {"result": "not-installed"}
        info = bundle_info(self.app) or {}
        have = info.get("version")
        try:
            behind = config.version_tuple(str(have)) < config.version_tuple(self.version)
        except (ValueError, config.ConfigurationError):
            behind = True
        if not behind:
            shutil.rmtree(self.staged, ignore_errors=True)
            return {"result": "current", "version": have}
        if not os.access(self.app.parent, os.W_OK):
            return {"result": "not-writable", "version": have,
                    "detail": f"{self.app.parent} is not writable by this user; install "
                              f"{ZIP.format(version=self.version)} by hand"}
        if not self.system.team(self.app):
            # A local or ad hoc build has no team to match, so any download would be refused:
            # say so before fetching anything.
            return {"result": "unsigned-install", "version": have,
                    "detail": "the installed app carries no Developer ID team; install the released app once by hand"}
        try:
            bundle = self._staged_bundle() or self._download()
        except AppUpdateError as exc:
            shutil.rmtree(self.staged, ignore_errors=True)
            return {"result": "refused", "version": have, "detail": str(exc)[:300]}
        if self.system.running() or not self._swap(bundle):
            return {"result": "waiting-for-quit", "version": have, "pending": self.version}
        shutil.rmtree(self.staged, ignore_errors=True)
        return {"result": "updated", "from": have, "version": self.version}

    # --- verification ----------------------------------------------------------------

    def _check(self, bundle: Path) -> None:
        info = bundle_info(bundle) or {}
        if info.get("id") != BUNDLE_ID:
            raise AppUpdateError(f"the bundle names {info.get('id')!r}, not {BUNDLE_ID}")
        if info.get("version") != self.version:
            raise AppUpdateError(f"the bundle is version {info.get('version')!r}, not {self.version}")
        why = self.system.verify(bundle)
        if why:
            raise AppUpdateError(why)
        expected, actual = self.system.team(self.app), self.system.team(bundle)
        if not expected or actual != expected:
            raise AppUpdateError("the new bundle is not signed by the team that signed the installed app")

    def _staged_bundle(self) -> Path | None:
        """A bundle staged by an earlier pass, verified again before it is used."""
        bundle = self.staged / self.version / APP_NAME
        if not bundle.is_dir():
            return None
        self._check(bundle)
        return bundle

    def _download(self) -> Path:
        import engine_update as eu
        fetcher = self.fetcher or eu.Fetcher()
        api, repo = eu.release_source()
        try:
            doc = fetcher.get_json(f"{api}/repos/{repo}/releases/tags/v{self.version}")
        except eu.UpdateError as exc:
            raise AppUpdateError(f"the release v{self.version} could not be read: {exc}") from None
        assets = {row.get("name"): row for row in doc.get("assets") or [] if isinstance(row, dict)}
        name = ZIP.format(version=self.version)
        zipped_row, sums_row = assets.get(name), assets.get(eu.SUMS)
        if not zipped_row or not sums_row:
            raise AppUpdateError(f"release v{self.version} has no {name} or {eu.SUMS}")
        zipped_asset, sums_asset = eu._asset(zipped_row), eu._asset(sums_row)
        if not zipped_asset or not sums_asset or not zipped_asset.digest or not sums_asset.digest:
            raise AppUpdateError(f"GitHub publishes no sha256 digest for {name} or {eu.SUMS}")
        shutil.rmtree(self.staged, ignore_errors=True)
        work = self.staged / self.version
        work.mkdir(parents=True, mode=0o700)
        try:
            sums_file = work / eu.SUMS
            digest, _ = fetcher.fetch_file(sums_asset.url, sums_file, eu.MAX_SUMS)
            if digest != sums_asset.digest:
                raise AppUpdateError(f"{eu.SUMS} does not match its GitHub asset digest")
            listed = eu.parse_sums(sums_file.read_text(encoding="utf-8", errors="replace")).get(name)
            zipped = work / name
            digest, size = fetcher.fetch_file(zipped_asset.url, zipped, MAX_ZIP)
            if zipped_asset.size is not None and size != zipped_asset.size:
                raise AppUpdateError(f"{name} is {size} bytes; the release says {zipped_asset.size}")
            if digest != zipped_asset.digest or digest != listed:
                raise AppUpdateError(f"{name} does not match its GitHub digest and its line in {eu.SUMS}")
            unpacked = Path(tempfile.mkdtemp(prefix=".unpack-", dir=work))
            self.system.extract(zipped, unpacked)
            found = [p for p in unpacked.iterdir() if p.suffix == ".app" and p.is_dir() and not p.is_symlink()]
            if len(found) != 1:
                raise AppUpdateError(f"{name} holds {len(found)} app bundles, not one")
            bundle = work / APP_NAME
            os.rename(found[0], bundle)
            shutil.rmtree(unpacked, ignore_errors=True)
            zipped.unlink(missing_ok=True)
            sums_file.unlink(missing_ok=True)
            self._check(bundle)
            return bundle
        except eu.UpdateError as exc:
            raise AppUpdateError(str(exc)) from None

    # --- the swap --------------------------------------------------------------------

    def _swap(self, bundle: Path) -> bool:
        """Copy beside the installed app, then two renames; the old bundle is kept.
        False when the app was opened during the copy: nothing is replaced then."""
        dest = self.app
        installing = dest.parent / f".{APP_NAME}.installing"
        retired = dest.parent / f".{APP_NAME}.previous"
        shutil.rmtree(installing, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)
        self.system.copy(bundle, installing)
        self._check(installing)
        if self.system.running():
            shutil.rmtree(installing, ignore_errors=True)
            return False
        os.rename(dest, retired)
        try:
            os.rename(installing, dest)
        except OSError:
            os.rename(retired, dest)
            raise
        shutil.rmtree(self.previous, ignore_errors=True)
        self.previous.mkdir(parents=True, mode=0o700)
        try:
            self.system.copy(retired, self.previous / APP_NAME)
        finally:
            shutil.rmtree(retired, ignore_errors=True)
        self.system.register(dest)
        return True
