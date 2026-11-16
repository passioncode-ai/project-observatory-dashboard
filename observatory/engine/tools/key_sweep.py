#!/usr/bin/env python3
"""A key sweep over the estate's repositories: working trees and Git history (OBS-554, CP-08, decision D3).

    key_sweep.py                       one pass within today's budget (what the maintenance job runs)
    key_sweep.py --full                forget every repository's place in its history and start again
    key_sweep.py --settle FP --why T   close a sighting of an unknown value that is gone from the tree

WHAT IT LOOKS FOR. Two scanners, each optional on its own terms:

  known-value   the values this machine holds (the vault's slots, the env inventory's secrets, the
                installed OpenRouter keys: `scan_leaks.known_values`), compared in memory with the text
                of every file Git would commit (tracked, and untracked but not ignored) and with every
                line a commit added. A match is evidence: it is OUR value, not something shaped like one.
  external      a gitleaks-compatible scanner (gitleaks or Betterleaks) when one is installed, always run
                with `--redact`, its report written to an owner-only temporary folder and deleted right
                after its rule, file, line and commit are copied. Its `Secret`, `Match` and `Line` fields
                are never kept, not even in memory past the parse.

WHAT IT KEEPS (store/raw/key-sweep.json, `key-sweep/1`). Per sighting: the rule, the repository, the
path, the line, the commit for a history place, and a keyed digest: HMAC-SHA256 under the workspace's
fingerprint salt (`scan_env.salt`), never the value and never a plain hash of it, so the same value seen
twice is one sighting and a precomputed table cannot reverse it. For an external hit the digest is over
the matched span, re-read from the file in this process (or over the known value it contains, which then
names its slot). The value, the matched text and the line never reach a record, a log, a finding or a page.

RESUMABLE, ON A DAILY BUDGET. A pass spends at most PASS_BUDGET_S and the day at most DAILY_BUDGET_S
(`key-sweep-state.json` in the store folder: the day, what is left, and per repository the last commit
of its history read, the digest of the values it was read against, and the day its tree was read). A
history is read oldest commit first and resumes after the last one finished; a tree is read once a day,
whole or not at all. A history read against fewer values than the machine holds now is read again.

WHEN A SIGHTING CLOSES. A value of a known slot: when it is gone from the working tree AND the slot holds
another value (rotated; its metadata's `rotated` time is shown when recorded). A value of no known slot:
when it is gone from the working tree AND a person settled it (`--settle`, kept in
`key-sweep-settled.json`); settling is refused while the value is still in the tree or its slot still
holds it. A value only in history stays open until then: removing it from the tree does not unpublish it.

NEVER DONE HERE: provider verification of a found key (it would send the value), writing the leak
register (`vault.py leak` stays a person's or an agent's confirmed act), and rewriting history.
"""
from __future__ import annotations

import argparse
import codecs
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "collectors", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCHEMA = "key-sweep/1"
#: The rule a known-value match carries.
KNOWN = "known-value"
#: Seconds one maintenance pass may spend, and seconds a UTC day may spend over all passes.
PASS_BUDGET_S = 120.0
DAILY_BUDGET_S = 900.0
#: A file larger than this is not read (named in the repository's `degraded`).
MAX_FILE_BYTES = 2 << 20
#: A commit whose added text is larger than this is not read (named in `degraded`).
MAX_DIFF_BYTES = 16 << 20
#: Values shorter than this prove nothing by matching (scan_leaks.MIN_LEN).
MIN_LEN = 12
#: Places kept per sighting: a value pasted into many commits stays one sighting with its first places.
MAX_PLACES = 50
#: Scanner binaries tried in order (Betterleaks is command-compatible).
SCANNERS = ("gitleaks", "betterleaks")
#: The fields of an external report that are copied; every other field is dropped at the parse.
KEPT_FIELDS = ("RuleID", "File", "StartLine", "EndLine", "StartColumn", "EndColumn", "Commit")
FINGERPRINT = re.compile(r"hmac-sha256:[0-9a-f]{64}")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fingerprint(key: bytes, text: str) -> str:
    """The keyed digest of a value or a matched span: never the value, never its plain hash."""
    message = b"key-sweep/1\x00" + text.encode("utf-8", "surrogateescape")
    return "hmac-sha256:" + hmac.new(key, message, hashlib.sha256).hexdigest()


def values_digest(key: bytes, values: dict[str, str]) -> str:
    """One digest of the set of values a history was read against: a new value means read it again."""
    fps = sorted(fingerprint(key, v) for v in values)
    return "hmac-sha256:" + hmac.new(key, "\n".join(fps).encode(), hashlib.sha256).hexdigest()


