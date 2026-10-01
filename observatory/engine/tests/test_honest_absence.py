#!/usr/bin/env python3
"""Absent is not broken, and a slow answer is not a missing one.

A service that is `degraded` asks a person to act. Measured on one operator's
machine, eight reasons kept the service degraded and most of them asked for
nothing a person could do — or claimed something that was not true:

* RDAP 404s for TLDs whose registries run no RDAP service at all, while the same
  domains were delegated in DNS. Nobody can fix a registry that has no RDAP.
* A repository whose remote names exactly one branch, `develop`, reported as
  having "no default branch", because only `origin/HEAD`, `main` and `master`
  were tried.
* An external CLI that took 31 s once while the load average stood near 200,
  reported as a failure on that single attempt.
* A companion tool that is simply not installed on the machine, reported in the
  same words as one that is installed and broken.
* An OpenRouter listing deliberately bounded to the newest keys, used as proof
  that older keys "no longer exist", and a provisioning key reported missing
  from a listing that never includes provisioning keys.
* A Google credential set up for one surface reported as degraded on the other
  surface, which another credential already reads.
* A receipt left behind by an integration the workspace has turned OFF, read as
  a live measurement for days.

Each is fixed by saying the TRUE thing: a source that does not apply here goes on
a `not_applicable` list beside `degraded`, visible and with its reason, and stops
holding the service degraded. A real gap stays on `degraded`. Every case below
also plants the old behaviour's input and watches the case that must still
degrade, so the fix cannot be "say nothing".

Nothing here reaches the network: every provider call is replaced in process.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "collectors"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402
import configuration  # noqa: E402
import paths  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def set_integration(name: str, on: bool | None) -> None:
    settings = paths.HOME / "config/settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    section = doc.setdefault("integrations", {})
    if on is None:
        section.pop(name, None)
    else:
        section[name] = on
    settings.write_text(json.dumps(doc), encoding="utf-8")


def write_receipt(name: str, doc: dict) -> pathlib.Path:
    paths.SCRATCH.mkdir(parents=True, exist_ok=True)
    f = paths.SCRATCH / name
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


class Patched:
    """Replace module attributes for one block and always put them back."""

    def __init__(self, module, **attrs):
        self.module, self.attrs, self.saved = module, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.module, k)
            setattr(self.module, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.module, k, v)


# ── the degradation channel ───────────────────────────────────────────────────

def test_a_receipt_of_an_integration_that_is_off_is_not_a_measurement() -> None:
    import degradations
    write_receipt("bitbucket.json", {"scanned_at": "2026-01-01T00:00:00Z",
                                     "degraded": [{"source": "bitbucket:example-org",
                                                   "reason": "no credential"}]})
    try:
        set_integration("bitbucket", True)
        check("an enabled integration's receipt still degrades",
              "bitbucket.json" in degradations.every_collector())
        set_integration("bitbucket", None)
        everyone = degradations.every_collector()
        check("a receipt left by an integration that is off degrades nothing",
              "bitbucket.json" not in everyone, str(sorted(everyone)))
        check("and the single-receipt reader agrees",
              degradations.collector("bitbucket.json") == [])
        notes = degradations.every_not_applicable().get("bitbucket.json") or []
        check("the leftover is still visible, named as an integration that is off",
              any("off" in str(n.get("reason")) and "2026-01-01" in str(n.get("reason"))
                  for n in notes), str(notes))
    finally:
        (paths.SCRATCH / "bitbucket.json").unlink(missing_ok=True)
        set_integration("bitbucket", None)


def test_every_receipt_mapping_names_a_real_integration() -> None:
    import degradations
    sys.path.insert(0, str(ROOT / "tools"))
    import tick_lease
    known = set(tick_lease.INTEGRATION_STEPS.values())
    stray = {r: i for r, i in degradations.RECEIPT_INTEGRATIONS.items() if i not in known}
    check("every receipt is mapped to an integration the tick knows", not stray, str(stray))


def test_not_applicable_rows_do_not_degrade_the_service() -> None:
    import degradations
    import service_health
    set_integration("domains", True)
    write_receipt("domains_live.json", {
        "scanned_at": "2026-01-01T00:00:00Z", "hosts": {}, "rdap": {}, "degraded": [],
        "not_applicable": [{"source": "rdap:alpha.example", "reason": "no RDAP service for .example"}]})
    try:
        check("a receipt with only not-applicable rows is not a degraded collector",
              "domains_live.json" not in degradations.every_collector())
        check("and its rows are readable as notes",
              degradations.every_not_applicable().get("domains_live.json", [{}])[0].get("source")
              == "rdap:alpha.example")
        src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
        check("the board still shows them, as an info row per receipt",
              "degradations.every_not_applicable()" in src and '"collector.not_applicable"' in src)
        snap = service_health.snapshot({"register": False})
        check("the service snapshot does not count them",
              not any(d["source"] == "collector:domains_live" for d in snap["degraded"]),
              str(snap["degraded"]))
    finally:
        (paths.SCRATCH / "domains_live.json").unlink(missing_ok=True)
        set_integration("domains", None)


# ── RDAP for a TLD that runs no RDAP service ─────────────────────────────────

def domains_run(rdap_answers: dict, dns: dict, bootstrap) -> dict:
    """Run `scan_domains.main` in process with DNS, RDAP and the IANA bootstrap planted."""
    import scan_domains as S
    reg = paths.REGISTRY
    saved = {n: (reg / n).read_text(encoding="utf-8") if (reg / n).is_file() else None
             for n in ("domains.json", "projects.json")}
    (reg / "domains.json").write_text(json.dumps({"domains": [{"name": n} for n in dns]}),
                                      encoding="utf-8")
    projects = json.loads(saved["projects.json"] or '{"projects": []}')
    for p in projects.get("projects", []):
        p["sites"] = []
    (reg / "projects.json").write_text(json.dumps(projects), encoding="utf-8")

    def probe(host):
        ns, a = dns[host]
        return host, {"checked_at": "2026-01-01T00:00:00Z", "nameservers": ns, "a": a,
                      "cname": [], "registrable": S.registrable(host),
                      "resolves": bool(a), "http": 200 if a else 0, **({} if a else {"dark": True})}

    dest = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-rdap-")) / "domains_live.json"
    import shutil as real_shutil
    try:
        with Patched(S, probe=probe, rdap=lambda name: rdap_answers[name],
                     rdap_bootstrap=lambda: bootstrap), \
                Patched(configuration, enabled=lambda *a, **k: True), \
                Patched(real_shutil, which=lambda tool: "/usr/bin/" + tool):
            S.main(["scan_domains.py", str(dest)])
    finally:
        for n, text in saved.items():
            if text is not None:
                (reg / n).write_text(text, encoding="utf-8")
    return json.loads(dest.read_text(encoding="utf-8"))


def test_a_tld_without_rdap_is_not_a_gap_when_the_domain_is_delegated() -> None:
    import scan_domains as S
    miss = S.rdap_missing("alpha.me")
    found = ({"about": "beta.com", "statuses": ["active"], "expiration": "2030-01-01",
              "registrar": "Example Registrar"}, "")
    answers = {"alpha.me": miss, "gamma.me": S.rdap_missing("gamma.me"),
               "delta.com": S.rdap_missing("delta.com"), "beta.com": found}
    dns = {"alpha.me": (["ns1.example.net"], ["192.0.2.1"]),   # delegated and resolving
           "gamma.me": ([], []),                               # nothing in DNS either
           "delta.com": (["ns1.example.net"], ["192.0.2.3"]),  # its TLD DOES run RDAP
           "beta.com": (["ns1.example.net"], ["192.0.2.2"])}
    doc = domains_run(answers, dns, ({"com", "net"}, "2026-01-01T00:00:00Z", None))
    deg = {d["source"]: d["reason"] for d in doc["degraded"]}
    na = {d["source"]: d["reason"] for d in doc.get("not_applicable") or []}
    check("a TLD outside the IANA bootstrap whose domain is delegated is not applicable",
          "rdap:alpha.me" in na and "rdap:alpha.me" not in deg, f"deg={deg} na={na}")
    check("and the reason says why: no RDAP service, and the DNS delegation that proves it is held",
          "bootstrap" in na.get("rdap:alpha.me", "") and "ns1.example.net" in na.get("rdap:alpha.me", ""),
          na.get("rdap:alpha.me", ""))
    check("a domain with no RDAP AND nothing in DNS is still a gap",
          "rdap:gamma.me" in deg, str(deg))
    check("a 404 under a TLD that runs RDAP is still a gap, and says it may be unregistered",
          "rdap:delta.com" in deg and "unregistered" in deg["rdap:delta.com"], str(deg))
    check("a record that was read is a record", "beta.com" in doc["rdap"])

    doc = domains_run(answers, dns, (None, None, "the bootstrap could not be fetched"))
    deg = {d["source"]: d["reason"] for d in doc["degraded"]}
    check("when the bootstrap cannot be read, nothing is excused",
          "rdap:alpha.me" in deg and not doc.get("not_applicable"),
          f"deg={deg} na={doc.get('not_applicable')}")


# ── a default branch that only the remote names ──────────────────────────────

def git(*args, cwd=None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


def checkout_with_remote_branches(names: list[str]) -> str:
    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-branch-"))
    seed, bare, clone = base / "seed", base / "remote.git", base / "clone"
    git("init", "-q", "-b", names[0], str(seed))
    (seed / "README.md").write_text("synthetic\n", encoding="utf-8")
    git("add", "README.md", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    for extra in names[1:]:
        git("branch", extra, cwd=seed)
    git("clone", "-q", "--bare", str(seed), str(bare))
    git("clone", "-q", str(bare), str(clone))
    # What a clone made by some tools looks like: no `origin/HEAD` at all.
    git("remote", "set-head", "origin", "-d", cwd=clone)
    return str(clone)


def test_a_remote_with_one_branch_names_the_default() -> None:
    import scan_git_hygiene as H
    repo = checkout_with_remote_branches(["develop"])
    check("one remote branch and no origin/HEAD resolves to that branch",
          H.default_branch(repo) == "develop", str(H.default_branch(repo)))
    repo = checkout_with_remote_branches(["develop", "release"])
    check("two remote branches and no origin/HEAD is still unresolved — guessing is worse",
          H.default_branch(repo) is None, str(H.default_branch(repo)))
    why = H.unresolved_reason(repo)
    check("and the reason names the one command that records it",
          "set-head origin --auto" in why and "2" in why, why)
    repo = checkout_with_remote_branches(["main", "develop"])
    check("main is still found when it exists", H.default_branch(repo) == "main")


# ── a slow command is asked again before it is reported ──────────────────────

def test_a_command_that_times_out_once_is_retried_with_backoff() -> None:
    import slow_command
    flag = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-slow-")) / "seen"
    # Slow the first time, quick the second: the shape of one load spike.
    script = (f"import os, time\nf={str(flag)!r}\n"
              "if not os.path.exists(f):\n    open(f,'w').close(); time.sleep(5)\nprint('ok')\n")
    slept: list[float] = []
    started = time.monotonic()
    proc, why = slow_command.run([sys.executable, "-c", script], timeouts=(1, 10), backoff=0.1,
                                 sleep=slept.append, capture_output=True, text=True)
    check("a second attempt answers after one timeout", proc is not None and proc.stdout.strip() == "ok",
          why)
    check("with a backoff between the two", slept == [0.1], str(slept))
    check("and the whole call stays bounded", time.monotonic() - started < 15)

    proc, why = slow_command.run([sys.executable, "-c", "import time; time.sleep(5)"],
                                 timeouts=(0.5, 1), backoff=0, sleep=lambda s: None,
                                 capture_output=True, text=True)
    check("a command that never answers is still reported", proc is None and why, why)
    check("with every duration it was given", "0.5s" in why and "1s" in why, why)
    check("and the load at the time, which is what tells a slow machine from a hung tool",
          "load average" in why, why)


def test_the_collectors_ask_twice() -> None:
    fs = (ROOT / "collectors/scan_filesystem.py").read_text(encoding="utf-8")
    hk = (ROOT / "collectors/scan_heroku.py").read_text(encoding="utf-8")
    check("the filesystem scan's git calls retry a timeout", "slow_command.run(" in fs)
    check("the heroku token is asked again before the scan gives up", "slow_command.run(" in hk)

    import scan_heroku
    calls: list[tuple] = []

    def never(args, **kw):
        calls.append(tuple(args))
        return None, "`heroku auth:token` did not answer in 30s, nor in 60s"
    import slow_command
    with Patched(slow_command, run=never), Patched(scan_heroku.shutil, which=lambda t: "/bin/" + t):
        tok, why = scan_heroku.token()
    check("a heroku token that never comes is a reason, not a crash",
          tok is None and "did not answer" in why, why)


# ── a consumer that is not installed is not a broken one ─────────────────────

def test_a_companion_that_is_not_installed_is_not_a_broken_one() -> None:
    from session_fixture import estate as session_estate
    with session_estate() as f:
        f.collector.STORE = f.root / "not-installed" / "companion.db"
        out = f.collector.scan()
        check("no companion directory at all is not a degradation",
              out["degraded"] == [], str(out["degraded"]))
        check("it is named as not installed", any("not installed" in n["reason"]
                                                   for n in out.get("not_applicable") or []),
              str(out.get("not_applicable")))
        f.collector.STORE = f.root / "missing.db"
        out = f.collector.scan()
        check("a companion directory without its database is still broken",
              any("falls back to commits alone" in d["reason"] for d in out["degraded"]),
              str(out["degraded"]))


# ── OpenRouter: a bounded listing is not proof of absence ────────────────────

KEY_A = "sk-or-v1-" + "aaa" + "0" * 55 + "a01"
KEY_B = "sk-or-v1-" + "bbb" + "0" * 55 + "b02"
KEY_P = "sk-or-v1-" + "ppp" + "0" * 55 + "p03"


def openrouter_run(*, deep_rows: list[dict], remembered: dict | None = None,
                   by_hash: dict | None = None, companion: bool = False,
                   deep_partial: bool = False) -> dict:
    import scan_openrouter as S
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-or-honest-"))
    (d / "state").mkdir()
    (d / "state/key").write_text(KEY_A + "\n", encoding="utf-8")
    (d / "secrets").mkdir()
    (d / "secrets/openrouter").write_text(KEY_B + "\n", encoding="utf-8")
    (d / "secrets/provisioning").write_text(KEY_P + "\n", encoding="utf-8")
    if companion:
        (d / "companion").mkdir()
    out = d / "openrouter.json"
    if remembered is not None:
        out.write_text(json.dumps({"destination_hashes": remembered}), encoding="utf-8")
    newest = [{"label": f"sk-or-v1-xxx...{i:03d}", "name": f"PRODUCTION_user_{i}", "hash": f"h{i}"}
              for i in range(3)]
    asked: list[dict] = []

    def listing(prov, offset=0, pages=S.MAX_PAGES, until=None):
        asked.append({"offset": offset, "pages": pages})
        if offset == 0:
            return newest, "stopped after 1 pages (3 keys); the account holds more and this list is partial"
        rows = deep_rows
        if until is not None:
            for i in range(len(rows)):
                if until(rows[: i + 1]):
                    return rows[: i + 1], None
        return rows, ("stopped after the deeper budget; the account holds more" if deep_partial else None)

    def key_by_hash(prov, h):
        row = (by_hash or {}).get(h)
        return (row, None, None) if row else (None, 404, "HTTP 404")

    with Patched(S, DESTINATIONS={"observatory": d / "state/key",
                                  "claude-mem": d / "companion/.env",
                                  "gateway": d / "secrets/openrouter",
                                  "provisioning": d / "secrets/provisioning"},
                 listing=listing, key_by_hash=key_by_hash,
                 whoami=lambda prov: ({"label": S.label_of(KEY_P), "is_provisioning_key": True}, None)), \
            Patched(configuration, enabled=lambda *a, **k: True):
        S.main(["scan_openrouter.py", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    doc["_asked"] = asked
    return doc


def test_a_bounded_listing_is_searched_before_a_key_is_called_gone() -> None:
    import scan_openrouter as S
    a, b = S.label_of(KEY_A), S.label_of(KEY_B)
    deep = [{"label": "sk-or-v1-xxx...900", "name": "PRODUCTION_user_9", "hash": "h9"},
            {"label": a, "name": "observatory", "hash": "hA"},
            {"label": b, "name": "gateway", "hash": "hB"},
            {"label": "sk-or-v1-xxx...901", "name": "PRODUCTION_user_10", "hash": "h10"}]
    doc = openrouter_run(deep_rows=deep)
    deg = {x["source"]: x["reason"] for x in doc["degraded"]}
    na = {x["source"]: x["reason"] for x in doc.get("not_applicable") or []}
    served = {k["label"]: k["serves"] for k in doc["keys"]}
    check("keys older than the bounded listing are found by searching further",
          served.get(a) == "observatory" and served.get(b) == "gateway", str(served))
    check("and the deeper search stops once every consumer's key is found",
          len(doc["_asked"]) == 2 and not any(k["label"].endswith("901") for k in doc["keys"]),
          str(doc["_asked"]))
    check("no consumer is reported as holding a key the account lacks",
          not any(s in deg for s in ("openrouter:observatory", "openrouter:gateway",
                                     "openrouter:provisioning")), str(deg))
    check("the provisioning key is confirmed by asking about itself — listings never include one",
          "openrouter:provisioning" not in deg and doc.get("verified", {}).get("provisioning"),
          str(doc.get("verified")))
    check("the hash each consumer's key was found under is remembered for the next run",
          doc.get("destination_hashes", {}).get("observatory", {}).get("hash") == "hA",
          str(doc.get("destination_hashes")))
    check("a deliberately bounded listing that missed no consumer is a note, not a gap",
          "openrouter:listing" in na and "openrouter:listing" not in deg, f"deg={deg} na={na}")
    check("a consumer that is not installed on this machine is not a degradation",
          "openrouter:claude-mem" not in deg and "not installed" in na.get("openrouter:claude-mem", ""),
          f"deg={deg} na={na}")

    doc = openrouter_run(deep_rows=deep, companion=True)
    deg = {x["source"]: x["reason"] for x in doc["degraded"]}
    check("an installed consumer with no key file still degrades",
          "openrouter:claude-mem" in deg, str(deg))

    doc = openrouter_run(deep_rows=[deep[0]])
    deg = {x["source"]: x["reason"] for x in doc["degraded"]}
    check("a key absent from a listing walked to its end is not in the account",
          "not in this account's listing" in deg.get("openrouter:observatory", ""), str(deg))

    doc = openrouter_run(deep_rows=[deep[0]], deep_partial=True)
    deg = {x["source"]: x["reason"] for x in doc["degraded"]}
    check("a key the bounded deeper search did not reach is UNKNOWN, not gone",
          "unknown" in deg.get("openrouter:observatory", "")
          and "no longer exists" not in deg.get("openrouter:observatory", ""), str(deg))
    check("and the listing's partiality is then still a gap", "openrouter:listing" in deg, str(deg))


def test_a_remembered_hash_answers_without_a_deep_search() -> None:
    import scan_openrouter as S
    a, b = S.label_of(KEY_A), S.label_of(KEY_B)
    remembered = {"observatory": {"label": a, "hash": "hA"}, "gateway": {"label": b, "hash": "hB"}}
    doc = openrouter_run(deep_rows=[], remembered=remembered,
                         by_hash={"hA": {"label": a, "name": "observatory", "hash": "hA"}})
    deg = {x["source"]: x["reason"] for x in doc["degraded"]}
    served = {k["label"]: k["serves"] for k in doc["keys"]}
    check("a key is confirmed by its remembered hash", served.get(a) == "observatory", str(served))
    check("a remembered hash the provider no longer knows is a DELETED key — a real problem",
          "openrouter:gateway" in deg and "deleted" in deg["openrouter:gateway"], str(deg))
    check("and no deep search ran: the hash answered both", len(doc["_asked"]) == 1,
          str(doc["_asked"]))


# ── Google: a credential not set up for a surface another one reads ───────────

def test_a_surface_read_by_another_credential_is_not_a_gap() -> None:
    import scan_google as G
    disabled = ("HTTP 403: Google Search Console API has not been used in project 100000000001 "
                "before or it is disabled.")
    creds = [{"file": "a.json", "client_email": "analytics@example.invalid", "cloud_project": "p-a"},
             {"file": "b.json", "client_email": "search@example.invalid", "cloud_project": "p-b"}]

    def analytics(c):
        if c["file"] == "a.json":
            return [{"account": "accounts/1"}], [], []
        return [], [], [{"source": f"ga4 via {c['client_email']}", "surface": "ga4",
                         "reason": disabled.replace("Google Search Console", "Google Analytics Admin"),
                         "effect": "no account list"}]

    def search(c, ok_for=("b.json",)):
        if c["file"] in ok_for:
            return [{"site": "sc-domain:example.invalid", "read_with": c["client_email"]}], []
        return [], [{"source": f"search console via {c['client_email']}", "surface": "search_console",
                     "reason": disabled, "effect": "no site", "remedy": "enable it"}]

    out = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-google-honest-")) / "google.json"
    with Patched(G, credentials=lambda: creds, scan_analytics=analytics, scan_search_console=search), \
            Patched(configuration, enabled=lambda *a, **k: True):
        G.main(["scan_google.py", str(out), "--force"])
    doc = json.loads(out.read_text(encoding="utf-8"))
    na = [x["source"] for x in doc.get("not_applicable") or []]
    check("an API switched off for a credential whose surface another credential reads is a note",
          not doc["degraded"] and len(na) == 2, f"deg={doc['degraded']} na={na}")
    check("and the note names who does read it",
          all("read through" in x["reason"] for x in doc["not_applicable"]),
          str(doc.get("not_applicable")))

    with Patched(G, credentials=lambda: creds, scan_analytics=analytics,
                 scan_search_console=lambda c: search(c, ok_for=())), \
            Patched(configuration, enabled=lambda *a, **k: True):
        G.main(["scan_google.py", str(out), "--force"])
    doc = json.loads(out.read_text(encoding="utf-8"))
    check("when no credential reads a surface, the refusals stay degraded",
          sum(1 for x in doc["degraded"] if x.get("surface") == "search_console") == 2,
          str(doc["degraded"]))

    sys.path.insert(0, str(ROOT / "tools"))
    import google_findings
    rows = google_findings.findings({"properties": [{"name": "X", "standing": "linked", "users_30d": 1}],
                                     "degraded": [],
                                     "not_applicable": [{"source": "search console via x",
                                                         "reason": disabled, "remedy": "enable it"}]},
                                    "2026-01-01")
    check("the switched-off API stays on the board as information",
          any(r["type"] == "analytics.api_disabled" and r["severity"] == "info" for r in rows), str(rows))


if __name__ == "__main__":
    print("honest absence — not applicable is not degraded, and slow is not missing\n")
    for fn in (test_a_receipt_of_an_integration_that_is_off_is_not_a_measurement,
               test_every_receipt_mapping_names_a_real_integration,
               test_not_applicable_rows_do_not_degrade_the_service,
               test_a_tld_without_rdap_is_not_a_gap_when_the_domain_is_delegated,
               test_a_remote_with_one_branch_names_the_default,
               test_a_command_that_times_out_once_is_retried_with_backoff,
               test_the_collectors_ask_twice,
               test_a_companion_that_is_not_installed_is_not_a_broken_one,
               test_a_bounded_listing_is_searched_before_a_key_is_called_gone,
               test_a_remembered_hash_answers_without_a_deep_search,
               test_a_surface_read_by_another_credential_is_not_a_gap):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a crash is a failed case, named
            check(f"{fn.__name__} ran to the end", False, f"{type(exc).__name__}: {exc}")
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32meach absence says what it is, and each real gap still degrades\033[0m")
