#!/usr/bin/env python3
"""Open an issue in a project's own repository for a finding of an allowed class (OBS-557, CP-14, decision D6).

WHAT. `registry/findings.json` is read; each finding of a class the person allowed, whose project resolves to a
GitHub repository this machine's access can write to, becomes one issue there. When the finding closes the issue gets
one comment and is closed. On by default for `dep.vulnerable` and `integration.*` (the switches
`integrations.github_issues` and, per class, `github_issues_dependencies`, `github_issues_integrations` and
`github_issues_keys`, which is off); changed only through the configuration door.

NEVER A VALUE. A body is built from a finding's title, detail, action and evidence NAMES. A text that
`credential_shape` reads as a credential refuses that issue (the reason says so, never what it saw). A key finding
(`integration.key_invalid`, and the `credential.`, `secret.`, `env.` and `ssh.` classes) is never filed into a
repository that is not private, whatever the switches say.

NEVER TWICE. A finding's fingerprint is a hash of its type and subject. It is written into the issue as a hidden
marker (`<!-- observatory-fingerprint:... -->`) and kept in `store/state/issues.json` (fingerprint to repo, number,
state). When the local map is lost, the repository's own labelled issues are read once per run and a marker found
there is adopted instead of filing again. An issue closed by the person or by us, whose finding returns, gets a new
issue: the closed one is history.

SERIAL AND BOUNDED. Writes go one at a time, at least one second apart (GitHub's secondary limits: 80 creating
requests a minute, 500 an hour; research.md, "Filing issues"), at most `CAP` creations or closings a run; the rest
waits for the next run. A 403 or 429 stops the run and records the `retry-after`.

THE CODE HOST is injected. `GhHost` speaks through the `gh` CLI the engine already uses (its token stays in the
keyring), the body on stdin, never in argv. Tests use a fake: nothing here reaches a network by itself.

    issue_filer.py             file and close (a dry run when `gh` is not signed in or the switch is off)
    issue_filer.py --dry-run   plan only; nothing is written anywhere
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA = "issues/1"
LABEL = "observatory"
MARKER = "<!-- observatory-fingerprint:{fp} -->"
MARKER_RE = re.compile(r"<!-- observatory-fingerprint:([0-9a-f]{16}) -->")
#: Creations and closings in one run; the rest waits for the next run.
CAP = 10
#: Seconds between two writes (GitHub asks for at least one).
GAP = 1.0
TITLE_LIMIT = 200
#: Finding classes by group: (prefix or exact type, group). The first match wins.
CLASS_GROUPS = (("dep.vulnerable", "dependencies"), ("integration.", "integrations"),
                ("credential.", "keys"), ("secret.", "keys"), ("env.", "keys"), ("ssh.", "keys"))
#: Types that are about a key, in whatever group: never filed into a repository that is not private.
KEY_TYPES = ("integration.key_invalid", "credential.", "secret.", "env.", "ssh.")
SWITCH = {"dependencies": "github_issues_dependencies", "integrations": "github_issues_integrations",
          "keys": "github_issues_keys"}


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def fingerprint(finding: dict) -> str:
    return hashlib.sha256(f"observatory|{finding.get('type')}|{finding.get('subject')}".encode()).hexdigest()[:16]


def group_of(kind: str) -> str | None:
    for prefix, group in CLASS_GROUPS:
        if kind == prefix or (prefix.endswith(".") and kind.startswith(prefix)):
            return group
    return None


def is_key_finding(kind: str) -> bool:
    return any(kind == p or (p.endswith(".") and kind.startswith(p)) for p in KEY_TYPES)


# ---- the code host -------------------------------------------------------------------------------------

class HostError(RuntimeError):
    """A refusal by the code host: its HTTP status and the `retry-after` it asked for. Never a body."""

    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def gh_runner(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    import subprocess
    proc = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


class GhHost:
    """The `gh` CLI as a code host. The body travels on stdin as JSON (`--input -`), never in argv."""

    def __init__(self, runner=gh_runner):
        self.run = runner

    def _call(self, args: list[str], payload: dict | None = None):
        code, out, err = self.run(args, json.dumps(payload) if payload is not None else None)
        if code != 0:
            status = re.search(r"\bHTTP (\d{3})\b", err or "")
            retry = re.search(r"retry-after:\s*(\d+)", err or "", re.I)
            raise HostError(f"the code host refused ({status.group(1) if status else 'error'})",
                            int(status.group(1)) if status else None, int(retry.group(1)) if retry else None)
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError:
            raise HostError("the code host answered something that is not JSON") from None

    def can_write(self, repo: str) -> tuple[bool, str | None]:
        try:
            doc = self._call(["api", f"repos/{repo}"])
        except HostError as exc:
            return False, str(exc)
        if not (doc.get("permissions") or {}).get("push"):
            return False, "no push access"
        if doc.get("has_issues") is False:
            return False, "issues are disabled on the repository"
        return True, None

    def marked(self, repo: str) -> dict[str, tuple[int, str]]:
        """{fingerprint: (issue number, open|closed)} of the repository's labelled issues, the newest number
        winning: the first hundred, which is far more than a run files."""
        rows = self._call(["api", f"repos/{repo}/issues?labels={LABEL}&state=all&per_page=100&sort=created"])
        out: dict[str, tuple[int, str]] = {}
        for row in rows if isinstance(rows, list) else []:
            m = MARKER_RE.search(str(row.get("body") or ""))
            if m and (m.group(1) not in out or row["number"] > out[m.group(1)][0]):
                out[m.group(1)] = (row["number"], row.get("state") or "open")
        return out

    def create(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        doc = self._call(["api", f"repos/{repo}/issues", "--method", "POST", "--input", "-"],
                         {"title": title, "body": body, "labels": labels})
        return int(doc["number"])

    def comment(self, repo: str, number: int, text: str) -> None:
        self._call(["api", f"repos/{repo}/issues/{number}/comments", "--method", "POST", "--input", "-"],
                   {"body": text})

    def close(self, repo: str, number: int) -> None:
        self._call(["api", f"repos/{repo}/issues/{number}", "--method", "PATCH", "--input", "-"],
                   {"state": "closed", "state_reason": "completed"})


# ---- the map -------------------------------------------------------------------------------------------

def _read(path: pathlib.Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA, "issues": {}}
    return doc if isinstance(doc, dict) and isinstance(doc.get("issues"), dict) else {"schema": SCHEMA, "issues": {}}


def _write(path: pathlib.Path, doc: dict) -> None:
    import atomic
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(path, doc)


def _enabled(name: str, settings: dict | None, offline: bool) -> bool:
    import configuration
    return not offline and configuration.enabled(name, settings=settings, environ={})


# ---- the issue -----------------------------------------------------------------------------------------

def body_of(finding: dict, fp: str, day: str) -> str:
    lines = [f"{finding.get('detail') or finding.get('title')}", ""]
    if finding.get("action"):
        lines += [f"What to do: {finding['action']}", ""]
    names = [str(e) for e in finding.get("evidence") or [] if isinstance(e, str)]
    if names:
        lines += ["Evidence: " + ", ".join(names), ""]
    lines += [f"Finding `{finding.get('type')}` on `{finding.get('subject')}`, first filed {day}.", "",
              "Opened by Project Observatory for the person who owns this repository. It is closed when the finding "
              "closes. To stop these issues, switch off `integrations.github_issues` in Observatory's configuration "
              "(or one class: `github_issues_dependencies`, `github_issues_integrations`, `github_issues_keys`).",
              "", MARKER.format(fp=fp)]
    return "\n".join(lines)


def title_of(finding: dict) -> str:
    return f"[observatory] {finding.get('title') or finding.get('type')}"[:TITLE_LIMIT]


def _credential(text: str) -> bool:
    import credential_shape
    return credential_shape.shaped(text)


# ---- the run -------------------------------------------------------------------------------------------

def run(findings: list[dict], repos: dict[str, dict], host, path: pathlib.Path, *, settings: dict | None = None,
        today: str | None = None, cap: int = CAP, sleep=time.sleep, offline: bool = False) -> dict:
    """File, adopt and close. `repos` maps a project id to its repository row (`name_with_owner`, `visibility`,
    `host`). `host` None is a dry run: the record lists what would be done and nothing is written anywhere."""
    day = today or today_utc()
    doc = _read(path)
    issues: dict[str, dict] = doc["issues"]
    record = {"date": day, "enabled": _enabled("github_issues", settings, offline), "dry_run": host is None,
              "created": 0, "closed": 0, "deferred": 0, "planned": [], "skipped": [], "stopped": None}
    if not record["enabled"]:
        return record
    allowed = {g: _enabled(SWITCH[g], settings, offline) for g in SWITCH}
    current = {fingerprint(f): f for f in findings if isinstance(f, dict) and f.get("type")}
    used = 0
    first_write = True
    can: dict[str, tuple[bool, str | None]] = {}
    marked: dict[str, dict] = {}

    def pause():
        nonlocal first_write
        if not first_write:
            sleep(GAP)
        first_write = False

    def skip(fp, finding, reason, repo=None):
        record["skipped"].append({"fingerprint": fp, "reason": reason})
        if host is not None:
            issues[fp] = {"repo": repo, "number": None, "state": "not-filed", "reason": reason,
                          "type": finding.get("type"), "subject": finding.get("subject"), "seen_on": day}

    try:
        for fp, finding in sorted(current.items(), key=lambda kv: (kv[1].get("type"), str(kv[1].get("subject")))):
            group = group_of(finding["type"])
            if group is None or not allowed.get(group):
                continue
            entry = issues.get(fp)
            if entry and entry.get("state") == "open":
                continue
            project = finding.get("project")
            if not project:
                skip(fp, finding, "no project: the finding is about the estate, not one repository")
                continue
            repo_row = repos.get(project)
            if not repo_row or not repo_row.get("name_with_owner"):
                skip(fp, finding, "no repository found for the project")
                continue
            repo = repo_row["name_with_owner"]
            if repo_row.get("host") not in (None, "github.com", "github"):
                skip(fp, finding, "the repository is not on GitHub", repo)
                continue
            if is_key_finding(finding["type"]) and repo_row.get("visibility") != "private":
                skip(fp, finding, "a key finding is never filed into a public repository", repo)
                continue
            body = body_of(finding, fp, day)
            if _credential(body) or _credential(title_of(finding)):
                skip(fp, finding, "the text looked like it held a credential, so no issue was filed", repo)
                continue
            if host is None:
                record["planned"].append({"fingerprint": fp, "repo": repo, "action": "create",
                                          "type": finding["type"], "subject": finding.get("subject")})
                continue
            if repo not in can:
                can[repo] = host.can_write(repo)
            if not can[repo][0]:
                skip(fp, finding, f"cannot write to the repository: {can[repo][1]}", repo)
                continue
            if repo not in marked:
                marked[repo] = host.marked(repo)
            found = marked[repo].get(fp)
            if found and found[1] == "open":
                issues[fp] = {"repo": repo, "number": found[0], "state": "open", "type": finding["type"],
                              "subject": finding.get("subject"), "filed_on": (entry or {}).get("filed_on") or day}
                continue
            if used >= cap:
                record["deferred"] += 1
                continue
            pause()
            number = host.create(repo, title_of(finding), body, [LABEL])
            used += 1
            record["created"] += 1
            issues[fp] = {"repo": repo, "number": number, "state": "open", "type": finding["type"],
                          "subject": finding.get("subject"), "filed_on": day}
        for fp, entry in sorted(issues.items()):
            if entry.get("state") != "open" or fp in current:
                continue
            if host is None:
                record["planned"].append({"fingerprint": fp, "repo": entry.get("repo"), "action": "close",
                                          "type": entry.get("type"), "subject": entry.get("subject")})
                continue
            if used >= cap:
                record["deferred"] += 1
                continue
            pause()
            host.comment(entry["repo"], entry["number"],
                         f"Project Observatory no longer reports this finding as of {day}. Closing.")
            pause()
            host.close(entry["repo"], entry["number"])
            used += 1
            record["closed"] += 1
            issues[fp] = {**entry, "state": "closed", "closed_on": day}
    except HostError as exc:
        if exc.status in (403, 429):
            record["stopped"] = {"reason": "rate-limited", "status": exc.status, "retry_after": exc.retry_after}
        else:
            record["stopped"] = {"reason": "host-error", "status": exc.status, "retry_after": None}
    if host is not None:
        doc["schema"] = SCHEMA
        doc["last_run"] = record
        _write(path, doc)
    return record


# ---- the banner ----------------------------------------------------------------------------------------

def banner(path: pathlib.Path, settings: dict | None = None, offline: bool = False) -> dict:
    """Whether Home and the Integrations page tell the person, once, that issues are filed and how to turn it off."""
    on = _enabled("github_issues", settings, offline)
    dismissed = (_read(path).get("banner") or {}).get("dismissed_on")
    return {"show": bool(on and not dismissed), "enabled": on, "dismissed_on": dismissed}


def dismiss_banner(path: pathlib.Path, today: str | None = None) -> dict:
    doc = _read(path)
    doc.setdefault("schema", SCHEMA)
    doc["banner"] = {"dismissed_on": (doc.get("banner") or {}).get("dismissed_on") or today or today_utc()}
    _write(path, doc)
    return {"dismissed_on": doc["banner"]["dismissed_on"]}


# ---- the agent's view ----------------------------------------------------------------------------------

def default_path() -> pathlib.Path:
    import paths
    return paths.STATE / "state" / "issues.json"


def agent_view(path: pathlib.Path | None = None, state: str | None = None, settings: dict | None = None) -> dict:
    path = path or default_path()
    doc = _read(path)
    degraded: list[dict] = []
    last = doc.get("last_run")
    if not last:
        degraded.append({"source": "issues", "reason": "the issue filer has not run"})
    elif last.get("stopped"):
        degraded.append({"source": "issues", "reason": f"the last run stopped: {last['stopped'].get('reason')}"})
    elif last.get("enabled") is False:
        degraded.append({"source": "issues", "reason": "the issue filer is switched off (integrations.github_issues)"})
    rows = [{"fingerprint": fp, **{k: e.get(k) for k in ("repo", "number", "state", "type", "subject", "filed_on",
                                                         "closed_on", "reason")}}
            for fp, e in sorted(doc["issues"].items())]
    if state:
        rows = [r for r in rows if r["state"] == state]
    return {"issues": rows, "lastRun": last, "banner": banner(path, settings), "degraded": degraded}


def repos_by_project(registry_dir: pathlib.Path) -> dict[str, dict]:
    """project id -> its repository row, through the `implemented_by` relations."""
    def read(name):
        try:
            return json.loads((registry_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    repos = {r.get("id"): r for r in read("repositories.json").get("repositories") or []}
    out: dict[str, dict] = {}
    for rel in read("relations.json").get("relations") or []:
        if rel.get("type") == "implemented_by" and rel.get("to") in repos and rel.get("from") not in out:
            row = repos[rel["to"]]
            out[rel["from"]] = {"name_with_owner": row.get("name_with_owner"), "visibility": row.get("visibility"),
                                "host": row.get("host")}
    return out


def main(argv: list[str], host=None, runner=gh_runner) -> int:
    import configuration
    import paths
    path = default_path()
    try:
        findings = json.loads((paths.REGISTRY / "findings.json").read_text(encoding="utf-8")).get("findings") or []
    except (OSError, ValueError):
        print("issues: no findings yet; nothing to file")
        return 0
    offline = configuration.offline()
    dry = "--dry-run" in argv
    if host is None and not dry:
        host = GhHost(runner)
        code, _out, _err = runner(["auth", "status"], None)
        if code != 0:
            host = None  # not signed in: a dry run says what it would do
    record = run(findings, repos_by_project(paths.REGISTRY), None if dry else host, path, offline=offline)
    print(f"issues: {record['created']} filed, {record['closed']} closed, {record['deferred']} deferred, "
          f"{len(record['skipped'])} not filed" + ("; dry run" if record["dry_run"] else "")
          + (f"; stopped: {record['stopped']['reason']}" if record.get("stopped") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