class Budget:
    """Seconds left for this pass: the pass's own share or what the day has left, whichever is less."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.clock = clock
        self.start = clock()
        self.seconds = max(0.0, seconds)

    def spent(self) -> float:
        return max(0.0, self.clock() - self.start)

    def left(self) -> float:
        return self.seconds - self.spent()

    def out(self) -> bool:
        return self.left() <= 0


# ---- reading ---------------------------------------------------------------------------------------

def _git(args: list[str], repo: pathlib.Path, timeout: float = 60) -> subprocess.CompletedProcess:
    import safe_git
    return safe_git.run(args, repo=repo, text=False, timeout=max(1.0, timeout))


def tree_files(repo: pathlib.Path) -> list[str] | None:
    """What Git would commit: tracked files and untracked ones not ignored. None when Git cannot say."""
    p = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], repo)
    if p.returncode != 0:
        return None
    return sorted({x.decode("utf-8", "surrogateescape") for x in p.stdout.split(b"\0") if x})


def read_file(repo: pathlib.Path, rel: str) -> tuple[str | None, str | None]:
    """(text, why not) of one regular file inside the repository; a link, a large or a binary file is not read."""
    path = repo / rel
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None              # listed as tracked but deleted in the tree: nothing there
    except OSError as exc:
        return None, f"could not be examined ({type(exc).__name__})"
    if not stat.S_ISREG(info.st_mode):
        return None, None              # a link or a submodule: its target is not this repository's text
    if info.st_size > MAX_FILE_BYTES:
        return None, f"larger than {MAX_FILE_BYTES >> 20} MiB; not read"
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return None, f"could not be read ({type(exc).__name__})"
    if b"\0" in raw[:8192]:
        return None, None              # binary
    return raw.decode("utf-8", "surrogateescape"), None


def commits(repo: pathlib.Path, through: str | None) -> tuple[list[str], str | None]:
    """(commits to read, oldest first, after `through`; why the history is read from its start, or None)."""
    head = _git(["rev-parse", "--verify", "-q", "HEAD"], repo)
    if head.returncode != 0:
        return [], None                # no commit yet
    restart = None
    if through:
        anc = _git(["merge-base", "--is-ancestor", through, "HEAD"], repo)
        if anc.returncode == 0:
            p = _git(["rev-list", "--reverse", "--topo-order", f"{through}..HEAD"], repo)
            if p.returncode == 0:
                return p.stdout.decode().split(), None
        restart = "the commit last read is no longer in the history (rewritten or reset); read from the start"
    p = _git(["rev-list", "--reverse", "--topo-order", "HEAD"], repo)
    return (p.stdout.decode().split() if p.returncode == 0 else []), restart


def _diff_path(raw: bytes) -> str | None:
    """The new path of a `+++ ` line: `b/<path>`, C-quoted when it holds unusual bytes, or /dev/null."""
    text = raw.decode("utf-8", "surrogateescape").rstrip("\r")
    if text == "/dev/null":
        return None
    if text.startswith('"') and text.endswith('"'):
        body = codecs.escape_decode(text[1:-1].encode("utf-8", "surrogateescape"))[0]
        text = body.decode("utf-8", "surrogateescape")
    return text[2:] if text.startswith("b/") else text


HUNK = re.compile(rb"^@@ -\S+ \+(\d+)(?:,\d+)? @@")


def added_lines(diff: bytes):
    """(path, line number in the new file, added text) for every line a commit's patch adds."""
    path, line = None, 0
    for raw in diff.split(b"\n"):
        if raw.startswith(b"+++ "):
            path = _diff_path(raw[4:])
            continue
        if raw.startswith(b"@@"):
            m = HUNK.match(raw)
            line = int(m.group(1)) if m else 0
            continue
        if raw.startswith(b"+") and path is not None:
            yield path, line, raw[1:].decode("utf-8", "surrogateescape")
            line += 1


def commit_patch(repo: pathlib.Path, sha: str, timeout: float) -> bytes | None:
    p = _git(["diff-tree", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "-p", "-U0",
              "--root", "-r", sha], repo, timeout=timeout)
    return p.stdout if p.returncode == 0 else None


class Matcher:
    """The known values as one alternation, longest first; a match answers the value and its label."""

    def __init__(self, values: dict[str, str]):
        self.values = {v: label for v, label in values.items() if isinstance(v, str) and len(v) >= MIN_LEN}
        alts = sorted(self.values, key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(v) for v in alts)) if alts else None

    def lines(self, text: str):
        """(line number, value) for each occurrence in a text."""
        if self.pattern is None:
            return
        line, at = 1, 0
        for m in self.pattern.finditer(text):
            line += text.count("\n", at, m.start())
            at = m.start()
            yield line, m.group(0)

    def within(self, text: str) -> str | None:
        if self.pattern is None:
            return None
        m = self.pattern.search(text)
        return m.group(0) if m else None


