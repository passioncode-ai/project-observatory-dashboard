# Vendored from passioncode-ai/fabric-agent-adapter aaaa93f97577 (fabric-agent-adapter 0.4.0),
# plugins/fabric-agent-adapter/skills/building-fabric-services/scripts/check_service.py,
# upstream sha256 f25aa81f9d4b1dcc175b2243e6c5bf6f5bd9ce9ca9481a476f785544a0bc3762 (the bytes below this header).
# Do not edit here: update the kit upstream and copy it again (tests/test_fabric_service.py checks the digest).
#!/usr/bin/env python3
"""Live conformance probe for a fabric-service/0.1 service.

  check_service.py <id>[.<instance>]          # find the descriptor in the services directory
  check_service.py --descriptor PATH
  options: --services-dir DIR  --json  --skip-login

Every rule gets PASS, FAIL or NOT_RUN with its evidence. Exit 0 when nothing
FAILs, 1 when something does, 2 on a usage error. The probe only reads, except
that it redeems one login code it asked for itself (that creates one session).
"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabric_service as fs  # noqa: E402

Result = Dict[str, str]


class Probe:
    def __init__(self, descriptor_path: Path, descriptor: Dict[str, Any], services_dir: Path, skip_login: bool):
        self.path = descriptor_path
        self.d = descriptor
        self.dir = services_dir
        self.skip_login = skip_login
        self.results: List[Result] = []
        self.wk: Optional[Dict[str, Any]] = None
        self.token: Optional[str] = None
        try:
            self.port = fs.port_of(str(descriptor.get("origin", "")))
        except fs.ServiceError:
            self.port = 0

    def add(self, rule: str, verdict: str, evidence: str) -> None:
        self.results.append({"rule": rule, "verdict": verdict, "evidence": evidence})

    def request(self, method: str, path: str, headers: Optional[Dict[str, str]] = None,
                body: Optional[bytes] = None) -> Tuple[int, Dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        base = {"Host": "127.0.0.1:%d" % self.port}
        base.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=base)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def auth_headers(self) -> Dict[str, str]:
        auth = self.d.get("auth", {})
        header = auth.get("header", "Authorization")
        value = self.token or ""
        return {header: ("Bearer " + value) if auth.get("scheme", "Bearer") == "Bearer" else value}

    # descriptor ------------------------------------------------------------
    def descriptor_rules(self) -> None:
        problems = fs.validate_descriptor(self.d)
        self.add("descriptor.valid", "FAIL" if problems else "PASS", "; ".join(problems) or str(self.path))
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.add("descriptor.private", "PASS" if mode & 0o077 == 0 else "FAIL", "mode %o" % mode)
        me = "%s.%s" % (self.d.get("id"), self.d.get("instance", "default"))
        clashes = []
        for path, other in fs.read_descriptors(self.dir):
            key = "%s.%s" % (other.get("id"), other.get("instance", "default"))
            if path.resolve() == self.path.resolve():
                continue
            if key == me:
                clashes.append("%s declared again in %s" % (me, path.name))
            elif other.get("origin") == self.d.get("origin"):
                clashes.append("port %d also claimed by %s" % (self.port, key))
        self.add("descriptor.port-claim", "FAIL" if clashes else "PASS", "; ".join(clashes) or "port %d is unique" % self.port)

    # well-known ----------------------------------------------------------------
    def well_known_rules(self) -> None:
        try:
            timings = []
            for _ in range(3):
                started = time.perf_counter()
                status, headers, body = self.request("GET", "/.well-known/fabric-service")
                timings.append((time.perf_counter() - started) * 1000)
        except OSError as exc:
            self.add("well-known.answers", "FAIL", "no answer on %s: %s" % (self.d.get("origin"), exc))
            return
        if status != 200:
            self.add("well-known.answers", "FAIL", "HTTP %d" % status)
            return
        try:
            self.wk = json.loads(body)
        except ValueError:
            self.add("well-known.answers", "FAIL", "body is not JSON")
            return
        self.add("well-known.answers", "PASS", "HTTP 200")
        median = sorted(timings)[1]
        self.add("well-known.fast", "PASS" if median < 100 else "FAIL", "median %.1f ms" % median)
        wk = self.wk
        problems = []
        if wk.get("protocol") != fs.PROTOCOL:
            problems.append("protocol %r" % wk.get("protocol"))
        if "degraded" not in wk:
            problems.append("degraded missing")
        build = (wk.get("service") or {}).get("build") or {}
        if not (build.get("commit") or build.get("digest")):
            problems.append("build has neither commit nor digest")
        if wk.get("status") not in fs.STATUSES:
            problems.append("status %r" % wk.get("status"))
        if "events" not in (wk.get("surfaces") or {}):
            problems.append("surfaces.events missing")
        self.add("well-known.shape", "FAIL" if problems else "PASS", "; ".join(problems) or "protocol, build, status, degraded, surfaces")
        svc = wk.get("service") or {}
        answered = "%s.%s" % (svc.get("id"), svc.get("instance"))
        expected = "%s.%s" % (self.d.get("id"), self.d.get("instance", "default"))
        self.add("well-known.identity", "PASS" if answered == expected else "FAIL",
                 "answers as %s" % answered + ("" if answered == expected else ", descriptor says %s" % expected))
        ready_bad = wk.get("status") == "ready" and bool(wk.get("degraded"))
        self.add("well-known.ready-means-healthy", "FAIL" if ready_bad else "PASS",
                 "status %s, %d degraded" % (wk.get("status"), len(wk.get("degraded") or [])))

    # network ------------------------------------------------------------------
    def network_rules(self) -> None:
        for rule, headers in (
            ("network.host-check", {"Host": "evil.example"}),
            ("network.origin-check", {"Origin": "http://evil.example"}),
            ("network.cross-site-check", {"Sec-Fetch-Site": "cross-site"}),
        ):
            try:
                status, _, _ = self.request("GET", "/.well-known/fabric-service", headers)
                self.add(rule, "PASS" if status == 403 else "FAIL", "HTTP %d" % status)
            except OSError as exc:
                self.add(rule, "NOT_RUN", str(exc))
        if not shutil.which("lsof"):
            self.add("network.loopback-only", "NOT_RUN", "lsof is not installed")
            return
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % self.port, "-sTCP:LISTEN"], capture_output=True, text=True).stdout
        names = re.findall(r"TCP (\S+) \(LISTEN\)", out)
        wide = [n for n in names if not n.startswith(("127.0.0.1:", "[::1]:", "localhost:"))]
        self.add("network.loopback-only", "FAIL" if wide or not names else "PASS",
                 ", ".join(names) or "nothing listens on %d" % self.port)

    # auth, events, login ----------------------------------------------------------
    def auth_rules(self) -> None:
        token_file = str((self.d.get("auth") or {}).get("tokenFile", ""))
        try:
            self.token = fs.read_token(fs.expand(token_file))
            self.add("auth.token-file", "PASS", "%s is 0600 and owned by you" % token_file)
        except (fs.ServiceError, OSError) as exc:
            self.add("auth.token-file", "FAIL", str(exc))
        events_path = ((self.wk or {}).get("surfaces") or {}).get("events", {}).get("path", "/fabric/v1/events")
        try:
            status, _, _ = self.request("GET", events_path + "?limit=1")
            self.add("events.requires-token", "PASS" if status == 401 else "FAIL", "HTTP %d without a token" % status)
        except OSError as exc:
            self.add("events.requires-token", "NOT_RUN", str(exc))
        if not self.token:
            self.add("events.page", "NOT_RUN", "no readable token")
            return
        try:
            status, _, body = self.request("GET", events_path + "?limit=5", self.auth_headers())
        except OSError as exc:
            self.add("events.page", "NOT_RUN", str(exc))
            return
        if status != 200:
            self.add("events.page", "FAIL", "HTTP %d with the token" % status)
            return
        try:
            page = json.loads(body)
            events = page["events"]
            assert "cursor" in page
        except (ValueError, KeyError, AssertionError):
            self.add("events.page", "FAIL", "not an events page")
            return
        bad = []
        for e in events:
            if e.get("level") not in fs.LEVELS or not re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", str(e.get("kind", ""))):
                bad.append(str(e.get("id")))
            elif " " not in str(e.get("text", "")).strip():
                bad.append("%s (text is not a sentence)" % e.get("id"))
        self.add("events.page", "FAIL" if bad else "PASS",
                 ("bad events: " + ", ".join(bad)) if bad else "%d event(s), cursor %r" % (len(events), page["cursor"]))

    def login_rules(self) -> None:
        dashboard = ((self.wk or {}).get("surfaces") or {}).get("dashboard")
        if not dashboard or not dashboard.get("login"):
            self.add("login.single-use", "NOT_RUN", "dashboard declares no login")
            return
        if self.skip_login or not self.token:
            self.add("login.single-use", "NOT_RUN", "--skip-login" if self.skip_login else "no readable token")
            return
        try:
            status, _, body = self.request("POST", "/fabric/v1/login-code", self.auth_headers())
            code = json.loads(body)
            url = code["url"]
            status1, headers1, _ = self.request("GET", url)
            status2, _, _ = self.request("GET", url)
        except (OSError, ValueError, KeyError) as exc:
            self.add("login.single-use", "FAIL", "login-code flow broke: %s" % exc)
            return
        cookie = headers1.get("set-cookie", "")
        ok = status1 in (302, 303) and "HttpOnly" in cookie and "SameSite=Strict" in cookie and status2 not in (302, 303)
        self.add("login.single-use", "PASS" if ok else "FAIL",
                 "first redeem HTTP %d (%s), second HTTP %d" % (status1, "cookie ok" if "HttpOnly" in cookie else "no HttpOnly cookie", status2))

    # lifecycle ----------------------------------------------------------------
    def lifecycle_rules(self) -> None:
        life = self.d.get("lifecycle") or {}
        data = fs.expand(str((self.d.get("paths") or {}).get("data", "~/")))
        inside = subprocess.run(["git", "-C", str(data), "rev-parse", "--show-toplevel"], capture_output=True, text=True) if data.is_dir() and shutil.which("git") else None
        self.state_rule(data, inside)
        self.lock_rule(data)
        if life.get("manager") != "launchd":
            self.add("lifecycle.launchd", "NOT_RUN", "lifecycle.manager is %s" % life.get("manager"))
            return
        plist_path = fs.expand(str(life.get("plist")))
        try:
            plist = plistlib.loads(plist_path.read_bytes())
        except (OSError, ValueError) as exc:
            self.add("lifecycle.plist", "FAIL", "cannot read %s: %s" % (plist_path, exc))
            return
        problems = []
        if plist.get("Label") != life.get("label"):
            problems.append("Label %r" % plist.get("Label"))
        if plist.get("RunAtLoad") is not True:
            problems.append("RunAtLoad is not true")
        if plist.get("KeepAlive") is not True:
            problems.append("KeepAlive is %r, not true" % plist.get("KeepAlive"))
        for key, value in (plist.get("EnvironmentVariables") or {}).items():
            if re.search(r"(TOKEN|SECRET|PASSWORD|KEY)$", key) and not key.endswith("_FILE"):
                problems.append("secret-like variable %s in the plist" % key)
            if self.token and self.token in str(value):
                problems.append("the service token itself is in the plist")
        self.add("lifecycle.plist", "FAIL" if problems else "PASS", "; ".join(problems) or "%s: RunAtLoad, KeepAlive, no secrets" % plist_path.name)
        if not shutil.which("launchctl"):
            self.add("lifecycle.one-copy", "NOT_RUN", "launchctl is not available")
            return
        out = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), life.get("label"))], capture_output=True, text=True)
        match = re.search(r"^\s*pid = (\d+)", out.stdout, re.MULTILINE)
        served = ((self.wk or {}).get("process") or {}).get("pid")
        if out.returncode != 0:
            self.add("lifecycle.one-copy", "FAIL", "launchd job %s is not loaded" % life.get("label"))
        elif not match:
            self.add("lifecycle.one-copy", "FAIL", "launchd job loaded but not running")
        else:
            same = int(match.group(1)) == served
            self.add("lifecycle.one-copy", "PASS" if same else "FAIL",
                     "launchd pid %s, answering pid %s" % (match.group(1), served))

    def state_rule(self, data: Path, inside: Optional[subprocess.CompletedProcess]) -> None:
        """State must not live in the service's CODE: its own checkout or a release.

        A repository that exists to version the data itself (a registry, a plan) is
        a store, not code; deleting the service's checkout does not touch it."""
        rule = "state.outside-code"
        if "/releases/" in str(data):
            self.add(rule, "FAIL", "%s is inside a release directory" % data)
            return
        if inside is None:
            self.add(rule, "NOT_RUN", "data directory %s missing or git absent" % data)
            return
        if inside.returncode != 0:
            self.add(rule, "PASS", "%s is not inside a repository" % data)
            return
        top = inside.stdout.strip()
        remote = subprocess.run(["git", "-C", top, "remote", "get-url", "origin"], capture_output=True, text=True)
        source = str((self.d.get("source") or {}).get("repository") or "")
        if not source:
            self.add(rule, "NOT_RUN", "data is inside repository %s and the descriptor names no source.repository to tell code from data" % top)
        elif remote.returncode == 0 and same_repository(remote.stdout.strip(), source):
            self.add(rule, "FAIL", "data lives in the service's own code checkout %s (%s)" % (top, source))
        else:
            self.add(rule, "PASS", "data is in repository %s, which is not the service's code (%s)" % (top, remote.stdout.strip() or "no remote"))

    def lock_rule(self, data: Path) -> None:
        lock = data / "service.lock"
        if not lock.exists():
            self.add("lifecycle.instance-lock", "FAIL", "%s does not exist" % lock)
            return
        try:
            import fcntl
        except ImportError:
            self.add("lifecycle.instance-lock", "NOT_RUN", "fcntl unavailable")
            return
        fd = os.open(str(lock), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            held = exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)
            self.add("lifecycle.instance-lock", "PASS" if held else "FAIL", "held by the running service" if held else str(exc))
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            self.add("lifecycle.instance-lock", "FAIL", "%s exists but nobody holds it" % lock)
        finally:
            os.close(fd)

    def run(self) -> List[Result]:
        self.descriptor_rules()
        if not self.port:
            return self.results
        self.well_known_rules()
        if self.wk is not None:
            self.network_rules()
            self.auth_rules()
            self.login_rules()
        self.lifecycle_rules()
        return self.results


