#!/usr/bin/env python3
"""Install, inspect or remove the observatory-log Claude Code plugin.

install  adds the GitHub marketplace, installs the plugin, turns plugin
         auto-update ON (pass --no-auto-update to keep it off) and sets
         OBSERVATORY_ROOT / OBSERVATORY_HOME for the plugin's hooks.
status   reports the marketplace source, auto-update, installed version and
         whether the hooks can find this engine and workspace.
uninstall removes the plugin, its marketplace and the keys install added.

The same plugin can also reach Claude Code through another channel: a launcher
(the PassionCode one installs it from its local `passioncode` marketplace as
`observatory-log@passioncode`) or any other marketplace. An installed and enabled
copy under such an id is recognised as the plugin: `status` is satisfied by it and
names the channel that manages it, `install` then writes only the hook
environment and never adds the engine's own id beside it (two copies fire every
hook twice, and the launcher would uninstall the second one again), and
`uninstall` keeps the hook environment that copy still reads.

Only Claude Code's own CLI and its user settings are touched. Settings are
backed up once before the first change and rewritten atomically; unrelated
keys are preserved. CLAUDE_CONFIG_DIR selects a different Claude Code home.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration  # noqa: E402

MARKETPLACE = "observatory-log"
PLUGIN = f"{MARKETPLACE}@{MARKETPLACE}"
PLUGIN_NAME = MARKETPLACE
#: Marketplaces whose owner is a launcher, named the way the operator knows it.
LAUNCHERS = {"passioncode": "the passioncode launcher"}
#: The repository moved to the PassionCode.ai organization in 0.4.0. An install
#: that still points at the old address is replaced by `install`, which is the
#: same path an earlier directory-sourced install takes (GitHub redirects the
#: old address meanwhile, so nothing breaks before the operator acts).
REPOSITORY = os.environ.get("OBSERVATORY_PLUGIN_REPOSITORY", "passioncode-ai/project-observatory-dashboard")
PREVIOUS_REPOSITORIES = ("ssheleg/project-observatory-dashboard",)
SOURCE = {"source": "github", "repo": REPOSITORY}
ENV_KEYS = ("OBSERVATORY_ROOT", "OBSERVATORY_HOME", "OBSERVATORY_PYTHON")


class PluginError(RuntimeError):
    pass


def claude_home() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value).expanduser() if value else Path.home() / ".claude"


def claude_bin() -> str:
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not found:
        raise PluginError("Claude Code (`claude`) is not on PATH. Install Claude Code, or add the "
                          f"marketplace by hand: `/plugin marketplace add {REPOSITORY}` then "
                          f"`/plugin install {PLUGIN}`")
    return found


def run_claude(*args: str) -> subprocess.CompletedProcess:
    p = subprocess.run([claude_bin(), "plugin", *args], capture_output=True, text=True, timeout=300)
    return p


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise PluginError(f"{path} is a symbolic link; refusing to edit it")
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PluginError(f"{path} is not valid JSON; fix it before installing") from None
    if not isinstance(doc, dict):
        raise PluginError(f"{path} must hold a JSON object")
    return doc


def write_json(path: Path, doc: dict) -> None:
    """Back up once, then replace atomically, keeping the file's mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-observatory")
        if not backup.exists():
            shutil.copy2(path, backup)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def settings_path() -> Path:
    return claude_home() / "settings.json"


def known_path() -> Path:
    return claude_home() / "plugins" / "known_marketplaces.json"


def installed_plugins() -> dict:
    doc = read_json(claude_home() / "plugins" / "installed_plugins.json")
    plugins = doc.get("plugins") or {}
    return plugins if isinstance(plugins, dict) else {}


def installed_version(plugin: str = PLUGIN) -> str | None:
    rows = installed_plugins().get(plugin) or []
    return rows[0].get("version") if rows and isinstance(rows[0], dict) else None


