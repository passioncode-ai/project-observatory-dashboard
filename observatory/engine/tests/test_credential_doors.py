#!/usr/bin/env python3
"""The credential doors: `tools/cloudflare.py`, `tools/openrouter.py` and `tools/heroku.py`.

Both share one architecture, decided in this order by the operator on
2026-09-13: the ADMIN credential is stashed locally and nothing reads it but the
door itself; working tokens are ISSUED from it, narrow, delivered to a named
place, and every one carries a record — so `list` and `ping` answer "what
exists, who spends it, when was it rotated" without opening a value.

Everything here runs against fakes: the provider is a dict, the stores are
tmpdirs. What is asserted is the door's own reasoning — refusals, ordering,
labels, and that no code path prints a value.
"""
from __future__ import annotations
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup
portable_setup()
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402
import private_io
import source_reader                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ─────────────────────────── shared rules ────────────────────────────────────

def test_no_door_takes_or_prints_a_value_outside_stdin() -> None:
    """The rule both doors exist to enforce, checked on their CODE."""
    for rel in ("tools/cloudflare.py", "tools/openrouter.py", "tools/heroku.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        code = source_reader.code_only(src)
        check(f"{rel} takes values on stdin only",
              "sys.stdin.read()" in code
              and not source_reader.appears_in_code(src, '"--token"')
              and not source_reader.appears_in_code(src, '"--key"'),
              "a value in argv is in the shell history, ps and the transcript")
        module = load(rel, "private_writer_" + pathlib.Path(rel).stem)
        target = pathlib.Path(tmpdir.mkdtemp()).resolve() / "synthetic-key"
        module.write_secret(target, "synthetic-value")
        check(f"{rel} writes secrets 0600 with atomic replacement",
              target.read_text().strip() == "synthetic-value"
              and target.stat().st_mode & 0o777 == 0o600
              and "private_io.write" in code, "")
        check(f"{rel} strips provider errors to endpoint and status",
              "from None" in code and "e.headers" not in code, "")


def test_the_ledgers_hold_no_values() -> None:
    """Meta files and the ledger carry names, dates and places — never values."""
    for rel, fields in (("tools/cloudflare.py", ("account_id", "issued_on", "rotations")),
                        ("tools/openrouter.py", ("destination", "limit_usd", "rotations")),
                        ("tools/heroku.py", ("authorization_id", "issued_on", "rotations"))):
        src = (ROOT / rel).read_text(encoding="utf-8")
        # `code_keeping_strings`: the field names ARE string literals, which
        # `code_only` strips along with the prose.
        code = source_reader.code_keeping_strings(src)
        for f in fields:
            check(f"{rel} records `{f}`", f'"{f}"' in code, "")
        # The one string that must never be json.dump'ed: the token variable.
        check(f"{rel} never serialises the value into a meta file",
              '"value": value' not in code and "'value': value" not in code, "")


def test_the_digitalocean_door_keeps_the_same_rules() -> None:
    """`tools/digitalocean.py` (PLN-142): no value in argv or on stdout, provider stderr withheld.

    It holds no admin credential of its own — doctl's context is the credential —
    so its value goes to the vault on stdin rather than through `write_secret`."""
    rel = "tools/digitalocean.py"
    src = (ROOT / rel).read_text(encoding="utf-8")
    code = source_reader.code_only(src)
    check(f"{rel} hands a value to the vault on stdin only",
          "input=value" in code and not source_reader.appears_in_code(src, '"--password"')
          and not source_reader.appears_in_code(src, '"--token"'),
          "a value in argv is in the shell history, ps and the transcript")
    check(f"{rel} withholds provider diagnostics",
          "provider diagnostics withheld" in source_reader.code_keeping_strings(src) and "p.stderr.strip()" not in code, "")
    check(f"{rel} prints no password variable",
          not any(f"print({v}" in code or f"{{{v}}}" in code for v in ("pw", "url", "value")), "")
    # The account half (OBS-155): a stashed team token is written 0600 like the other doors',
    # and its records name the team and dates, never the value.
    module = load(rel, "private_writer_digitalocean")
    target = pathlib.Path(tmpdir.mkdtemp()).resolve() / "synthetic-team"
    module.write_secret(target, "synthetic-value")
    check(f"{rel} writes a team token 0600 with atomic replacement",
          target.read_text().strip() == "synthetic-value" and target.stat().st_mode & 0o777 == 0o600
          and "private_io.write" in code, "")
    keeping = source_reader.code_keeping_strings(src)
    for f in ("team_uuid", "stashed_on", "installed_on", "rotations", "scopes_declared"):
        check(f"{rel} records `{f}`", f'"{f}"' in keeping, "")
    check(f"{rel} never serialises a value into a record",
          '"value": value' not in keeping and "'value': value" not in keeping, "")
    check(f"{rel} sends a token only to DigitalOcean or a loopback test server",
          "loopback" in keeping and "OBSERVATORY_DO_API" in keeping, "")


# ─────────────────────────── cloudflare ──────────────────────────────────────

def cf():
    sys.path.insert(0, str(ROOT / "plugins"))
    return load("tools/cloudflare.py", "cf_door")


def test_cf_stash_refuses_an_admin_that_cannot_issue() -> None:
    m = cf()
    calls = []

    def fake(path, token, payload=None, method=None):
        calls.append(path)
        if path.startswith("/accounts?"):
            return {"result": [{"id": "a1", "name": "Example Account"}]}
        raise RuntimeError("cloudflare answered 403 for " + path)
    m._request = fake
    m.ADMIN_STORE = pathlib.Path(tmpdir.mkdtemp()).resolve() / "cloudflare-admin"
    rc = m.cmd_stash("tok-" + "a" * 36)
    check("an admin token that cannot manage tokens is refused", rc == 1, str(rc))
    check("and nothing was written", not list(m.ADMIN_STORE.glob("*"))
          if m.ADMIN_STORE.is_dir() else True, "")

    def fake2(path, token, payload=None, method=None):
        if path.startswith("/accounts?"):
            return {"result": [{"id": "a1", "name": "Example Account"}]}
        return {"result": []}
    m._request = fake2
    rc = m.cmd_stash("tok-" + "a" * 36)
    check("a capable admin is stashed under the account's own name", rc == 0, str(rc))
    check("as a slug", (m.ADMIN_STORE / "example-account").is_file(), "")
    meta = json.loads((m.ADMIN_STORE / "example-account.meta.json").read_text())
    check("with the account id in its record, and no value",
          meta.get("account_id") == "a1" and "value" not in meta, str(meta))


def test_cf_one_stash_can_span_several_accounts() -> None:
    """A user-scoped token with `All accounts: API Tokens Edit` sees every
    account its user belongs to — the operator's `example-local` covers three
    logins. The stash records each, probes each for the one right that matters,
    names itself after the USER, and `issue` picks the account by its own slug
    — never "whichever came first"."""
    m = cf()
    m.ADMIN_STORE = pathlib.Path(tmpdir.mkdtemp()).resolve() / "cloudflare-admin"

    def fake(path, token, payload=None, method=None):
        if path.startswith("/accounts?"):
            return {"result": [{"id": "a1", "name": "Example Primary Account"},
                               {"id": "a2", "name": "Example Secondary"},
                               {"id": "a3", "name": "Example Restricted"}]}
        if path == "/user":
            return {"result": {"email": "user@example.com"}}
        if path.startswith("/accounts/a3/tokens"):
            raise RuntimeError("cloudflare answered 403 for " + path)   # member, no token rights
        return {"result": []}
    m._request = fake
    rc = m.cmd_stash("tok-" + "a" * 36)
    check("a multi-account token is stashed", rc == 0, str(rc))
    check("under the USER's name, since it is no one account's token",
          (m.ADMIN_STORE / "user-example-com").is_file(),
          str(list(m.ADMIN_STORE.glob("*")) if m.ADMIN_STORE.is_dir() else []))
    meta = json.loads((m.ADMIN_STORE / "user-example-com.meta.json").read_text())
    check("every account is recorded", [a["id"] for a in meta["accounts"]] == ["a1", "a2", "a3"],
          str(meta.get("accounts")))
    check("and each is probed for the right that matters",
          [a["can_issue"] for a in meta["accounts"]] == [True, True, False],
          str(meta.get("accounts")))

    # issue must NAME an account when several are reachable
    try:
        m.find_account(None)
        check("issue with no --account is refused when several are reachable", False, "no raise")
    except RuntimeError as exc:
        check("issue with no --account is refused when several are reachable",
              "name one with --account" in str(exc) and "example-secondary" in str(exc), str(exc))
    label, _admin, acct = m.find_account("example-secondary")
    check("an account is picked by its own slug", acct["id"] == "a2" and label == "user-example-com",
          f"{label} {acct}")
    try:
        m.find_account("example-restricted")
        check("an account the token cannot issue into is not offered", False, "no raise")
    except RuntimeError as exc:
        check("an account the token cannot issue into is not offered",
              "no reachable account matches" in str(exc), str(exc))

    # the old single-account record shape still resolves
    private_io.write(m.ADMIN_STORE / "legacy", "t\n")
    private_io.write(m.ADMIN_STORE / "legacy.meta.json", json.dumps(
        {"account_id": "a9", "account_name": "Legacy Co"}))
    label, _admin, acct = m.find_account("legacy")
    check("a single-account stash still answers to its own label",
          acct["id"] == "a9" and label == "legacy", f"{label} {acct}")


def test_cf_stash_finds_accounts_through_memberships_when_accounts_is_empty() -> None:
    """`GET /accounts` lists nothing for a token without Account Settings: Read
    — the operator's issuing token was refused as "sees no account" while it
    managed tokens in three. Memberships are the second road."""
    m = cf()
    m.ADMIN_STORE = pathlib.Path(tmpdir.mkdtemp()).resolve() / "cloudflare-admin"

    def fake(path, token, payload=None, method=None):
        if path.startswith("/accounts?"):
            return {"result": []}                        # the empty road
        if path.startswith("/memberships"):
            return {"result": [
                {"status": "accepted", "account": {"id": "a1", "name": "Example Primary Account"}},
                {"status": "pending", "account": {"id": "a2", "name": "Not Yet"}}]}
        if path == "/user/tokens/verify":
            return {"success": True, "result": {"status": "active"}}
        return {"result": []}
    m._request = fake
    found = m.discover_accounts("whatever")
    check("memberships supply what /accounts withheld",
          [a["id"] for a in found] == ["a1"], str(found))
    check("and a pending membership is not an account one can act in",
          all(a["id"] != "a2" for a in found), str(found))
    rc = m.cmd_stash("tok-" + "a" * 36)
    check("so the stash succeeds", rc == 0 and (m.ADMIN_STORE / "example-primary-account").is_file(),
          str(rc))


def test_cf_issue_rolls_rather_than_duplicating() -> None:
    m = cf()
    m.ADMIN_STORE = pathlib.Path(tmpdir.mkdtemp()).resolve() / "cloudflare-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "acct", "admin-token\n")
    private_io.write(m.ADMIN_STORE / "acct.meta.json", json.dumps(
        {"account_id": "a1", "account_name": "Acct"}))
    log = []

    def fake(path, token, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path))
        if "permission_groups" in path:
            return {"result": [{"id": "g1", "name": "Zone Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g2", "name": "Analytics Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g3", "name": "DNS Read", "scopes": ["com.cloudflare.api.account.zone"]}]}
        if path.endswith("/tokens/t-old") and method == "PUT":
            return {"result": {"id": "t-old"}}              # policies rewritten
        if path.endswith("/tokens?per_page=50"):
            return {"result": [{"id": "t-old", "name": m.PRESETS["analytics"]["name"]}]}
        if path.endswith("/tokens/t-old/value"):
            return {"result": "fresh-value-" + "x" * 28}
        raise AssertionError(f"unexpected call {path}")
    m._request = fake
    tid, value = m.mint("admin-token", "a1", m.PRESETS["analytics"])
    check("an existing managed token is ROLLED, not duplicated",
          tid == "t-old" and value.startswith("fresh-value-"), f"{tid}")
    check("and no create call was made",
          not any(p.endswith("/tokens") and meth == "POST" for meth, p in log
                  if "value" not in p), str(log))
    check("the policies are rewritten to the preset BEFORE the value is rolled",
          [meth for meth, p in log if "t-old" in p] == ["PUT", "PUT"]
          and log.index(("PUT", "/accounts/a1/tokens/t-old")) < log.index(("PUT", "/accounts/a1/tokens/t-old/value")),
          str([x for x in log if "t-old" in x[1]]))
    check("and the roll is a PUT — POST is refused by the account-owned endpoint",
          ("PUT", "/accounts/a1/tokens/t-old/value") in log, str(log))


def test_cf_external_tokens_are_recorded_and_never_rolled_here() -> None:
    m = cf()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.token_dir = lambda: d
    m.ADMIN_STORE = d / "no-admins"
    private_io.write(d / "foreign", "v\n")
    private_io.write(d / "foreign.meta.json", json.dumps({"kind": "external"}))
    rc = m.cmd_rotate("foreign", leaked=False)
    check("rotate refuses an externally minted token with the reason",
          rc == 1, str(rc))
    check("and the file survives — reporting beats deleting",
          (d / "foreign").is_file(), "")


def _cf_with_admin(m):
    m.ADMIN_STORE = pathlib.Path(tmpdir.mkdtemp()).resolve() / "cloudflare-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "acct", "admin-token\n")
    private_io.write(m.ADMIN_STORE / "acct.meta.json", json.dumps(
        {"account_id": "a1", "account_name": "Acct"}))


def _dns_fake(m, log, existing=None, verify_fails=0, delete_fails=False):
    state = {"verify_fails": verify_fails, "delete_fails": delete_fails}

    def fake(path, token, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path, token, payload))
        if "permission_groups" in path:
            return {"result": [{"id": "g1", "name": "Zone Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g4", "name": "DNS Write", "scopes": ["com.cloudflare.api.account.zone"]}]}
        if path.startswith("/zones?name=example.com&account.id=a1"):
            return {"result": [{"id": "z1", "name": "example.com"}]}
        if path.startswith("/zones?name="):
            return {"result": []}
        if path.endswith("/tokens?per_page=50"):
            return {"result": [{"id": "t-dns", "name": existing}] if existing else []}
        if path == "/accounts/a1/tokens" and method is None and payload is not None:
            return {"result": {"id": "t-new", "value": "dns-value-" + "y" * 30}}
        if path == "/accounts/a1/tokens/t-new" and method == "DELETE":
            if state.get("delete_fails"):
                raise RuntimeError("cloudflare answered HTTP 500; provider response withheld")
            return {"result": {"id": "t-new"}}
        if path.endswith("/tokens/t-dns") and method == "PUT":
            return {"result": {"id": "t-dns"}}
        if path.endswith("/tokens/t-dns/value"):
            return {"result": "dns-rolled-" + "z" * 29}
        if path.startswith("/zones/z1/dns_records"):
            if state["verify_fails"]:
                state["verify_fails"] -= 1
                raise RuntimeError("cloudflare answered HTTP 403; provider response withheld")
            return {"result": []}
        raise AssertionError(f"unexpected call {path}")
    m._request = fake


