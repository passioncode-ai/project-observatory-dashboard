#!/usr/bin/env python3
"""What an agent should know about the project it just opened, in one line.

    tools/session_start.py                 # hook payload on stdin ({"cwd": ..., "session_id": ...})
    tools/session_start.py --cwd <dir>     # by hand

The harness tells the agent at the moment it matters. The observatory knows
each project's keys by name, its open findings and its last activity; without
this, an agent opening the project learns none of it, and either reads `.env`
by hand (which is how values leak into transcripts) or works in a folder the
registry has never joined.

The rule this obeys: a session-start hook must not compete with the operator's
own instructions. An injector that prints pages of doctrine at every start
crowds them out, so this prints AT MOST ONE LINE, and only when it is
actionable. A folder that is not a git repository, the observatory's own
checkout, an unreadable registry: silence, exit 0. It never blocks a session
and never raises; a failure goes to `hooks.jsonl` under the state logs with its
reason, which is how a silent hook is told apart from a broken one.

What it prints:

  known project     "observatory: <name> · N critical / M warning · keys by
                    name: K (registry, .env) · last activity · use_secret.py
                    names <name> · a link to the project's board entry"
  unknown folder    a line saying the folder is not in the registry, and a row
                    in `sessions-seen.jsonl` (cwd, remote, when), so a folder
                    an agent works in and the registry never joined becomes a
                    measured fact instead of a memory. A folder under the
                    configured projects root is picked up by the next scan; a
                    folder elsewhere is named as unobserved rather than pretended.
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths              

SEEN = paths.SCRATCH / "sessions-seen.jsonl"
HOOK_LOG = paths.STATE / "logs" / "hooks.jsonl"
GIT = "/opt/homebrew/bin/git" if pathlib.Path("/opt/homebrew/bin/git").exists() else "git"


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(row: dict) -> None:
    try:
        HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HOOK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now(), "hook": "session-start", **row}, ensure_ascii=False) + "\n")
        HOOK_LOG.chmod(0o600)
    except OSError:
        pass


def git_top(cwd: pathlib.Path) -> pathlib.Path | None:
    try:
        p = subprocess.run([GIT, "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return pathlib.Path(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None


def remote_of(top: pathlib.Path) -> str:
    try:
        p = subprocess.run([GIT, "-C", str(top), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def nwo(remote: str) -> str:
    """`owner/name` out of any GitHub remote spelling, or ''."""
    r = remote.strip()
    for pre in ("git@github.com:", "https://github.com/", "ssh://git@github.com/", "git://github.com/"):
        if r.startswith(pre):
            r = r[len(pre):]
            return r[:-4] if r.endswith(".git") else r
    return ""


def load(name: str) -> dict:
    p = paths.REGISTRY / name
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def resolve(top: pathlib.Path, remote: str) -> dict | None:
    """The project this checkout belongs to — by folder under the estate first,
    by the remote's owner/name through the repositories second."""
    projects = load("projects.json").get("projects") or []
    try:
        rel = top.resolve().relative_to(paths.DATA.resolve())
        folder = rel.parts[0] if rel.parts else None
    except ValueError:
        folder = None
    if folder:
        for p in projects:
            if folder in (p.get("local_folders") or []):
                return p
    owner_name = nwo(remote)
    if owner_name:
        repos = {r.get("name_with_owner"): r for r in load("repositories.json").get("repositories") or []}
        rep = repos.get(owner_name)
        if rep:
            edges = load("relations.json").get("relations") or []
            for e in edges:
                if e.get("to") == rep.get("id") or e.get("repository") == rep.get("id"):
                    pid = e.get("from") or e.get("project")
                    for p in projects:
                        if p.get("id") == pid:
                            return p
    return None


def _translator():
    """The hook speaks the workspace's language (`interface.locale`), like the
    dashboard; an unreadable setting falls back to English rather than
    silencing the line, which is actionable in either language."""
    sys.path.insert(0, str(paths.ROOT / "dashboard"))
    import i18n
    try:
        import configuration
        return i18n.Translator(configuration.interface_locale())
    except Exception:
        return i18n.Translator()