def channel_name(marketplace: str) -> str:
    return LAUNCHERS.get(marketplace, f"the {marketplace} marketplace")


def other_channels(settings: dict | None = None) -> list[dict]:
    """Copies of this plugin installed AND enabled under another marketplace's id.

    Enabled means `enabledPlugins[id] is True` in user settings, the key Claude
    Code's own `plugin install` writes; a copy the operator disabled is not a
    channel that runs the hooks, so it does not stop the engine installing its own.
    """
    candidates = []
    for plugin_id, rows in sorted(installed_plugins().items()):
        name, _, market = plugin_id.partition("@")
        if name == PLUGIN_NAME and market and market != MARKETPLACE and rows:
            candidates.append((plugin_id, market))
    if not candidates:
        return []
    # Settings are read only when another copy exists, so a machine without one
    # meets the same checks in the same order as before (missing CLI first).
    if settings is None:
        settings = read_json(settings_path())
    enabled = settings.get("enabledPlugins") or {}
    found = []
    for plugin_id, market in candidates:
        if enabled.get(plugin_id) is not True:
            continue
        found.append({"plugin": plugin_id, "marketplace": market, "managed_by": channel_name(market),
                      "version": installed_version(plugin_id)})
    return found


def managing_channel() -> dict | None:
    """Who keeps the plugin installed: the engine's own channel, another one, or nobody.

    Returns {"plugin", "marketplace", "managed_by", "own", "version"} or None when
    no enabled copy exists. Another channel wins over the engine's own: that is the
    copy `install` must not duplicate.
    """
    settings = read_json(settings_path())
    others = other_channels(settings)
    if others:
        return dict(others[0], own=False)
    if installed_version() and (settings.get("enabledPlugins") or {}).get(PLUGIN) is not False:
        return {"plugin": PLUGIN, "marketplace": MARKETPLACE, "managed_by": "this engine (`full agent install`)",
                "own": True, "version": installed_version()}
    return None


def update_advice() -> str:
    """The `next` line after an engine update, fitted to the channel that manages the plugin."""
    try:
        channel = managing_channel()
    except (PluginError, OSError):
        return "Restart Claude Code sessions and `project-observatory full agent install` if the plugin is used"
    if channel is None:
        return "Restart Claude Code sessions"
    if channel["own"]:
        return ("Restart Claude Code sessions and run `project-observatory full agent install`: "
                "it brings the plugin to the version this engine ships")
    return (f"Restart Claude Code sessions: the plugin is managed by {channel['managed_by']} "
            f"({channel['plugin']}), which keeps it updated")


def shipped_version() -> str:
    manifest = ROOT / "skill/plugins/observatory-log/.claude-plugin/plugin.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


def workspace_env() -> dict:
    return {"OBSERVATORY_ROOT": str(ROOT), "OBSERVATORY_HOME": str(configuration.home()),
            "OBSERVATORY_PYTHON": sys.executable}


def write_hook_env() -> dict:
    """Point the plugin's hooks at this engine and workspace; returns the replaced values."""
    settings = read_json(settings_path())
    previous_env = {k: (settings.get("env") or {}).get(k) for k in ENV_KEYS}
    settings.setdefault("env", {}).update(workspace_env())
    write_json(settings_path(), settings)
    return {k: v for k, v in previous_env.items() if v and v != workspace_env()[k]}