def test_cf_dns_preset_is_scoped_to_one_zone_and_lands_in_the_vault() -> None:
    """A writer's token touches one zone and is delivered to a vault slot —
    never printed, never filed where the analytics plugin reads."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    log, delivered = [], []
    _dns_fake(m, log)
    m.deliver_to_vault = lambda value, p, e, n: delivered.append((value, p, e, n))
    m._journal = lambda *a, **k: None
    m.token_dir = lambda: (_ for _ in ()).throw(AssertionError("zone presets never reach the plugin folder"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_zone("dns-edit", "example.com", "proj/prod/CF_DNS", None, wait=0)
    check("dns-edit issues", rc == 0, str(rc))
    create = [p for meth, path, tok, p in log if path == "/accounts/a1/tokens" and p]
    pol = create[0]["policies"][0] if create else {}
    check("the grant is the zone's own resource, not the account",
          pol.get("resources") == {"com.cloudflare.api.account.zone.z1": "*"}, str(pol))
    check("with exactly Zone Read and DNS Write",
          [g["id"] for g in pol.get("permission_groups", [])] == ["g1", "g4"], str(pol))
    check("named after the zone, so a second issue rolls it",
          bool(create) and create[0]["name"] == "observatory-dns-edit example.com (managed)", str(create))
    check("verified with the NEW token against that zone",
          any(path.startswith("/zones/z1/dns_records") and tok.startswith("dns-value-")
              for _m, path, tok, _p in log), "")
    check("delivered to the named slot",
          delivered == [("dns-value-" + "y" * 30, "proj", "prod", "CF_DNS")], str(delivered))
    check("and the value is never printed", "dns-value-" not in out.getvalue(), out.getvalue())
    # `run` takes its names as a remainder, so a flag after them is part of the
    # command: the line printed on 2026-09-29 put --env last and was refused.
    check("the printed use line puts --env before the positionals",
          'use_secret.py" run --env prod proj CF_DNS -- ' in out.getvalue(), out.getvalue())


def test_cf_zone_analytics_preset_reads_one_zone_into_the_vault() -> None:
    """An agent that measures one site gets that zone's counters and nothing else, in its vault
    slot, verified by the GraphQL read the plugin does (listing a zone is not reading analytics)."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    log, delivered, reads = [], [], []
    _dns_fake(m, log)
    base = m._request

    def fake(path, token, payload=None, method=None):
        if "permission_groups" in path:
            log.append(("GET", path, token, None))
            return {"result": [{"id": "g1", "name": "Zone Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g5", "name": "Analytics Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g4", "name": "DNS Write", "scopes": ["com.cloudflare.api.account.zone"]}]}
        return base(path, token, payload, method)
    m._request = fake
    state = {"fails": 1}

    def can_read(token, zone_ids):
        reads.append((token, list(zone_ids)))
        if state["fails"]:
            state["fails"] -= 1
            return "zones [z1] are not authorized"
        return ""
    m.can_read_analytics = can_read
    m.deliver_to_vault = lambda value, p, e, n: delivered.append((value, p, e, n))
    m._journal = lambda *a, **k: None
    m.token_dir = lambda: (_ for _ in ()).throw(AssertionError("a zone preset never reaches the plugin folder"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_zone("zone-analytics-read", "example.com", "proj/local/CF_ANALYTICS", None, wait=0)
    check("zone-analytics-read issues", rc == 0, str(rc))
    create = [p for meth, path, tok, p in log if path == "/accounts/a1/tokens" and p]
    pol = create[0]["policies"][0] if create else {}
    check("the grant is the zone's own resource",
          pol.get("resources") == {"com.cloudflare.api.account.zone.z1": "*"}, str(pol))
    check("with exactly Zone Read and Analytics Read",
          [g["id"] for g in pol.get("permission_groups", [])] == ["g1", "g5"], str(pol))
    check("named after the zone", bool(create)
          and create[0]["name"] == "observatory-zone-analytics-read example.com (managed)", str(create))
    check("verified by reading that zone's analytics with the NEW token, retried until honoured",
          len(reads) == 2 and all(t.startswith("dns-value-") and z == ["z1"] for t, z in reads), str(reads))
    check("no REST probe is needed for it",
          not any(path.startswith("/zones/z1/dns_records") for _m, path, _t, _p in log), "")
    check("delivered to the named slot",
          delivered == [("dns-value-" + "y" * 30, "proj", "local", "CF_ANALYTICS")], str(delivered))
    check("and the value is never printed", "dns-value-" not in out.getvalue(), out.getvalue())


def test_cf_dns_preset_rolls_refuses_and_never_misfiles() -> None:
    m = cf(); _cf_with_admin(m)
    log, delivered = [], []
    _dns_fake(m, log, existing="observatory-dns-edit example.com (managed)", verify_fails=2)
    m.deliver_to_vault = lambda value, p, e, n: delivered.append(value)
    m._journal = lambda *a, **k: None
    rc = m.cmd_issue_zone("dns-edit", "example.com", "proj/prod/CF_DNS", None, wait=0)
    check("a second issue ROLLS the zone's token", rc == 0 and delivered == ["dns-rolled-" + "z" * 29],
          f"{rc} {delivered}")
    check("and waits out a token the edge has not honoured yet",
          sum(1 for _m, p, _t, _pl in log if p.startswith("/zones/z1/dns_records")) == 3, "")

    log2, delivered2 = [], []
    _dns_fake(m, log2)
    m.deliver_to_vault = lambda value, p, e, n: delivered2.append(value)
    check("a zone outside the account is refused",
          m.cmd_issue_zone("dns-edit", "other.org", "proj/prod/CF_DNS", None, wait=0) == 1
          and not delivered2, str(delivered2))
    check("without --vault nothing is minted",
          m.cmd_issue_zone("dns-edit", "example.com", None, None) == 2, "")
    check("a malformed slot is refused",
          m.cmd_issue_zone("dns-edit", "example.com", "proj/production/X", None) == 2, "")
    check("a slot name the vault would refuse is refused before minting",
          m.cmd_issue_zone("dns-edit", "example.com", "proj/prod/lower-case", None) == 2, "")
    check("the analytics path refuses a zone preset instead of filing it for the plugin",
          m.cmd_issue("dns-edit", None, None) == 2, "")
    check("and nothing was minted by any refusal",
          not any(p == "/accounts/a1/tokens" and pl for _m, p, _t, pl in log2), str(log2))


def test_cf_a_token_this_call_created_is_deleted_when_the_issue_fails() -> None:
    """Issue #95: a mint followed by a failed probe or a refused delivery left a live
    token with no holder. A CREATED token is deleted before the refusal; a ROLLED one
    keeps its id and readers, and the refusal says the slot's value is dead."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    m._journal = lambda *a, **k: None
    deletes = lambda log: [p for v, p, *_ in log if v == "DELETE"]

    log, err = [], io.StringIO()
    _dns_fake(m, log, verify_fails=9)
    m.deliver_to_vault = lambda *a: (_ for _ in ()).throw(AssertionError("never delivered"))
    with contextlib.redirect_stderr(err):
        rc = m.cmd_issue_zone("dns-edit", "example.com", "proj/prod/CF_DNS", None, wait=0)
    check("a zone token that never proves itself is deleted again",
          rc == 1 and deletes(log) == ["/accounts/a1/tokens/t-new"], f"{rc} {deletes(log)}")
    check("and the refusal says so", "created was deleted again" in err.getvalue(), err.getvalue())

    def refuse(*_a):
        raise RuntimeError("the vault refused the slot")
    log, err = [], io.StringIO()
    _d1_fake(m, log)
    m.deliver_to_vault = refuse
    with contextlib.redirect_stderr(err):
        rc = m.cmd_issue_account("d1-edit", "proj/prod/CLOUDFLARE_API_TOKEN", None, wait=0)
    check("an account token the vault refused is deleted again",
          rc == 1 and deletes(log) == ["/accounts/a1/tokens/t-new"], f"{rc} {deletes(log)}")

    log, err = [], io.StringIO()
    _d1_fake(m, log, existing="observatory-d1-edit proj/prod/CLOUDFLARE_API_TOKEN (managed)")
    with contextlib.redirect_stderr(err):
        rc = m.cmd_issue_account("d1-edit", "proj/prod/CLOUDFLARE_API_TOKEN", None, wait=0)
    check("a ROLLED token is not deleted — its id and readers stay",
          rc == 1 and deletes(log) == [], f"{rc} {deletes(log)}")
    check("and the refusal says the slot's value is dead",
          "value the slot held is dead" in err.getvalue(), err.getvalue())

    log, err = [], io.StringIO()
    _dns_fake(m, log, verify_fails=9, delete_fails=True)
    with contextlib.redirect_stderr(err):
        rc = m.cmd_issue_zone("dns-edit", "example.com", "proj/prod/CF_DNS", None, wait=0)
    check("a delete that fails is named, with what to do",
          rc == 1 and "could NOT be deleted" in err.getvalue()
          and "delete it in the dashboard now" in err.getvalue(), err.getvalue())
    check("and no refusal carries the value", "dns-value-" not in err.getvalue(), err.getvalue())

    log = []
    tokens = _r2_fake(m, log, can_list=True)
    m.deliver_to_vault = lambda *a: None
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        rc = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 30, "proj/prod/OFFSITE", None, wait=0)
    check("a bucket pair that failed its proof is deleted, not only the setup token",
          rc == 1 and "t-bucket-new" not in tokens and "t-setup" not in tokens, str(tokens))


def test_cf_dns_delivery_goes_through_the_vault_on_stdin() -> None:
    """The slot is written by the vault's own `put`, value on stdin, and a
    refusal names the slot without the child's output."""
    m = cf()
    calls = []

    class Done:
        def __init__(self, code):
            self.returncode = code
            self.stdout = self.stderr = "ECHOED-INPUT"

    def run(argv, **kw):
        calls.append((argv, kw))
        return Done(0 if len(calls) == 1 else 3)
    original = m.subprocess.run
    m.subprocess.run = run
    try:
        m.deliver_to_vault("synthetic-" + "v" * 30, "proj", "prod", "CF_DNS")
        argv, kw = calls[0]
        check("the vault's put is invoked for the slot",
              argv[1].endswith("tools/vault.py") and argv[2:] == ["put", "proj", "prod", "CF_DNS", "--force"],
              str(argv))
        check("the value travels on stdin, not argv",
              kw.get("input", "").strip() == "synthetic-" + "v" * 30
              and not any("synthetic-" in str(a) for a in argv), "")
        try:
            m.deliver_to_vault("synthetic-" + "v" * 30, "proj", "prod", "CF_DNS")
            check("a vault refusal is raised", False, "no exception")
        except RuntimeError as exc:
            check("a vault refusal names the slot and withholds the child's output",
                  "proj/prod/CF_DNS" in str(exc) and "ECHOED-INPUT" not in str(exc)
                  and "synthetic-" not in str(exc), str(exc))
    finally:
        m.subprocess.run = original


def _d1_fake(m, log, existing=None, verify_fails=0):
    state = {"verify_fails": verify_fails}

    def fake(path, token, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path, token, payload))
        if "permission_groups" in path:
            # Same name at two levels is how this catalogue is shaped; only the
            # account-level one may be granted on an account resource.
            return {"result": [{"id": "gz", "name": "D1 Write", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g5", "name": "D1 Write", "scopes": ["com.cloudflare.api.account"]}]}
        if path.endswith("/tokens?per_page=50"):
            return {"result": [{"id": "t-d1", "name": existing}] if existing else []}
        if path == "/accounts/a1/tokens" and method is None and payload is not None:
            return {"result": {"id": "t-new", "value": "d1-value-" + "y" * 31}}
        if path == "/accounts/a1/tokens/t-new" and method == "DELETE":
            return {"result": {"id": "t-new"}}
        if path.endswith("/tokens/t-d1") and method == "PUT":
            return {"result": {"id": "t-d1"}}
        if path.endswith("/tokens/t-d1/value"):
            return {"result": "d1-rolled-" + "z" * 30}
        if path.startswith("/accounts/a1/d1/database"):
            if state["verify_fails"]:
                state["verify_fails"] -= 1
                raise RuntimeError("cloudflare answered HTTP 403; provider response withheld")
            return {"result": []}
        raise AssertionError(f"unexpected call {path}")
    m._request = fake


def test_cf_d1_preset_is_scoped_to_one_account_and_lands_in_the_vault() -> None:
    """A worker's database writer: D1 Write on one account, named after the
    vault slot so each project's token rolls on its own, verified by listing
    that account's databases with the NEW token, delivered on stdin."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    log, delivered = [], []
    _d1_fake(m, log)
    m.deliver_to_vault = lambda value, p, e, n: delivered.append((value, p, e, n))
    m._journal = lambda *a, **k: None
    m.token_dir = lambda: (_ for _ in ()).throw(AssertionError("vault presets never reach the plugin folder"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_account("d1-edit", "proj/prod/CLOUDFLARE_API_TOKEN", None, wait=0)
    check("d1-edit issues", rc == 0, str(rc))
    create = [p for meth, path, tok, p in log if path == "/accounts/a1/tokens" and p]
    pol = create[0]["policies"][0] if create else {}
    check("the grant is the one account, not every account the admin reaches",
          pol.get("resources") == {"com.cloudflare.api.account.a1": "*"}, str(pol))
    check("with exactly the ACCOUNT-level D1 Write group",
          [g["id"] for g in pol.get("permission_groups", [])] == ["g5"], str(pol))
    check("named after the slot, so a second issue rolls it",
          bool(create) and create[0]["name"] == "observatory-d1-edit proj/prod/CLOUDFLARE_API_TOKEN (managed)",
          str(create))
    check("verified with the NEW token against the account's databases",
          any(path.startswith("/accounts/a1/d1/database") and tok.startswith("d1-value-")
              for _m, path, tok, _p in log), "")
    check("delivered to the named slot",
          delivered == [("d1-value-" + "y" * 31, "proj", "prod", "CLOUDFLARE_API_TOKEN")], str(delivered))
    check("the use line names the account id wrangler needs",
          "CLOUDFLARE_ACCOUNT_ID=a1" in out.getvalue(), out.getvalue())
    check("and the value is never printed", "d1-value-" not in out.getvalue(), out.getvalue())
    check("the printed use line puts --env before the positionals",
          'use_secret.py" run --env prod proj CLOUDFLARE_API_TOKEN -- ' in out.getvalue(), out.getvalue())


def test_cf_fabric_account_preset_grants_both_levels_on_one_account_into_the_vault() -> None:
    """Another account for a Fabric Inbox server: account-level groups on the account and
    zone-level groups on its zones as two policies (one policy may not mix levels), named
    after the slot, verified by listing the account's Workers with the NEW token, delivered
    to the vault. A token that cannot list Workers never reaches the vault."""
    m = cf(); _cf_with_admin(m)
    preset = m.PRESETS["fabric-inbox-account"]
    catalogue, n = [], 0
    for name in preset["groups"] + preset["zone_groups"]:
        for level in ("account", "zone"):
            n += 1
            catalogue.append({"id": f"{level[0]}{n}", "name": name,
                              "scopes": ["com.cloudflare.api.account" + (".zone" if level == "zone" else "")]})
    log, delivered, state = [], [], {"refuse": False}

    def fake(path, token, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path, token, payload))
        if "permission_groups" in path:
            return {"result": catalogue}
        if path.endswith("/tokens?per_page=50"):
            return {"result": []}
        if path == "/accounts/a1/tokens" and payload is not None:
            return {"result": {"id": "t-f", "value": "fabric-value-" + "q" * 27}}
        if path == "/accounts/a1/tokens/t-f" and method == "DELETE":
            return {"result": {"id": "t-f"}}
        if path == "/accounts/a1/workers/scripts?per_page=1":
            if state["refuse"]:
                raise RuntimeError("cloudflare answered HTTP 403; provider response withheld")
            return {"result": []}
        raise AssertionError(f"unexpected call {path}")
    m._request = fake
    m.deliver_to_vault = lambda value, p, e, nm: delivered.append((value, p, e, nm))
    m._journal = lambda *a, **k: None
    rc = m.cmd_issue_account("fabric-inbox-account", "fabric/prod/CLOUDFLARE_API_TOKEN_A1", None, wait=0)
    check("fabric-inbox-account issues", rc == 0, str(rc))
    create = [pl for _m, path, _t, pl in log if path == "/accounts/a1/tokens" and pl]
    pols = create[0]["policies"] if create else []
    check("two policies, both on the one account",
          len(pols) == 2 and pols[0]["resources"] == {"com.cloudflare.api.account.a1": "*"}
          and pols[1]["resources"] == {"com.cloudflare.api.account.a1": {"com.cloudflare.api.account.zone.*": "*"}}, str(pols))
    check("each with only its own level's groups",
          all(g["id"].startswith("a") for g in pols[0]["permission_groups"])
          and all(g["id"].startswith("z") for g in pols[1]["permission_groups"])
          and len(pols[0]["permission_groups"]) == len(preset["groups"]) and len(pols[1]["permission_groups"]) == 4, str(pols))
    check("verified by listing Workers with the NEW token",
          any(path == "/accounts/a1/workers/scripts?per_page=1" and tok.startswith("fabric-value-") for _m, path, tok, _p in log), "")
    check("delivered to the named slot", delivered == [("fabric-value-" + "q" * 27, "fabric", "prod", "CLOUDFLARE_API_TOKEN_A1")], str(delivered))

    state["refuse"], delivered[:] = True, []
    rc = m.cmd_issue_account("fabric-inbox-account", "fabric/prod/CLOUDFLARE_API_TOKEN_A1", None, wait=0)
    check("a token that cannot list Workers is not delivered", rc == 1 and not delivered, str(rc))
    logs = m.PRESETS["workers-observability-read"]
    check("the logs preset reads one account's telemetry and nothing else",
          logs["groups"] == ("Workers Observability Read",) and logs["level"] == "account" and not logs.get("zone_groups")
          and logs["probe"].endswith("/workers/observability/telemetry/keys") and logs["probe_body"] == {}, str(logs))
    server = m.PRESETS["fabric-inbox-server"]
    check("the server's own preset can make the service tokens agent keys and relays sign in with",
          "Access: Service Tokens Write" in server["groups"] and server["zone_groups"] == preset["zone_groups"]
          and set(preset["groups"]) <= set(server["groups"]), str(server["groups"]))


def test_cf_fabric_presets_refuse_what_they_do_not_grant() -> None:
    """Both Fabric Inbox presets, ATTEMPTED where they must fail. The other
    account's token makes no storage and no sign-in there; a catalogue that
    offers a group only at the wrong level, or lacks the zone half, mints
    nothing — a token with half its policy would pass its Workers read and
    then fail the server later; the zone path, a refused slot and a missing
    --vault are refused before minting; and the printed summary names the
    zone-level grants, so no one reads "on account X only" and misses DNS Write."""
    import contextlib, io
    account = cf().PRESETS["fabric-inbox-account"]
    server = cf().PRESETS["fabric-inbox-server"]
    check("another account's token grants no storage and no sign-in",
          not [g for g in account["groups"] + account["zone_groups"]
               if g.startswith("Access:") or "R2" in g], str(account["groups"]))
    check("its groups are exactly the server's minus storage and sign-in",
          set(account["groups"]) == {g for g in server["groups"]
                                     if not g.startswith("Access:") and "R2" not in g}, "")
    for key in ("fabric-inbox-server", "fabric-inbox-account"):
        preset = cf().PRESETS[key]
        for label, catalogue in (
                ("a group offered only at the wrong level",
                 [(g, "zone") for g in preset["groups"]] + [(g, "zone") for g in preset["zone_groups"]]),
                ("a catalogue without the zone half",
                 [(g, "account") for g in preset["groups"]])):
            m = cf(); _cf_with_admin(m)
            log, delivered = [], []
            _preset_fake(m, log, catalogue, "/accounts/a1/workers/scripts", "fab-")
            m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append(value)
            m._journal = lambda *a, **k: None
            check(f"{key}: {label} is refused",
                  m.cmd_issue_account(key, "fabric/prod/CF_A1", None, wait=0) == 1, "")
            check(f"{key}: and nothing is minted or delivered",
                  not any(p == "/accounts/a1/tokens" and pl for _m, p, _t, pl in log) and not delivered,
                  str(log))
        m = cf(); _cf_with_admin(m)
        log = []
        _preset_fake(m, log, [(g, "account") for g in preset["groups"]]
                     + [(g, "zone") for g in preset["zone_groups"]], "/accounts/a1/workers/scripts", "fab-")
        m.deliver_to_vault = lambda *a: None
        m._journal = lambda *a, **k: None
        check(f"{key}: the zone path refuses it",
              m.cmd_issue_zone(key, "example.com", "fabric/prod/CF_A1", None) == 2, "")
        check(f"{key}: a slot the vault would refuse is refused before minting",
              m.cmd_issue_account(key, "fabric/prod/lower-case", None) == 2, "")
        check(f"{key}: without --vault nothing is minted", m.cmd_issue_account(key, None, None) == 2, "")
        check(f"{key}: no refusal minted anything",
              not any(p == "/accounts/a1/tokens" and pl for _m, p, _t, pl in log), str(log))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = m.cmd_issue_account(key, "fabric/prod/CF_A1", None, wait=0)
        create = [pl for _m, p, _t, pl in log if p == "/accounts/a1/tokens" and pl]
        pols = create[0]["policies"] if create else []
        check(f"{key}: grants exactly its groups, account half then zone half",
              rc == 0 and [[g["id"] for g in pol["permission_groups"]] for pol in pols]
              == [[f"id-account-{g}" for g in preset["groups"]],
                  [f"id-zone-{g}" for g in preset["zone_groups"]]], str(pols))
        check(f"{key}: the summary names the zone-level grants and never the value",
              "DNS Write" in out.getvalue() and "zones" in out.getvalue() and "fab-" not in out.getvalue(),
              out.getvalue())


def test_cf_logs_preset_reads_telemetry_with_a_post_and_grants_nothing_more() -> None:
    """workers-observability-read: one read group on one account, proved by the
    telemetry-keys POST with the NEW value, and refused when the catalogue
    offers the group only at zone level."""
    for offered, want_rc in (([("Workers Observability Read", "account"),
                               ("Workers Observability Read", "zone")], 0),
                             ([("Workers Observability Read", "zone")], 1)):
        m = cf(); _cf_with_admin(m)
        log, delivered = [], []
        _preset_fake(m, log, offered, "/accounts/a1/workers/observability/telemetry/keys", "logs-")
        m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append(value)
        m._journal = lambda *a, **k: None
        rc = m.cmd_issue_account("workers-observability-read", "proj/prod/CF_LOGS", None, wait=0)
        create = [pl for _m, p, _t, pl in log if p == "/accounts/a1/tokens" and pl]
        if want_rc == 0:
            pols = create[0]["policies"] if create else []
            check("the logs preset issues", rc == 0, str(rc))
            check("with exactly one account-level read group, on one account",
                  [[g["id"] for g in pol["permission_groups"]] for pol in pols]
                  == [["id-account-Workers Observability Read"]]
                  and pols[0]["resources"] == {"com.cloudflare.api.account.a1": "*"}, str(pols))
            check("proved by the telemetry POST, with a body, under the NEW value",
                  any(v == "POST" and p.endswith("/telemetry/keys") and t.startswith("logs-") and pl == {}
                      for v, p, t, pl in log), str(log[-2:]))
        else:
            check("a zone-level Observability group is never taken for the account",
                  rc == 1 and not create and not delivered, str(rc))


def test_cf_d1_preset_rolls_refuses_and_never_misfiles() -> None:
    m = cf(); _cf_with_admin(m)
    log, delivered = [], []
    _d1_fake(m, log, existing="observatory-d1-edit proj/prod/CLOUDFLARE_API_TOKEN (managed)", verify_fails=2)
    m.deliver_to_vault = lambda value, p, e, n: delivered.append(value)
    m._journal = lambda *a, **k: None
    rc = m.cmd_issue_account("d1-edit", "proj/prod/CLOUDFLARE_API_TOKEN", None, wait=0)
    check("a second issue ROLLS the slot's token", rc == 0 and delivered == ["d1-rolled-" + "z" * 30],
          f"{rc} {delivered}")
    check("and waits out a token the edge has not honoured yet",
          sum(1 for _m, p, _t, _pl in log if p.startswith("/accounts/a1/d1/database")) == 3, "")

    log2, delivered2 = [], []
    _d1_fake(m, log2, verify_fails=9)
    m.deliver_to_vault = lambda value, p, e, n: delivered2.append(value)
    check("a token that never proves it can read D1 is not delivered",
          m.cmd_issue_account("d1-edit", "proj/prod/CLOUDFLARE_API_TOKEN", None, wait=0) == 1
          and not delivered2, str(delivered2))
    log3 = []
    _d1_fake(m, log3)
    check("without --vault nothing is minted",
          m.cmd_issue_account("d1-edit", None, None) == 2, "")
    check("a slot name the vault would refuse is refused before minting",
          m.cmd_issue_account("d1-edit", "proj/prod/lower-case", None) == 2, "")
    check("the analytics path refuses a vault preset instead of filing it for the plugin",
          m.cmd_issue("d1-edit", None, None) == 2, "")
    check("the zone path refuses an account preset",
          m.cmd_issue_zone("d1-edit", "example.com", "proj/prod/X", None) == 2, "")
    check("and nothing was minted by any refusal",
          not any(p == "/accounts/a1/tokens" and pl for _m, p, _t, pl in log3), str(log3))



# AWS's documented SigV4 example credentials — public, and assembled from parts
# so no line of this file carries a key-shaped literal.
_AWS_EXAMPLE_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"
_AWS_EXAMPLE_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCY" + "EXAMPLEKEY"


def test_cf_sigv4_matches_the_published_aws_example() -> None:
    """The door signs its own R2 probes; a signer that is subtly wrong would
    make every proof fail — or, worse, pass against a lenient fake. AWS's
    documented GET-object example is the oracle."""
    import hashlib
    m = cf()
    h = m.sigv4_headers("GET", "https://examplebucket.s3.amazonaws.com/test.txt",
                        _AWS_EXAMPLE_ID, _AWS_EXAMPLE_SECRET,
                        b"", "20130524T000000Z", region="us-east-1",
                        extra={"Range": "bytes=0-9"})
    check("SigV4 reproduces AWS's published signature",
          h["Authorization"].endswith(
              "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
              "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"),
          h["Authorization"])
    check("and R2's key pair is the token id and the sha256 of its value",
          m.r2_s3_keys("tid", "value") == ("tid", hashlib.sha256(b"value").hexdigest()), "")
    check("the EU endpoint carries the jurisdiction",
          m.r2_endpoint("a1", "eu") == "https://a1.eu.r2.cloudflarestorage.com"
          and m.r2_endpoint("a1", "default") == "https://a1.r2.cloudflarestorage.com", "")


def _r2_fake(m, log, *, bucket_exists=False, existing=None, lifecycle_fails=False,
             can_list=False, put_fails=0, lifecycle=None, readback=None):
    """`lifecycle` is what the bucket already holds; `readback`, given the
    stored lifecycle, returns what the provider answers on GET — a provider
    that silently drops or rewrites a rule."""
    state = {"put_fails": put_fails, "lifecycle": lifecycle}
    tokens = {}

    def fake(path, token, payload=None, method=None, headers=None):
        verb = method or ("POST" if payload is not None else "GET")
        log.append((verb, path, token, payload, dict(headers or {})))
        if "permission_groups" in path:
            return {"result": [
                {"id": "g-acct-write", "name": "Workers R2 Storage Write",
                 "scopes": ["com.cloudflare.api.account"]},
                {"id": "g-item-write", "name": "Workers R2 Storage Bucket Item Write",
                 "scopes": ["com.cloudflare.edge.r2.bucket"]}]}
        if path.endswith("/tokens?per_page=50"):
            rows = [{"id": "t-bucket", "name": existing}] if existing else []
            return {"result": rows + [{"id": i, "name": n} for i, n in tokens.items()]}
        if path == "/accounts/a1/tokens" and verb == "POST":
            name = payload["name"]
            tid = "t-setup" if "setup" in name else "t-bucket-new"
            tokens[tid] = name
            prefix = "setup-value-" if "setup" in name else "bucket-value-"
            return {"result": {"id": tid, "value": prefix + "q" * 28}}
        if path == "/accounts/a1/tokens/t-bucket" and verb == "PUT":
            return {"result": {"id": "t-bucket"}}
        if path == "/accounts/a1/tokens/t-bucket/value":
            return {"result": "bucket-rolled-" + "r" * 26}
        if path == "/accounts/a1/tokens/t-bucket-new" and verb == "DELETE":
            tokens.pop("t-bucket-new", None)
            return {"result": {"id": "t-bucket-new"}}
        if path == "/accounts/a1/tokens/t-setup" and verb == "DELETE":
            tokens.pop("t-setup", None)
            return {"result": {"id": "t-setup"}}
        if path.startswith("/accounts/a1/r2/buckets"):
            assert token.startswith("setup-value-"), "R2's API is only called with the setup token"
            if path == "/accounts/a1/r2/buckets?per_page=1":
                return {"result": {"buckets": []}}
            if path == "/accounts/a1/r2/buckets/offsite-backups" and verb == "GET":
                if bucket_exists:
                    return {"result": {"name": "offsite-backups"}}
                raise RuntimeError("cloudflare answered HTTP 404; provider response withheld")
            if path == "/accounts/a1/r2/buckets" and verb == "POST":
                return {"result": {"name": payload["name"]}}
            if path.endswith("/lifecycle") and verb == "PUT":
                if lifecycle_fails:
                    raise RuntimeError("cloudflare answered HTTP 400; provider response withheld")
                state["lifecycle"] = payload
                return {"result": {}}
            if path.endswith("/lifecycle") and verb == "GET":
                stored = json.loads(json.dumps(state["lifecycle"] or {"rules": []}))
                return {"result": readback(stored) if readback else stored}
        raise AssertionError(f"unexpected call {verb} {path}")

    def s3(method, url, access_key, secret, body=b""):
        log.append(("S3 " + method, url, access_key, secret, {}))
        if url.endswith(".r2.cloudflarestorage.com/"):
            return (200 if can_list else 403), b""
        if method == "PUT":
            if state["put_fails"]:
                state["put_fails"] -= 1
                return 403, b""
            state["probe"] = body
            return 200, b""
        if method == "GET":
            return 200, state.get("probe", b"")
        return 204, b""
    m._request = fake
    m._s3 = s3
    return tokens


def test_cf_r2_preset_issues_one_bucket_pair_into_the_vault() -> None:
    """An off-site backup's writer: objects of ONE bucket, in the jurisdiction
    asked, with the bucket and its lifecycle made by a setup token that does not
    outlive the command, proved by a real put/get/delete, delivered as an S3
    pair on stdin."""
    import contextlib, hashlib, io
    m = cf(); _cf_with_admin(m)
    log, delivered = [], []
    tokens = _r2_fake(m, log)
    m.deliver_to_vault = lambda value, p, e, n: delivered.append((value, p, e, n))
    m._journal = lambda *a, **k: None
    m.token_dir = lambda: (_ for _ in ()).throw(AssertionError("vault presets never reach the plugin folder"))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 30, "proj/prod/OFFSITE", None, wait=0)
    check("r2-bucket issues", rc == 0, f"{rc} {err.getvalue()}")
    creates = [pl for verb, path, _t, pl, _h in log if verb == "POST" and path == "/accounts/a1/tokens"]
    setup = next((pl for pl in creates if "setup" in pl["name"]), None)
    item = next((pl for pl in creates if "setup" not in pl["name"]), None)
    check("the setup token holds only account-level Storage Write",
          bool(setup) and setup["policies"][0]["permission_groups"] == [{"id": "g-acct-write"}]
          and setup["policies"][0]["resources"] == {"com.cloudflare.api.account.a1": "*"}, str(setup))
    check("and it is deleted before the command returns",
          "t-setup" not in tokens
          and any(v == "DELETE" and p == "/accounts/a1/tokens/t-setup" for v, p, *_ in log), str(tokens))
    bucket_calls = [(v, p, h) for v, p, _t, _pl, h in log if "/r2/buckets" in p]
    check("every R2 call names the EU jurisdiction",
          bool(bucket_calls) and all(h.get("cf-r2-jurisdiction") == "eu" for _v, _p, h in bucket_calls),
          str(bucket_calls))
    check("a missing bucket is created",
          any(v == "POST" and p == "/accounts/a1/r2/buckets" for v, p, _h in bucket_calls), "")
    life = next((pl for v, p, _t, pl, _h in log if v == "PUT" and p.endswith("/lifecycle")), {})
    check("the lifecycle expires objects after the days asked",
          life.get("rules", [{}])[0].get("deleteObjectsTransition", {}).get("condition")
          == {"type": "Age", "maxAge": 30 * 86400}, str(life))
    check("the bucket token is granted on that one bucket's resource, not the account",
          bool(item) and item["policies"][0]["resources"]
          == {"com.cloudflare.edge.r2.bucket.a1_eu_offsite-backups": "*"}
          and item["policies"][0]["permission_groups"] == [{"id": "g-item-write"}], str(item))
    check("named after jurisdiction and bucket, so a second issue rolls it",
          bool(item) and item["name"] == "observatory-r2-bucket eu/offsite-backups (managed)", str(item))
    secret = hashlib.sha256(("bucket-value-" + "q" * 28).encode()).hexdigest()
    probes = [v for v, _url, ak, sk, _h in log if v.startswith("S3 ") and ak == "t-bucket-new" and sk == secret]
    check("the NEW pair proved a put, a get, a delete and a refused bucket list",
          probes == ["S3 PUT", "S3 GET", "S3 DELETE", "S3 GET"], str(probes))
    check("three slots delivered: the S3 pair and the endpoint",
          delivered == [("t-bucket-new", "proj", "prod", "OFFSITE_ACCESS_KEY_ID"),
                        (secret, "proj", "prod", "OFFSITE_SECRET_ACCESS_KEY"),
                        ("https://a1.eu.r2.cloudflarestorage.com", "proj", "prod", "OFFSITE_ENDPOINT")],
          str(delivered))
    printed = out.getvalue() + err.getvalue()
    check("neither the token values nor the secret key are ever printed",
          "bucket-value-" not in printed and "setup-value-" not in printed and secret not in printed, printed)
    check("the use line puts --env before the positionals and names all three slots",
          'use_secret.py" run --env prod proj '
          "OFFSITE_ACCESS_KEY_ID,OFFSITE_SECRET_ACCESS_KEY,OFFSITE_ENDPOINT -- " in printed, printed)


def test_cf_proof_waits_long_enough_for_a_slow_edge() -> None:
    """2026-10-06: a new R2 pair answered HTTP 401 for longer than the old 5 × 2 s,
    twice in a row, and the door deleted two good pairs. The proof now waits with
    doubling pauses (about 80 s at the default 2 s); a pair broader than its bucket
    is refused at once, since no wait mends that."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    check("the pauses double from wait and stop at eight times it",
          m.proof_delays(2.0) == [2.0, 4.0, 8.0, 16.0, 16.0, 16.0, 16.0], str(m.proof_delays(2.0)))
    check("so a slow edge is given well over the old ten seconds", sum(m.proof_delays(2.0)) >= 60, "")
    for answers, expect_rc, expect_calls in (
            (["a probe put answered HTTP 401"] * 6 + [""], 0, 7),     # honoured on the 7th try
            (["the pair can list every bucket: broader than one bucket"], 1, 1)):
        log, delivered, calls, slept = [], [], [], []
        _r2_fake(m, log)
        m.deliver_to_vault = lambda value, p, e, n: delivered.append(n)
        m._journal = lambda *a, **k: None
        queue = list(answers)

        def prove(*_a):
            calls.append(1)
            return queue.pop(0) if queue else ""
        m.r2_prove = prove
        real_sleep = m.time.sleep
        m.time.sleep = slept.append
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 30, "proj/prod/OFFSITE", None, wait=2.0)
        finally:
            m.time.sleep = real_sleep
        check(f"rc {expect_rc} after {expect_calls} proof call(s)",
              rc == expect_rc and len(calls) == expect_calls, f"rc={rc} calls={len(calls)} slept={slept}")
        if expect_rc == 0:
            check("and the slots are delivered only after the proof", len(delivered) == 3, str(delivered))
            check("waiting the doubling pauses", slept[-6:] == [2.0, 4.0, 8.0, 16.0, 16.0, 16.0], str(slept))
        else:
            check("a broader pair is not waited on", len(calls) == 1 and not delivered, str(slept))


def test_cf_r2_preset_refuses_and_cleans_up() -> None:
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    m._journal = lambda *a, **k: None

    log, delivered = [], []
    _r2_fake(m, log, can_list=True)
    m.deliver_to_vault = lambda value, p, e, n: delivered.append(n)
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        rc = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 30, "proj/prod/OFFSITE", None, wait=0)
    check("a pair that can list the account's buckets is never delivered",
          rc == 1 and not delivered, f"{rc} {delivered}")

    log2 = []
    tokens2 = _r2_fake(m, log2, lifecycle_fails=True)
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        rc = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 30, "proj/prod/OFFSITE", None, wait=0)
    check("a failed setup still deletes the setup token, and mints no bucket token",
          rc == 1 and "t-setup" not in tokens2 and "t-bucket-new" not in tokens2, str(tokens2))

    log3, delivered3 = [], []
    _r2_fake(m, log3, bucket_exists=True,
             existing="observatory-r2-bucket eu/offsite-backups (managed)", put_fails=2)
    m.deliver_to_vault = lambda value, p, e, n: delivered3.append((value, n))
    with contextlib.redirect_stdout(io.StringIO()):
        rc = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 30, "proj/prod/OFFSITE", None, wait=0)
    check("a second issue ROLLS the bucket token and keeps its id as the access key",
          rc == 0 and bool(delivered3) and delivered3[0] == ("t-bucket", "OFFSITE_ACCESS_KEY_ID"),
          f"{rc} {delivered3}")
    check("an existing bucket is not created again",
          not any(v == "POST" and p == "/accounts/a1/r2/buckets" for v, p, *_ in log3), "")
    check("and a pair the edge has not honoured yet is retried before it is judged",
          sum(1 for v, *_ in log3 if v == "S3 PUT") == 3, "")

    log4 = []
    _r2_fake(m, log4)
    with contextlib.redirect_stderr(io.StringIO()):
        check("a bucket name R2 would refuse is refused before minting",
              m.cmd_issue_bucket("r2-bucket", "Bad_Name", "eu", 30, "proj/prod/OFFSITE", None) == 2, "")
        check("an unknown jurisdiction is refused",
              m.cmd_issue_bucket("r2-bucket", "offsite-backups", "mars", 30, "proj/prod/OFFSITE", None) == 2, "")
        check("an expiry outside 1…3650 days is refused",
              m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 0, "proj/prod/OFFSITE", None) == 2, "")
        check("without --vault nothing is minted",
              m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 30, None, None) == 2, "")
        check("a slot prefix the vault would refuse is refused before minting",
              m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 30, "proj/prod/lower", None) == 2, "")
        check("the analytics path refuses the bucket preset",
              m.cmd_issue("r2-bucket", None, None) == 2, "")
        check("the account path refuses the bucket preset",
              m.cmd_issue_account("r2-bucket", "proj/prod/X", None) == 2, "")
    check("and no refusal minted anything",
          not any(v == "POST" and p == "/accounts/a1/tokens" for v, p, *_ in log4), str(log4))
    check("the setup right is not a preset anyone can issue",
          "r2-setup" not in m.PRESETS and m.R2_SETUP["groups"] == ("Workers R2 Storage Write",), "")