def window_lines(text: str, targets: set[tuple[str, int]], key: bytes):
    """(line, fingerprint) of every span of `text` whose keyed digest is a target's: a value this machine no
    longer holds (a rotated slot, an external match) is found again by its digest and length alone."""
    wanted: dict[int, set[str]] = {}
    for fp, length in targets:
        if isinstance(length, int) and length >= MIN_LEN:
            wanted.setdefault(length, set()).add(fp)
    for length, fps in wanted.items():
        line, at = 1, 0
        for i in range(0, len(text) - length + 1):
            if fingerprint(key, text[i:i + length]) in fps:
                line += text.count("\n", at, i)
                at = i
                yield line, fingerprint(key, text[i:i + length])


# ---- the external scanner -------------------------------------------------------------------------

def find_scanner(which=shutil.which) -> str | None:
    for name in SCANNERS:
        found = which(name)
        if found:
            return found
    return None


def run_external(scanner: str, mode: str, repo: pathlib.Path, *, log_opts: str | None, timeout: float,
                 runner=subprocess.run) -> tuple[list[dict], str | None]:
    """Run the scanner with redaction into an owner-only temporary report, copy KEPT_FIELDS, delete it.

    Answers (rows, problem). The report is deleted in every case, a failed or stopped run included; the
    scanner's own output is discarded unread, so nothing it printed reaches a record."""
    folder = tempfile.mkdtemp(prefix="observatory-key-sweep-")  # paths-check: allow - owner-only report folder, removed in the finally below on every path
    report = os.path.join(folder, "report.json")
    try:
        fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        argv = [scanner, mode, str(repo), "-f", "json", "-r", report, "--redact", "--no-banner", "--exit-code", "0"]
        if mode == "git" and log_opts:
            argv += ["--log-opts", log_opts]
        try:
            p = runner(argv, cwd=str(repo), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            return [], f"the external scanner ({mode}) ran out of the pass's time"
        except OSError as exc:
            return [], f"the external scanner could not start ({type(exc).__name__})"
        if getattr(p, "returncode", 0) not in (0, None):
            return [], f"the external scanner ({mode}) exited {p.returncode}"
        try:
            with open(report, "rb") as fh:
                doc = json.loads(fh.read().decode("utf-8", "replace") or "[]")
        except (OSError, ValueError):
            return [], f"the external scanner's report ({mode}) could not be read"
        rows = []
        for item in doc if isinstance(doc, list) else []:
            if isinstance(item, dict):
                rows.append({k: item.get(k) for k in KEPT_FIELDS})
        del doc
        return rows, None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _relative(repo: pathlib.Path, file: str) -> str:
    f = str(file or "")
    for base in (str(repo), str(repo.resolve())):
        if f.startswith(base + os.sep):
            return f[len(base) + 1:]
    return f[2:] if f.startswith("./") else f


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def external_span(text: str | None, row: dict) -> str | None:
    """The span a report row names, re-read from the file's text; columns are 1-based and inclusive."""
    start, end = _int(row.get("StartLine")), _int(row.get("EndLine")) or _int(row.get("StartLine"))
    if text is None or start is None or end is None or end < start:
        return None
    lines = text.split("\n")
    if end > len(lines):
        return None
    chunk = lines[start - 1:end]
    first, last = _int(row.get("StartColumn")), _int(row.get("EndColumn"))
    if first and last:
        if len(chunk) == 1:
            chunk = [chunk[0][first - 1:last]]
        else:
            chunk = [chunk[0][first - 1:]] + chunk[1:-1] + [chunk[-1][:last]]
    span = "\n".join(chunk)
    return span or None


# ---- the sweep ------------------------------------------------------------------------------------

class Sweep:
    def __init__(self, key: bytes, values: dict[str, str], previous: dict | None, state: dict | None,
                 settled: dict | None, *, today: str, at: str, budget: Budget, scanner: str | None,
                 runner=subprocess.run, full: bool = False, rotated_at=None):
        self.key = key
        self.match = Matcher(values)
        self.labels = {label: v for v, label in self.match.values.items()}
        self.today, self.at, self.budget = today, at, budget
        self.scanner, self.runner = scanner, runner
        self.rotated_at = rotated_at or (lambda label: None)
        self.settled = settled if isinstance(settled, dict) else {}
        self.values_digest = values_digest(key, self.match.values)
        state = state if isinstance(state, dict) else {}
        self.state = {"schema": SCHEMA + "-state", "repos": dict(state.get("repos") or {})}
        if full:
            self.state["repos"] = {}
        self.sightings: dict[str, dict] = {}
        for s in (previous or {}).get("sightings") or []:
            if isinstance(s, dict) and FINGERPRINT.fullmatch(str(s.get("fingerprint") or "")):
                self.sightings[s["fingerprint"]] = {**s, "rules": list(s.get("rules") or []),
                                                    "places": list(s.get("places") or [])}
        self.reports: dict[str, dict] = {}
        self.degraded: list[dict] = []

    # A place is (where, repo, path, line, commit); a sighting holds at most MAX_PLACES of them.
    def note(self, fp: str, rule: str, place: dict, *, slot: str | None, length: int | None, digest_of: str):
        s = self.sightings.setdefault(fp, {"fingerprint": fp, "rules": [], "places": [], "slot": slot,
                                           "digest_of": digest_of, "first_seen": self.at})
        if rule not in s["rules"]:
            s["rules"].append(rule)
        if slot and not s.get("slot"):
            s["slot"] = slot
        if length and not s.get("length"):
            s["length"] = length
        if digest_of == "value" and s.get("digest_of") != "value":
            s["digest_of"] = "value"
        s["last_seen"] = self.at
        key = (place.get("where"), place.get("repo"), place.get("path"), place.get("line"), place.get("commit"))
        if key not in {(p.get("where"), p.get("repo"), p.get("path"), p.get("line"), p.get("commit"))
                       for p in s["places"]} and len(s["places"]) < MAX_PLACES:
            s["places"].append(place)

    def report(self, repo: dict) -> dict:
        return self.reports.setdefault(repo["repo"], {"repo": repo["repo"], "project": repo.get("project"),
                                                      "degraded": []})

    def run(self, repos: list[dict]) -> None:
        repo_state = self.state["repos"]
        live = {r["repo"] for r in repos}
        for gone in [k for k in repo_state if k not in live]:
            repo_state.pop(gone)
        # Trees first (each once a day), then histories, each in the order of what waited longest.
        order = sorted(repos, key=lambda r: (not (repo_state.get(r["repo"]) or {}).get("tree_cursor"),
                                             (repo_state.get(r["repo"]) or {}).get("tree_on") or "", r["repo"]))
        for repo in order:
            if self.budget.out():
                break
            rs = repo_state.get(repo["repo"]) or {}
            if rs.get("tree_on") != self.today or rs.get("tree_cursor"):
                self.tree(repo)
        for repo in sorted(repos, key=lambda r: r["repo"]):
            if self.budget.out():
                break
            self.history(repo)
        for repo in repos:
            rs = repo_state.setdefault(repo["repo"], {})
            rep = self.report(repo)
            rep.update({"history_through": rs.get("history_through"), "history_complete": bool(rs.get("history_done")),
                        "tree_scanned_on": rs.get("tree_on"), "commits_read": rs.get("commits_read", 0),
                        "external_through": rs.get("external_through")})

    # ---- the working tree ----
    def tree(self, repo: dict) -> None:
        rep, path = self.report(repo), repo["path"]
        files = tree_files(path)
        if files is None:
            rep["degraded"].append({"code": "not-a-repository", "reason": "Git could not list its files"})
            return
        listed = set(files)
        targets: dict[str, set[tuple[str, int]]] = {}
        for s in self.sightings.values():
            if s.get("length") and s.get("digest_of") in ("value", "match"):
                for p in s["places"]:
                    if p.get("where") == "tree" and p.get("repo") == repo["repo"]:
                        targets.setdefault(p.get("path"), set()).add((s["fingerprint"], s["length"]))
        rs = self.state["repos"].setdefault(repo["repo"], {})
        # A tree larger than one pass is read over several: the files after `tree_cursor` are left, and what the
        # files before it held waits in `tree_found` (digests and places only) until the read is whole.
        cursor = rs.get("tree_cursor") if isinstance(rs.get("tree_cursor"), str) else None
        found: list = [tuple(x) for x in rs.get("tree_found") or [] if isinstance(x, list) and len(x) == 6]
        unread = int(rs.get("tree_unread") or 0)
        for rel in files:
            if cursor is not None and rel <= cursor:
                continue
            if self.budget.out():
                rs.update(tree_cursor=cursor, tree_found=[list(x) for x in found], tree_unread=unread)
                rep["degraded"].append({"code": "out-of-time", "reason": "the tree was not read to its end; the next "
                                        "pass resumes it, and its sightings keep the previous whole read until then"})
                return
            cursor = rel
            text, why = read_file(path, rel)
            if why:
                unread += 1
                continue
            if text is None:
                continue
            for line, value in self.match.lines(text):
                found.append((fingerprint(self.key, value), KNOWN,
                              {"where": "tree", "repo": repo["repo"], "path": rel, "line": line},
                              self.match.values[value], len(value), "value"))
            for line, fp in window_lines(text, targets.get(rel, set()), self.key):
                s = self.sightings.get(fp) or {}
                for rule in s.get("rules") or [KNOWN]:
                    found.append((fp, rule, {"where": "tree", "repo": repo["repo"], "path": rel, "line": line},
                                  s.get("slot"), s.get("length"), s.get("digest_of") or "value"))
        if self.scanner:
            rows, problem = run_external(self.scanner, "dir", path, log_opts=None, timeout=self.budget.left(),
                                         runner=self.runner)
            if problem:
                rep["degraded"].append({"code": "external-scanner", "reason": problem})
            for row in rows:
                rel = _relative(path, row.get("File"))
                if rel not in listed:
                    continue            # an ignored file (`dir` reads them): not part of the repository
                text, _why = read_file(path, rel)
                found.append(self._external(row, rel, text, {"where": "tree", "repo": repo["repo"], "path": rel,
                                                             "line": _int(row.get("StartLine"))}))
        # The read is whole: this repository's tree places are replaced by what it found now.
        for s in self.sightings.values():
            s["places"] = [p for p in s["places"] if not (p.get("where") == "tree" and p.get("repo") == repo["repo"])]
        for fp, rule, place, slot, length, digest_of in found:
            self.note(fp, rule, place, slot=slot, length=length, digest_of=digest_of)
        if unread:
            rep["degraded"].append({"code": "files-unread", "reason": f"{unread} file(s) too large or unreadable; "
                                    "not read"})
        for k in ("tree_cursor", "tree_found", "tree_unread"):
            rs.pop(k, None)
        rs["tree_on"] = self.today

    def _external(self, row: dict, rel: str, text: str | None, place: dict):
        rule = str(row.get("RuleID") or "external")[:80]
        span = external_span(text, row)
        value = self.match.within(span) if span else None
        if value:
            return fingerprint(self.key, value), rule, place, self.match.values[value], len(value), "value"
        if span:
            return fingerprint(self.key, span), rule, place, None, len(span), "match"
        where = "\x00".join(str(x) for x in (rule, place.get("repo"), place.get("path"), place.get("line"),
                                             place.get("commit")))
        return fingerprint(self.key, "place\x00" + where), rule, place, None, None, "place"

    # ---- the history ----
    def history(self, repo: dict) -> None:
        rep, path = self.report(repo), repo["path"]
        rs = self.state["repos"].setdefault(repo["repo"], {})
        through = rs.get("history_through")
        if rs.get("history_values") != self.values_digest:
            if through:
                rep["degraded"].append({"code": "values-changed", "reason": "the values this machine holds changed "
                                        "since the history was read; it is read again from the start"})
            through = None
            rs.update(history_through=None, history_values=self.values_digest, history_done=False, commits_read=0)
        todo, restart = commits(path, through)
        if restart:
            rep["degraded"].append({"code": "history-rewritten", "reason": restart})
            rs.update(history_through=None, commits_read=0)
        rs["history_done"] = not todo
        if self.match.pattern is not None:
            for sha in todo:
                if self.budget.out():
                    rep["degraded"].append({"code": "out-of-time", "reason": "the history was not read to its end; "
                                            "the next pass resumes after the last commit read"})
                    break
                patch = commit_patch(path, sha, self.budget.left())
                if patch is None:
                    rep["degraded"].append({"code": "commit-unread", "reason": f"commit {sha[:12]} could not be read"})
                elif len(patch) > MAX_DIFF_BYTES:
                    rep["degraded"].append({"code": "commit-too-large",
                                            "reason": f"commit {sha[:12]} adds more than {MAX_DIFF_BYTES >> 20} MiB; "
                                                      "not read"})
                else:
                    for rel, line, text in added_lines(patch):
                        for _n, value in self.match.lines(text):
                            self.note(fingerprint(self.key, value), KNOWN,
                                      {"where": "history", "repo": repo["repo"], "path": rel, "line": line,
                                       "commit": sha},
                                      slot=self.match.values[value], length=len(value), digest_of="value")
                rs["history_through"] = sha
                rs["commits_read"] = int(rs.get("commits_read") or 0) + 1
            else:
                rs["history_done"] = True
        else:
            rs["history_done"] = True
        if self.scanner and not self.budget.out():
            self._external_history(repo, rs, rep)

    def _external_history(self, repo: dict, rs: dict, rep: dict) -> None:
        path = repo["path"]
        head = _git(["rev-parse", "--verify", "-q", "HEAD"], path)
        if head.returncode != 0:
            return
        head_sha = head.stdout.decode().strip()
        since = rs.get("external_through")
        if since == head_sha:
            return
        if since and _git(["merge-base", "--is-ancestor", since, "HEAD"], path).returncode != 0:
            since = None
        rows, problem = run_external(self.scanner, "git", path, log_opts=(f"{since}..{head_sha}" if since else None),
                                     timeout=self.budget.left(), runner=self.runner)
        if problem:
            rep["degraded"].append({"code": "external-scanner", "reason": problem})
            return
        for row in rows:
            sha = str(row.get("Commit") or "")
            rel = _relative(path, row.get("File"))
            text = None
            if re.fullmatch(r"[0-9a-f]{7,64}", sha) and rel:
                p = _git(["cat-file", "-p", f"{sha}:{rel}"], path, timeout=30)
                if p.returncode == 0 and b"\0" not in p.stdout[:8192] and len(p.stdout) <= MAX_FILE_BYTES:
                    text = p.stdout.decode("utf-8", "surrogateescape")
            fp, rule, place, slot, length, digest_of = self._external(
                row, rel, text, {"where": "history", "repo": repo["repo"], "path": rel,
                                 "line": _int(row.get("StartLine")), "commit": sha or None})
            self.note(fp, rule, place, slot=slot, length=length, digest_of=digest_of)
        rs["external_through"] = head_sha

    # ---- the answer ----
    def slot_state(self, s: dict) -> str | None:
        label = s.get("slot")
        if not label:
            return None
        current = self.labels.get(label)
        if current is None:
            return "gone"
        return "current" if fingerprint(self.key, current) == s["fingerprint"] else "rotated"

    def document(self, repos: list[dict], *, scanner_name: str | None, enabled: bool = True) -> dict:
        sightings = []
        for s in sorted(self.sightings.values(), key=lambda x: x["fingerprint"]):
            if not s["places"]:
                continue
            in_tree = any(p.get("where") == "tree" for p in s["places"])
            state = self.slot_state(s)
            settled = self.settled.get(s["fingerprint"]) if isinstance(self.settled.get(s["fingerprint"]), dict) else None
            closed = None
            if not in_tree and state == "rotated":
                closed = "rotated"
            elif not in_tree and state != "current" and settled:
                closed = "settled"
            row = {k: s.get(k) for k in ("fingerprint", "rules", "slot", "digest_of", "length", "places",
                                         "first_seen", "last_seen")}
            row.update(slot_state=state, in_tree=in_tree, status="closed" if closed else "open", closed_by=closed)
            if state == "rotated":
                row["rotated_at"] = self.rotated_at(s["slot"])
            if settled:
                row["settled"] = {k: settled.get(k) for k in ("at", "by", "why")}
            sightings.append(row)
        by_repo: dict[str, dict[str, int]] = {}
        for s in sightings:
            if s["status"] != "open":
                continue
            for repo in {p.get("repo") for p in s["places"]}:
                for rule in s["rules"]:
                    by_repo.setdefault(repo, {})[rule] = by_repo.setdefault(repo, {}).get(rule, 0) + 1
        out_repos = []
        for repo in sorted(repos, key=lambda r: r["repo"]):
            rep = dict(self.report(repo))
            rep["sightings_by_rule"] = by_repo.get(repo["repo"], {})
            rep["open"] = sum(1 for s in sightings
                              if s["status"] == "open" and any(p.get("repo") == repo["repo"] for p in s["places"]))
            out_repos.append(rep)
        complete = all(r.get("history_complete") and r.get("tree_scanned_on") == self.today for r in out_repos)
        return {"schema": SCHEMA, "at": self.at, "enabled": enabled, "complete": complete,
                "scanners": {"known_value": bool(self.match.pattern), "known_values": len(self.match.values),
                             "external": pathlib.Path(scanner_name).name if scanner_name else None},
                "repositories": out_repos, "sightings": sightings, "degraded": self.degraded}


def repositories(projects: list[dict], data_root: pathlib.Path) -> list[dict]:
    """Each project's local folders that are Git working trees, once each by real path."""
    out, seen = [], set()
    for p in projects:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str):
            continue
        for folder in p.get("local_folders") or []:
            if not isinstance(folder, str) or not folder or folder.startswith("/") or ".." in pathlib.PurePosixPath(folder).parts:
                continue
            path = data_root / folder
            if not (path / ".git").exists():
                continue
            real = str(path.resolve())
            if real in seen:
                continue
            seen.add(real)
            out.append({"repo": folder, "project": p["id"], "path": path})
    return out