def state_line(p: dict) -> str:
    pid, name = p["id"], p.get("name") or p["id"]
    f = load("findings.json").get("findings") or []
    # A finding names its subject by project id, by name, or by FOLDER (a
    # vault slot or an env file path carries the folder), so all three spell
    # "this project" here; the board's own subjects are the source.
    folders = [fo for fo in (p.get("local_folders") or []) if fo]
    def _mine(subj: str) -> bool:
        return (subj == pid or pid in subj or subj.endswith(":" + name) or f":{name}/" in subj
                or any(f":{fo}/" in subj or subj.endswith(":" + fo) or f"/{fo}/" in subj for fo in folders))
    mine = [x for x in f if _mine(str(x.get("subject", "")))]
    crit = sum(1 for x in mine if x.get("severity") == "critical")
    warn = sum(1 for x in mine if x.get("severity") == "warning")
    creds = load("credentials.json").get("credentials") or []
    vault = sum(1 for c in creds if pid in (c.get("used_by") or []))
    env_files = load("env-inventory.json").get("files") or []
    folders = set(p.get("local_folders") or [])
    env_keys = sum(1 for fl in env_files if fl.get("project") in folders
                   for v in fl.get("variables") or [] if v.get("class") == "secret")
    last = p.get("last_activity_on") or "—"
    t = _translator()
    bits = [f"observatory: {name}"]
    bits.append(t("{crit} critical / {warn} warning", crit=crit, warn=warn) if (crit or warn) else t("no findings"))
    bits.append(t("keys by name: {total} (registry {vault}, .env {env})", total=vault + env_keys, vault=vault, env=env_keys))
    bits.append(t("activity {date}", date=last))
    tail = f" · use_secret.py names {name} · projects.html#project:{pid.split(':', 1)[-1]}"
    return " · ".join(bits) + tail + organization_line(p, t)


def organization_line(p: dict, t) -> str:
    """Whose accounts this project uses, and the standing duty to report what is
    created. On its own line, because it is an instruction rather than a status:
    every session in a watched project reads it before its first turn."""
    if not p.get("organization_source"):
        return ""
    import organizations
    org = p.get("organization")
    if p["organization_source"] == "conflict":
        head = t("organization unresolved: {why} — ask the operator before creating accounts",
                 why=p.get("organization_why", ""))
    elif org == "external":
        return "\n" + t("organization: external — not the estate's accounts; create nothing on its behalf")
    else:
        d = organizations.Assigner().destinations(org)
        where = []
        if d.get("ga4Account"):
            where.append(t("GA {account}", account=d["ga4Account"]))
        if d.get("figmaTeam"):
            where.append(t("Figma {team}", team=d["figmaTeam"] + ("/" + d["figmaProject"] if d.get("figmaProject") else "")))
        head = t("organization: {name}", name=d.get("label") or org) + (" → " + ", ".join(where) if where else "")
    n = len([r for r in p.get("resources") or [] if isinstance(r, dict)])
    duty = t("resources recorded: {n}. Anything you create for it — analytics, Firebase/Google Cloud, "
             "servers, cloud or payment accounts, Figma files — is reported with observatory_propose "
             "{\"resources\": [...]} in the same turn", n=n)
    return "\n" + head + " · " + duty


def unknown_line(top: pathlib.Path, remote: str, session_id: str) -> str:
    try:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        with SEEN.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now(), "cwd": str(top), "remote": nwo(remote) or remote or None,
                                 "session_id": session_id or None}, ensure_ascii=False) + "\n")
        SEEN.chmod(0o600)
    except OSError:
        pass
    inside = False
    try:
        top.resolve().relative_to(paths.DATA.resolve())
        inside = True
    except ValueError:
        pass
    t = _translator()
    if inside:
        return "observatory: " + t("folder {name} is not in the registry — the next tick picks it up "
                                   "(if the schedule is on), or now: project-observatory full local", name=top.name)
    return "observatory: " + t("{path} is outside the configured projects folder — the observatory does not scan it; "
                               "recorded in sessions-seen.jsonl. To observe it, move it into the projects folder "
                               "or change sources.projects", path=top)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", help="the session's working directory (default: hook payload on stdin, then $PWD)")
    a = ap.parse_args(argv[1:])
    cwd, session_id = a.cwd, ""
    if not cwd and not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            cwd, session_id = payload.get("cwd") or "", payload.get("session_id") or ""
        except ValueError:
            payload = {}
    cwd = pathlib.Path(cwd or os.getcwd())
    try:
        if not cwd.is_dir():
            return 0
        top = git_top(cwd)
        if top is None:
            return 0                                                                       
        if top.resolve() == ROOT.resolve():
            return 0                                                           
        remote = remote_of(top)
        p = resolve(top, remote)
        print(state_line(p) if p else unknown_line(top, remote, session_id))
    except Exception as exc:                                                                  
        log({"cwd": str(cwd), "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