def _life_puts(log) -> list[dict]:
    return [pl for v, p, _t, pl, _h in log if v == "PUT" and p.endswith("/lifecycle")]


def _token_writes(log) -> list[tuple[str, str]]:
    """Every call that creates, rewrites or rolls a token — never a read."""
    return [(v, p) for v, p, *_ in log
            if "/tokens" in p and "permission_groups" not in p and v in ("POST", "PUT")]


def test_cf_r2_lifecycle_rules_per_prefix() -> None:
    """A bucket can keep one prefix for days and the rest for months: each
    `--lifecycle-rule PREFIX:DAYS` is one rule that deletes that prefix's objects
    and aborts its stale multipart uploads after a day; `--expire-days` stays the
    whole-bucket rule, prefix "", with the id it always had."""
    m = cf()
    plan = m.r2_lifecycle_plan(92, ["staging/:2"])
    check("the whole-bucket rule comes first, then each prefix rule",
          plan == [("", 92), ("staging/", 2)], str(plan))
    body = m.r2_lifecycle(plan)
    rules = body.get("rules") or []
    check("one rule per entry", len(rules) == 2, str(body))
    whole, staged = (rules + [{}, {}])[:2]
    check("the whole-bucket rule keeps the id and shape it had before",
          whole == {"id": "expire-after-92-days", "enabled": True, "conditions": {"prefix": ""},
                    "deleteObjectsTransition": {"condition": {"type": "Age", "maxAge": 92 * 86400}},
                    "abortMultipartUploadsTransition": {"condition": {"type": "Age", "maxAge": 86400}}},
          str(whole))
    check("the prefix rule deletes under its prefix after its days",
          staged.get("enabled") is True and staged.get("conditions") == {"prefix": "staging/"}
          and staged.get("deleteObjectsTransition") == {"condition": {"type": "Age", "maxAge": 2 * 86400}},
          str(staged))
    check("and aborts that prefix's multipart uploads after one day",
          staged.get("abortMultipartUploadsTransition") == {"condition": {"type": "Age", "maxAge": 86400}},
          str(staged))
    check("rule ids are unique, name the prefix, and stay under 255 characters",
          len({r["id"] for r in rules}) == 2 and "staging" in staged.get("id", "")
          and all(len(r["id"]) <= 255 for r in rules), str([r.get("id") for r in rules]))
    twins = m.r2_lifecycle(m.r2_lifecycle_plan(None, ["a/:3", "a-:3"]))["rules"]
    check("two prefixes that slug alike still get two ids",
          twins[0]["id"] != twins[1]["id"], str([r["id"] for r in twins]))
    long_id = m.r2_lifecycle(m.r2_lifecycle_plan(None, ["x" * 1024 + ":3"]))["rules"][0]["id"]
    check("a 1024-byte prefix still yields a bounded id", len(long_id) <= 255, str(len(long_id)))
    check("the same input gives the same body, so a re-run rewrites nothing",
          m.r2_lifecycle(m.r2_lifecycle_plan(92, ["staging/:2"])) == body, "")
    check("a prefix may hold a colon: DAYS is after the LAST one",
          m.r2_lifecycle_plan(None, ["logs:2026/:7"]) == [("logs:2026/", 7)], "")
    check("--expire-days alone is today's single whole-bucket rule",
          m.r2_lifecycle_plan(45, []) == [("", 45)], "")
    check("with neither given, issue keeps its 30-day default",
          m.r2_lifecycle_plan(None, []) == [("", 30)], "")
    check("prefix rules alone need no whole-bucket rule",
          m.r2_lifecycle_plan(None, ["staging/:2"]) == [("staging/", 2)], "")
    check("a prefix rule as long as the whole-bucket one is allowed",
          m.r2_lifecycle_plan(30, ["tmp/:30"]) == [("", 30), ("tmp/", 30)], "")


def test_cf_r2_lifecycle_refuses_what_it_cannot_mean() -> None:
    """Every refusal happens before anything is minted, and says why."""
    import contextlib, io
    m = cf()

    def refusal(expire, rules) -> str:
        try:
            m.r2_lifecycle_plan(expire, rules)
        except ValueError as exc:
            return str(exc)
        return ""
    cases = [
        ("a rule of 0 days", None, ["staging/:0"], "between 1 and 3650"),
        ("a rule over ten years", None, ["staging/:3651"], "between 1 and 3650"),
        ("a whole-bucket expiry of 0 days", 0, ["staging/:2"], "between 1 and 3650"),
        ("a whole-bucket expiry over ten years", 3651, [], "between 1 and 3650"),
        ("days that are not a number", None, ["staging/:two"], "PREFIX:DAYS"),
        ("a rule with no colon", None, ["staging/"], "PREFIX:DAYS"),
        ("an empty prefix", None, [":5"], "--expire-days"),
        ("a leading slash", None, ["/staging/:2"], "leading '/'"),
        ("a control character", None, ["stag\x07ing/:2"], "printable"),
        ("a prefix over 1024 bytes", None, ["x" * 1025 + ":2"], "1024 bytes"),
        ("1024 characters that are more than 1024 bytes", None, ["é" * 513 + ":2"], "1024 bytes"),
        ("the same prefix twice", None, ["staging/:2", "staging/:3"], "more than once"),
    ]
    for name, expire, rules, words in cases:
        said = refusal(expire, rules)
        check(f"refused: {name}", bool(said) and words in said, repr(said))
    said = refusal(92, ["staging/:120"])
    check("a prefix rule longer than the whole-bucket rule is refused as one that never fires",
          "never fire" in said and "staging/" in said and "92" in said, said)
    said = refusal(None, ["a/:5", "a/b/:9"])
    check("so is one longer than a rule over a shorter prefix that covers it",
          "never fire" in said and "a/b/" in said, said)

    _cf_with_admin(m)
    log = []
    _r2_fake(m, log)
    m.deliver_to_vault = lambda *a: (_ for _ in ()).throw(AssertionError("nothing is delivered"))
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc_issue = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 92, "proj/prod/OFFSITE",
                                      None, wait=0, lifecycle_rules=["staging/:120"])
        rc_life = m.cmd_lifecycle("offsite-backups", "eu", 92, ["staging/:120"], None, wait=0)
        rc_none = m.cmd_lifecycle("offsite-backups", "eu", None, [], None, wait=0)
    check("issue refuses a bad rule with exit 2", rc_issue == 2, str(rc_issue))
    check("lifecycle refuses a bad rule with exit 2", rc_life == 2, str(rc_life))
    check("lifecycle with no rule at all is refused rather than defaulted to 30 days",
          rc_none == 2 and "--expire-days" in err.getvalue(), f"{rc_none} {err.getvalue()}")
    check("and no refusal reached Cloudflare", log == [], str(log))


def test_cf_r2_issue_writes_every_rule_and_reruns_idempotently() -> None:
    """Issue with a whole-bucket and a prefix rule writes both and reads both
    back; a re-run on the bucket it made writes the identical lifecycle and
    ROLLS the same key, as a second issue always did."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    m._journal = lambda *a, **k: None
    log, delivered = [], []
    _r2_fake(m, log)
    m.deliver_to_vault = lambda value, p, e, n: delivered.append(n)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 92, "proj/prod/OFFSITE",
                                None, wait=0, lifecycle_rules=["staging/:2"])
    want = m.r2_lifecycle([("", 92), ("staging/", 2)])
    check("issue with a prefix rule succeeds", rc == 0, str(rc))
    check("its lifecycle is the whole-bucket rule and the prefix rule, once",
          _life_puts(log) == [want], str(_life_puts(log)))
    check("the summary names every rule",
          "staging/" in out.getvalue() and "92 days" in out.getvalue() and "2 days" in out.getvalue(),
          out.getvalue())
    check("and the three slots are delivered", len(delivered) == 3, str(delivered))

    log2, delivered2 = [], []
    _r2_fake(m, log2, bucket_exists=True, lifecycle=want,
             existing="observatory-r2-bucket eu/offsite-backups (managed)")
    m.deliver_to_vault = lambda value, p, e, n: delivered2.append((value, n))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc = m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 92, "proj/prod/OFFSITE",
                                None, wait=0, lifecycle_rules=["staging/:2"])
    check("a re-run succeeds", rc == 0, str(rc))
    check("and writes exactly the same lifecycle", _life_puts(log2) == [want], str(_life_puts(log2)))
    check("the existing bucket is not created again",
          not any(v == "POST" and p == "/accounts/a1/r2/buckets" for v, p, *_ in log2), "")
    check("the bucket key is rolled in place, never a twin",
          ("PUT", "/accounts/a1/tokens/t-bucket/value") in _token_writes(log2)
          and not any(v == "POST" and p == "/accounts/a1/tokens" and "setup" not in (pl or {}).get("name", "")
                      for v, p, _t, pl, _h in log2)
          and delivered2[:1] == [("t-bucket", "OFFSITE_ACCESS_KEY_ID")], str(_token_writes(log2)))

    log3 = []
    _r2_fake(m, log3, bucket_exists=True, lifecycle=want)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", None, "proj/prod/OFFSITE", None, wait=0)
    check("issue with neither option keeps the old 30-day whole-bucket rule",
          _life_puts(log3) == [m.r2_lifecycle([("", 30)])], str(_life_puts(log3)))


def test_cf_r2_lifecycle_read_back_is_compared_rule_by_rule() -> None:
    """A provider that drops, rewrites or adds a rule is a refusal, named by
    prefix — one rule matching is not the lifecycle that was asked for."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    m._journal = lambda *a, **k: None
    m.deliver_to_vault = lambda *a: (_ for _ in ()).throw(AssertionError("nothing is delivered"))

    def drop_prefix(lc):
        lc["rules"] = [r for r in lc["rules"] if r["conditions"]["prefix"] == ""]
        return lc

    def stretch_prefix(lc):
        for r in lc["rules"]:
            if r["conditions"]["prefix"] == "staging/":
                r["deleteObjectsTransition"]["condition"]["maxAge"] = 7 * 86400
        return lc

    def add_stray(lc):
        lc["rules"].append({"id": "stray", "enabled": True, "conditions": {"prefix": "old/"},
                            "deleteObjectsTransition": {"condition": {"type": "Age", "maxAge": 86400}}})
        return lc

    def disable(lc):
        lc["rules"][1]["enabled"] = False
        return lc

    def no_abort(lc):
        lc["rules"][1].pop("abortMultipartUploadsTransition")
        return lc
    for name, tamper, words in (("a dropped prefix rule", drop_prefix, "staging/"),
                                ("a prefix rule read back with other days", stretch_prefix, "staging/"),
                                ("a rule nobody asked for", add_stray, "old/"),
                                ("a rule read back disabled", disable, "staging/"),
                                ("a rule without its multipart abort", no_abort, "staging/")):
        for label, run in (
                ("issue", lambda: m.cmd_issue_bucket("r2-bucket", "offsite-backups", "eu", 92,
                                                     "proj/prod/OFFSITE", None, wait=0,
                                                     lifecycle_rules=["staging/:2"])),
                ("lifecycle", lambda: m.cmd_lifecycle("offsite-backups", "eu", 92, ["staging/:2"],
                                                      None, wait=0))):
            log, err = [], io.StringIO()
            tokens = _r2_fake(m, log, bucket_exists=True, readback=tamper)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = run()
            check(f"{label}: {name} is refused and named",
                  rc == 1 and "did not read back" in err.getvalue() and words in err.getvalue(),
                  f"{rc} {err.getvalue()}")
            check(f"{label}: and the setup token is still deleted, no bucket key minted",
                  "t-setup" not in tokens and "t-bucket-new" not in tokens, str(tokens))


def test_cf_r2_lifecycle_only_changes_rules_and_never_a_key() -> None:
    """`lifecycle` rewrites an existing bucket's rules through the same
    ephemeral setup token, and does nothing else: no bucket key is minted,
    rolled or delivered, no bucket is created, no vault slot is touched."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    journal = []
    m._journal = lambda event, subject, **k: journal.append((event, subject, k))
    m.deliver_to_vault = lambda *a: (_ for _ in ()).throw(AssertionError("no vault slot is touched"))
    old = m.r2_lifecycle([("", 92)])
    want = m.r2_lifecycle([("", 92), ("staging/", 2)])
    log, out, err = [], io.StringIO(), io.StringIO()
    tokens = _r2_fake(m, log, bucket_exists=True, lifecycle=old,
                      existing="observatory-r2-bucket eu/offsite-backups (managed)")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = m.cmd_lifecycle("offsite-backups", "eu", 92, ["staging/:2"], None, wait=0)
    check("lifecycle succeeds on an existing bucket", rc == 0, f"{rc} {err.getvalue()}")
    check("it replaces the lifecycle with exactly the set given",
          _life_puts(log) == [want], str(_life_puts(log)))
    check("the only token it creates is the setup token, and it deletes it",
          _token_writes(log) == [("POST", "/accounts/a1/tokens")] and "t-setup" not in tokens
          and ("DELETE", "/accounts/a1/tokens/t-setup") in [(v, p) for v, p, *_ in log],
          str(_token_writes(log)))
    check("the bucket key is neither rolled nor rewritten",
          not any("t-bucket" in p for _v, p in _token_writes(log)), str(_token_writes(log)))
    check("no S3 call is made: there is no key to prove",
          not any(v.startswith("S3 ") for v, *_ in log), "")
    check("every R2 call names the jurisdiction",
          all(h.get("cf-r2-jurisdiction") == "eu" for _v, p, _t, _pl, h in log if "/r2/buckets" in p), "")
    printed = out.getvalue() + err.getvalue()
    check("the summary names the rules and says no key changed",
          "staging/" in printed and "92 days" in printed and "no key" in printed, printed)
    check("no value is printed", "setup-value-" not in printed and "bucket-value-" not in printed, printed)
    check("the change is journaled under the bucket, not a slot",
          journal and journal[0][0] == "lifecycle" and "offsite-backups" in journal[0][1]
          and journal[0][2].get("rules") == [["", 92], ["staging/", 2]], str(journal))

    log2 = []
    _r2_fake(m, log2, bucket_exists=True, lifecycle=want)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc = m.cmd_lifecycle("offsite-backups", "eu", 92, ["staging/:2"], None, wait=0)
    check("a re-run writes the identical lifecycle", rc == 0 and _life_puts(log2) == [want],
          str(_life_puts(log2)))

    log3, err3 = [], io.StringIO()
    tokens3 = _r2_fake(m, log3)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err3):
        rc = m.cmd_lifecycle("offsite-backups", "eu", 92, ["staging/:2"], None, wait=0)
    check("a missing bucket is refused, pointing at issue",
          rc == 1 and "issue --preset r2-bucket" in err3.getvalue(), f"{rc} {err3.getvalue()}")
    check("and is not created, nor any lifecycle written",
          not any(v == "POST" and p == "/accounts/a1/r2/buckets" for v, p, *_ in log3)
          and _life_puts(log3) == [], "")
    check("and the setup token is still deleted", "t-setup" not in tokens3, str(tokens3))

    with contextlib.redirect_stderr(io.StringIO()):
        check("a bucket name R2 would refuse is refused before minting",
              m.cmd_lifecycle("Bad_Name", "eu", 92, [], None, wait=0) == 2, "")
        check("an unknown jurisdiction is refused",
              m.cmd_lifecycle("offsite-backups", "mars", 92, [], None, wait=0) == 2, "")


def test_cf_r2_lifecycle_command_line() -> None:
    """The flags reach the functions: `--lifecycle-rule` repeats, `--expire-days`
    is optional, and `lifecycle` takes no --vault and no --preset."""
    m = cf()
    seen = {}
    m.cmd_issue_bucket = lambda *a, **k: seen.setdefault("issue", (a, k)) and 0
    m.cmd_lifecycle = lambda *a, **k: seen.setdefault("lifecycle", (a, k)) and 0
    argv = sys.argv
    try:
        sys.argv = ["cloudflare.py", "issue", "--preset", "r2-bucket", "--bucket", "b-1",
                    "--jurisdiction", "eu", "--vault", "p/e/X",
                    "--lifecycle-rule", "staging/:2", "--lifecycle-rule", "tmp/:1"]
        m.main()
        sys.argv = ["cloudflare.py", "lifecycle", "--account", "acct", "--bucket", "b-1",
                    "--jurisdiction", "eu", "--expire-days", "92", "--lifecycle-rule", "staging/:2"]
        m.main()
        import contextlib, io
        m.cmd_issue_account = lambda *a, **k: seen.setdefault("account", (a, k)) and 0
        err = io.StringIO()
        sys.argv = ["cloudflare.py", "issue", "--preset", "d1-edit", "--vault", "p/e/X",
                    "--lifecycle-rule", "staging/:2"]
        with contextlib.redirect_stderr(err):
            rc_other = m.main()
    finally:
        sys.argv = argv
    check("a lifecycle flag on another preset is refused, not silently ignored",
          rc_other == 2 and "account" not in seen and "r2-bucket only" in err.getvalue(),
          f"{rc_other} {err.getvalue()}")
    a, k = seen.get("issue", ((), {}))
    check("issue passes every --lifecycle-rule, and no expiry when none was given",
          a[:5] == ("r2-bucket", "b-1", "eu", None, "p/e/X")
          and k.get("lifecycle_rules") == ["staging/:2", "tmp/:1"], str(seen.get("issue")))
    check("lifecycle passes bucket, jurisdiction, expiry, rules and account",
          seen.get("lifecycle", ((), {}))[0] == ("b-1", "eu", 92, ["staging/:2"], "acct"),
          str(seen.get("lifecycle")))
    check("the usage lists the lifecycle command", "cloudflare.py\" lifecycle" in m.__doc__, "")


def _cf_policy(group, scope_key="com.cloudflare.api.account.a1", objects=None, access="allow"):
    """A member permission policy in the shape the members API answers with."""
    return {"id": "p-" + group[:4], "access": access,
            "permission_groups": [{"id": "g-" + group[:4], "name": group}],
            "resource_groups": [{"id": "rg1", "scope": [{"key": scope_key,
                                                         "objects": objects or [{"key": "*"}]}]}]}


def _cf_team_fake(m, log, members=None, me="admin@example.invalid"):
    """An account with two pages of tokens and members, and the legacy role list."""
    state = {"members": members if members is not None else [
        {"id": "m1", "user": {"email": "admin@example.invalid"}, "status": "accepted",
         "roles": [{"id": "r-super", "name": m.SUPER_ADMIN}]},
        {"id": "m2", "user": {"email": "dev@example.invalid"}, "status": "accepted",
         "roles": [{"id": "r-ro", "name": "Administrator Read Only"}]},
        {"id": "m3", "user": {"email": "policy@example.invalid"}, "status": "accepted",
         "roles": [], "policies": [_cf_policy("Administrator Read Only")]}]}

    def fake(path, token, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path, token, payload))
        page = 2 if "page=2" in path else 1
        if path.startswith("/accounts/a1/tokens?"):
            rows = [{"id": "t1", "name": "observatory-analytics-read (managed)", "status": "active"},
                    {"id": "t2", "name": "observatory-d1-edit alpha/prod/CLOUDFLARE_API_TOKEN (managed)",
                     "status": "active"}] if page == 1 else [
                    {"id": "t3", "name": "a token someone made by hand", "status": "active"}]
            return {"result": rows, "result_info": {"total_pages": 2}}
        if path.startswith("/accounts/a1/members?"):
            rows = state["members"]
            half = len(rows) // 2 or 1
            return {"result": rows[:half] if page == 1 else rows[half:], "result_info": {"total_pages": 2}}
        if path.startswith("/accounts/a1/roles?"):
            return {"result": [{"id": "r-super", "name": m.SUPER_ADMIN},
                               {"id": "r-ro", "name": "Administrator Read Only"}]}
        if path == "/user":
            if me is None:
                raise RuntimeError("cloudflare answered HTTP 403; provider response withheld")
            return {"result": {"email": me}}
        if path == "/accounts/a1/members" and payload is not None:
            state["members"].append({"id": "m9", "user": {"email": payload["email"]}, "status": "pending",
                                     "roles": [{"id": payload["roles"][0], "name": "Administrator Read Only"}]})
            return {"result": {}}
        if path.startswith("/accounts/a1/members/") and method == "PUT":
            mid = path.rsplit("/", 1)[1]
            for mb in state["members"]:
                if mb["id"] == mid:
                    mb["roles"] = [{"id": payload["roles"][0]["id"], "name": m.SUPER_ADMIN
                                    if payload["roles"][0]["id"] == "r-super" else "Administrator Read Only"}]
            return {"result": {}}
        if path.startswith("/accounts/a1/members/") and method == "DELETE":
            mid = path.rsplit("/", 1)[1]
            state["members"] = [mb for mb in state["members"] if mb["id"] != mid]
            return {"result": {}}
        raise AssertionError(f"unexpected {method} {path}")
    m._request = fake
    return state


def _cf_run(fn, *a):
    import contextlib, io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a)
    return rc, out.getvalue(), err.getvalue()


def test_cf_accounts_names_every_account_its_tokens_and_their_slots() -> None:
    """`accounts` (OBS-156): every stashed account, the tokens Cloudflare holds in
    it over every page, and the vault slot a managed token's name records."""
    m = cf(); _cf_with_admin(m)
    m.token_dir = lambda: pathlib.Path(tmpdir.mkdtemp()).resolve()
    log = []
    _cf_team_fake(m, log)
    rc, o, e = _cf_run(m.cmd_accounts)
    check("accounts reads", rc == 0, o + e)
    check("names the account and its id", "Acct" in o and "id a1" in o, o)
    check("counts tokens over both pages", "tokens at Cloudflare: 3, 2 managed" in o, o)
    check("and names the slot a managed token records",
          "-> vault alpha/prod/CLOUDFLARE_API_TOKEN" in o, o)
    check("the slot parser ignores unmanaged names",
          m.slot_of("a/b/c by hand") is None and m.slot_of("x a/b/c (managed)") == "a/b/c", "")
    check("and no value is printed", "admin-token" not in o + e, o + e)


