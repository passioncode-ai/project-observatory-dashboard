#!/usr/bin/env python3
"""Ask DNS and RDAP what is actually true of every domain the registry names.

WHY. `registry/domains.json` marks domains `ownership: owned`, and every field in
it can be a transcription of documents from one day — a registrar CSV export, a
table pasted from the DNS provider. Nothing had ever been measured. A domain that
expired, was transferred, or was put on registrar hold would go on reading
`owned` forever, and most of them carry `auto_renew: false`.

That is not a one-domain problem, though that is how it gets found: a domain
can turn out to be registered at a registrar nobody listed — and, far more
usefully, be on `client hold` and resolve nowhere, while the registry presents
it as a project's homepage.

WHAT IT DOES NOT DO. It never overwrites what the operator transcribed. Measured
facts land in `live` and `rdap`; where they disagree with the transcription the
disagreement is NAMED, not resolved. A collector that silently corrected the
operator would destroy the only record of what was believed and when.

Credential-free: public DNS and public RDAP. Not part of the thirty-minute tick.

    scan_domains.py store/raw/domains_live.json [--only <host> ...]
"""
from __future__ import annotations
import concurrent.futures as cf
import shutil
import json, pathlib, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths              

WORKERS = 6
RDAP = "https://rdap.org/domain/"
RDAP_RETRIES = 4
RDAP_BACKOFF = 2.0
# One RDAP query at a time. The registry side is a shared public service and
# 58 names arriving at once is what earned the 429s; the whole pass still
# finishes in under a minute.
RDAP_WORKERS = 1
#: IANA's list of which TLD registries run an RDAP service, and where. `rdap.org`
#: redirects through it, so a TLD missing from it answers 404 for every domain —
#: registered or not. Read once per run; nothing else is asked of IANA.
IANA_BOOTSTRAP = "https://data.iana.org/rdap/dns.json"
# Enough for this estate; a multi-label public suffix (co.uk) would need the PSL,
# and asserting one is not the same as having it — see `registrable()`.
TWO_LABEL_SUFFIXES = {"co.uk", "com.br", "com.au", "co.jp", "com.ua", "co.il"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def registrable(host: str) -> str:
    """The name a registrar actually holds — RDAP answers for that, not a subdomain."""
    parts = host.strip(".").lower().split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LABEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def dig(host: str, rtype: str) -> tuple[list[str], str | None]:
    """Records, and the reason there are none when that reason is not the DNS.

    It used to return `[]` for both "this host has no such record" and "dig
    could not run" — the same value for a measurement and for the absence of
    one. That is the exact shape AGENTS.md rule 7 forbids, in the one collector
    that reaches outside this machine, and it is not a small conflation: an
    empty A record makes a domain `dark`, `domain.dark` says it "is being paid
    for and serves nothing", and `site.dead` is CRITICAL — "a published claim
    that is currently false".

    Measured 2026-09-07 by running this collector with `dig` and `curl` off
    PATH: **all 58 hosts came back `resolves: false, dark: true`** with
    `degraded` naming only RDAP. Fourteen dark domains would have become
    fifty-three, three critical findings would have become dozens, and the
    emitter would have committed every one of them to the canonical registry.
    """
    try:
        r = subprocess.run(["dig", "+short", "+time=5", "+tries=2", rtype, host],
                           capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return [], "dig is not installed here"
    except subprocess.TimeoutExpired:
        return [], "dig timed out after 15s"
    except OSError as exc:
        return [], f"dig could not run: {exc}"
    if r.returncode != 0:
        return [], f"dig exited {r.returncode}: {(r.stderr or '').strip()[:80]}"
    return [ln.strip().rstrip(".") for ln in r.stdout.splitlines() if ln.strip()], None


def http_status(host: str) -> tuple[int, str | None]:
    """The status, and the reason there is none.

    The docstring here used to read "0 means the connection never completed —
    which is what a dark domain looks like", which admitted the conflation and
    called it a feature. A site that is down and a `curl` that is missing look
    identical from the outside and mean opposite things about the estate.
    """
    try:
        r = subprocess.run(["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}",
                            "--max-time", "12", f"https://{host}"],
                           capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return 0, "curl is not installed here"
    except subprocess.TimeoutExpired:
        return 0, "curl timed out after 20s"
    except OSError as exc:
        return 0, f"curl could not run: {exc}"
    try:
        return int(r.stdout.strip() or 0), None
    except ValueError:
        return 0, f"curl returned {r.stdout.strip()[:40]!r} instead of a status"


#: A 404 from `rdap()`, marked so the caller can tell "no record" from every
#: other failure without parsing the sentence. It never reaches the receipt.
NO_RECORD = "_no_record"


def rdap_missing(name: str) -> tuple[dict, str]:
    """What `rdap()` returns for a 404: no record, marked as such."""
    return {NO_RECORD: True}, (f"RDAP has no record for {name} (404). For a TLD outside the "
                               f"RDAP bootstrap — .me and .co answer this way while resolving "
                               f"normally — it means no RDAP service, NOT an unregistered domain")


def rdap_bootstrap() -> tuple[set[str] | None, str | None, str | None]:
    """(TLDs with an RDAP service, the bootstrap's publication stamp, why it is unknown)."""
    req = urllib.request.Request(IANA_BOOTSTRAP, headers={"Accept": "application/json",
                                                          "User-Agent": "project-observatory"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            doc = json.loads(r.read() or b"{}")
        tlds = {t.lower() for entry in doc.get("services") or [] for t in (entry or [[]])[0]}
    except (OSError, ValueError, TypeError, IndexError) as exc:
        return None, None, f"the IANA RDAP bootstrap could not be read: {type(exc).__name__}"
    if not tlds:
        return None, None, "the IANA RDAP bootstrap listed no TLD at all"
    return tlds, doc.get("publication"), None


def classify_missing(name: str, reason: str, tlds: set[str] | None, published: str | None,
                     why_unknown: str | None, hosts: list[dict]) -> tuple[str, dict]:
    """("not_applicable" | "degraded", row) for a domain RDAP has no record of.

    A registry that runs no RDAP service has no record to give, so its 404 says
    nothing about the domain. Measured on one estate: seven domains under four
    such TLDs, every one delegated in DNS and six of them serving — reported on
    every run as sources "not fully read", which no person can fix. Such a 404
    is NOT APPLICABLE when two facts hold: the TLD is absent from IANA's
    bootstrap, and the DNS shows the domain is held (nameservers answer for it,
    or it resolves). Without either fact the old degradation stands: a TLD that
    DOES run RDAP and has no record is a domain that may be unregistered, and a
    domain nothing in DNS knows about has no evidence of being held at all.
    """
    tld = name.rsplit(".", 1)[-1].lower()
    if tlds is None:
        return "degraded", {"source": f"rdap:{name}", "reason": f"{reason}; {why_unknown}, "
                                                               f"so whether .{tld} runs RDAP is unknown"}
    if tld in tlds:
        return "degraded", {"source": f"rdap:{name}",
                            "reason": (f"the .{tld} registry runs RDAP and has no record for {name} "
                                       f"(404): it may be unregistered or just expired — check it "
                                       f"at its registrar")}
    ns = sorted({n for h in hosts for n in (h.get("nameservers") or [])})
    resolves = any(h.get("resolves") is True for h in hosts)
    if not ns and not resolves:
        return "degraded", {"source": f"rdap:{name}",
                            "reason": (f"the .{tld} registry runs no RDAP service, and nothing in "
                                       f"DNS answers for {name} either, so nothing shows it is held")}
    shown = ", ".join(ns[:2]) + (f" and {len(ns) - 2} more" if len(ns) > 2 else "")
    evidence = f"delegated to {shown}" if ns else "resolving"
    return "not_applicable", {
        "source": f"rdap:{name}",
        "reason": (f"the .{tld} registry runs no RDAP service (absent from the IANA RDAP "
                   f"bootstrap{' of ' + str(published)[:10] if published else ''}), so there is no "
                   f"registration record to read; {name} is {evidence} in DNS, which shows it is "
                   f"held. Expiry and registrar for it come from the registrar, not from here")}


def rdap(name: str, attempt: int = 0) -> tuple[dict, str]:
    """One RDAP lookup, with the failure modes told apart rather than blurred.

    The first version reported every HTTP error as "this TLD has no RDAP
    service". Run against 58 hosts it produced that sentence for a wall of
    **429**s — rate limiting caused by its own concurrency — and the message was
    the only evidence anyone would have had. A classifier that maps every
    failure to one cause is worse than no message: it explains, confidently,
    something that did not happen.
    """
    req = urllib.request.Request(RDAP + name, headers={
        "Accept": "application/rdap+json", "User-Agent": "project-observatory"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 429 and attempt < RDAP_RETRIES:
            wait = float(e.headers.get("Retry-After") or 0) or RDAP_BACKOFF * (2 ** attempt)
            time.sleep(min(wait, 30))
            return rdap(name, attempt + 1)
        if e.code == 429:
            return {}, (f"RDAP rate-limited {name} (429) after {RDAP_RETRIES} retries — "
                        f"this says nothing about the domain, only about the query rate")
        if e.code == 404:
            return rdap_missing(name)
        return {}, f"RDAP returned HTTP {e.code} for {name}"
    except OSError as e:
        return {}, f"RDAP unreachable for {name}: {e}"

    # WHICH name this record describes. Without it, a domain under a multi-label
    # public suffix silently inherits the suffix's facts: a lookup for a name
    # under a suffix like `xx.com` can return the registrar and expiry of the
    # suffix itself, which are true statements about the suffix and about
    # nothing else. Recording `about` lets the emitter refuse a comparison
    # rather than make a false one.
    out: dict = {"about": (d.get("ldhName") or name).lower(), "statuses": d.get("status") or []}
    for ev in d.get("events") or []:
        act = (ev.get("eventAction") or "").replace(" ", "_")
        if act in ("registration", "expiration", "last_changed"):
            out[act] = str(ev.get("eventDate") or "")[:10]
    for ent in d.get("entities") or []:
        if "registrar" in (ent.get("roles") or []):
            for row in (ent.get("vcardArray") or [None, []])[1]:
                if row and row[0] == "fn":
                    out["registrar"] = row[3]
    # WHICH FOREIGN KEYS WERE NOT THERE, named on the record itself.
    #
    # Every field above is `d.get(...) or <default>`, so an absent key and an
    # empty value produce the same answer — and one of those answers carries a
    # CRITICAL finding. `statuses` decides `domain.hold`: if RDAP renamed
    # `status`, every record would read `[]`, `any("hold" in s for s in [])`
    # would be False for every domain, and the only signal that a domain is on
    # registrar hold would vanish with no degradation anywhere, because the
    # fetch itself succeeded. `expiration` decides `domain.expiring` the same
    # way.
    #
    # In practice an empty `statuses` or a missing `expiration` is rare, so an
    # empty one is more likely a key that could not be read than a real state.
    # RFC 9083 makes `status` optional, so a TLD may legitimately omit it; then
    # this list says so for that domain, which is true rather than alarmist.
    unread = [k for k, present in (("status", bool(d.get("status"))),
                                   ("expiration", bool(out.get("expiration"))),
                                   ("registrar", bool(out.get("registrar"))))
              if not present]
    if unread:
        out["unread"] = unread
    return out, ""


def probe(host: str) -> tuple[str, dict]:
    """Three outcomes, not two: resolves, does not resolve, WAS NOT MEASURED.

    `resolves: None` is the third, and it is what stops a failed probe from
    being read as a dark domain. Every consumer that asks `if not
    h.get("resolves")` would otherwise treat "we could not look" as "it serves
    nothing" — `tools/build_findings.py` did, at critical severity.
    """
    (ns, ns_err) = dig(host, "NS")
    (a, a_err) = dig(host, "A")
    (cname, cn_err) = dig(host, "CNAME")
    info: dict = {"checked_at": now(), "nameservers": ns, "a": a, "cname": cname,
                  "registrable": registrable(host)}

    # The A and CNAME lookups are what decide "resolves". If EITHER could not be
    # performed, the question was not answered — and an unanswered question is
    # not a negative answer.
    probe_err = a_err or cn_err
    if probe_err:
        info["resolves"] = None
        info["unmeasured"] = probe_err
        info["http"] = None
        return host, info

    resolves = bool(a or cname)
    info["resolves"] = resolves
    if resolves:
        status, http_err = http_status(host)
        info["http"] = None if http_err else status
        if http_err:
            info["http_unmeasured"] = http_err
    else:
        info["http"] = 0
        # Stated plainly: this is the state that makes a listed homepage a lie.
        # Only reached when the DNS actually answered.
        info["dark"] = True
    return host, info


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("domains"):
        print("domains: not configured (integration disabled)")
        return 0
    if len(argv) < 2:
        sys.exit(__doc__)
    dest = pathlib.Path(argv[1])
    only = set(argv[argv.index("--only") + 1:]) if "--only" in argv else None

    domains = [d["name"] for d in
               json.loads((paths.REGISTRY / "domains.json").read_text())["domains"]]
    sites = [s["host"] for p in
             json.loads((paths.REGISTRY / "projects.json").read_text())["projects"]
             for s in p.get("sites", [])]
    hosts = sorted({h for h in domains + sites if h and (only is None or h in only)})
    if not hosts:
        sys.exit("scan_domains: nothing to probe")

    # PREFLIGHT. A missing `dig` is one fact about this machine, not fifty-eight
    # facts about the estate — and writing 58 false rows over a good liveness
    # file is worse than not writing: the collectors' own rule is that missing
    # input must read as "not measured this run", never as "measured and empty".
    missing = [tool for tool in ("dig", "curl") if shutil.which(tool) is None]
    if missing:
        print(f"scan_domains: {', '.join(missing)} not on PATH — REFUSING to scan. "
              f"Every host would come back unmeasured, and overwriting "
              f"{dest.name} with that would turn a tooling gap into 58 claims "
              f"about the estate. The previous file is left untouched.",
              file=sys.stderr)
        return 1

    live: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for host, info in pool.map(probe, hosts):
            live[host] = info

    names = sorted({info["registrable"] for info in live.values()})
    whois: dict[str, dict] = {}
    degraded: list[dict] = []
    not_applicable: list[dict] = []
    bootstrap: tuple | None = None
    with cf.ThreadPoolExecutor(max_workers=RDAP_WORKERS) as pool:
        for name, (data, err) in zip(names, pool.map(rdap, names)):
            if err and data.get(NO_RECORD):
                if bootstrap is None:
                    bootstrap = rdap_bootstrap()
                hosts = [i for i in live.values() if i.get("registrable") == name]
                kind, row = classify_missing(name, err, *bootstrap, hosts)
                (not_applicable if kind == "not_applicable" else degraded).append(row)
            elif err:
                degraded.append({"source": f"rdap:{name}", "reason": err})
            else:
                whois[name] = data

    # A SUCCESSFUL FETCH THAT ANSWERED NOTHING is still a gap, and it used to
    # leave no trace: the record carried `statuses: []` and the hold check read
    # a clean bill. Grouped by the missing key rather than one row per domain —
    # a rename upstream affects every domain at once, and 49 identical lines
    # would bury the one case where a single TLD omits a field.
    by_key: dict[str, list[str]] = {}
    for name, rec in whois.items():
        for key in rec.get("unread") or []:
            by_key.setdefault(key, []).append(name)
    for key, names_ in sorted(by_key.items()):
        degraded.append({
            "source": f"rdap:{key}",
            "reason": (f"RDAP answered for {len(names_)} domain(s) without a "
                       f"`{key}`: {', '.join(sorted(names_)[:6])}"
                       + (f" and {len(names_) - 6} more" if len(names_) > 6 else "")
                       + f". Whatever reads `{key}` has nothing to read for them, "
                       f"and an empty answer there is not the same as a negative "
                       f"one")})

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"scanned_at": now(), "hosts": live, "rdap": whois,
                                "degraded": degraded, "not_applicable": not_applicable},
                               ensure_ascii=False, indent=1),
                    encoding="utf-8")

    dark = [h for h, i in live.items() if i.get("dark")]
    held = [n for n, w in whois.items()
            if any("hold" in s for s in w.get("statuses", []))]
    soon = [(n, w["expiration"]) for n, w in whois.items()
            if w.get("expiration") and w["expiration"] <= "2026-12-05"]
    print(f"probed {len(live)} host(s), {len(whois)} RDAP record(s) -> {dest}")
    print(f"  dark (no A and no CNAME): {len(dark)}" + (f" — {dark[:6]}" if dark else ""))
    print(f"  registrar hold:           {len(held)}" + (f" — {held[:6]}" if held else ""))
    print(f"  expiring within 90 days:  {len(soon)}" + (f" — {soon[:6]}" if soon else ""))
    for d in degraded[:6]:
        print(f"  degraded {d['source']}: {d['reason'][:90]}")
    for d in not_applicable[:6]:
        print(f"  not applicable {d['source']}: {d['reason'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