def install(auto_update: bool = True) -> dict:
    others = other_channels()
    if others:
        # Another channel owns the plugin: installing the engine's id beside it is
        # the duplicate this module exists to avoid. Only the hook env is ours.
        channel = others[0]
        replaced = write_hook_env()
        steps = [f"plugin {channel['plugin']} is managed by {channel['managed_by']}: left unchanged, "
                 f"no second copy installed", "hook environment written"]
        if installed_version():
            steps.append(f"{PLUGIN} is installed too: remove it with `claude plugin uninstall {PLUGIN}` "
                         "so hooks fire once")
        return {"status": "managed-elsewhere", "plugin": channel["plugin"], "managed_by": channel["managed_by"],
                "env": workspace_env(), "replaced_env": replaced, "steps": steps,
                "next": "Restart Claude Code sessions: hooks read the environment at session start."}
    steps = []
    known = read_json(known_path())
    current = (known.get(MARKETPLACE) or {}).get("source")
    if current and current != SOURCE:
        p = run_claude("marketplace", "remove", MARKETPLACE)
        if p.returncode:
            raise PluginError("could not remove the previous observatory-log marketplace: "
                              + (p.stderr or p.stdout).strip()[-300:])
        steps.append(f"replaced marketplace source {current.get('source')}")
        current = None
    p = run_claude("marketplace", "update", MARKETPLACE) if current else \
        run_claude("marketplace", "add", REPOSITORY)
    if p.returncode:
        raise PluginError("marketplace step failed: " + (p.stderr or p.stdout).strip()[-300:])
    steps.append("marketplace updated" if current else f"marketplace added from {REPOSITORY}")
    p = run_claude("install", PLUGIN)
    installed = p.returncode == 0
    # `install` succeeds without upgrading a plugin that is already present, so an
    # older copy is brought to the version this engine ships explicitly.
    if not installed or installed_version() != shipped_version():
        p = run_claude("update", PLUGIN)
        if p.returncode:
            raise PluginError("plugin install failed: " + (p.stderr or p.stdout).strip()[-300:])
        steps.append("plugin updated")
    else:
        steps.append("plugin installed")
    settings = read_json(settings_path())
    settings.setdefault("extraKnownMarketplaces", {})[MARKETPLACE] = {
        "source": dict(SOURCE), "autoUpdate": auto_update}
    settings.setdefault("enabledPlugins", {})[PLUGIN] = True
    previous_env = {k: (settings.get("env") or {}).get(k) for k in ENV_KEYS}
    settings.setdefault("env", {}).update(workspace_env())
    write_json(settings_path(), settings)
    known = read_json(known_path())
    if MARKETPLACE in known:
        known[MARKETPLACE]["autoUpdate"] = auto_update
        write_json(known_path(), known)
    steps.append(f"auto-update {'on' if auto_update else 'off'}")
    return {"status": "installed", "plugin": PLUGIN, "source": SOURCE, "auto_update": auto_update,
            "env": workspace_env(), "replaced_env": {k: v for k, v in previous_env.items()
                                                      if v and v != workspace_env()[k]},
            "steps": steps, "next": "Restart Claude Code sessions: plugins load at session start."}


def status() -> dict:
    settings = read_json(settings_path())
    others = other_channels(settings)
    if others:
        return managed_elsewhere_status(settings, others)
    known = read_json(known_path()).get(MARKETPLACE) or {}
    extra = (settings.get("extraKnownMarketplaces") or {}).get(MARKETPLACE) or {}
    env = settings.get("env") or {}
    have, ship = installed_version(), shipped_version()
    wanted = workspace_env()
    problems = []
    if not have:
        problems.append("plugin not installed: run `project-observatory full agent install`")
    elif have != ship:
        problems.append(f"installed {have}, this engine ships {ship}: run `claude plugin update {PLUGIN}`")
    source = known.get("source") or extra.get("source")
    if source and source != SOURCE:
        if source.get("source") == "github" and source.get("repo") in PREVIOUS_REPOSITORIES:
            problems.append(f"marketplace points at the previous address {source.get('repo')}; "
                            f"`agent install` moves it to {REPOSITORY}")
        else:
            problems.append(f"marketplace comes from {source.get('source')}, not GitHub; "
                            "`agent install` switches it and enables updates")
    auto = known.get("autoUpdate", extra.get("autoUpdate", False))
    if have and not auto:
        problems.append("auto-update is off: `project-observatory full agent install` turns it on")
    problems += hook_env_problems({"env": env})
    return {"plugin": PLUGIN, "installed_version": have, "shipped_version": ship,
            "marketplace_source": source, "auto_update": bool(auto),
            "hook_env": {k: env.get(k) for k in ENV_KEYS}, "ok": not problems, "problems": problems}