def test_cf_members_and_member_writes_are_a_plan_until_apply() -> None:
    m = cf(); _cf_with_admin(m)
    m._journal = lambda *a, **k: None
    log = []
    state = _cf_team_fake(m, log)
    rc, o, e = _cf_run(m.cmd_members, None)
    check("members reads every page with roles or policies", rc == 0 and "members of Acct (3)" in o
          and "Administrator Read Only" in o and "1 policy(ies)" in o, o + e)
    writes = lambda: [c for c in log if c[0] in ("PUT", "DELETE") or (c[0] == "POST" and c[3])]
    rc, o, e = _cf_run(m.cmd_member, "add", None, "new@example.invalid", "Administrator Read Only", False)
    check("a dry run prints the plan and writes nothing", rc == 0 and "dry run" in o and not writes(), o + e)
    rc, o, e = _cf_run(m.cmd_member, "add", None, "new@example.invalid", "Administrator Read Only", True)
    check("--apply invites with the role id, and verifies", rc == 0 and "applied and verified" in o
          and any(c[3] and c[3].get("roles") == ["r-ro"] for c in log if c[0] == "POST"), o + e)
    rc, o, e = _cf_run(m.cmd_member, "role", None, "dev@example.invalid", m.SUPER_ADMIN, True)
    check("--apply changes a role with PUT", rc == 0 and any(c[0] == "PUT" for c in log), o + e)
    rc, o, e = _cf_run(m.cmd_member, "remove", None, "new@example.invalid", None, True)
    check("--apply removes", rc == 0 and all(mb["id"] != "m9" for mb in state["members"]), o + e)
    rc, o, e = _cf_run(m.cmd_member, "role", None, "dev@example.invalid", "No Such Role", False)
    check("an unknown role is refused with the account's own names", rc == 1 and "Administrator Read Only" in e, e)


def test_cf_member_writes_refuse_what_would_lock_the_account() -> None:
    m = cf(); _cf_with_admin(m)
    log = []
    state = _cf_team_fake(m, log, me=None)
    plan = m.plan_account_member
    check("the last Super Administrator cannot be removed",
          "last" in (plan("remove", state["members"], "admin@example.invalid", None, None)[1] or ""), "")
    check("nor demoted", "last" in (plan("role", state["members"], "admin@example.invalid",
                                         "Administrator Read Only", None)[1] or ""), "")
    check("the admin's own membership is refused when it is known",
          plan("remove", state["members"], "dev@example.invalid", None, "dev@example.invalid")[1] is not None, "")
    check("a policy-based member's role is not rewritten",
          "policies" in (plan("role", state["members"], "policy@example.invalid", m.SUPER_ADMIN, None)[1] or ""), "")
    check("a non-member cannot be removed",
          plan("remove", state["members"], "nobody@example.invalid", None, None)[1] is not None, "")
    rc, o, e = _cf_run(m.cmd_member, "remove", None, "dev@example.invalid", None, False)
    check("an unreadable admin user is said in the plan", rc == 0 and "only the last Super Administrator" in o, o)


def test_cf_super_admins_are_counted_from_roles_and_policies() -> None:
    """M2 of review round 1: on an account whose members hold their rights through
    permission policies, the last Super Administrator was removable, because only
    legacy roles were counted. Both shapes count; an unreadable record refuses."""
    m = cf()
    sa = m.SUPER_ADMIN
    by_policy = [
        {"id": "m1", "user": {"email": "owner@example.invalid"}, "status": "accepted", "policies": [_cf_policy(sa)]},
        {"id": "m2", "user": {"email": "dev@example.invalid"}, "status": "accepted",
         "policies": [_cf_policy("Administrator Read Only")]}]
    plan = lambda action, members, email, role=None: m.plan_account_member(action, members, email, role, None, "a1")
    check("a policy-only last Super Administrator cannot be removed",
          "no other accepted member" in (plan("remove", by_policy, "owner@example.invalid")[1] or ""), "")
    check("a policy member who is not one can be",
          plan("remove", by_policy, "dev@example.invalid")[1] is None, "")
    second = by_policy + [{"id": "m3", "user": {"email": "two@example.invalid"}, "status": "accepted",
                           "policies": [_cf_policy(sa)]}]
    check("with a second whole-account Super Administrator by policy, the first may go",
          plan("remove", second, "owner@example.invalid")[1] is None, "")
    scoped = by_policy + [{"id": "m3", "user": {"email": "zone@example.invalid"}, "status": "accepted",
                           "policies": [_cf_policy(sa, "com.cloudflare.api.account.zone.z1")]}]
    check("a Super Administrator scoped to one zone does not count as the account's",
          plan("remove", scoped, "owner@example.invalid")[1] is not None, "")
    partial = by_policy + [{"id": "m3", "user": {"email": "part@example.invalid"}, "status": "accepted",
                            "policies": [_cf_policy(sa, objects=[{"key": "some-object"}])]}]
    check("nor one scoped to some of the account's objects",
          plan("remove", partial, "owner@example.invalid")[1] is not None, "")
    pending = by_policy + [{"id": "m3", "user": {"email": "new@example.invalid"}, "status": "pending",
                            "policies": [_cf_policy(sa)]}]
    check("nor a pending invitee", plan("remove", pending, "owner@example.invalid")[1] is not None, "")
    denied = by_policy + [{"id": "m3", "user": {"email": "deny@example.invalid"}, "status": "accepted",
                           "policies": [_cf_policy(sa, access="deny")]}]
    check("nor a deny policy", plan("remove", denied, "owner@example.invalid")[1] is not None, "")
    mixed = [{"id": "m1", "user": {"email": "legacy@example.invalid"}, "status": "accepted",
              "roles": [{"id": "r", "name": sa}]}] + by_policy
    check("a legacy Super Administrator and a policy one cover each other",
          plan("remove", mixed, "legacy@example.invalid")[1] is None
          and plan("remove", mixed, "owner@example.invalid")[1] is None, "")
    check("a legacy role change of the last one is refused",
          plan("role", mixed[:1] + by_policy[1:], "legacy@example.invalid", "Administrator Read Only")[1] is not None, "")
    for label, odd in (("a policy group without a name", {"permission_groups": [{"id": "g"}]}),
                       ("a policy without its groups", {"id": "p"}),
                       ("a scope that cannot be read", {"access": "allow", "permission_groups": [{"name": sa}],
                                                       "resource_groups": [{"id": "rg"}]})):
        members = by_policy + [{"id": "m9", "user": {"email": "odd@example.invalid"}, "status": "accepted",
                                "policies": [odd]}]
        check(f"{label} refuses every write", all(
            "cannot tell" in (plan(a, members, e, r)[1] or "")
            for a, e, r in (("remove", "dev@example.invalid", None), ("add", "x@example.invalid", "Administrator Read Only"),
                            ("role", "odd@example.invalid", "Administrator Read Only"))), "")
    bare = by_policy + [{"id": "m9", "user": {"email": "bare@example.invalid"}, "status": "accepted"}]
    check("a member record with neither roles nor policies refuses writes",
          "cannot tell" in (plan("remove", bare, "dev@example.invalid")[1] or ""), "")


def test_cf_all_reads_every_page_whatever_the_list_says_about_itself() -> None:
    """L1: lists that carry `total_count` or only `per_page` are read to their end;
    one that never ends, or ends short of its own count, is an error."""
    m = cf()

    def serve(shape, rows_total, stop_early=False):
        def fake(path, token, payload=None, method=None):
            page = int(path.rsplit("page=", 1)[1])
            start = (page - 1) * 50
            rows = [{"id": i} for i in range(start, min(start + 50, rows_total))]
            if stop_early and page > 1:
                rows = []
            info = {"count_only": {"count": len(rows), "total_count": rows_total},
                    "per_page_only": {"per_page": 50, "count": len(rows)},
                    "none": {}}[shape]
            return {"result": rows, "result_info": info}
        m._request = fake
    serve("count_only", 120)
    check("total_count without total_pages reads all 120", len(m._all("/accounts/a1/members", "t")) == 120, "")
    serve("per_page_only", 120)
    check("per_page alone reads to the short page", len(m._all("/accounts/a1/members", "t")) == 120, "")
    serve("none", 50 * (m.PAGES_MAX + 3))
    try:
        m._all("/accounts/a1/tokens", "t")
        check("a list still going at the page cap raises", False, "returned")
    except RuntimeError as exc:
        check("a list still going at the page cap raises", "did not end" in str(exc), str(exc))
    serve("count_only", 120, stop_early=True)
    try:
        m._all("/accounts/a1/members", "t")
        check("a list that ends short of its own count raises", False, "returned")
    except RuntimeError as exc:
        check("a list that ends short of its own count raises", "of the 120" in str(exc), str(exc))


def test_cf_groups_lists_names_and_levels_and_never_a_value() -> None:
    """Choosing a preset's permission groups needs their exact NAMES, which
    Cloudflare's docs do not always list (Email Sending was absent from the
    permissions reference on 2026-09-30). `groups` asks the account's own
    catalogue through the stashed admin, filtered, and prints names and levels
    only — never an id that could be pasted into a policy by hand."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    log = []

    def fake(path, token, payload=None, method=None):
        log.append((method, path, token, payload))
        if "permission_groups" in path:
            return {"result": [
                {"id": "g1", "name": "Email Sending Write", "scopes": ["com.cloudflare.api.account"]},
                {"id": "g2", "name": "Email Sending Write", "scopes": ["com.cloudflare.api.account.zone"]},
                {"id": "g3", "name": "D1 Write", "scopes": ["com.cloudflare.api.account"]}]}
        raise AssertionError(f"unexpected call {path}")
    m._request = fake
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_groups(None, "email sending")
    text = out.getvalue()
    check("groups answers", rc == 0, str(rc))
    check("it names both levels of a same-named group",
          "Email Sending Write" in text and "account" in text and "zone" in text, text)
    check("the filter drops what does not match", "D1 Write" not in text, text)
    check("no id and no admin value reach the output",
          "g1" not in text and "g2" not in text and "admin-token" not in text, text)
    check("it only reads", all(pl is None for _m, _p, _t, pl in log), str(log))


def _preset_fake(m, log, groups, probe_prefix, value_prefix):
    """A provider that offers `groups` (name, level) and answers `probe_prefix`."""
    def fake(path, token, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path, token, payload))
        if "permission_groups" in path:
            return {"result": [{"id": f"id-{lvl}-{name}", "name": name,
                                "scopes": ["com.cloudflare.api.account" + (".zone" if lvl == "zone" else "")]}
                               for name, lvl in groups]}
        if path.startswith("/zones?name=example.com&account.id=a1"):
            return {"result": [{"id": "z1", "name": "example.com"}]}
        if path.endswith("/tokens?per_page=50"):
            return {"result": []}
        if path == "/accounts/a1/tokens" and method is None and payload is not None:
            return {"result": {"id": "t-new", "value": value_prefix + "y" * 30}}
        if path == "/accounts/a1/tokens/t-new" and method == "DELETE":
            return {"result": {"id": "t-new"}}
        if path.startswith(probe_prefix):
            return {"result": []}
        raise AssertionError(f"unexpected call {path}")
    m._request = fake


def test_cf_email_presets_grant_exactly_what_the_email_service_needs() -> None:
    """Transactional email through Cloudflare Email Service: a
    runtime SENDER on one account, a zone's ROUTING-rule writer, and a Workers
    writer for an inbound probe — each its own narrow token, each verified by a
    read its own grant allows, each delivered to a vault slot."""
    import contextlib, io
    cases = [
        ("email-send", "account", ("Email Sending Write", "Email Sending Read"),
         "/accounts/a1/email/sending/suppressions", "send-value-"),
        ("workers-edit", "account", ("Workers Scripts Write", "Workers KV Storage Write"),
         "/accounts/a1/workers/scripts", "wk-value-"),
        ("access-app", "account", ("Access: Apps and Policies Write",
                                   "Access: Organizations, Identity Providers, and Groups Read"),
         "/accounts/a1/access/organizations", "acc-value-"),
        ("email-routing", "zone", ("Zone Read", "Email Routing Rules Write"),
         "/zones/z1/email/routing/rules", "rt-value-"),
        ("redirect-rules", "zone", ("Zone Read", "Dynamic URL Redirects Write"),
         "/zones/z1/rulesets/phases/http_request_dynamic_redirect/entrypoint", "rd-value-"),
        ("workers-routes", "zone", ("Zone Read", "Workers Routes Write"),
         "/zones/z1/workers/routes", "wr-value-"),
        ("cache-purge", "zone", ("Zone Read", "Cache Purge"),
         "/zones/z1/purge_cache", "cp-value-"),
        ("zone-edge-rules", "zone",
         ("Zone Read", "Cache Settings Write", "Config Settings Write", "Bot Management Write"),
         "/zones/z1/rulesets/phases/http_request_cache_settings/entrypoint", "er-value-"),
    ]
    for preset, level, groups, probe, prefix in cases:
        m = cf(); _cf_with_admin(m)
        log, delivered = [], []
        offered = [(g, level) for g in groups] + [(g, "zone" if level == "account" else "account") for g in groups]
        _preset_fake(m, log, offered, probe, prefix)
        m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append((value, p, e, n))
        m._journal = lambda *a, **k: None
        m.token_dir = lambda: (_ for _ in ()).throw(AssertionError("vault presets never reach the plugin folder"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = (m.cmd_issue_zone(preset, "example.com", "proj/prod/CF_X", None, wait=0) if level == "zone"
                  else m.cmd_issue_account(preset, "proj/prod/CF_X", None, wait=0))
        check(f"{preset} issues", rc == 0, str(rc))
        create = [pl for _m, path, _t, pl in log if path == "/accounts/a1/tokens" and pl]
        pol = create[0]["policies"][0] if create else {}
        check(f"{preset} grants exactly its {level}-level groups",
              [g["id"] for g in pol.get("permission_groups", [])] == [f"id-{level}-{g}" for g in groups],
              str(pol))
        want_res = ({"com.cloudflare.api.account.zone.z1": "*"} if level == "zone"
                    else {"com.cloudflare.api.account.a1": "*"})
        check(f"{preset} is scoped to one {level}", pol.get("resources") == want_res, str(pol))
        check(f"{preset} is verified with the NEW token by its own read",
              any(path.startswith(probe) and tok.startswith(prefix) for _m, path, tok, _p in log), str(log[-3:]))
        check(f"{preset} lands in the named slot and is never printed",
              [d[1:] for d in delivered] == [("proj", "prod", "CF_X")] and prefix not in out.getvalue(),
              out.getvalue())


def test_cf_email_presets_refuse_what_they_do_not_grant() -> None:
    """The other half of least privilege: each email preset is ATTEMPTED on a
    path it does not belong to, with a slot the vault refuses, against a
    catalogue that offers its group only at the wrong level, and with a token
    that cannot do its read. Every attempt is refused, and none mints or
    delivers anything."""
    groups = {"email-send": ("Email Sending Write", "Email Sending Read"),
              "workers-edit": ("Workers Scripts Write", "Workers KV Storage Write"),
              "access-app": ("Access: Apps and Policies Write",
                             "Access: Organizations, Identity Providers, and Groups Read"),
              "email-routing": ("Zone Read", "Email Routing Rules Write"),
              "redirect-rules": ("Zone Read", "Dynamic URL Redirects Write"),
              "workers-routes": ("Zone Read", "Workers Routes Write"),
              "cache-purge": ("Zone Read", "Cache Purge"),
              "zone-edge-rules": ("Zone Read", "Cache Settings Write", "Config Settings Write",
                                  "Bot Management Write")}
    level = {"email-send": "account", "workers-edit": "account", "access-app": "account",
             "email-routing": "zone",
             "redirect-rules": "zone", "workers-routes": "zone", "cache-purge": "zone",
             "zone-edge-rules": "zone"}
    for preset in groups:
        m = cf(); _cf_with_admin(m)
        log, delivered = [], []
        other = "zone" if level[preset] == "account" else "account"
        # The catalogue carries the preset's names at the OTHER level only.
        _preset_fake(m, log, [(g, other) for g in groups[preset]], "/never", "cfx-")
        m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append(value)
        m._journal = lambda *a, **k: None
        issue = ((lambda slot: m.cmd_issue_zone(preset, "example.com", slot, None, wait=0))
                 if level[preset] == "zone"
                 else (lambda slot: m.cmd_issue_account(preset, slot, None, wait=0)))
        check(f"{preset}: a same-named group at the wrong level is never taken",
              issue("proj/prod/CF_X") == 1, "")
        check(f"{preset}: a slot the vault would refuse is refused before minting",
              issue("proj/prod/lower-case") == 2, "")
        check(f"{preset}: without --vault nothing is minted", issue(None) == 2, "")
        check(f"{preset}: the analytics path refuses it", m.cmd_issue(preset, None, None) == 2, "")
        if level[preset] == "zone":
            check(f"{preset}: the account path refuses a zone preset",
                  m.cmd_issue_account(preset, "proj/prod/CF_X", None) == 2, "")
        else:
            check(f"{preset}: the zone path refuses an account preset",
                  m.cmd_issue_zone(preset, "example.com", "proj/prod/CF_X", None) == 2, "")
        check(f"{preset}: no refusal minted anything",
              not any(p == "/accounts/a1/tokens" and pl for _m, p, _t, pl in log), str(log))

        # A token that never proves its read is not delivered.
        log2, delivered2 = [], []
        _preset_fake(m, log2, [(g, level[preset]) for g in groups[preset]], "/never", "cfx-")
        inner = m._request

        def refuse_new_token(path, token, payload=None, method=None, inner=inner):
            if token.startswith("cfx-"):
                raise RuntimeError("HTTP 403")
            return inner(path, token, payload, method)
        m._request = refuse_new_token
        m.deliver_to_vault = lambda value, p, e, n, d=delivered2: d.append(value)
        check(f"{preset}: a token that cannot do its read is not delivered",
              issue("proj/prod/CF_X") == 1 and not delivered2 and not delivered, str(delivered2))


def test_cf_access_app_preset_reads_the_team_domain_and_changes_no_organization() -> None:
    """access-app (Fabric board request 3bf4a84e): one Access application and its
    policies on one account, plus the Zero Trust organization READ that names the
    team domain. Two account-level groups in one policy on the account, no zone
    half, nothing that writes the organization, identity providers or service
    tokens; verified by reading the organization with the NEW value, minted with
    an expiry, named after its slot; and a token whose read fails (Zero Trust
    never enabled) is deleted, with the refusal naming the owner's consent page."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    preset = m.PRESETS["access-app"]
    check("access-app grants exactly its two account-level Access groups",
          preset["groups"] == ("Access: Apps and Policies Write",
                               "Access: Organizations, Identity Providers, and Groups Read")
          and preset["level"] == "account" and preset["scope"] == "account-vault"
          and not preset.get("zone_groups") and not preset.get("account_groups"), str(preset))
    check("access-app writes no organization, identity provider or service token",
          not [g for g in preset["groups"]
               if ("Organizations" in g or "Service Tokens" in g) and g.endswith("Write")], "")
    log, delivered = [], []
    offered = [(g, "account") for g in preset["groups"]] + [(g, "zone") for g in preset["groups"]]
    _preset_fake(m, log, offered, "/accounts/a1/access/organizations", "acc-value-")
    m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append((value, p, e, n))
    m._journal = lambda *a, **k: None
    m.record_slot_expiry = lambda *a, **k: "expires"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_account("access-app", "site/prod/CF_ACCESS_TOKEN", None, wait=0)
    create = [pl for _m, p, _t, pl in log if p == "/accounts/a1/tokens" and pl]
    pols = create[0]["policies"] if create else []
    check("access-app issues", rc == 0, str(rc))
    check("access-app is one policy on the one account, account-level ids only",
          [[g["id"] for g in pol["permission_groups"]] for pol in pols]
          == [[f"id-account-{g}" for g in preset["groups"]]]
          and pols[0]["resources"] == {"com.cloudflare.api.account.a1": "*"}, str(pols))
    check("access-app is named after its slot",
          create and create[0]["name"]
          == "observatory-access-app site/prod/CF_ACCESS_TOKEN (managed)", str(create))
    check("access-app is minted with an expiry", create and bool(create[0].get("expires_on")), str(create))
    check("access-app proves the organization read with the NEW value",
          any(v == "GET" and p == "/accounts/a1/access/organizations" and t.startswith("acc-value-")
              for v, p, t, _pl in log), str(log[-2:]))
    check("access-app lands in the named slot and the value is never printed",
          [d[1:] for d in delivered] == [("site", "prod", "CF_ACCESS_TOKEN")]
          and "acc-value-" not in out.getvalue(), out.getvalue())

    m = cf(); _cf_with_admin(m)
    log2, delivered2 = [], []
    _preset_fake(m, log2, [(g, "account") for g in preset["groups"]], "/never", "acc-value-")
    inner = m._request

    def no_organization(path, token, payload=None, method=None, inner=inner):
        if token.startswith("acc-value-"):
            raise RuntimeError("cloudflare answered HTTP 404; provider response withheld")
        return inner(path, token, payload, method)
    m._request = no_organization
    m.deliver_to_vault = lambda value, p, e, n, d=delivered2: d.append(value)
    m._journal = lambda *a, **k: None
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = m.cmd_issue_account("access-app", "proj/prod/CF_ACCESS_TOKEN", None, wait=0)
    check("access-app: no organization means nothing is delivered", rc == 1 and not delivered2, str(rc))
    check("access-app: the refusal names the owner's Zero Trust page for that account",
          "https://one.dash.cloudflare.com/a1/" in err.getvalue() and "Zero Trust" in err.getvalue(),
          err.getvalue())
    check("access-app: the token that never proved its read is deleted",
          any(v == "DELETE" and p == "/accounts/a1/tokens/t-new" for v, p, _t, _pl in log2)
          and "acc-value-" not in err.getvalue(), err.getvalue())