def run_pass(repos: list[dict], values: dict[str, str], key: bytes, *, previous: dict | None, state: dict | None,
             settled: dict | None, today: str | None = None, at: str | None = None, clock=time.monotonic,
             pass_budget: float = PASS_BUDGET_S, daily_budget: float = DAILY_BUDGET_S, scanner: str | None = None,
             runner=subprocess.run, full: bool = False, rotated_at=None, notes: list[dict] | None = None
             ) -> tuple[dict, dict]:
    """One pass: (the document to write, the state to write). Pure apart from reading the repositories."""
    at = at or now_iso()
    today = today or at[:10]
    state = dict(state) if isinstance(state, dict) else {}
    left = float(state.get("budget_left_s", daily_budget)) if state.get("day") == today else float(daily_budget)
    budget = Budget(min(pass_budget, left), clock)
    sweep = Sweep(key, values, previous, state, settled, today=today, at=at, budget=budget, scanner=scanner,
                  runner=runner, full=full, rotated_at=rotated_at)
    sweep.degraded.extend(notes or [])
    if left <= 0:
        sweep.degraded.append({"code": "budget-spent", "reason": f"today's {int(daily_budget)} s are spent; "
                               "the sweep resumes tomorrow"})
    else:
        sweep.run(repos)
    if not sweep.match.pattern:
        sweep.degraded.append({"code": "no-values", "reason": "no value is held on this machine, so the "
                               "known-value scan had nothing to look for"})
    doc = sweep.document(repos, scanner_name=scanner)
    doc["budget"] = {"day": today, "daily_s": daily_budget, "pass_s": pass_budget,
                     "spent_s": round(budget.spent(), 1), "left_s": round(max(0.0, left - budget.spent()), 1)}
    new_state = {**sweep.state, "day": today, "budget_left_s": max(0.0, left - budget.spent())}
    return doc, new_state