def same_repository(a: str, b: str) -> bool:
    """git@github.com:o/r.git, https://github.com/o/r and github.com/o/r are one repository."""
    def norm(u: str) -> str:
        u = u.strip().lower()
        u = re.sub(r"^git@([^:]+):", r"\1/", u)
        u = re.sub(r"^[a-z+]+://", "", u)
        u = re.sub(r"^[^@/]+@", "", u)
        return re.sub(r"\.git$", "", u).rstrip("/")
    return bool(a and b) and norm(a) == norm(b)


def locate(target: Optional[str], descriptor: Optional[str], services_dir: Path) -> Path:
    if descriptor:
        return Path(descriptor).expanduser()
    if not target:
        raise SystemExit(2)
    service_id, _, instance = target.partition(".")
    return fs.descriptor_path(service_id, instance or "default", services_dir)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", nargs="?", help="<id> or <id>.<instance>")
    parser.add_argument("--descriptor")
    parser.add_argument("--services-dir")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-login", action="store_true")
    args = parser.parse_args(argv)
    if not args.target and not args.descriptor:
        parser.print_usage(sys.stderr)
        return 2
    services_dir = Path(args.services_dir).expanduser() if args.services_dir else fs.services_dir()
    path = locate(args.target, args.descriptor, services_dir)
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("No readable descriptor at %s: %s" % (path, exc), file=sys.stderr)
        return 1
    results = Probe(path, descriptor, services_dir, args.skip_login).run()
    failed = sum(r["verdict"] == "FAIL" for r in results)
    if args.json:
        print(json.dumps({"descriptor": str(path), "results": results, "failed": failed}, indent=2))
    else:
        width = max(len(r["rule"]) for r in results)
        for r in results:
            print("%-8s %-*s  %s" % (r["verdict"], width, r["rule"], r["evidence"]))
        print("\n%d rule(s), %d FAIL, %d NOT_RUN" % (len(results), failed, sum(r["verdict"] == "NOT_RUN" for r in results)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