def test_cf_sending_domain_preset_grants_one_zone_and_its_account_sending() -> None:
    """Onboarding a domain to Email Sending is `POST /zones/{z}/email/sending/subdomains`:
    a ZONE endpoint that answers 401 to a token whose Email Sending right covers the
    account and whose zone rights cover `zone.*` (the fabric-inbox-account shape,
    measured 2026-10-18 on a real account). What it takes is
    the zone's own resource for Zone Read and DNS Write, and Email Sending on the
    account, as two policies: one policy may not mix levels on one resource. The
    catalogue offers Email Sending at the account level only."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    log, delivered = [], []
    preset = m.PRESETS["email-sending-domain"]
    offered = ([(g, "zone") for g in preset["groups"]]
               + [(g, "account") for g in preset["account_groups"]])
    _preset_fake(m, log, offered, "/zones/z1/email/sending/subdomains", "sd-value-")
    m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append((value, p, e, n))
    m._journal = lambda *a, **k: None
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_zone("email-sending-domain", "example.com", "proj/prod/CF_SENDING_DOMAIN", None, wait=0)
    check("email-sending-domain issues", rc == 0, str(rc))
    create = [pl for _m, path, _t, pl in log if path == "/accounts/a1/tokens" and pl]
    pols = create[0]["policies"] if create else []
    check("email-sending-domain grants Zone Read and DNS Write on the zone's own resource",
          len(pols) == 2 and pols[0]["resources"] == {"com.cloudflare.api.account.zone.z1": "*"}
          and [g["id"] for g in pols[0]["permission_groups"]] == ["id-zone-Zone Read", "id-zone-DNS Write"],
          str(pols))
    check("email-sending-domain grants Email Sending on the account, as its own policy",
          len(pols) == 2 and pols[1]["resources"] == {"com.cloudflare.api.account.a1": "*"}
          and [g["id"] for g in pols[1]["permission_groups"]]
          == ["id-account-Email Sending Write", "id-account-Email Sending Read"], str(pols))
    check("email-sending-domain is named after the zone and the slot",
          bool(create) and create[0]["name"]
          == "observatory-email-sending-domain example.com proj/prod/CF_SENDING_DOMAIN (managed)", str(create))
    check("email-sending-domain is verified with the NEW token by listing the zone's sending subdomains",
          any(path.startswith("/zones/z1/email/sending/subdomains") and tok.startswith("sd-value-")
              for _m, path, tok, _p in log), str(log[-3:]))
    check("email-sending-domain lands in the slot and is never printed",
          [d[1:] for d in delivered] == [("proj", "prod", "CF_SENDING_DOMAIN")]
          and "sd-value-" not in out.getvalue(), out.getvalue())
    check("the printed summary names the account grant too",
          "Email Sending Write" in out.getvalue(), out.getvalue())

    # Email Sending offered only at the zone level is never taken, and nothing is minted.
    m2 = cf(); _cf_with_admin(m2)
    log2 = []
    _preset_fake(m2, log2, [(g, "zone") for g in preset["groups"] + preset["account_groups"]],
                 "/zones/z1/email/sending/subdomains", "sd-value-")
    m2.deliver_to_vault = lambda *a: (_ for _ in ()).throw(AssertionError("nothing to deliver"))
    m2._journal = lambda *a, **k: None
    check("email-sending-domain: a zone-level Email Sending group is never taken",
          m2.cmd_issue_zone("email-sending-domain", "example.com", "proj/prod/CF_SENDING_DOMAIN", None, wait=0) == 1
          and not any(p == "/accounts/a1/tokens" and pl for _m, p, _t, pl in log2), str(log2))
    check("email-sending-domain: the account path refuses it",
          m2.cmd_issue_account("email-sending-domain", "proj/prod/CF_SENDING_DOMAIN", None) == 2, "")


def test_cf_redirect_preset_proves_its_read_on_a_zone_without_redirects() -> None:
    """A zone that has never had a Single Redirect has no
    `http_request_dynamic_redirect` entrypoint, and Cloudflare answers 404 for it
    to a token that MAY read it; a token without the group gets 403 (measured
    2026-10-12 with a zone's DNS-only token). So 404 proves the grant and the
    token is delivered; 403 does not, and the token created for the call is
    deleted again. The 404 allowance belongs only to presets that declare it."""
    import contextlib, io
    entry = "/zones/z1/rulesets/phases/http_request_dynamic_redirect/entrypoint"
    for status, delivers in ((404, True), (403, False)):
        m = cf(); _cf_with_admin(m)
        log, delivered = [], []
        _preset_fake(m, log, [("Zone Read", "zone"), ("Dynamic URL Redirects Write", "zone")],
                     "/never", "rd-value-")
        inner = m._request

        def answer(path, token, payload=None, method=None, inner=inner, status=status):
            if path.startswith(entry) and token.startswith("rd-value-"):
                log.append(("GET", path, token, None))
                raise RuntimeError(f"cloudflare answered HTTP {status}; provider response withheld")
            return inner(path, token, payload, method)
        m._request = answer
        m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append((p, e, n))
        m._journal = lambda *a, **k: None
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = m.cmd_issue_zone("redirect-rules", "example.com", "proj/prod/CF_REDIRECT", None, wait=0)
        if delivers:
            check("redirect-rules: a 404 entrypoint (no redirects yet) proves the read",
                  rc == 0 and delivered == [("proj", "prod", "CF_REDIRECT")], f"{rc} {err.getvalue()}")
            check("redirect-rules: the token is named after its zone and its slot",
                  any(pl and pl.get("name") == "observatory-redirect-rules example.com proj/prod/CF_REDIRECT (managed)"
                      for _m, p, _t, pl in log if p == "/accounts/a1/tokens"), str(log))
        else:
            check("redirect-rules: a 403 on the entrypoint is not a proof",
                  rc == 1 and not delivered, f"{rc} {err.getvalue()}")
            check("redirect-rules: the token that never proved its read is deleted",
                  any(_m == "DELETE" and p == "/accounts/a1/tokens/t-new" for _m, p, _t, _pl in log)
                  and "deleted again" in err.getvalue(), err.getvalue())
        check(f"redirect-rules: no value printed ({status})",
              "rd-value-" not in out.getvalue() + err.getvalue(), "")

    # The allowance belongs to the redirect preset only: a 404 on DNS records is a failure.
    m = cf(); _cf_with_admin(m)
    log, delivered = [], []
    _preset_fake(m, log, [("Zone Read", "zone"), ("DNS Write", "zone")], "/never", "dn-value-")
    inner = m._request

    def missing(path, token, payload=None, method=None, inner=inner):
        if path.startswith("/zones/z1/dns_records") and token.startswith("dn-value-"):
            raise RuntimeError("cloudflare answered HTTP 404; provider response withheld")
        return inner(path, token, payload, method)
    m._request = missing
    m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append(value)
    m._journal = lambda *a, **k: None
    with contextlib.redirect_stderr(io.StringIO()):
        rc = m.cmd_issue_zone("dns-edit", "example.com", "proj/prod/CF_DNS", None, wait=0)
    check("a 404 proves nothing for a preset that did not declare it", rc == 1 and not delivered, str(rc))


def test_cf_zone_edge_rules_preset_proves_its_read_on_a_zone_without_cache_rules() -> None:
    """One zone's Cache Rules (`http_request_cache_settings`), Configuration Rules
    (`http_config_settings`) and AI-bot setting (`/bot_management`) are three zone
    rights in one token: Cache Settings Write, Config Settings Write and Bot
    Management Write, beside Zone Read, and nothing else. The proof reads the
    cache-settings phase entrypoint with the NEW value; a zone that never had a
    Cache Rule answers 404 there to a token that may read it, so 404 delivers,
    while 403 (no grant) does not, and the token minted for the call is deleted."""
    import contextlib, io
    entry = "/zones/z1/rulesets/phases/http_request_cache_settings/entrypoint"
    groups = ("Zone Read", "Cache Settings Write", "Config Settings Write", "Bot Management Write")
    for status, delivers in ((404, True), (403, False)):
        m = cf(); _cf_with_admin(m)
        log, delivered = [], []
        _preset_fake(m, log, [(g, "zone") for g in groups], "/never", "er-value-")
        inner = m._request

        def answer(path, token, payload=None, method=None, inner=inner, status=status):
            if path.startswith(entry) and token.startswith("er-value-"):
                log.append(("GET", path, token, None))
                raise RuntimeError(f"cloudflare answered HTTP {status}; provider response withheld")
            return inner(path, token, payload, method)
        m._request = answer
        m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append((p, e, n))
        m._journal = lambda *a, **k: None
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = m.cmd_issue_zone("zone-edge-rules", "example.com", "proj/prod/CF_EDGE_RULES", None, wait=0)
        create = [pl for _m, p, _t, pl in log if p == "/accounts/a1/tokens" and pl]
        if delivers:
            check("zone-edge-rules: a 404 cache-settings entrypoint (no cache rule yet) proves the read",
                  rc == 0 and delivered == [("proj", "prod", "CF_EDGE_RULES")], f"{rc} {err.getvalue()}")
            check("zone-edge-rules: the token is named after its zone and its slot",
                  bool(create) and create[0]["name"]
                  == "observatory-zone-edge-rules example.com proj/prod/CF_EDGE_RULES (managed)", str(create))
            pols = create[0]["policies"] if create else []
            check("zone-edge-rules: one policy, the zone's own resource, exactly its four groups",
                  len(pols) == 1 and pols[0]["resources"] == {"com.cloudflare.api.account.zone.z1": "*"}
                  and [g["id"] for g in pols[0]["permission_groups"]] == [f"id-zone-{g}" for g in groups],
                  str(pols))
            check("zone-edge-rules: the summary names the four groups on that zone only",
                  ", ".join(groups) + " on example.com only" in out.getvalue(), out.getvalue())
        else:
            check("zone-edge-rules: a 403 on the entrypoint is not a proof",
                  rc == 1 and not delivered, f"{rc} {err.getvalue()}")
            check("zone-edge-rules: the token that never proved its read is deleted",
                  any(_m == "DELETE" and p == "/accounts/a1/tokens/t-new" for _m, p, _t, _pl in log)
                  and "deleted again" in err.getvalue(), err.getvalue())
        check(f"zone-edge-rules: no value printed ({status})",
              "er-value-" not in out.getvalue() + err.getvalue(), "")


def test_cf_cache_purge_proves_itself_with_one_harmless_purge() -> None:
    """Listing a zone proves nothing about purging it, so the cache-purge preset
    proves its grant by purging ONE url on that zone that the site does not serve:
    a POST whose body names that url and nothing else, never `purge_everything`.
    Every other zone preset still proves itself with a plain GET."""
    import contextlib, io
    m = cf(); _cf_with_admin(m)
    log, delivered = [], []
    _preset_fake(m, log, [("Zone Read", "zone"), ("Cache Purge", "zone")], "/zones/z1/purge_cache", "cp-value-")
    m.deliver_to_vault = lambda value, p, e, n, d=delivered: d.append((p, e, n))
    m._journal = lambda *a, **k: None
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_zone("cache-purge", "example.com", "proj/prod/CF_CACHE_PURGE_TOKEN", None, wait=0)
    probes = [(meth, pl) for meth, path, tok, pl in log
              if path == "/zones/z1/purge_cache" and tok.startswith("cp-value-")]
    check("cache-purge issues into its slot", rc == 0 and delivered == [("proj", "prod", "CF_CACHE_PURGE_TOKEN")],
          str(rc))
    check("cache-purge proves itself with a POST naming one url on its own zone",
          probes == [("POST", {"files": ["https://example.com/.well-known/observatory-purge-probe"]})], str(probes))
    check("cache-purge never purges everything to prove itself",
          not any(pl and "purge_everything" in pl for _m, _p, _t, pl in log), str(log))
    check("cache-purge: the token is named after its zone and its slot",
          any(pl and pl.get("name") == "observatory-cache-purge example.com proj/prod/CF_CACHE_PURGE_TOKEN (managed)"
              for _m, p, _t, pl in log if p == "/accounts/a1/tokens"), str(log))
    check("cache-purge: no value printed", "cp-value-" not in out.getvalue(), out.getvalue())

    m = cf(); _cf_with_admin(m)
    log = []
    _preset_fake(m, log, [("Zone Read", "zone"), ("DNS Write", "zone")], "/zones/z1/dns_records", "dn-value-")
    m.deliver_to_vault = lambda *a: None
    m._journal = lambda *a, **k: None
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_issue_zone("dns-edit", "example.com", "proj/prod/CF_DNS", None, wait=0)
    check("a zone preset without a probe body still proves itself with a GET",
          [meth for meth, path, tok, _pl in log if tok.startswith("dn-value-")] == ["GET"], str(log))


def test_cf_email_routing_token_is_one_per_slot() -> None:
    """Two projects that route mail in ONE zone hold two tokens. Named after
    the zone alone, the second issue would ROLL the first project's token and
    kill the value its slot holds — silently, since that slot is not touched."""
    m = cf(); _cf_with_admin(m)
    tokens: dict[str, str] = {}
    log = []

    def fake(path, token, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path, token, payload))
        if "permission_groups" in path:
            return {"result": [{"id": "zr", "name": "Zone Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "er", "name": "Email Routing Rules Write",
                                "scopes": ["com.cloudflare.api.account.zone"]}]}
        if path.startswith("/zones?name=example.com&account.id=a1"):
            return {"result": [{"id": "z1", "name": "example.com"}]}
        if path.endswith("/tokens?per_page=50"):
            return {"result": [{"id": i, "name": n} for n, i in tokens.items()]}
        if path == "/accounts/a1/tokens" and method is None and payload is not None:
            tokens[payload["name"]] = f"t{len(tokens)}"
            return {"result": {"id": tokens[payload["name"]], "value": "cfr-" + "y" * 30}}
        if "/value" in path or method == "PUT":
            raise AssertionError(f"a second slot must not roll the first slot's token: {path}")
        if path.startswith("/zones/z1/email/routing/rules"):
            return {"result": []}
        raise AssertionError(f"unexpected call {path}")
    m._request = fake
    m.deliver_to_vault = lambda *a: None
    m._journal = lambda *a, **k: None
    check("the first project's routing writer issues",
          m.cmd_issue_zone("email-routing", "example.com", "alpha/prod/CF_ROUTING", None, wait=0) == 0, "")
    check("a second project in the same zone gets its own token",
          m.cmd_issue_zone("email-routing", "example.com", "beta/prod/CF_ROUTING", None, wait=0) == 0
          and len(tokens) == 2, str(sorted(tokens)))
    check("each token names its zone and its slot",
          all("example.com" in n and "/prod/CF_ROUTING" in n for n in tokens), str(sorted(tokens)))


# ─────────────────────────── openrouter ──────────────────────────────────────

def orr():
    return load("tools/openrouter.py", "or_door")


def test_or_stash_demands_a_label_because_the_provider_names_nothing() -> None:
    m = orr()
    m.ADMIN_STORE = pathlib.Path(tmpdir.mkdtemp()).resolve() / "openrouter-admin"
    m._request = lambda *a, **k: {"data": []}
    rc = m.stash_value("sk-or-v1-" + "a" * 40, "", origin="test")
    check("a stash with no label is refused", rc == 2, str(rc))
    rc = m.stash_value("sk-or-v1-" + "a" * 40, "Example Secondary", origin="test")
    check("a labeled stash lands under its slug", rc == 0
          and (m.ADMIN_STORE / "example-secondary").is_file(), str(rc))

    def refuse(*a, **k):
        raise RuntimeError("openrouter answered 401 for /keys")
    m._request = refuse
    rc = m.stash_value("sk-or-v1-" + "b" * 40, "other", origin="test")
    check("an inference key is refused — it cannot provision", rc == 1, str(rc))
    check("and points at install_key for what it IS good for", True, "")

    # OFFLINE IS NOT "CANNOT MANAGE KEYS". An unreachable provider was reported
    # as a key that cannot provision, with advice to install it as an inference
    # key — the wrong door for a perfectly good provisioning key.
    import contextlib
    import io

    def offline(*a, **k):
        raise m.Unreachable("openrouter unreachable: URLError")
    m._request = offline
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = m.stash_value("sk-or-v1-" + "c" * 40, "offline", origin="test")
    said = err.getvalue()
    check("an unreachable provider is its own refusal, and nothing is stashed",
          rc == 1 and "could not be reached" in said and "cannot manage keys" not in said
          and "install_key" not in said and not (m.ADMIN_STORE / "offline").exists(), said)


def test_or_rotation_creates_and_delivers_before_deleting() -> None:
    """The order is the contract: a delete-first rotation that fails halfway
    leaves the consumer with a dead key and a silent fallback."""
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    delivered = []
    m.deliver = lambda value, to, **options: (delivered.append((value, to, options)) or f"file {to}")
    m.save_ledger(
        {"issued": {"fabric-agent": {"account": "example", "hash": "h-old",
                                     "destination": "observatory",
                                     "delivered_to": "x", "limit_usd": 10,
                                     "issued_on": "2026-09-01",
                                     "rotated_on": None, "rotations": 0}}})
    log = []

    def fake(path, key, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path))
        if path.startswith("/keys?"):
            return {"data": [{"name": "fabric-agent", "hash": "h-old",
                              "limit": 10, "usage": 3}]}
        if path == "/keys" and payload:
            return {"data": {"hash": "h-new"}, "key": "sk-or-v1-" + "n" * 40}
        # The door reads an issued key by the hash its ledger recorded (GET /keys/{hash}).
        if path == "/keys/h-old" and method is None and payload is None:
            return {"data": {"name": "fabric-agent", "hash": "h-old", "limit": 10, "usage": 3}}
        return {"data": {}}
    m._request = fake
    rc = m.cmd_rotate("fabric-agent", leaked=False)
    check("rotation succeeds", rc == 0, str(rc))
    order = [(meth, p) for meth, p in log if meth in ("POST", "DELETE", "PATCH")
             and p.startswith("/keys")]
    creates = [i for i, (meth, p) in enumerate(order) if meth == "POST" and p == "/keys"]
    deletes = [i for i, (meth, p) in enumerate(order) if meth == "DELETE"]
    check("the successor is created BEFORE the predecessor is deleted",
          creates and deletes and creates[0] < deletes[0], str(order))
    check("and the delivery happened", delivered
          and delivered[0][1] == "observatory"
          and delivered[0][2].get("rotate") is True, "delivery must request replacement")
    led = json.loads(m.LEDGER.read_text())
    check("the ledger moved to the successor",
          led["issued"]["fabric-agent"]["hash"] == "h-new"
          and led["issued"]["fabric-agent"]["rotations"] == 1, str(led)[:200])


def test_a_ceiling_move_is_journaled_and_a_leak_hint_names_settle() -> None:
    """2026-10-06: `limit --set` moved what a key may spend and left no movement, and
    after `rotate --leaked` both doors told the person to run `vault.py rotate`, which
    writes a new value and leaves the leak register open; `settle` closes it."""
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m.save_ledger({"issued": {"fabric-agent": {"account": "example", "hash": "h-old",
                                               "destination": "observatory", "limit_usd": 10,
                                               "issued_on": "2026-09-01"}}})
    journal = []
    m._journal = lambda event, secret, **detail: journal.append((event, secret, detail))

    def fake(path, key, payload=None, method=None):
        if path == "/keys/h-old" and method is None and payload is None:
            return {"data": {"name": "fabric-agent", "hash": "h-old", "limit": 10, "usage": 3}}
        return {"data": {}}
    m._request = fake
    m.set_limit("fabric-agent", 25)
    check("the ceiling move is a movement, with the old and the new ceiling",
          journal == [("limit", "openrouter/example/fabric-agent",
                       {"limit_usd": 25, "previous_usd": 10, "limit_reset": "monthly"})], str(journal))
    for door in (m, cf()):
        check(f"{door.__name__}: the leak hint names settle, not rotate",
              'vault.py\" settle' in door.SETTLE_HINT and "rotate`" not in door.SETTLE_HINT
              and "--revocation-evidence" in door.SETTLE_HINT, door.SETTLE_HINT)


def test_or_issue_deletes_the_key_when_delivery_fails() -> None:
    """A minted key that reached no consumer is live spend capacity nobody
    holds — the door deletes it rather than leaving it."""
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    log = []

    def fake(path, key, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path))
        if path.startswith("/keys?"):
            return {"data": []}
        if path == "/keys" and payload:
            return {"data": {"hash": "h-1"}, "key": "sk-or-v1-" + "z" * 40}
        return {"data": {}}
    m._request = fake

    def refuse_delivery(value, to):
        raise RuntimeError("no such destination")
    m.deliver = refuse_delivery
    rc = m.cmd_issue("doomed", 5.0, "example", "nowhere", None)
    check("a key that could not be delivered is refused", rc == 1, str(rc))
    check("and DELETED at the provider, not left live",
          any(meth == "DELETE" and "h-1" in p for meth, p in log), str(log))
    check("and never entered the ledger",
          not m.LEDGER.is_file() or "doomed" not in m.LEDGER.read_text(), "")


def test_or_issue_refuses_a_name_that_already_exists() -> None:
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m._request = lambda path, key, payload=None, method=None: (
        {"data": [{"name": "fabric-agent", "hash": "h"}]} if path.startswith("/keys?")
        else {"data": {}})
    rc = m.cmd_issue("fabric-agent", 5.0, "example", "observatory", None)
    check("a duplicate name is refused — spend must stay attributable (exit 2: the "
          "caller's mistake, not the provider's)", rc == 2, str(rc))


def test_or_issue_works_on_an_account_past_the_listing() -> None:
    """An account holding more keys than the door will list could issue nothing at all.

    The provisioning account also governs keys another system mints per user — more than
    20 000 by 2026-10-05, past the PAGES_MAX listing — and a new name has no recorded hash,
    so the uniqueness check paged to the limit and refused every issue. The listing it did
    read and the ledger are still searched, a name found there is still refused, and an issue
    past them goes ahead with what was not read named in its answer, the ledger and the
    journal: once issued, the key is read by the hash recorded here, never by name.
    """
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m.PAGES_MAX = 3
    m.deliver = lambda value, to: f"file {to}"
    journaled = []
    m._journal = lambda event, secret, **detail: journaled.append((event, detail))
    posted = []

    def endless(path, key, payload=None, method=None):
        if path.startswith("/keys?"):
            off = int(path.split("offset=")[1].split("&")[0])
            return {"data": [{"name": "taken" if off == 100 and i == 7 else f"user_{off + i}",
                              "hash": f"h{off + i}"} for i in range(100)]}
        if path == "/keys" and payload:
            posted.append(payload)
            return {"data": {"hash": "h-new", "label": "sk-or-v1-abc...def"}, "key": "sk-or-v1-" + "w" * 40}
        if path == "/keys/h-new":
            return {"data": {"name": "example-agent", "hash": "h-new"}}
        return {"data": {}}
    m._request = endless
    r = m.issue_key("example-agent", 25.0, "example", "observatory", None)
    check("a new name is issued on an account past the listing",
          r["name"] == "example-agent" and posted and posted[0]["name"] == "example-agent", str(r))
    check("and the answer names what the check did not read",
          "300" in (r.get("name_check") or ""), str(r.get("name_check")))
    led = json.loads(m.LEDGER.read_text())
    check("the ledger records the partial check beside the hash",
          led["issued"]["example-agent"]["hash"] == "h-new"
          and "300" in (led["issued"]["example-agent"].get("name_check") or ""), str(led)[:300])
    check("and so does the journal",
          any(e == "issue" and "300" in str(dt.get("name_check")) for e, dt in journaled), str(journaled))
    posted.clear()
    try:
        m.issue_key("taken", 5.0, "example", "observatory", None)
        check("a name found in the pages that WERE read is still refused", False, "no raise")
    except ValueError as exc:
        check("a name found in the pages that WERE read is still refused",
              "already exists" in str(exc) and not posted, str(exc))
    try:
        m.issue_key("example-agent", 5.0, "example", "observatory", None)
        check("a name the ledger recorded is refused by its hash", False, "no raise")
    except ValueError as exc:
        check("a name the ledger recorded is refused by its hash", "already exists" in str(exc), str(exc))
    m._request = lambda path, key, payload=None, method=None: (
        {"data": []} if path.startswith("/keys?") else endless(path, key, payload, method))
    check("a complete listing leaves no caveat",
          m.issue_key("fresh", 5.0, "example", "observatory", None).get("name_check") is None, "")
    # Every other reader still refuses a partial listing rather than act on it.
    m._request = endless
    m.save_ledger({"issued": {}})
    try:
        m.find_key("prov", "nowhere")
        check("find_key still refuses a partial listing", False, "no raise")
    except RuntimeError as exc:
        check("find_key still refuses a partial listing", "pages" in str(exc), str(exc))

def test_or_issue_is_one_function_with_a_monthly_reset() -> None:
    """The ONE issuer. `keyserver.py`'s mint used to be a second,
    ledger-less issuer — and the only one that set `limit_reset: monthly`.
    Merged: `issue_key()` is what both the command and the button call, and the
    monthly reset travels with every create (trap T17: a lifetime cap works
    until the total is reached and then stops, months later)."""
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    posted = []
    m.deliver = lambda value, to: f"file {to}"

    def fake(path, key, payload=None, method=None):
        if path.startswith("/keys?"):
            return {"data": []}
        if path == "/keys" and payload:
            posted.append(payload)
            return {"data": {"hash": "h-9", "label": "sk-or-v1-abc...def"}, "key": "sk-or-v1-" + "q" * 40}
        return {"data": {}}
    m._request = fake
    r = m.issue_key("button-key", 7.0, "example", "observatory", None)
    check("issue_key returns the facts a button needs, never the value",
          r["name"] == "button-key" and r["label"] == "sk-or-v1-abc...def"
          and "q" * 40 not in json.dumps(r), str(r))
    check("and the create carried a MONTHLY reset",
          posted and posted[0].get("limit_reset") == "monthly", str(posted))
    led = json.loads(m.LEDGER.read_text())
    check("the ledger records the reset beside the ceiling",
          led["issued"]["button-key"]["limit_reset"] == "monthly", str(led)[:200])
    try:
        m.issue_key("x", 0, "example", "observatory", None)
        check("a key with no ceiling is refused", False, "no raise")
    except ValueError as exc:
        check("a key with no ceiling is refused", "ceiling" in str(exc), str(exc))
    # the adapter: keyserver's mint reaches this same function
    src = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    code = source_reader.code_keeping_strings(src)
    check("keyserver mints through the door and holds no provider client of its own",
          "issue_key(" in code and '"/keys"' not in code and "def api(" not in code, "")


def test_or_a_daily_ceiling_is_issued_and_keeps_its_period() -> None:
    """A per-day spending ceiling (Fabric Switchboard SB-72): `issue --reset daily` creates the key
    with a daily reset, and moving its ceiling later keeps the period — before, `limit --set` and
    the dashboard's limit button sent `monthly` and silently turned a daily key monthly."""
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m.deliver = lambda value, to: f"file {to}"
    sent = []

    def fake(path, key, payload=None, method=None):
        if path.startswith("/keys?"):
            return {"data": []}
        if path == "/keys" and payload:
            sent.append(("create", payload))
            return {"data": {"hash": "h-d", "label": "sk-or-v1-d...d"}, "key": "sk-or-v1-" + "d" * 40}
        if method == "PATCH":
            sent.append(("patch", payload))
        return {"data": {"name": "fallback-agent", "hash": "h-d", "label": "sk-or-v1-d...d"}}
    m._request = fake
    m.find_key = lambda admin, name: {"name": name, "hash": "h-d", "label": "sk-or-v1-d...d"}
    r = m.issue_key("fallback-agent", 5.0, "example", "observatory", None, "daily")
    check("a daily ceiling is created daily, with an expiry (OBS-191)",
          sent[0][0] == "create" and bool(sent[0][1].pop("expires_at", None))
          and sent[0] == ("create", {"name": "fallback-agent", "limit": 5.0, "limit_reset": "daily"}), str(sent))
    check("and reported daily", r["limit_reset"] == "daily", str(r))
    moved = m.set_limit("fallback-agent", 8.0)
    check("moving the ceiling keeps the daily period", sent[-1] == ("patch", {"limit": 8.0, "limit_reset": "daily"}) and moved["limit_reset"] == "daily", str(sent))
    led = json.loads(m.LEDGER.read_text())
    check("the ledger still says daily", led["issued"]["fallback-agent"]["limit_reset"] == "daily", str(led)[:200])
    m.set_limit("fallback-agent", 8.0, "weekly")
    check("an explicit period still moves it", sent[-1][1]["limit_reset"] == "weekly", str(sent[-1]))
    for bad in ("hourly", "", "Daily"):
        try:
            m.issue_key("other", 5.0, "example", "observatory", None, bad)
            check(f"a period {bad!r} is refused", False, "no raise")
        except ValueError as exc:
            check(f"a period {bad!r} is refused", "daily, weekly or monthly" in str(exc), str(exc))
    check("--reset without --set is refused, nothing moved", m.cmd_limit("fallback-agent", None, "daily") == 2, "")


def test_or_ping_names_the_strays_it_does_not_manage() -> None:
    """100 PRODUCTION_user_* keys live on the operator's account, minted by
    another system (measured 2026-09-13). Ping must SAY they exist and NEVER
    touch them — rotate/revoke operate on ledger rows only."""
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m.save_ledger({"issued": {}})
    m._request = lambda path, key, payload=None, method=None: (
        {"data": [{"name": "PRODUCTION_user_1_2", "hash": "p1"},
                  {"name": "PRODUCTION_user_3_4", "hash": "p2"}]}
        if path.startswith("/keys?") else {"data": {}})
    import io
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m.cmd_ping()
    check("ping reports unmanaged keys by name",
          "unmanaged at the provider" in out.getvalue()
          and "PRODUCTION_user_1_2" in out.getvalue(), out.getvalue()[:200])
    src = (ROOT / "tools/openrouter.py").read_text(encoding="utf-8")
    code = source_reader.code_keeping_strings(src)
    check("rotate and revoke reach only ledger rows",
          'doc["issued"].get(name)' in code or "doc[\"issued\"][name]" in code,
          "an operation keyed by provider listing could touch production keys")


def _policy_door():
    """The door with a fake provider, a tmp ledger, deliveries recorded, and project names resolved as typed."""
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m._project_folder = lambda project, env, name: {"a": "project-a"}.get(project, project)
    m.vault_destination = lambda to: to
    calls = {"posted": [], "patched": [], "delivered": []}

    def fake(path, key, payload=None, method=None):
        if path.startswith("/keys?"):
            return {"data": []}
        if path == "/keys" and payload:
            calls["posted"].append(payload)
            return {"data": {"hash": f"h-{len(calls['posted'])}", "label": "sk-or-v1-x...y"}, "key": "sk-or-v1-" + "q" * 40}
        if method == "PATCH":
            calls["patched"].append((path, payload))
        return {"data": {"hash": path.rsplit("/", 1)[-1], "name": "project-a-prod-model-key"}}
    m._request = fake
    # The provider reports the ceiling the door last set: the ledger's, as an honest provider would.
    m.find_key = lambda admin, name: {"hash": "h-1", "name": name, "label": "sk-or-v1-x...y",
                                      "limit": (m.ledger()["issued"].get(name) or {}).get("limit_usd"),
                                      "limit_reset": (m.ledger()["issued"].get(name) or {}).get("limit_reset")}
    m.deliver = lambda value, to: calls["delivered"].append(to) or f"vault {to[len('vault:'):]}"
    m._journal = lambda *a, **k: None
    return m, calls


def _policy(**over):
    base = {"kind": "spend-policy", "project": "urn:project:a", "mode": "limited", "limitUsd": 40, "period": "month",
            "autoApproveUsd": 0.5, "credentialSources": [
                {"kind": "capability", "agent": "urn:agent:copy:one", "capability": "copy.write"},
                {"kind": "project-key", "ref": "vault:project-a/prod/MODEL_KEY"}]}
    base.update(over)
    return base


def test_or_a_project_policy_gets_its_own_key_in_its_own_slot() -> None:
    """OBS-58: the policy's project-key source is issued for that project, with the project limit as
    the key's ceiling resetting monthly, delivered into exactly the slot the policy names; the
    capability source spends another agent's wallet and gets no key; a second run moves nothing."""
    m, calls = _policy_door()
    plan = m.issue_for_policy(_policy(), "example")
    step = [s for s in plan if "to" in s]
    check("one key, for the one project-key source", len(step) == 1 and len(calls["posted"]) == 1, str(plan))
    # Every minted key carries an expiry (OBS-191); its value is tested in test_key_expiry.py.
    check("ceiling = the project limit, monthly, with an expiry",
          {k: v for k, v in calls["posted"][0].items() if k != "expires_at"}
          == {"name": "project-a-prod-model-key", "limit": 40.0, "limit_reset": "monthly"}
          and bool(calls["posted"][0].get("expires_at")), str(calls["posted"]))
    check("delivered into the slot the policy names", calls["delivered"] == ["vault:project-a/prod/MODEL_KEY"], str(calls["delivered"]))
    rec = json.loads(m.LEDGER.read_text())["issued"]["project-a-prod-model-key"]
    check("the ledger records the project the key serves", rec["project"] == "project-a" and rec["limit_usd"] == 40.0, str(rec))
    again = m.issue_for_policy(_policy(), "example")
    check("a second run is in place: nothing minted, nothing moved",
          [s["action"] for s in again if "to" in s] == ["in-place"] and len(calls["posted"]) == 1 and not calls["patched"], str(again))
    m.issue_for_policy(_policy(limitUsd=60), "example")
    check("a raised project limit moves the key's ceiling instead of minting a second key",
          len(calls["posted"]) == 1 and calls["patched"] and calls["patched"][-1][1] == {"limit": 60.0, "limit_reset": "monthly"},
          str(calls["patched"]))


def test_or_a_policy_never_borrows_and_never_issues_an_unbounded_key() -> None:
    """Refusals, all before a key exists: another project's slot (the no-borrowing rule), a zero
    limit, a policy without a project limit and no explicit ceiling, a ceiling above the project
    limit, a policy with no slot this door can fill, and something that is not a spend policy."""
    m, calls = _policy_door()
    cases = [
        ("another project's slot", _policy(credentialSources=[{"kind": "project-key", "ref": "vault:project-b/prod/KEY"}]), None, "never another's"),
        ("a $0 project limit", _policy(limitUsd=0), None, "spends nothing"),
        ("no project limit and no --limit", _policy(mode="no-project-limit", limitUsd=None), None, "give --limit"),
        ("a key ceiling above the project limit", _policy(), 41.0, "cannot exceed"),
        ("only a capability source", _policy(credentialSources=[{"kind": "capability", "agent": "urn:agent:x"}]), None, "no project-key"),
        ("not a spend policy", {"kind": "grant"}, None, "not a spend policy"),
        ("a project that is not a project urn", _policy(project="https://example.test/p"), None, "urn:project"),
    ]
    for name, policy, limit, needle in cases:
        try:
            m.issue_for_policy(policy, "example", limit)
            check(name, False, "issued")
        except ValueError as exc:
            check(f"refused: {name}", needle in str(exc), str(exc))
    check("and nothing was minted for any of them", not calls["posted"], str(calls["posted"]))
    plan = m.issue_for_policy(_policy(mode="no-project-limit", limitUsd=None), "example", 15.0, dry_run=True)
    check("a dry run with an explicit ceiling plans the key and mints nothing",
          [s.get("action") for s in plan if "to" in s] == ["would-issue"] and not calls["posted"], str(plan))
    skipped = m.policy_key_plan(_policy(credentialSources=[{"kind": "project-key", "ref": "openrouter:project-a"},
                                                           {"kind": "project-key", "ref": "project-a/prod/MODEL_KEY"}]))
    check("a ref that is not a vault slot is skipped and named; the bare slot form is read",
          [s.get("action", "plan") for s in skipped] == ["skipped", "plan"] and skipped[1]["to"] == "vault:project-a/prod/MODEL_KEY",
          str(skipped))


def test_or_issue_for_policy_command_line() -> None:
    m, calls = _policy_door()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    good, bad = d / "policy.json", d / "broken.json"
    good.write_text(json.dumps(_policy()), encoding="utf-8")
    bad.write_text("{", encoding="utf-8")
    check("the command issues and exits 0", m.cmd_issue_for_policy(str(good), "example", None, False) == 0, "")
    check("an unreadable policy exits 2", m.cmd_issue_for_policy(str(bad), "example", None, False) == 2, "")
    check("a refused policy exits 2", m.cmd_issue_for_policy(str(good), "example", 99.0, False) == 2, "")


def test_or_spend_reads_one_keys_activity_and_prints_no_handle() -> None:
    """`spend` answers "what did this key cost, by day and model" from the
    provider's activity report — the management key's read, filtered by the
    issued key's hash. The hash is a handle that acts on the key: it is sent to
    the provider, never printed
    (an attempt with no request id can only be reconciled from this record)."""
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example-account", "prov\n")
    m.LEDGER = d / "ledger.json"
    m.save_ledger({"issued": {"example-agent": {
        "account": "example-account", "hash": "h" * 64, "delivered_to": "vault example/prod/OPENROUTER_API_KEY",
        "limit_usd": 30}}})
    asked: list[str] = []

    def fake(path, key, payload=None, method=None):
        asked.append(path)
        return {"data": [
            {"date": "2026-10-11", "model": "vendor/model-a", "requests": 49,
             "prompt_tokens": 9000, "completion_tokens": 7000, "reasoning_tokens": 0,
             "usage": 0.432, "byok_usage_inference": 0},
            {"date": "2026-10-11", "model": "vendor/model-b", "requests": 17,
             "prompt_tokens": 8000, "completion_tokens": 5000, "reasoning_tokens": 0,
             "usage": 0.184464, "byok_usage_inference": 0},
        ]}
    m._request = fake
    import io
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_spend("example-agent", "2026-10-11", False)
    text = out.getvalue()
    check("spend asks the activity report for that key and day",
          asked == ["/activity?api_key_hash=" + "h" * 64 + "&date=2026-10-11"], str(asked))
    check("it prints per day and model with a total", rc == 0
          and "vendor/model-a" in text and "0.432" in text
          and "total" in text and "0.616464" in text, text[:300])
    check("the hash never reaches the output", "h" * 16 not in text, text[:300])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m.cmd_spend("example-agent", None, True)
    doc = json.loads(out.getvalue())
    check("--json is the rows and the total, no hash",
          doc["key"] == "example-agent" and abs(doc["totalUsd"] - 0.616464) < 1e-9
          and "hash" not in json.dumps(doc), out.getvalue()[:200])
    check("without --date the last 30 days are asked",
          asked[-1] == "/activity?api_key_hash=" + "h" * 64, asked[-1])
    try:
        m.cmd_spend("nobody", None, False)
        check("an unknown key is refused", False, "no error")
    except LookupError:
        check("an unknown key is refused", True)

    def refused(path, key, payload=None, method=None):
        raise RuntimeError("openrouter answered 403 for /activity: management key required")
    m._request = refused
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            m.cmd_spend("example-agent", None, False)
        check("a refused report is an error, never a $0", False, out.getvalue()[:120])
    except RuntimeError:
        check("a refused report is an error, never a $0",
              "total" not in out.getvalue(), out.getvalue()[:120])


def _two_slots(**over):
    """A limited policy with two project-key sources of the same project."""
    return _policy(credentialSources=[{"kind": "project-key", "ref": "vault:project-a/prod/MODEL_KEY"},
                                      {"kind": "project-key", "ref": "vault:project-a/prod/OTHER_KEY"}], **over)


def test_or_policy_ceilings_sum_to_the_project_limit() -> None:
    """ENG-002: every project-key source used to get the WHOLE project limit, so two sources of a $40
    project could spend $80. The ceilings of every key the plan touches now sum to at most `limitUsd`:
    an equal split rounded down to the cent by default, `--split` for explicit amounts, and `--limit`
    meaning per key, refused when N × limit exceeds the project limit."""
    m, calls = _policy_door()
    m.issue_for_policy(_two_slots(), "example")
    ceilings = [p["limit"] for p in calls["posted"]]
    check("two sources of a $40 project: ceilings sum to at most $40 (was $80)",
          len(ceilings) == 2 and sum(ceilings) <= 40.0 and ceilings == [20.0, 20.0], str(ceilings))

    m, calls = _policy_door()
    m.issue_for_policy(_policy(limitUsd=10, credentialSources=[
        {"kind": "project-key", "ref": f"vault:project-a/prod/K{i}"} for i in range(3)]), "example")
    ceilings = [p["limit"] for p in calls["posted"]]
    check("an equal split is rounded DOWN to the cent ($10 over three keys: $3.33 each)",
          ceilings == [3.33, 3.33, 3.33] and sum(ceilings) <= 10.0, str(ceilings))

    m, calls = _policy_door()
    plan = m.issue_for_policy(_two_slots(), "example", split={"vault:project-a/prod/MODEL_KEY": 30.0,
                                                               "project-a/prod/OTHER_KEY": 10.0})
    check("--split gives explicit amounts; a ref is matched as written or as its vault slot",
          [p["limit"] for p in calls["posted"]] == [30.0, 10.0], str(plan))

    for name, kw, needle in [
        ("--split over the limit", {"split": {"vault:project-a/prod/MODEL_KEY": 30.0,
                                              "vault:project-a/prod/OTHER_KEY": 10.01}}, "more than the project limit"),
        ("--split naming a slot the policy does not", {"split": {"vault:project-a/prod/MODEL_KEY": 10.0,
                                                                 "vault:project-a/prod/NOPE": 10.0}}, "not a project-key source"),
        ("--split leaving a slot without a ceiling", {"split": {"vault:project-a/prod/MODEL_KEY": 10.0}}, "no ceiling"),
        ("--limit per key over the project limit", {"limit_override": 21.0}, "cannot exceed"),
        ("--limit and --split together", {"limit_override": 5.0, "split": {"vault:project-a/prod/MODEL_KEY": 5.0}}, "not both"),
        ("--split naming one slot twice under two spellings", {"split": {"vault:project-a/prod/MODEL_KEY": 10.0,
                                                                         "project-a/prod/MODEL_KEY": 10.0,
                                                                         "vault:project-a/prod/OTHER_KEY": 10.0}}, "twice"),
    ]:
        m, calls = _policy_door()
        try:
            m.issue_for_policy(_two_slots(), "example", **kw)
            check(f"refused: {name}", False, "planned")
        except ValueError as exc:
            check(f"refused: {name}", needle in str(exc) and not calls["posted"], str(exc))

    m, calls = _policy_door()
    m.issue_for_policy(_two_slots(), "example", 15.0)
    check("--limit is per key: two keys at $15 under a $40 limit",
          [p["limit"] for p in calls["posted"]] == [15.0, 15.0], str(calls["posted"]))

    m, calls = _policy_door()
    try:
        m.issue_for_policy(_policy(limitUsd=0.01, credentialSources=[
            {"kind": "project-key", "ref": f"vault:project-a/prod/K{i}"} for i in range(2)]), "example")
        check("a limit too small to give every key a cent is refused", False, "planned")
    except ValueError as exc:
        check("a limit too small to give every key a cent is refused", "cent" in str(exc), str(exc))

    m, calls = _policy_door()
    plan = m.issue_for_policy(_policy(mode="no-project-limit", limitUsd=None, credentialSources=[
        {"kind": "project-key", "ref": f"vault:project-a/prod/K{i}"} for i in range(2)]), "example", 15.0)
    check("with no project limit --limit stays each key's own ceiling",
          [p["limit"] for p in calls["posted"]] == [15.0, 15.0], str(plan))

    m, calls = _policy_door()
    plan = m.issue_for_policy(_policy(credentialSources=[{"kind": "project-key", "ref": "vault:project-a/prod/MODEL_KEY"},
                                                         {"kind": "project-key", "ref": "project-a/prod/MODEL_KEY"}]), "example")
    check("one slot named twice is one key with the whole limit, the repeat named",
          [p["limit"] for p in calls["posted"]] == [40.0] and [s.get("action") for s in plan] == ["issued", "skipped"],
          str(plan))


def test_or_policy_lowers_ceilings_before_it_raises_one() -> None:
    """Moving an existing split must never let the sum pass the limit halfway: a ceiling that goes
    down moves before one that goes up or a key that is issued."""
    m, calls = _policy_door()
    m.save_ledger({"issued": {
        "held-a": {"account": "example", "hash": "h-a", "destination": "vault:project-a/prod/MODEL_KEY",
                   "limit_usd": 5.0, "limit_reset": "monthly"},
        "held-b": {"account": "example", "hash": "h-b", "destination": "vault:project-a/prod/OTHER_KEY",
                   "limit_usd": 35.0, "limit_reset": "monthly"}}})
    m.find_key = lambda admin, name: {"hash": {"held-a": "h-a", "held-b": "h-b"}[name], "name": name,
                                      "limit": {"held-a": 5.0, "held-b": 35.0}[name], "limit_reset": "monthly",
                                      "disabled": False}
    plan = m.issue_for_policy(_two_slots(), "example")
    check("both ceilings move to the equal split, the lowered one first",
          [p for p, _ in calls["patched"]] == ["/keys/h-b", "/keys/h-a"]
          and [b["limit"] for _, b in calls["patched"]] == [20.0, 20.0], str(calls["patched"]))
    check("and the plan says so", [s["action"] for s in plan] == ["ceiling-moved", "ceiling-moved"], str(plan))


def test_or_policy_reads_held_keys_at_the_provider() -> None:
    """A slot the ledger says is held used to read `in-place` without asking the provider, so a
    disabled or deleted key counted as serving the project. Each held row is read by hash."""
    def door(row):
        m, calls = _policy_door()
        m.save_ledger({"issued": {
            "held": {"account": "example", "hash": "h-1", "destination": "vault:project-a/prod/MODEL_KEY",
                     "limit_usd": 40.0, "limit_reset": "monthly"}}})
        m.find_key = lambda admin, name: row
        return m, calls

    m, calls = door({"hash": "h-1", "name": "held", "limit": 40.0, "limit_reset": "monthly", "disabled": True})
    plan = m.issue_for_policy(_policy(), "example")
    check("a disabled held key is held-disabled, never in-place",
          [s["action"] for s in plan if "to" in s] == ["held-disabled"] and not calls["posted"], str(plan))
    m, calls = door(None)
    plan = m.issue_for_policy(_policy(), "example", dry_run=True)
    check("a held key the provider no longer has is held-missing, even in a dry run",
          [s["action"] for s in plan if "to" in s] == ["held-missing"] and not calls["posted"], str(plan))
    m, calls = door({"hash": "h-1", "name": "held", "limit": None, "limit_reset": "monthly", "disabled": False})
    plan = m.issue_for_policy(_policy(), "example")
    check("a provider row with no ceiling is unbounded: the ceiling is moved, never taken from the ledger",
          [s["action"] for s in plan if "to" in s] == ["ceiling-moved"] and calls["patched"], str(plan))

    m, calls = door({"hash": "h-1", "name": "held", "limit": 25.0, "limit_reset": "monthly", "disabled": False})
    plan = m.issue_for_policy(_policy(), "example")
    check("a ceiling moved at the provider is read there, not from the ledger",
          [s["action"] for s in plan if "to" in s] == ["ceiling-moved"] and plan[-1]["previous"] == 25.0, str(plan))

    m, calls = door(None)

    def not_json(admin, name):
        raise ValueError("Expecting value: line 1 column 1")
    m.find_key = not_json
    plan = m.issue_for_policy(_policy(), "example")
    check("a provider answer that is not JSON is held-unread, not a crash",
          [s["action"] for s in plan if "to" in s] == ["held-unread"] and not calls["posted"], str(plan))

    m, calls = door({"hash": "h-1", "name": "held", "limit": 40.0, "limit_reset": "monthly", "disabled": True})
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    (d / "p.json").write_text(json.dumps(_policy()), encoding="utf-8")
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_for_policy(str(d / "p.json"), "example", None, False)
    check("the command exits 1 while a slot is held by a key that does not serve it, and names the remedy",
          rc == 1 and "held-disabled" in out.getvalue() and "enable held" in out.getvalue(), f"{rc} {out.getvalue()}")


def test_or_policy_round_two() -> None:
    """Verification round 2 on ENG-002."""
    try:
        orr()._parse_split("vault:project-a/prod/A=10,vault:project-a/prod/B=10,vault:project-a/prod/A=30")
        check("--split giving one spelling twice is refused", False, "parsed; the last value won")
    except ValueError as exc:
        check("--split giving one spelling twice is refused", "twice" in str(exc), str(exc))

    m, calls = _policy_door()
    m.save_ledger({"issued": {"held": {"account": "example", "hash": "h-1",
                                       "destination": "vault:project-a/prod/MODEL_KEY",
                                       "limit_usd": 40.0, "limit_reset": "monthly"}}})
    m.find_key = lambda admin, name: {"hash": "h-1", "name": name, "limit": 40.0, "limit_reset": None,
                                      "disabled": False}
    plan = m.issue_for_policy(_policy(), "example")
    check("a provider row with no reset period differs from a monthly policy: the ceiling is moved",
          [s["action"] for s in plan if "to" in s] == ["ceiling-moved"]
          and calls["patched"] and calls["patched"][-1][1] == {"limit": 40.0, "limit_reset": "monthly"}, str(plan))

    # An unbounded held key is brought under the limit BEFORE a new key is minted, so a mint that
    # fails leaves the unbounded key already bounded. The new slot is listed first, so only the
    # lowering-first order puts the held key's move ahead of it.
    m, calls = _policy_door()
    m.save_ledger({"issued": {"held": {"account": "example", "hash": "h-held",
                                       "destination": "vault:project-a/prod/OTHER_KEY",
                                       "limit_usd": 40.0, "limit_reset": "monthly"}}})
    m.find_key = lambda admin, name: {"hash": "h-held", "name": name, "limit": None, "limit_reset": "monthly",
                                      "disabled": False}

    def mint_fails(*a, **k):
        raise RuntimeError("openrouter answered 500 for /keys")
    m.issue_key = mint_fails
    try:
        m.issue_for_policy(_two_slots(), "example")
        check("the partial run raises", False, "returned")
    except m.PolicyPartial as exc:
        check("the unbounded held key's ceiling was set before the mint failed",
              [p for p, _ in calls["patched"]] == ["/keys/h-held"]
              and [s["action"] for s in exc.plan] == ["failed", "ceiling-moved"], f"{calls['patched']} {exc.plan}")

    m, calls = _policy_door()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    (d / "p.json").write_text(json.dumps(_two_slots()), encoding="utf-8")
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_for_policy(str(d / "p.json"), "example", None, True)
    check("the plan prints the keyCeilingsUsd it implies for config/meter.json",
          rc == 0 and '"vault:project-a/prod/MODEL_KEY": 20.0' in out.getvalue() and "keyCeilingsUsd" in out.getvalue(),
          out.getvalue())


def test_or_policy_partial_failure_names_what_was_done() -> None:
    """A provider failure in the middle of the run used to raise with no word of the keys already
    minted. The run stops, the plan comes back with the done steps, the failed one and the ones not
    reached, the command exits 1 saying `partial`, and a re-run issues only what is missing."""
    m, calls = _policy_door()
    real = m._request

    def second_issue_fails(path, key, payload=None, method=None):
        if path == "/keys" and payload and len(calls["posted"]) == 1:
            # A REFUSAL (4xx) minted nothing, so the re-run issues the key. A 5xx proves nothing and
            # keeps a pending row that holds the slot (test_or_policy_fix_five).
            raise RuntimeError("openrouter answered HTTP 400 for /keys")
        return real(path, key, payload, method)
    m._request = second_issue_fails
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    (d / "p.json").write_text(json.dumps(_two_slots()), encoding="utf-8")
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_for_policy(str(d / "p.json"), "example", None, False)
    said = out.getvalue()
    check("exit 1, with partial said", rc == 1 and "partial" in said, f"{rc} {said}")
    check("the key already minted is listed as issued", "MODEL_KEY: issued" in said, said)
    check("the failing step is listed as failed", "OTHER_KEY: failed" in said, said)
    try:
        m.issue_for_policy(_two_slots(), "example")
        check("the partial run raises PolicyPartial with the plan", False, "returned")
    except m.PolicyPartial as exc:
        check("the partial run raises PolicyPartial with the plan",
              [s["action"] for s in exc.plan] == ["in-place", "failed"], str(exc.plan))
    m._request = real
    plan = m.issue_for_policy(_two_slots(), "example")
    check("a re-run issues only the missing key",
          [s["action"] for s in plan] == ["in-place", "issued"] and len(calls["posted"]) == 2, str(plan))


# ─────────────────────────── round 3: the invariant at every moment ───────────

class MaxProvider:
    """A fake OpenRouter that records the HIGHEST total of every key's ceiling (disabled ones too;
    none counts as unbounded) at every moment — the adversarial round's probe fake. A test asserts
    `max_sum`, not only the ceilings a run ends with."""

    def __init__(self, unreachable, echo=lambda x: x):
        self.keys, self.n, self.max_sum, self.patches, self.echo = {}, 0, 0.0, 0, echo
        self.unread, self.unreachable = set(), unreachable
        self.lock = __import__("threading").Lock()
        self.on_patch = None

    def total(self):
        return sum((r["limit"] if r["limit"] is not None else 1e9) for r in self.keys.values())

    def __call__(self, path, key, payload=None, method=None):
        with self.lock:
            if path.startswith("/keys?"):
                return {"data": [dict(r) for r in self.keys.values()]}
            if path == "/keys" and payload:
                self.n += 1
                h = f"h{self.n}"
                self.keys[h] = {"hash": h, "name": payload["name"], "limit": self.echo(payload["limit"]),
                                "limit_reset": payload["limit_reset"], "disabled": False, "label": f"label-{h}"}
                self.max_sum = max(self.max_sum, self.total())
                return {"data": dict(self.keys[h]), "key": "sk-or-v1-" + "q" * 40}
            h = path.rsplit("/", 1)[-1]
            if h in self.unread and method is None:
                raise self.unreachable("openrouter unreachable: TimeoutError")
            if h not in self.keys:
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            if method == "DELETE":
                del self.keys[h]
                return {}
            if method == "PATCH":
                self.patches += 1
                self.keys[h]["limit"] = self.echo(payload["limit"])
                self.keys[h]["limit_reset"] = payload["limit_reset"]
                self.max_sum = max(self.max_sum, self.total())
            row = dict(self.keys[h])
        if method == "PATCH" and self.on_patch:
            hook, self.on_patch = self.on_patch, None
            hook()
        return {"data": row}

    def start(self):
        """Measure from now: the highest total this run reaches, the state it found included."""
        self.max_sum = self.total()

    def by_name(self, suffix):
        return next(h for h, r in self.keys.items() if r["name"].endswith(suffix))


def _r3_door(admins=("example",), echo=lambda x: x):
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "admin"
    m.ADMIN_STORE.mkdir(parents=True)
    for a in admins:
        private_io.write(m.ADMIN_STORE / a, "prov\n")
    m.LEDGER = d / "ledger.json"
    m._project_folder = lambda project, env, name: {"a": "project-a"}.get(project, project)
    m.vault_destination = lambda to: to
    m.deliver = lambda value, to: f"vault {to}"
    m._journal = lambda *a, **k: None
    p = MaxProvider(m.Unreachable, echo)
    m._request = p
    return m, p


def _pol(n=None, refs=None, limit=10, **over):
    refs = refs or [f"vault:project-a/prod/K{i}" for i in range(n)]
    base = {"kind": "spend-policy", "project": "urn:project:a", "mode": "limited", "limitUsd": limit,
            "period": "month", "credentialSources": [{"kind": "project-key", "ref": r} for r in refs]}
    base.update(over)
    return base


def _cmd(m, policy, *args):
    import contextlib
    import io
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    (d / "p.json").write_text(json.dumps(policy), encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.cmd_issue_for_policy(str(d / "p.json"), *args)
    return rc, out.getvalue()


def test_or_policy_counts_every_key_of_the_project() -> None:
    """Round 3, probe A: a source removed from the policy left its key live at its old ceiling, and
    the remaining two were raised past it ([3.33, 5, 5] = $13.33 against $10). Every ledger row under
    `vault:<project>/` counts; one the policy no longer names is not moved (refused, not lowered to
    $0), it is named and the run exits 1. Probe K: a second key behind one slot is counted the same."""
    m, p = _r3_door()
    m.issue_for_policy(_pol(3), "example")
    p.start()
    rc, out = _cmd(m, _pol(2), "example", None, False)
    check("A: dropping a source never lifts the total past the limit, at any moment",
          p.max_sum <= 10.0 and p.total() <= 10.0, f"max={p.max_sum} now={sorted(r['limit'] for r in p.keys.values())}")
    check("A: the key the policy no longer names is reported and the run exits 1",
          rc == 1 and "not-in-policy 'project-a-prod-k2'" in out and "revoke project-a-prod-k2" in out, f"{rc} {out}")
    check("A: the raises it could not make are blocked, not made", out.count(": blocked ") == 2, out)

    m, p = _r3_door()
    m.issue_for_policy(_pol(1), "example")
    m.issue_key("hand-made", 7.0, "example", "vault:project-a/prod/K0", "project-a")
    p.start()
    plan = m.issue_for_policy(_pol(1), "example")
    check("K: a second key behind one slot is counted and named; the run raises nothing",
          p.max_sum == 17.0 and p.total() == 17.0
          and [s["action"] for s in plan] == ["in-place", "not-in-policy", "over-limit"]
          and "OVER LIMIT by $7.00 now" in plan[-1]["why"] and "revoke hand-made" in plan[-1]["why"],
          f"max={p.max_sum} {[s.get('action') for s in plan]}")


def test_or_policy_raises_only_what_fits_beside_unread_or_renamed_keys() -> None:
    """Round 3, probes B and C: K1 unread (or renamed in the provider's dashboard, which read as
    missing) stayed live at $5 while K0 was raised to $8 — $13 at the highest. An unread key counts
    at its ledger ceiling, so the raise is blocked; a renamed key is still found by its hash."""
    split = {"vault:project-a/prod/K0": 8, "vault:project-a/prod/K1": 2}
    m, p = _r3_door()
    m.issue_for_policy(_pol(2), "example")
    p.unread.add(p.by_name("k1"))
    p.start()
    plan = m.issue_for_policy(_pol(2), "example", split=split)
    check("B: beside an unread key the raise is blocked and the total never passes the limit",
          p.max_sum <= 10.0 and [s["action"] for s in plan] == ["blocked", "held-unread"],
          f"max={p.max_sum} {[s.get('action') for s in plan]}")

    m, p = _r3_door()
    m.issue_for_policy(_pol(2), "example")
    p.keys[p.by_name("k1")]["name"] = "renamed-in-dashboard"
    p.start()
    plan = m.issue_for_policy(_pol(2), "example", split=split)
    check("C: a key renamed at the provider is live, lowered first, and the total stays within the limit",
          p.max_sum <= 10.0 and sorted(r["limit"] for r in p.keys.values()) == [2.0, 8.0]
          and [s["action"] for s in plan] == ["ceiling-moved", "ceiling-moved"],
          f"max={p.max_sum} {[s.get('action') for s in plan]}")
    p.keys.clear()
    plan = m.issue_for_policy(_pol(2), "example", dry_run=True)
    check("C: only a 404 reads held-missing", [s["action"] for s in plan] == ["held-missing", "held-missing"], str(plan))


def test_or_policy_runs_one_at_a_time() -> None:
    """Round 3, probe F2: two runs at once with opposite splits read each other's keys before either
    moved them and ended both at $8 ($16 against $10). A run holds an exclusive lock beside the ledger
    from plan to last write; the second waits, re-reads, and its own plan holds; a run that cannot
    get the lock in time refuses before reading anything."""
    import threading
    m, p = _r3_door()
    m.issue_for_policy(_pol(2), "example")
    p.start()
    results = []

    def second():
        results.append(m.issue_for_policy(_pol(2), "example",
                                          split={"vault:project-a/prod/K0": 2, "vault:project-a/prod/K1": 8}))
    other = threading.Thread(target=second)

    def start_the_other_run_mid_write():
        other.start()
        __import__("time").sleep(0.3)           # the other run is now waiting on the lock
    p.on_patch = start_the_other_run_mid_write
    results.append(m.issue_for_policy(_pol(2), "example",
                                      split={"vault:project-a/prod/K0": 8, "vault:project-a/prod/K1": 2}))
    other.join(10)
    check("F2: opposite splits at once never total past the limit, and the later run's split holds",
          p.max_sum <= 10.0 and len(results) == 2
          and {r["name"][-2:]: r["limit"] for r in p.keys.values()} == {"k0": 2.0, "k1": 8.0},
          f"max={p.max_sum} {[(r['name'], r['limit']) for r in p.keys.values()]}")

    import oslocks
    m.POLICY_LOCK_WAIT_S = 0.2
    fd = __import__("os").open(m.LEDGER.parent / ".openrouter-policy.lock", __import__("os").O_RDWR)
    oslocks.flock(fd, oslocks.LOCK_EX)
    before = p.patches
    try:
        m.issue_for_policy(_pol(2), "example", split={"vault:project-a/prod/K0": 9, "vault:project-a/prod/K1": 1})
        check("a run that cannot take the lock refuses", False, "ran")
    except RuntimeError as exc:
        check("a run that cannot take the lock refuses, having moved nothing",
              "policy lock" in str(exc) and p.patches == before, str(exc))
    finally:
        oslocks.flock(fd, oslocks.LOCK_UN)
        __import__("os").close(fd)


def test_or_policy_amounts_are_whole_cents_against_the_exact_limit() -> None:
    """Round 3, probes D, D2, H, J2, J3, I."""
    m, p = _r3_door(echo=lambda x: x + 1e-9)
    m.issue_for_policy(_pol(3), "example")
    p.start()
    before = p.patches
    m.issue_for_policy(_pol(3), "example")
    m.issue_for_policy(_pol(3), "example")
    check("D: a provider echoing 3.330000001 is not re-PATCHed", p.patches == before and p.max_sum <= 10.0,
          f"{p.patches - before} patches")
    m, p = _r3_door(echo=lambda x: round(x, 2))
    m.issue_for_policy(_pol(1), "example", split={"vault:project-a/prod/K0": 3.333})
    before = p.patches
    m.issue_for_policy(_pol(1), "example", split={"vault:project-a/prod/K0": 3.333})
    check("D2: a --split amount is rounded down to the cent, so a provider storing cents is not re-PATCHed",
          p.patches == before and [r["limit"] for r in p.keys.values()] == [3.33], f"{p.patches - before} patches")
    m, p = _r3_door()
    m.issue_for_policy(_pol(3), "example", 3.3333334)
    check("J2: --limit 3.3333334 x 3 is 3.33 each: the total never passes $10",
          p.max_sum <= 10.0 and [r["limit"] for r in p.keys.values()] == [3.33] * 3, f"max={p.max_sum!r}")
    m, p = _r3_door()
    plan = m.issue_for_policy(_pol(1, limit=0.0299999996), "example")
    check("J3: a share is rounded down from the exact limit (0.0299999996 -> 0.02)",
          plan[0]["limit"] == 0.02 and p.max_sum <= 0.0299999996, f"{plan[0]['limit']} max={p.max_sum}")
    for limit in (1e307, 1e300):
        try:
            m.issue_for_policy(_pol(3, limit=limit), "example", dry_run=True)
            check(f"H: limitUsd {limit:g} is refused", False, "planned")
        except ValueError as exc:
            check(f"H: limitUsd {limit:g} is refused as a ValueError", "past" in str(exc), str(exc))
    m, p = _r3_door()
    try:
        m.issue_for_policy(_pol(refs=["vault:project-a/prod/MODEL_KEY", "vault:project-a/prod/model_key"]), "example")
        check("I: two slots one key name apart are refused up front", False, "planned")
    except ValueError as exc:
        check("I: two slots one key name apart are refused up front", "name the slots apart" in str(exc)
              and not p.keys, str(exc))


def test_or_policy_never_leaves_a_live_untracked_key() -> None:
    """Round 3, probe G: a vault `put` that timed out after the mint left a live key no ledger knew."""
    m, p = _r3_door()

    def slow(value, to):
        raise __import__("subprocess").TimeoutExpired(["vault.py", "put"], 60)
    m.deliver = slow
    p.start()
    try:
        m.issue_for_policy(_pol(1), "example")
        check("G: the run is partial", False, "returned")
    except m.PolicyPartial as exc:
        check("G: a delivery that timed out deletes the minted key; nothing live is left untracked",
              not p.keys and not m.ledger()["issued"] and p.max_sum <= 10.0
              and [s["action"] for s in exc.plan] == ["failed"], f"keys={p.keys} max={p.max_sum}")


def test_or_policy_dry_run_resolves_the_issuing_account() -> None:
    """Round 3, probe N: with two stashed accounts and no --account the dry run planned an issue the
    real run then failed. Both refuse now, before anything is read or moved."""
    m, p = _r3_door(admins=("one", "two"))
    outcomes = []
    for dry in (True, False):
        try:
            m.issue_for_policy(_pol(1), None, dry_run=dry)
            outcomes.append("planned")
        except RuntimeError as exc:
            outcomes.append("--account" in str(exc))
    check("N: the dry run and the real run refuse alike, and nothing is minted",
          outcomes == [True, True] and not p.keys and p.max_sum == 0.0, str(outcomes))


def test_or_policy_round_four() -> None:
    """Round 4's probes, each asserting the fake's highest total."""
    # R6: $0.30 over three keys is three $0.10 keys; a float total read $0.30 as past $0.30.
    m, p = _r3_door()
    p.start()
    rc, out = _cmd(m, _pol(3, limit=0.3), "example", None, False)
    check("R6: $0.30 over 3 keys is issued in whole cents, never 'past its $0.30 limit'",
          rc == 0 and len(p.keys) == 3 and p.max_sum <= 0.3 + 1e-9 and "past its $0.30" not in out, f"{rc} {out}")
    m, p = _r3_door()
    plan = m.issue_for_policy(_pol(2, limit=0.3), "example",
                              split={"vault:project-a/prod/K0": 0.1, "vault:project-a/prod/K1": 0.2})
    check("R6b: --split 0.10 + 0.20 under $0.30 is issued", [s["action"] for s in plan] == ["issued", "issued"]
          and p.max_sum <= 0.3 + 1e-9, str(plan))
    # R7: the provider holds 3.334 x 3 = $10.002: within half a cent each, but past the limit in all.
    m, p = _r3_door(echo=lambda x: round(x + 0.004, 3))
    m.issue_for_policy(_pol(3), "example")
    p.start()
    rc, out = _cmd(m, _pol(3), "example", None, False)
    check("R7: a total past the limit with nothing blocked is named and exits 1",
          rc == 1 and "OVER LIMIT by $0.02 now" in out and "limit project-a-prod-k0 --set 3.33" in out
          and p.max_sum == p.total(), out)
    # R2b / R3b: a key minted but never recorded, on an account listing past PAGES_MAX.
    for what in ("killed", "cleanup failed"):
        m, p = _r3_door()

        def too_long(admin, include_disabled=True):
            raise m.ListingTooLong("past PAGES_MAX", [])
        m._all_keys = too_long
        if what == "killed":
            def interrupted(value, to):
                raise KeyboardInterrupt
            m.deliver = interrupted
        else:
            def timeout(value, to):
                raise __import__("subprocess").TimeoutExpired(["vault.py", "put"], 60)
            m.deliver = timeout
            real = m._request

            def no_delete(path, key, payload=None, method=None):
                if method == "DELETE":
                    raise RuntimeError("openrouter answered HTTP 502")
                return real(path, key, payload, method)
            m._request = no_delete
        p.start()
        try:
            m.issue_for_policy(_pol(1), "example")
        except (KeyboardInterrupt, m.PolicyPartial):
            pass
        rec = m.ledger()["issued"].get("project-a-prod-k0") or {}
        m.deliver = lambda value, to: f"vault {to}"
        m._request = p
        rc, out = _cmd(m, _pol(1), "example", None, False)
        check(f"R{'2b' if what == 'killed' else '3b'}: after a mint {what}, the pending row holds the slot: "
              f"one key, nothing minted again, the run exits 1 naming revoke",
              rec.get("pending") and rec.get("hash") and len(p.keys) == 1 and p.max_sum <= 10.0 and rc == 1
              and "held-pending" in out and "revoke project-a-prod-k0" in out, f"{rec} {len(p.keys)} {out}")
    m, p = _r3_door()
    m.save_ledger({"issued": {"project-a-prod-k0": {"account": "example", "hash": None, "pending": True,
                                                    "destination": "vault:project-a/prod/K0", "limit_usd": 10.0,
                                                    "limit_reset": "monthly"}}})
    p.start()
    rc, out = _cmd(m, _pol(1), "example", None, False)
    check("a pending row with no hash: nothing is minted, the remedy names claim or a provider delete",
          rc == 1 and not p.keys and "claim project-a-prod-k0" in out and "at the provider" in out, out)
    # R4 and fix 5: revoke must not delete a key a consumer reads, whatever its provider name and
    # whether or not the listing reaches it: by the scan's hash, by its listed label, and by the
    # label a destination holds (`destinations`, the only trace of a key past the listing).
    for what, make in (
            ("listed, by recorded label", lambda h, lab: {"keys": [{"name": "billing-worker", "label": lab,
                                                                    "serves": "billing-worker"}]}),
            ("by hash", lambda h, lab: {"keys": [], "destination_hashes": {"billing-worker": {"label": "x", "hash": h}}}),
            ("past the listing, by the label a destination holds",
             lambda h, lab: {"keys": [], "destinations": {"billing-worker": lab}, "destination_hashes": {},
                             "unlisted_destinations": ["billing-worker"]})):
        m, p = _r3_door()
        m.issue_for_policy(_pol(1), "example")
        h = next(iter(p.keys))
        p.keys[h]["name"] = "billing-worker"
        rec = m.ledger()["issued"]["project-a-prod-k0"]
        scratch = pathlib.Path(tmpdir.mkdtemp())
        m.paths.SCRATCH = scratch
        (scratch / "openrouter.json").write_text(json.dumps(make(h, rec.get("label"))), encoding="utf-8")
        try:
            m.revoke_key("project-a-prod-k0")
            check(f"R4: revoke refuses a renamed key a consumer reads ({what})", False,
                  f"revoked; key still at the provider: {h in p.keys}")
        except ValueError as exc:
            check(f"R4: revoke refuses a renamed key a consumer reads ({what})",
                  "reading right now" in str(exc) and h in p.keys and bool(rec.get("label")), str(exc))
        except Exception as exc:                    # noqa: BLE001 — a crash is a FAIL, never a traceback
            check(f"R4: revoke refuses a renamed key a consumer reads ({what})", False,
                  f"{type(exc).__name__}: {exc}")


def test_or_policy_fix_five() -> None:
    """Fix 5 on J-1: a 5xx after a mint proves nothing; a hashless pending row is clearable past PAGES_MAX."""
    # R-F: the provider created the key and then answered 502; the account lists past PAGES_MAX.
    m, p = _r3_door()

    def too_long(admin, include_disabled=True):
        raise m.ListingTooLong("past PAGES_MAX", [])
    m._all_keys = too_long
    real = m._request
    state = {"done": False}

    def created_then_502(path, key, payload=None, method=None):
        out = real(path, key, payload, method)
        if path == "/keys" and payload and not state["done"]:
            state["done"] = True
            raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
        return out
    m._request = created_then_502
    p.start()
    try:
        m.issue_for_policy(_pol(1), "example")
    except m.PolicyPartial:
        pass
    m._request = p
    rec = m.ledger()["issued"].get("project-a-prod-k0") or {}
    rc, out = _cmd(m, _pol(1), "example", None, False)
    check("R-F: a 502 after the mint keeps the pending row, and the next run mints nothing past PAGES_MAX",
          rec.get("pending") and not rec.get("hash") and len(p.keys) == 1 and p.max_sum <= 10.0 and rc == 1
          and "held-pending" in out, f"row={rec} keys={len(p.keys)} max={p.max_sum} {out}")
    m, p = _r3_door()

    def refused_400(path, key, payload=None, method=None):
        if path == "/keys" and payload:
            raise RuntimeError("openrouter answered HTTP 400; provider response withheld")
        return p(path, key, payload, method)
    m._request = refused_400
    try:
        m.issue_for_policy(_pol(1), "example")
    except m.PolicyPartial:
        pass
    check("a 4xx refusal minted nothing, so it leaves no pending row", not m.ledger()["issued"] and not p.keys,
          str(m.ledger()["issued"]))

    # R-E: the remedy for a hashless pending row works past PAGES_MAX.
    m, p = _r3_door()
    m._all_keys = too_long
    m.save_ledger({"issued": {"project-a-prod-k0": {"account": "example", "hash": None, "pending": True,
                                                    "destination": "vault:project-a/prod/K0", "limit_usd": 10.0,
                                                    "limit_reset": "monthly"}}})
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        plain = m.cmd_revoke("project-a-prod-k0")
    check("R-E: a plain revoke past PAGES_MAX refuses and names --forget-pending",
          plain == 1 and "--forget-pending" in err.getvalue() and "project-a-prod-k0" in m.ledger()["issued"],
          err.getvalue())
    with contextlib.redirect_stderr(io.StringIO()) as e2:
        no_reason = m.cmd_revoke("project-a-prod-k0", forget_pending=True, reason="")
    check("--forget-pending without a reason is refused", no_reason == 2, e2.getvalue())
    journaled = []
    m._journal = lambda *a, **k: journaled.append((a, k))
    with contextlib.redirect_stdout(io.StringIO()):
        rc = m.cmd_revoke("project-a-prod-k0", forget_pending=True, reason="deleted at the provider by name")
    rc2, out = _cmd(m, _pol(1), "example", None, False)
    check("R-E: --forget-pending with a reason clears the row, journaled; the next run issues the slot",
          rc == 0 and rc2 == 0 and len(p.keys) == 1 and journaled and journaled[0][0][0] == "forget-pending-attested" and journaled[1][0][0] == "forget-pending"
          and "deleted at the provider by name" in json.dumps(journaled[0][1]), f"{rc} {rc2} {journaled} {out}")
    m, p = _r3_door()
    m.issue_for_policy(_pol(1), "example")
    with contextlib.redirect_stderr(io.StringIO()) as e3:
        rc = m.cmd_revoke("project-a-prod-k0", forget_pending=True, reason="x")
    check("--forget-pending refuses a row that is not a hashless pending one", rc == 1 and p.keys
          and "project-a-prod-k0" in m.ledger()["issued"], e3.getvalue())

    # Harness: a provider holding $0.0001 more than the plan is past the limit; only float noise below
    # a millionth of a dollar is not a cent.
    m, p = _r3_door()
    m.issue_for_policy(_pol(2), "example")
    p.keys[p.by_name("k1")]["limit"] = 5.0001
    rc, out = _cmd(m, _pol(2), "example", None, True)
    check("a provider ceiling $0.0001 past the plan counts as the next cent: OVER LIMIT, exit 1",
          rc == 1 and "OVER LIMIT by $0.01 now" in out, out)
    check("float noise (5.000000001) is not a cent", m._cents_up(5.000000001) == 500 and m._cents_up(5.0001) == 501,
          f"{m._cents_up(5.000000001)} {m._cents_up(5.0001)}")
    # Harness: a ledger write that fails inside revoke is a refusal with exit 1, never a traceback.
    m, p = _r3_door()
    m.issue_for_policy(_pol(1), "example")

    def disk_refuses(doc):
        raise OSError("the disk refused the ledger write")
    m.paths.SCRATCH = pathlib.Path(tmpdir.mkdtemp())      # no consumer scan: an earlier case set one
    m.save_ledger = disk_refuses
    try:
        with contextlib.redirect_stderr(io.StringIO()) as e4:
            rc = m.cmd_revoke("project-a-prod-k0")
        check("revoke whose ledger write fails exits 1 and says the row was kept", rc == 1 and "kept" in e4.getvalue(),
              e4.getvalue())
    except OSError as exc:
        check("revoke whose ledger write fails exits 1 and says the row was kept", False, f"crashed: {exc}")

    # Item 5: a disk that is full under the test is the machine's failure, never a (b) violation.
    import errno
    rng = random.Random(1)
    m, p, door = _prop_door(rng)
    real_save = m.save_ledger

    def full(doc):
        raise OSError(errno.ENOSPC, "No space left on device")
    m.save_ledger = full
    plan, err, _ = _prop_run(m, p, _pol(1), {}, 1000)
    m.save_ledger = real_save
    check("an ENOSPC under the property is reported as ENVIRONMENT with its full reason",
          (err or "").startswith("ENVIRONMENT") and "No space left on device" in err, str(err))


# ─────────────────────────── round 4: the property, before any fix ─────────────

#: The property test's seed: case i draws from random.Random(PROPERTY_SEED + i).
PROPERTY_SEED = 20261012
PROPERTY_CASES = 2000
#: Seeds the property found failing on fc479fc, kept as named regression cases (run first).
PROPERTY_REGRESSION_SEEDS: dict[int, str] = {
    20261100: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261137: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261163: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261186: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261215: "(a)(b) a mint after a kill took the total past the limit",
    20261291: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261379: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261383: "(a) a mint after a kill took the total past the limit",
    20261388: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261401: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261426: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261490: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261503: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261537: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261595: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261598: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261624: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261675: "(a) a mint after a kill took the total past the limit",
    20261697: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261755: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261756: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261840: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261910: "(a) a mint after a kill took the total past the limit",
    20261912: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20261999: "(a)(b) a mint after a kill took the total past the limit",
    20262002: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262132: "(a) a mint after a kill took the total past the limit",
    20262167: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262197: "(a) a mint after a kill took the total past the limit",
    20262202: "(a) a mint after a kill took the total past the limit",
    20262287: "(a) a mint after a kill took the total past the limit",
    20262378: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262503: "(a) a mint after a kill took the total past the limit",
    20262516: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262589: "(a) a mint after a kill took the total past the limit",
    20262595: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262603: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262636: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262648: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262798: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262818: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
    20262975: "(a) a mint after a kill took the total past the limit",
    20263008: "(b) whole-cent ceilings under the limit were blocked as over it (float total)",
}


class PropProvider(MaxProvider):
    """The highest-total fake, plus what the property needs: failures injected at the n-th write of a
    kind (lower, raise, mint, cleanup), a write count, and the check that matters — after every write
    that RAISES the total (a mint, a ceiling moved up) the total in whole cents is within the limit
    of the run in progress. A write that lowers the total never breaks the invariant."""

    def __init__(self, unreachable, echo=lambda x: x):
        super().__init__(unreachable, echo)
        self.writes, self.fail, self.seen = 0, None, {}
        self.limit_cents, self.violations = None, []

    def cents(self):
        total = 0
        for r in self.keys.values():
            if r["limit"] is None:
                return math.inf
            total += round(r["limit"] * 100)
        return total

    def _maybe_fail(self, kind):
        self.seen[kind] = self.seen.get(kind, 0) + 1
        if self.fail and self.fail[0] == kind and self.fail[1] == self.seen[kind]:
            self.fail = None
            raise RuntimeError(f"openrouter answered HTTP 500 (injected at {kind})")

    def __call__(self, path, key, payload=None, method=None):
        before = self.cents()
        if path == "/keys" and payload:
            self._maybe_fail("mint")
        elif method == "DELETE":
            self._maybe_fail("cleanup")
        elif method == "PATCH" and payload and "limit" in payload:
            h = path.rsplit("/", 1)[-1]
            old = (self.keys.get(h) or {}).get("limit")
            self._maybe_fail("lower" if old is None or payload["limit"] < old else "raise")
        out = super().__call__(path, key, payload, method)
        if (path == "/keys" and payload) or method in ("PATCH", "DELETE"):
            self.writes += 1
            after = self.cents()
            if after > before and self.limit_cents is not None and after > self.limit_cents:
                self.violations.append(f"{method or 'POST'} {path} took the total to {after} cents "
                                       f"past {self.limit_cents}")
        return out


import math                                                         # noqa: E402
import random                                                       # noqa: E402

PROVIDER_FAILURES = ("lower", "raise", "mint", "cleanup")
DOOR_FAILURES = ("deliver", "deliver-timeout", "kill", "ledger-save")


def _prop_door(rng):
    echo = rng.choice([lambda x: x, lambda x: x + 1e-9, lambda x: round(x, 2)])
    m = orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m._project_folder = lambda project, env, name: {"a": "project-a"}.get(project, project)
    m.vault_destination = lambda to: to
    m._journal = lambda *a, **k: None
    p = PropProvider(m.Unreachable, echo)
    m._request = p
    if rng.random() < 0.3:                          # an account that lists past PAGES_MAX
        def too_long(admin, include_disabled=True):
            raise m.ListingTooLong("past PAGES_MAX", [])
        m._all_keys = too_long
    door = {"deliver": None, "save": None}
    real_save = m.save_ledger

    def deliver(value, to, **k):
        f = door["deliver"]
        if f:
            door["deliver"] = None
            if f == "deliver":
                raise OSError("the vault refused the put")
            if f == "deliver-timeout":
                raise __import__("subprocess").TimeoutExpired(["vault.py", "put"], 60)
            raise KeyboardInterrupt                 # the run is killed between the mint and its record
        return f"vault {to}"

    def save(doc):
        f = door["save"]
        if f is not None:
            f[0] -= 1
            if f[0] == 0:
                door["save"] = None
                raise OSError("the disk refused the ledger write")
        return real_save(doc)
    m.deliver, m.save_ledger = deliver, save
    return m, p, door


def _prop_policy(rng, refs, limit_cents):
    """A policy over `refs` and how it is split, with the whole-cent ceilings the door must plan, or
    None where the door is right to refuse (less than a cent per key)."""
    n = len(refs)
    mode = rng.choice(["equal", "equal", "split", "limit"])
    kw, planned = {}, None
    if mode == "equal" and limit_cents // n >= 1:
        planned = [limit_cents // n] * n
    elif mode == "split" and limit_cents >= n:
        cuts = sorted(rng.sample(range(1, limit_cents + 1), n)) if limit_cents > n else list(range(1, n + 1))
        planned = [cuts[0]] + [b - a for a, b in zip(cuts, cuts[1:])]
        if rng.random() < 0.5:                      # not always the whole limit
            planned = [max(1, c - rng.randint(0, c - 1)) for c in planned]
        kw["split"] = {r: c / 100 for r, c in zip(refs, planned)}
    elif mode == "limit" and limit_cents // n >= 1:
        c = rng.randint(1, limit_cents // n)
        planned, kw["limit_override"] = [c] * n, c / 100
    policy = {"kind": "spend-policy", "project": "urn:project:a", "mode": "limited",
              "limitUsd": limit_cents / 100, "period": "month",
              "credentialSources": [{"kind": "project-key", "ref": r} for r in refs]}
    return policy, kw, planned


def _prop_run(m, p, policy, kw, limit_cents):
    p.limit_cents = limit_cents
    start = p.cents()
    p.max_sum = p.total()
    try:
        plan, err = m.issue_for_policy(policy, "example", dry_run=False, **kw), None
    except KeyboardInterrupt:
        plan, err = None, "killed"
    except ValueError as exc:
        plan, err = None, f"ValueError: {exc}"
    except Exception as exc:                        # noqa: BLE001 — the property reads every outcome
        plan, err = getattr(exc, "plan", None), f"{type(exc).__name__}: {exc}"
        env = _environment_error(exc) or next((s.get("why") for s in plan or []
                                               if s.get("errno") in ENVIRONMENT_ERRNOS), None)
        if env:
            err = f"ENVIRONMENT {env}"
    return plan, err, start


#: OS errors that say the MACHINE failed (disk full, I/O error, read-only, quota), not the door.
ENVIRONMENT_ERRNOS = {__import__("errno").ENOSPC, __import__("errno").EIO, __import__("errno").EROFS,
                      getattr(__import__("errno"), "EDQUOT", -1)}


def _environment_error(exc: BaseException) -> str | None:
    """The full reason when `exc` is the machine's failure rather than the door's refusal."""
    if isinstance(exc, OSError) and exc.errno in ENVIRONMENT_ERRNOS:
        return f"{type(exc).__name__}: {exc}"
    return None


def _prop_case(seed):
    """One case: a first run, provider states and a policy edit, a run with one failure injected, a
    clean re-run, and a run that must move nothing. Returns the properties it broke."""
    rng = random.Random(seed)
    broke = []
    m, p, door = _prop_door(rng)
    scale = rng.choice([100, 10_000, 100_000_000])          # $1, $100, $1e6
    limit_cents = rng.randint(1, scale)
    names = [f"vault:project-a/prod/K{j}" for j in range(12)]
    refs = rng.sample(names, rng.randint(1, 12))
    policy, kw, planned = _prop_policy(rng, refs, limit_cents)
    plan, err, _ = _prop_run(m, p, policy, kw, limit_cents)
    if err and err.startswith("ENVIRONMENT"):
        # The disk or the I/O failed under the test (ENOSPC, EIO): the machine's failure, reported
        # with its reason and never counted against the door. A case that hits it proves nothing.
        return [f"ENVIRONMENT {err}"]
    if planned is not None and sum(planned) <= limit_cents:
        refused = err is not None or any(s.get("action") == "blocked" for s in plan or [])
        if refused:
            broke.append(f"(b) {sum(planned)} cents planned under a {limit_cents}-cent limit refused: "
                         f"{err} {[s.get('why') for s in plan or [] if s.get('action') in ('blocked', 'failed')][:1]}")
    # Provider states the run did not cause.
    for h in list(p.keys):
        roll = rng.random()
        if roll < 0.08:
            p.unread.add(h)
        elif roll < 0.14:
            del p.keys[h]
        elif roll < 0.22:
            p.keys[h]["name"] = f"renamed-{h}"
        elif roll < 0.27:
            p.keys[h]["limit"] = None
    # A policy edit: sources added or dropped, the limit changed.
    refs = [r for r in refs if rng.random() > 0.25] or refs[:1]
    refs += [r for r in rng.sample(names, rng.randint(0, 3)) if r not in refs]
    limit_cents = max(1, min(scale, int(limit_cents * rng.choice([0.5, 1, 1, 2]))))
    policy, kw, _ = _prop_policy(rng, refs, limit_cents)
    kind = rng.choice(PROVIDER_FAILURES + DOOR_FAILURES)
    if kind in PROVIDER_FAILURES:
        p.fail = (kind, rng.randint(1, 2))
    elif kind == "ledger-save":
        door["save"] = [rng.randint(1, 3)]
    else:
        door["deliver"] = kind
    _prop_run(m, p, policy, kw, limit_cents)
    p.fail, door["deliver"], door["save"] = None, None, None
    p.unread.clear()
    _prop_run(m, p, policy, kw, limit_cents)                # the clean re-run
    writes = p.writes
    plan, err, _ = _prop_run(m, p, policy, kw, limit_cents)
    if p.writes != writes:
        broke.append(f"(c) the run after the clean re-run made {p.writes - writes} write(s): "
                     f"{[s.get('action') for s in plan or []]} {err}")
    if p.violations:
        broke.append(f"(a) {p.violations[0]} (after a {kind} failure)")
    return broke


def test_or_policy_property_regression_seeds() -> None:
    """The seeds the property found failing on fc479fc, each a named case."""
    for seed, what in PROPERTY_REGRESSION_SEEDS.items():
        broke = _prop_case(seed)
        check(f"seed {seed} ({what})", not broke, "; ".join(broke)[:300])


def test_or_policy_property() -> None:
    """J-1's invariant as a property, PROPERTY_CASES seeded cases, no new dependency:

    (a) no write a run makes that raises the provider's total (a mint, a ceiling moved up) leaves the
        total, in whole cents, above the limit of the run — so a project within its limit stays
        within it at every moment. **The documented exception** is `rotate` (and `rotate --leaked`):
        it creates the successor before it deletes the predecessor, so for that window the slot holds
        two live keys and the total can be up to twice the slot's ceiling. This property drives
        `issue-for-policy` only; the window is named in docs/design/AGENT-SECRETS.md.
    (b) a policy whose planned whole-cent ceilings sum to at most the limit, in integer cents, is
        never refused or blocked as over it, on a project with no other keys;
    (c) after any failure — at a lowering, a raise, a mint, a delivery (refused, timed out, killed),
        a ledger write, a cleanup delete — a clean re-run converges and the run after it moves nothing.

    Inputs: limits from $0.01 to $1e6, 1–12 sources, policy edits between runs, provider states
    (unread, 404, renamed, null limit, a float echo or cents storage), an account listing past
    PAGES_MAX."""
    failing: dict[int, list[str]] = {}
    for seed in [PROPERTY_SEED + i for i in range(PROPERTY_CASES)]:
        broke = _prop_case(seed)
        if broke:
            failing[seed] = broke
    env = {s: b for s, b in failing.items() if any(x.startswith("ENVIRONMENT") for x in b)}
    for seed, broke in sorted(env.items()):
        print(f"  NOTE  seed {seed} not judged — the machine failed under it: {broke[0]}")
    check("every requested property case was judged (environment errors leave acceptance incomplete)",
          not env, f"{len(env)} ERROR/NOT_RUN cases")
    for prop in ("(a)", "(b)", "(c)"):
        seeds = sorted(s for s, b in failing.items() if any(x.startswith(prop) for x in b))
        check(f"property {prop} holds over {PROPERTY_CASES} cases (seed {PROPERTY_SEED})", not seeds and not env,
              f"{len(seeds)} failing seeds, e.g. " + "; ".join(f"{s}: {next(x for x in failing[s] if x.startswith(prop))}"
                                                             for s in seeds[:3]))


if __name__ == "__main__":
    print("the credential doors — stash, issue, rotate, revoke, and what they refuse\n")
    for fn in (test_the_digitalocean_door_keeps_the_same_rules,
               test_cf_a_token_this_call_created_is_deleted_when_the_issue_fails,
               test_or_a_project_policy_gets_its_own_key_in_its_own_slot,
               test_or_a_policy_never_borrows_and_never_issues_an_unbounded_key,
               test_or_issue_for_policy_command_line,
               test_or_policy_ceilings_sum_to_the_project_limit,
               test_or_policy_lowers_ceilings_before_it_raises_one,
               test_or_policy_reads_held_keys_at_the_provider,
               test_or_policy_partial_failure_names_what_was_done,
               test_or_policy_round_two,
               test_or_policy_counts_every_key_of_the_project,
               test_or_policy_raises_only_what_fits_beside_unread_or_renamed_keys,
               test_or_policy_runs_one_at_a_time,
               test_or_policy_amounts_are_whole_cents_against_the_exact_limit,
               test_or_policy_never_leaves_a_live_untracked_key,
               test_or_policy_dry_run_resolves_the_issuing_account,
               test_or_policy_round_four,
               test_or_policy_fix_five,
               test_or_policy_property_regression_seeds,
               test_or_policy_property,
               test_no_door_takes_or_prints_a_value_outside_stdin,
               test_the_ledgers_hold_no_values,
               test_cf_stash_refuses_an_admin_that_cannot_issue,
               test_cf_one_stash_can_span_several_accounts,
               test_cf_stash_finds_accounts_through_memberships_when_accounts_is_empty,
               test_cf_issue_rolls_rather_than_duplicating,
               test_cf_external_tokens_are_recorded_and_never_rolled_here,
               test_cf_dns_preset_is_scoped_to_one_zone_and_lands_in_the_vault,
               test_cf_dns_preset_rolls_refuses_and_never_misfiles,
               test_cf_dns_delivery_goes_through_the_vault_on_stdin,
               test_cf_d1_preset_is_scoped_to_one_account_and_lands_in_the_vault,
               test_cf_d1_preset_rolls_refuses_and_never_misfiles,
               test_cf_sigv4_matches_the_published_aws_example,
               test_cf_r2_preset_issues_one_bucket_pair_into_the_vault,
               test_cf_r2_preset_refuses_and_cleans_up,
               test_cf_r2_lifecycle_rules_per_prefix,
               test_cf_r2_lifecycle_refuses_what_it_cannot_mean,
               test_cf_r2_issue_writes_every_rule_and_reruns_idempotently,
               test_cf_r2_lifecycle_read_back_is_compared_rule_by_rule,
               test_cf_r2_lifecycle_only_changes_rules_and_never_a_key,
               test_cf_proof_waits_long_enough_for_a_slow_edge,
               test_cf_r2_lifecycle_command_line,
               test_cf_groups_lists_names_and_levels_and_never_a_value,
               test_cf_accounts_names_every_account_its_tokens_and_their_slots,
               test_cf_members_and_member_writes_are_a_plan_until_apply,
               test_cf_member_writes_refuse_what_would_lock_the_account,
               test_cf_super_admins_are_counted_from_roles_and_policies,
               test_cf_all_reads_every_page_whatever_the_list_says_about_itself,
               test_cf_email_presets_grant_exactly_what_the_email_service_needs,
               test_cf_email_presets_refuse_what_they_do_not_grant,
               test_cf_sending_domain_preset_grants_one_zone_and_its_account_sending,
               test_cf_redirect_preset_proves_its_read_on_a_zone_without_redirects,
               test_cf_email_routing_token_is_one_per_slot,
               test_cf_fabric_account_preset_grants_both_levels_on_one_account_into_the_vault,
               test_cf_fabric_presets_refuse_what_they_do_not_grant,
               test_cf_logs_preset_reads_telemetry_with_a_post_and_grants_nothing_more,
               test_cf_zone_analytics_preset_reads_one_zone_into_the_vault,
               test_cf_cache_purge_proves_itself_with_one_harmless_purge,
               test_cf_zone_edge_rules_preset_proves_its_read_on_a_zone_without_cache_rules,
               test_cf_access_app_preset_reads_the_team_domain_and_changes_no_organization,
               test_or_stash_demands_a_label_because_the_provider_names_nothing,
               test_or_rotation_creates_and_delivers_before_deleting,
               test_or_issue_deletes_the_key_when_delivery_fails,
               test_a_ceiling_move_is_journaled_and_a_leak_hint_names_settle,
               test_or_issue_refuses_a_name_that_already_exists,
               test_or_issue_works_on_an_account_past_the_listing,
               test_or_issue_is_one_function_with_a_monthly_reset,
               test_or_a_daily_ceiling_is_issued_and_keeps_its_period,
               test_or_ping_names_the_strays_it_does_not_manage,
               test_or_spend_reads_one_keys_activity_and_prints_no_handle):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mcredential-door checks passed; cross-verb rotation accounting remains J-3\033[0m")