def settle(doc: dict | None, settled: dict | None, fp: str, why: str, *, by: str, at: str | None = None) -> dict:
    """The settled map with `fp` added, or a refusal: {"error": {"code", "message"}}."""
    if not FINGERPRINT.fullmatch(fp or ""):
        return {"error": {"code": "invalid-fingerprint", "message": "a fingerprint is hmac-sha256: and 64 hex"}}
    why = " ".join(str(why or "").split())
    if not why or len(why) > 300:
        return {"error": {"code": "invalid-reason", "message": "say in at most 300 characters how it was dealt with"}}
    row = next((s for s in (doc or {}).get("sightings") or [] if s.get("fingerprint") == fp), None)
    if row is None:
        return {"error": {"code": "unknown-fingerprint", "message": "no sighting of the last sweep has this fingerprint"}}
    if row.get("in_tree"):
        return {"error": {"code": "still-in-tree", "message": "the value is still in a working tree; remove it, "
                          "commit, and let the next sweep read the tree"}}
    if row.get("slot_state") == "current":
        return {"error": {"code": "slot-holds-it", "message": f"{row.get('slot')} still holds this value; rotate it, "
                          "and the sighting closes by itself"}}
    out = dict(settled or {})
    out[fp] = {"at": at or now_iso(), "by": by, "why": why}
    return {"settled": out}