def hook_env_problems(settings: dict) -> list[str]:
    env, wanted = settings.get("env") or {}, workspace_env()
    # "not set" for an absent variable: `None` is Python's word, not the user's.
    return [(f"{k} is {env[k]!r}" if env.get(k) is not None else f"{k} is not set")
            + f" in Claude Code settings; hooks need {wanted[k]!r}"
            for k in ENV_KEYS if env.get(k) != wanted[k]]


def managed_elsewhere_status(settings: dict, others: list[dict]) -> dict:
    """Status when another channel installs the plugin: its update policy is not ours
    to judge, so only duplicates and the hook environment can be problems."""
    channel, ship = others[0], shipped_version()
    problems, notes = [], []
    if installed_version() and (settings.get("enabledPlugins") or {}).get(PLUGIN) is not False:
        problems.append(f"observatory-log is installed twice ({PLUGIN} and {channel['plugin']}), so every hook "
                        f"fires twice: remove the engine's copy with `claude plugin uninstall {PLUGIN}`")
    for extra in others[1:]:
        problems.append(f"observatory-log is installed twice ({channel['plugin']} and {extra['plugin']}), so every "
                        f"hook fires twice: disable one of them in Claude Code")
    if channel["version"] != ship:
        notes.append(f"{channel['plugin']} is at {channel['version']}, this engine ships {ship}; "
                     f"{channel['managed_by']} updates it")
    problems.extend(hook_env_problems(settings))
    env = settings.get("env") or {}
    return {"plugin": channel["plugin"], "managed_by": channel["managed_by"],
            "installed_version": channel["version"], "shipped_version": ship,
            "hook_env": {k: env.get(k) for k in ENV_KEYS}, "ok": not problems, "problems": problems,
            "notes": notes}


def uninstall() -> dict:
    steps = []
    for args in (("uninstall", PLUGIN), ("marketplace", "remove", MARKETPLACE)):
        p = run_claude(*args)
        steps.append(f"{' '.join(args)}: {'ok' if p.returncode == 0 else 'not present'}")
    settings = read_json(settings_path())
    changed = False
    if MARKETPLACE in (settings.get("extraKnownMarketplaces") or {}):
        del settings["extraKnownMarketplaces"][MARKETPLACE]; changed = True
    if PLUGIN in (settings.get("enabledPlugins") or {}):
        del settings["enabledPlugins"][PLUGIN]; changed = True
    env = settings.get("env") or {}
    others = other_channels(settings)
    if others:
        steps.append(f"hook environment kept: {others[0]['plugin']} is managed by {others[0]['managed_by']} "
                     "and its hooks still read it")
    else:
        for k, v in workspace_env().items():
            if env.get(k) == v:
                del env[k]; changed = True
    if changed:
        write_json(settings_path(), settings)
        steps.append("settings keys removed")
    return {"status": "uninstalled", "steps": steps,
            "next": "Restart Claude Code sessions. Your workspace was not touched."}


def parser() -> argparse.ArgumentParser:
    """Shared by `main` and the gate's parse-only check (`workspace.parse`)."""
    ap = argparse.ArgumentParser(prog="project-observatory full agent",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="action", required=True)
    inst = sub.add_parser("install")
    inst.add_argument("--no-auto-update", action="store_true",
                      help="install without turning plugin auto-update on")
    sub.add_parser("status")
    sub.add_parser("uninstall")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        if a.action == "install":
            result = install(auto_update=not a.no_auto_update)
        elif a.action == "status":
            result = status()
        else:
            result = uninstall()
    except (PluginError, configuration.ConfigurationError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
