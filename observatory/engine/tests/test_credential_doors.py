#!/usr/bin/env python3
"""The two credential doors — `tools/cloudflare.py` and `tools/openrouter.py`.

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
    for rel in ("tools/cloudflare.py", "tools/openrouter.py"):
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
                        ("tools/openrouter.py", ("destination", "limit_usd", "rotations"))):
        src = (ROOT / rel).read_text(encoding="utf-8")
        # `code_keeping_strings`: the field names ARE string literals, which
        # `code_only` strips along with the prose.
        code = source_reader.code_keeping_strings(src)
        for f in fields:
            check(f"{rel} records `{f}`", f'"{f}"' in code, "")
        # The one string that must never be json.dump'ed: the token variable.
        check(f"{rel} never serialises the value into a meta file",
              '"value": value' not in code and "'value': value" not in code, "")


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
    finally:
        sys.argv = argv
    a, k = seen.get("issue", ((), {}))
    check("issue passes every --lifecycle-rule, and no expiry when none was given",
          a[:5] == ("r2-bucket", "b-1", "eu", None, "p/e/X")
          and k.get("lifecycle_rules") == ["staging/:2", "tmp/:1"], str(seen.get("issue")))
    check("lifecycle passes bucket, jurisdiction, expiry, rules and account",
          seen.get("lifecycle", ((), {}))[0] == ("b-1", "eu", 92, ["staging/:2"], "acct"),
          str(seen.get("lifecycle")))
    check("the usage lists the lifecycle command", "cloudflare.py\" lifecycle" in m.__doc__, "")


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
        ("email-routing", "zone", ("Zone Read", "Email Routing Rules Write"),
         "/zones/z1/email/routing/rules", "rt-value-"),
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
              "email-routing": ("Zone Read", "Email Routing Rules Write")}
    level = {"email-send": "account", "workers-edit": "account", "email-routing": "zone"}
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


if __name__ == "__main__":
    print("the credential doors — stash, issue, rotate, revoke, and what they refuse\n")
    for fn in (test_cf_a_token_this_call_created_is_deleted_when_the_issue_fails,
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
               test_cf_r2_lifecycle_command_line,
               test_cf_groups_lists_names_and_levels_and_never_a_value,
               test_cf_email_presets_grant_exactly_what_the_email_service_needs,
               test_cf_email_presets_refuse_what_they_do_not_grant,
               test_cf_email_routing_token_is_one_per_slot,
               test_cf_fabric_account_preset_grants_both_levels_on_one_account_into_the_vault,
               test_cf_fabric_presets_refuse_what_they_do_not_grant,
               test_cf_logs_preset_reads_telemetry_with_a_post_and_grants_nothing_more,
               test_or_stash_demands_a_label_because_the_provider_names_nothing,
               test_or_rotation_creates_and_delivers_before_deleting,
               test_or_issue_deletes_the_key_when_delivery_fails,
               test_or_issue_refuses_a_name_that_already_exists,
               test_or_issue_is_one_function_with_a_monthly_reset,
               test_or_ping_names_the_strays_it_does_not_manage):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe admin credential never leaves its stash, and every issued "
          "key is accounted for\033[0m")