def agent_view(doc: dict | None, *, repository: str | None = None) -> dict:
    """What `observatory_key_sweep` answers: per repository what was read and the open sightings by rule."""
    if not isinstance(doc, dict):
        return {"repositories": [], "sightings": [], "enabled": None,
                "degraded": [{"source": "key-sweep", "reason": "no sweep has run yet"}]}
    if doc.get("enabled") is False:
        return {"repositories": [], "sightings": [], "enabled": False,
                "degraded": [{"source": "key-sweep", "reason": "switched off (integrations.key_sweep)"}]}
    repos = [r for r in doc.get("repositories") or [] if repository in (None, r.get("repo"))]
    if repository is not None and not repos:
        return {"error": {"code": "unknown-repository", "message": "no swept repository has this name"}}
    names = {r.get("repo") for r in repos}
    rows = [{k: s.get(k) for k in ("fingerprint", "rules", "slot", "slot_state", "in_tree", "status", "closed_by",
                                    "places", "first_seen", "last_seen")}
            for s in doc.get("sightings") or [] if any(p.get("repo") in names for p in s.get("places") or [])]
    degraded = [{"source": "key-sweep", **d} for d in doc.get("degraded") or []]
    for r in repos:
        degraded += [{"source": f"key-sweep:{r.get('repo')}", **d} for d in r.get("degraded") or []]
    return {"at": doc.get("at"), "enabled": True, "complete": doc.get("complete"), "scanners": doc.get("scanners"),
            "budget": doc.get("budget"),
            "repositories": [{k: r.get(k) for k in ("repo", "project", "history_through", "history_complete",
                                                    "tree_scanned_on", "commits_read", "sightings_by_rule", "open")}
                             for r in repos],
            "sightings": rows, "degraded": degraded}


# ---- files ----------------------------------------------------------------------------------------

def _read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def files():
    import paths
    return {"out": paths.SCRATCH / "key-sweep.json", "state": paths.STATE / "key-sweep-state.json",
            "settled": paths.STATE / "key-sweep-settled.json"}


def _rotated_at(label: str) -> str | None:
    """`rotated` from a vault slot's metadata (`vault.py rotate` writes it), or None."""
    import scan_leaks
    if not label.startswith("vault:"):
        return None
    parts = label[len("vault:"):].split("/")
    if len(parts) != 3:
        return None
    meta = _read_json(scan_leaks.VAULT / parts[0] / parts[1] / (parts[2] + ".meta.json"))
    return meta.get("rotated") if isinstance(meta, dict) and isinstance(meta.get("rotated"), str) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="key-sweep", description=__doc__.split("\n\n")[0])
    ap.add_argument("--full", action="store_true", help="forget every history's place and read again")
    ap.add_argument("--settle", metavar="FINGERPRINT", help="close a sighting of an unknown value gone from the tree")
    ap.add_argument("--why", metavar="TEXT", help="--settle: how it was dealt with")
    ap.add_argument("--json", action="store_true", help="print the pass's summary as one JSON line")
    ap.add_argument("--budget", type=float, default=PASS_BUDGET_S, help="seconds this pass may spend")
    a = ap.parse_args(argv)
    import atomic
    import configuration
    import paths
    f = files()
    if a.settle:
        got = settle(_read_json(f["out"]), _read_json(f["settled"]), a.settle, a.why or "", by="person:cli")
        if "error" in got:
            print(f"key-sweep: {got['error']['message']} ({got['error']['code']})", file=sys.stderr)
            return 2
        atomic.write_json(f["settled"], got["settled"])
        print(f"key-sweep: settled {a.settle[:24]}; the next sweep closes it")
        return 0
    if not configuration.enabled("key_sweep"):
        doc = {"schema": SCHEMA, "at": now_iso(), "enabled": False, "repositories": [], "sightings": [],
               "degraded": []}
        atomic.write_json(f["out"], doc)
        print(json.dumps({"result": "off"}) if a.json else "key-sweep: switched off (integrations.key_sweep)")
        return 0
    import scan_env
    try:
        key = scan_env.salt().encode("ascii")
    except Exception as exc:  # noqa: BLE001: the salt is absent or unreadable
        print(f"key-sweep: the workspace's fingerprint salt cannot be read ({type(exc).__name__}); nothing swept",
              file=sys.stderr)
        return 1
    try:
        projects = json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8")).get("projects") or []
    except (OSError, ValueError) as exc:
        print(f"key-sweep: the project registry cannot be read ({type(exc).__name__}); nothing swept", file=sys.stderr)
        return 1
    import scan_leaks
    values, _homes, skipped = scan_leaks.known_values()
    notes = [{"code": "values-unreadable", "reason": f"{k.get('what')}: {k.get('why')}"}
             for k in skipped if isinstance(k, dict) and k.get("unreadable")][:20]
    doc, state = run_pass(repositories(projects, paths.DATA), values, key, previous=_read_json(f["out"]),
                          state=_read_json(f["state"]), settled=_read_json(f["settled"]),
                          pass_budget=a.budget, scanner=find_scanner(), full=a.full, rotated_at=_rotated_at,
                          notes=notes)
    del values
    atomic.write_json(f["out"], doc)
    atomic.write_json(f["state"], state)
    summary = {"result": "ran", "repositories": len(doc["repositories"]), "complete": doc["complete"],
               "open": sum(1 for s in doc["sightings"] if s["status"] == "open"),
               "spent_s": doc["budget"]["spent_s"], "external": doc["scanners"]["external"]}
    print(json.dumps(summary) if a.json else
          f"key-sweep: {summary['repositories']} repositories, {summary['open']} open sighting(s), "
          f"{'complete' if summary['complete'] else 'resumes next pass'}; {summary['spent_s']} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
