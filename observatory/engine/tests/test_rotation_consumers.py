#!/usr/bin/env python3
"""Rotation never cuts a live consumer (OBS-561, CP-11, decision D5), against fake providers.

A door's rotation used to deliver the successor to its one destination and delete the predecessor at once; a copy of
the old value in a Heroku app's config vars kept a dead key (a production bot answered 401 for three hours). What is
asserted here, with no network:

- the finder (`tools/key_consumers.py`) finds a copy of the value in a fake Heroku app and in a local env file by the
  value itself, names a service started from the slot, and names every source it could not read in `degraded`;
- OpenRouter's `rotate` delivers the successor to the Heroku copy, reads it back, and deletes the predecessor only
  then; a failed delivery or a read-back that does not match keeps the predecessor alive and the row held; `--force`
  deletes it anyway and journals the copy it cut; an unreadable source holds before anything is minted; a held row
  is finished by the next `rotate` once the copy is reachable;
- `issue` into a destination that holds a value delivers to that value's copies; `rotate --dry-run` acts on nothing;
- the Cloudflare and Heroku doors refuse to roll while a copy outside the vault is found, and `--force` rolls;
- no value appears in stdout, stderr or the journal.
"""
from __future__ import annotations
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402  sandboxes HOME first
import private_io                                                   # noqa: E402
import key_consumers                                                # noqa: E402

FAILURES: list[str] = []
#: Fixture values, not credentials: one letter repeated.
OLD = "sk-or-v1-" + "o" * 40
NEW = "sk-or-v1-" + "n" * 40
OTHER = "sk-or-v1-" + "x" * 40


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def clean(text: str) -> bool:
    return OLD not in text and NEW not in text and OTHER not in text and "o" * 40 not in text


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run(fn, *a, **k):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(*a, **k)
        except Exception as exc:                                                # noqa: BLE001  returned to the check
            rc = exc
    return rc, out.getvalue(), err.getvalue()


class FakeHeroku:
    """`/apps` and `/apps/{id}/config-vars` as the Heroku door's wire answers them; errors carry no body."""

    def __init__(self, apps: dict[str, dict]):
        self.apps = apps                     # id -> {"name", "vars"}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.refuse_patch: set[str] = set()
        self.ignore_patch: set[str] = set()  # accepted, but the value does not change (a read-back mismatch)
        self.unreadable: set[str] = set()

    def list(self, path, tok):
        self.calls.append(("GET", path, None))
        return [{"id": i, "name": a["name"]} for i, a in sorted(self.apps.items())]

    def request(self, method, path, tok, payload=None):
        self.calls.append((method, path, payload))
        app_id = path.split("/")[2]
        if method == "GET":
            if app_id in self.unreadable:
                raise RuntimeError("heroku answered HTTP 403 on /apps/<id>/config-vars; provider response withheld")
            return dict(self.apps[app_id]["vars"])
        if method == "PATCH":
            if app_id in self.refuse_patch:
                raise RuntimeError("heroku answered HTTP 422 on /apps/<id>/config-vars; provider response withheld")
            if app_id not in self.ignore_patch:
                self.apps[app_id]["vars"].update(payload)
            return dict(self.apps[app_id]["vars"])
        raise AssertionError(method)


def heroku_with_copy() -> FakeHeroku:
    return FakeHeroku({"app-1": {"name": "alpha-web", "vars": {"OPENROUTER_API_KEY": OLD, "MODE": "prod"}},
                       "app-2": {"name": "beta-worker", "vars": {"OTHER_KEY": OTHER}}})


def heroku_source(fake: FakeHeroku) -> key_consumers.HerokuSource:
    return key_consumers.HerokuSource(tokens=lambda: (["fake-token"], None, None), api=fake)


def env_inventory(files: dict[str, str]) -> pathlib.Path:
    """A folder of env files and the inventory `scan_env.py` would write for them (names only)."""
    base = pathlib.Path(tmpdir.mkdtemp()).resolve()
    records = []
    for rel, text in files.items():
        f = base / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        os.chmod(f, 0o640)
        records.append({"path": rel, "project": rel.split("/")[0], "git": "ignored", "variables": []})
    inv = base / "env.json"
    inv.write_text(json.dumps({"root": str(base), "files": records, "degraded": []}), encoding="utf-8")
    return inv


# ─────────────────────────── the finder ─────────────────────────────────────

def test_the_finder_names_every_copy_and_every_unread_source() -> None:
    fake = heroku_with_copy()
    inv = env_inventory({"alpha-web/.env": f'MODE=dev\nexport OPENROUTER_API_KEY="{OLD}"\n',
                         "beta/.env": f"KEY={OTHER}\n"})
    services = key_consumers.LaunchdSource(("alpha", "prod", "K"),
                                           consumers=lambda *s: [{"consumer": "org.example.alpha", "at": "x"}])
    found = key_consumers.find(OLD, [heroku_source(fake), key_consumers.EnvFileSource(inventory=inv), services])
    kinds = sorted(key_consumers.describe(c) for c in found["consumers"])
    check("a Heroku copy, an env file copy and a service are found", kinds == sorted([
        "heroku app alpha-web, config var OPENROUTER_API_KEY",
        f"env file {inv.parent / 'alpha-web/.env'}, variable OPENROUTER_API_KEY",
        "service org.example.alpha"]), str(kinds))
    check("and nothing was unread", found["degraded"] == [], str(found["degraded"]))
    check("the answer carries no value", clean(json.dumps(key_consumers.public(found))), "")
    fake.unreadable.add("app-2")
    gone = env_inventory({})
    gone.unlink()
    found = key_consumers.find(OLD, [heroku_source(fake),
                                     key_consumers.EnvFileSource(inventory=gone, projects=gone.parent)])
    sources = sorted(d["source"] for d in found["degraded"])
    check("an app whose config cannot be read and a missing inventory are degraded, never 'no consumer'",
          sources == ["env-file", "heroku"] and key_consumers.blocks(found), str(found["degraded"]))
    off = key_consumers.HerokuSource(tokens=lambda: ([], "no Heroku admin token is stashed and the heroku "
                                                         "integration is off", None), api=fake)
    found = key_consumers.find(OLD, [off])
    check("a source not in use here is skipped and holds nothing",
          found["skipped"] and not key_consumers.blocks(found), str(found))
    found = key_consumers.find(OLD, [key_consumers.EnvFileSource(inventory=gone, projects=gone.parent / "none")])
    check("no projects folder at all: the env files are skipped, not unread",
          found["skipped"] and not found["degraded"], str(found))


def test_an_env_file_is_rewritten_in_place_keeping_its_mode() -> None:
    inv = env_inventory({"alpha-web/.env": f'# keys\nA=1\nexport OPENROUTER_API_KEY="{OLD}"  # main\nB=2\n'})
    src = key_consumers.EnvFileSource(inventory=inv)
    found = key_consumers.find(OLD, [src])
    outcomes = key_consumers.deliver_all(found, OLD, NEW)
    f = inv.parent / "alpha-web/.env"
    text = f.read_text(encoding="utf-8")
    check("the env file copy is delivered and read back", [o["status"] for o in outcomes] == ["delivered"],
          str(outcomes))
    check("only that value changed, quoting, comment and neighbours kept",
          text == f'# keys\nA=1\nexport OPENROUTER_API_KEY="{NEW}"  # main\nB=2\n', "")
    check("and the file keeps its mode", os.name == "nt" or oct(f.stat().st_mode & 0o777) == "0o640",
          oct(f.stat().st_mode))
    tracked = {"kind": "env-file", "path": str(f), "var": "OPENROUTER_API_KEY", "git": "tracked"}
    held = key_consumers.blocks({"consumers": [tracked], "degraded": []})
    check("a file Git tracks is never written: it holds the revocation", held and "tracked in Git" in held[0], "")


# ─────────────────────────── OpenRouter ─────────────────────────────────────

def or_door(fake: FakeHeroku, inv: pathlib.Path | None = None, destination_value=OLD):
    m = load("tools/openrouter.py", "or_rotation_door")
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m.save_ledger({"issued": {"alpha": {"account": "example", "hash": "h-old", "destination": "vault:alpha/prod/K",
                                        "delivered_to": "vault alpha/prod/K", "limit_usd": 10,
                                        "issued_on": "2026-09-01", "rotated_on": None, "rotations": 0}}})
    state = {"log": [], "journal": [], "delivered": [], "keys": {"h-old": {"name": "alpha"}}}

    def request(path, key, payload=None, method=None):
        verb = method or ("POST" if payload is not None else "GET")
        state["log"].append((verb, path))
        if verb == "POST" and path == "/keys":
            state["keys"]["h-new"] = {"name": payload["name"]}
            return {"data": {"hash": "h-new", "label": "sk-or-v1-nnn...nnn"}, "key": NEW}
        if path.startswith("/keys/"):
            h = path.split("/")[2]
            if verb == "DELETE":
                state["keys"].pop(h, None)
                return {}
            if verb == "PATCH":
                return {"data": {"hash": h}}
            if h not in state["keys"]:
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            return {"data": {"hash": h, "name": state["keys"][h]["name"], "limit": 10}}
        if path.startswith("/activity"):
            return {"data": []}
        return {"data": []}
    m._request = request
    m.deliver = lambda value, to, **o: (state["delivered"].append((to, o)) or "vault alpha/prod/K")
    m._journal = lambda event, secret, **detail: state["journal"].append({"event": event, "secret": secret, **detail})
    m._record_slot_expiry = lambda *a: None
    m._byok_after = lambda *a, **k: None
    m.destination_value = lambda to: (destination_value, None)
    sources = [heroku_source(fake)] + ([key_consumers.EnvFileSource(inventory=inv)] if inv else [])
    m._consumer_sources = lambda to: sources
    return m, state


def deleted(state, h: str) -> bool:
    return ("DELETE", f"/keys/{h}") in state["log"]


def test_rotate_delivers_reads_back_then_revokes() -> None:
    fake = heroku_with_copy()
    m, state = or_door(fake)
    rc, out, err = run(m.cmd_rotate, "alpha", False)
    check("the rotation succeeds", rc == 0, out + err)
    check("the Heroku copy now holds the successor", fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] == NEW, "")
    patch = next(i for i, c in enumerate(fake.calls) if c[0] == "PATCH")
    reads = [i for i, c in enumerate(fake.calls) if c[0] == "GET" and "config-vars" in c[1] and i > patch]
    log = state["log"]
    check("the copy was read back after the write", reads, str(fake.calls))
    check("the PATCH carried only that variable", fake.calls[patch][2] == {"OPENROUTER_API_KEY": NEW}, "")
    check("and the predecessor was deleted after the delivery", deleted(state, "h-old")
          and log.index(("POST", "/keys")) < log.index(("DELETE", "/keys/h-old")), str(log))
    check("the output names the copy as delivered", "alpha-web, config var OPENROUTER_API_KEY: delivered" in out, out)
    check("the delivery is journaled by name", any(j["event"] == "consumer-delivered" for j in state["journal"]), "")
    check("no value in stdout, stderr or the journal", clean(out + err + json.dumps(state["journal"])), "")


def test_a_failed_delivery_keeps_the_old_key_alive() -> None:
    for mode in ("refuse_patch", "ignore_patch"):
        fake = heroku_with_copy()
        getattr(fake, mode).add("app-1")
        m, state = or_door(fake)
        rc, out, err = run(m.cmd_rotate, "alpha", False)
        row = json.loads(m.LEDGER.read_text())["issued"]["alpha"]
        check(f"{mode}: the rotation is held, exit 1", rc == 1 and "HELD" in err and "alpha-web" in err, err)
        check(f"{mode}: the predecessor is NOT deleted", not deleted(state, "h-old") and "h-old" in state["keys"],
              str(state["log"]))
        check(f"{mode}: the row says held and names the copy", row["rotation_status"] == m.HELD
              and row["predecessor_hash"] == "h-old" and "alpha-web" in " ".join(row["held_consumers"]), str(row))
        check(f"{mode}: the hold is journaled", any(j["event"] == "rotation-held" for j in state["journal"]), "")
        check(f"{mode}: no value anywhere", clean(out + err + json.dumps(state["journal"]) + json.dumps(row)), "")
    # A clean OpenRouter scan, so the consumer guard (OBS-594) passes and the hold itself is what refuses.
    scratch, was = pathlib.Path(tmpdir.mkdtemp()).resolve(), m.paths.SCRATCH
    (scratch / "openrouter.json").write_text(json.dumps({"keys": []}), encoding="utf-8")
    m.paths.SCRATCH = scratch
    try:
        rc, out, err = run(m.cmd_revoke, "alpha")
    finally:
        m.paths.SCRATCH = was
    check("a held row cannot be revoked out from under its live predecessor", rc == 1 and "held" in err, err)


def test_force_cuts_and_journals_the_consumers() -> None:
    fake = heroku_with_copy()
    fake.refuse_patch.add("app-1")
    m, state = or_door(fake)
    rc, out, err = run(m.cmd_rotate, "alpha", False, force=True)
    cut = [j for j in state["journal"] if j["event"] == "rotation-forced"]
    check("--force rotates anyway", rc == 0 and deleted(state, "h-old"), out + err)
    check("and journals the copy it cut", cut and "alpha-web" in json.dumps(cut), str(state["journal"]))
    check("and says so", "--force cut" in err and "alpha-web" in err, err)
    check("no value anywhere", clean(out + err + json.dumps(state["journal"])), "")


def test_an_unreadable_source_holds_before_anything_is_minted() -> None:
    fake = heroku_with_copy()
    fake.unreadable.add("app-2")
    m, state = or_door(fake)
    rc, out, err = run(m.cmd_rotate, "alpha", False)
    check("held, exit 1, the source named", rc == 1 and "beta-worker" in err, err)
    check("nothing was minted or deleted", ("POST", "/keys") not in state["log"]
          and not deleted(state, "h-old"), str(state["log"]))


def test_a_held_rotation_finishes_once_the_copy_is_reachable() -> None:
    fake = heroku_with_copy()
    fake.refuse_patch.add("app-1")
    m, state = or_door(fake)
    run(m.cmd_rotate, "alpha", False)
    fake.refuse_patch.clear()
    real_retired = key_consumers.retired_value
    key_consumers.retired_value = lambda *slot, matches: next((v for v in (OTHER, OLD) if matches(v)), None)
    m.destination_value = lambda to: (NEW, None)
    try:
        row = m.ledger()["issued"]["alpha"]
        # The provider's hash of a key is its value's SHA-256: how the held rotation finds the old value again.
        row["predecessor_hash"] = hashlib.sha256(OLD.encode()).hexdigest()
        m.save_ledger({"issued": {"alpha": row}})
        state["keys"][row["predecessor_hash"]] = {"name": "alpha-next"}
        rc, out, err = run(m.cmd_rotate, "alpha", False)
    finally:
        key_consumers.retired_value = real_retired
    check("the next rotate delivers to the copy and finishes", rc == 0
          and fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] == NEW, out + err)
    check("and deletes the predecessor it held", deleted(state, row["predecessor_hash"]), str(state["log"]))
    check("no value anywhere", clean(out + err + json.dumps(state["journal"])), "")


def test_issue_into_an_occupied_destination_delivers_to_its_copies() -> None:
    fake = heroku_with_copy()
    inv = env_inventory({"alpha-web/.env": f"OPENROUTER_API_KEY={OLD}\n"})
    m, state = or_door(fake, inv)
    doc = m.ledger()
    doc["issued"] = {}
    m.save_ledger(doc)
    m.vault_destination = lambda to: to
    m._name_check = lambda admin, name: None
    rc, out, err = run(m.cmd_issue, "fresh", 5.0, "example", "vault:alpha/prod/K", None)
    check("the issue delivers to the slot and to both copies", rc == 0
          and fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] == NEW
          and (inv.parent / "alpha-web/.env").read_text(encoding="utf-8") == f"OPENROUTER_API_KEY={NEW}\n",
          out + err)
    check("no value anywhere", clean(out + err + json.dumps(state["journal"])), "")


def test_dry_run_acts_on_nothing() -> None:
    fake = heroku_with_copy()
    m, state = or_door(fake)
    rc, out, err = run(m.cmd_rotate, "alpha", False, dry_run=True)
    check("the dry run names the copy", rc == 0 and "alpha-web" in out and "1 other consumer" in out, out + err)
    check("and mints, writes and deletes nothing", not [c for c in state["log"] if c[0] != "GET"]
          and not [c for c in fake.calls if c[0] != "GET"], str(state["log"]))


# ─────────────────────────── Cloudflare and Heroku roll in place ────────────

def test_rolling_doors_refuse_while_a_copy_is_found() -> None:
    fake = heroku_with_copy()
    sources = [heroku_source(fake)]
    refuse, restart = key_consumers.roll_guard(OLD, sources)
    check("a copy outside the vault refuses the roll", refuse and "alpha-web" in refuse[0], str(refuse))
    services = key_consumers.LaunchdSource(("a", "b", "C"), consumers=lambda *s: [{"consumer": "org.example.svc"}])
    refuse, restart = key_consumers.roll_guard(OTHER.replace("x", "y"), [heroku_source(fake), services])
    check("a service started from the slot only needs a restart", not refuse and "org.example.svc" in restart[0],
          str(refuse) + str(restart))

    hk = load("tools/heroku.py", "heroku_rotation_door")
    base = pathlib.Path(tmpdir.mkdtemp()).resolve()
    hk.ADMIN_STORE, hk.LEDGER = base / "heroku-admin", base / "heroku-issued.json"
    journal = []
    hk._journal = lambda event, secret, **d: journal.append({"event": event, **d})
    hk.save_ledger({"issued": {"alpha/prod/K": {"authorization_id": "auth-1", "preset": "read"}}})
    hk.read_admin = lambda label: ("admin", "fake-admin")
    calls = []
    hk._request = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError("stop here"))
    # Both doors read the one roll plan (`key_consumers.plan_roll`, OBS-601, OBS-607) over the one `consumer_sources`.
    # A copy the door can write is delivered now (below); what still refuses is a place it cannot read.
    unread = key_consumers.HerokuSource(tokens=lambda: ([], None, "the admin stash could not be read"), api=fake)
    sources = [unread]
    with mock.patch.object(key_consumers, "vault_value", lambda *slot: OLD), \
            mock.patch.object(key_consumers, "consumer_sources", lambda slot=None, exclude=None: sources):
        rc, out, err = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0)
        check("Heroku: held, nothing regenerated", rc == 1 and "HELD" in err and not calls, err)
        rc, out, err = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0, force=True)
    check("Heroku: --force regenerates and journals the cut",
          calls and any(j["event"] == "rotation-forced" for j in journal), err)

    sys.path.insert(0, str(ROOT / "plugins"))
    cf = load("tools/cloudflare.py", "cloudflare_rotation_door")
    tokens = base / "cf-tokens"
    tokens.mkdir()
    token = tokens / "example"
    private_io.write(token, OLD + "\n")
    cf.issued = lambda: [token]
    cf.read_meta = lambda p: {"account_id": "acc", "preset": "analytics", "account_name": "Example"}
    cf._journal = lambda event, secret, **d: journal.append({"event": event, **d})
    cf.read_admin = lambda label: ("admin", "fake-admin")
    minted = []
    cf.mint_answered = lambda *a, **k: minted.append(a) or (_ for _ in ()).throw(RuntimeError("stop here"))
    with mock.patch.object(key_consumers, "consumer_sources", lambda slot=None, exclude=None: sources):
        rc, out, err = run(cf.cmd_rotate, "example", False)
        check("Cloudflare: held, nothing rolled", rc == 1 and "HELD" in err and not minted, err)
        rc, out, err = run(cf.cmd_rotate, "example", False, True)
    check("Cloudflare: --force rolls", minted, err)
    check("no value in either door's output or journal", clean(out + err + json.dumps(journal)), "")


# ─────────────────────────── OBS-607: the rolling doors deliver ─────────────

def test_a_vault_slot_holding_the_value_is_a_copy_and_takes_the_successor() -> None:
    import vault
    vault.store_value("alpha", "prod", "K", OLD, replace=True, tool="test")
    vault.store_value("beta", "prod", "K_COPY", OLD, replace=True, tool="test")
    vault.store_value("gamma", "prod", "OTHER", OTHER, replace=True, tool="test")
    src = key_consumers.VaultSource(("alpha", "prod", "K"))
    found = key_consumers.find(OLD, [src])
    check("another slot with the value is found, the key's own slot is not",
          [c.get("slot") for c in found["consumers"]] == ["beta/prod/K_COPY"], str(found["consumers"]))
    outcomes = key_consumers.deliver_all(found, OLD, NEW)
    check("delivered through the vault and read back", [o["status"] for o in outcomes] == ["delivered"],
          str(outcomes))
    check("the copy now holds the successor", key_consumers.vault_value("beta", "prod", "K_COPY") == NEW, "")
    check("every door looks there", [s.name for s in key_consumers.consumer_sources("alpha/prod/K")]
          == ["heroku", "env-file", "launchd", "vault"], "")
    check("no value in the answer", clean(json.dumps(key_consumers.public(found)) + json.dumps(outcomes)), "")


class HerokuWire:
    """The Heroku door's own wire for a successor rotation: POST an authorization, the proof read, DELETE."""

    def __init__(self, events: list):
        self.events = events
        self.deleted: list = []

    def __call__(self, method, path, token, payload=None, accept=None):
        self.events.append(("door", method, path))
        if method == "POST" and path == "/oauth/authorizations":
            return {"id": "auth-2", "scope": payload["scope"], "access_token": {"token": NEW, "expires_in": 7776000}}
        if method == "DELETE":
            self.deleted.append(path)
            return {}
        return {"id": "x"}


def heroku_door_with_copy(fake: FakeHeroku):
    hk = load("tools/heroku.py", "heroku_delivery_door")
    base = pathlib.Path(tmpdir.mkdtemp()).resolve()
    hk.ADMIN_STORE, hk.LEDGER = base / "heroku-admin", base / "heroku-issued.json"
    journal: list = []
    hk._journal = lambda event, secret, **d: journal.append({"event": event, "secret": secret, **d})
    hk.save_ledger({"issued": {"alpha/prod/K": {"authorization_id": "auth-1", "preset": "read", "scopes": ["read"],
                                                "description": "observatory read alpha/prod/K (managed)",
                                                "admin": "main", "expire_days": 90}}})
    hk.read_admin = lambda label: ("main", "fake-admin")
    slots = {"alpha/prod/K": OLD}
    hk.deliver_to_vault = lambda value, p, e, n: slots.__setitem__(f"{p}/{e}/{n}", value)
    hk.record_slot_expiry = lambda *a: "expires in 90 days"
    events: list = []
    wire = HerokuWire(events)
    hk._request = wire
    real_request = fake.request

    def logged(method, path, tok, payload=None):
        events.append(("app", method, path))
        return real_request(method, path, tok, payload)
    fake.request = logged
    return hk, journal, slots, wire, events


def test_heroku_delivers_to_a_copy_then_revokes_the_old_authorization() -> None:
    fake = heroku_with_copy()
    hk, journal, slots, wire, events = heroku_door_with_copy(fake)
    sources = [heroku_source(fake)]
    with mock.patch.object(key_consumers, "vault_value", lambda *slot: slots.get("/".join(slot))), \
            mock.patch.object(key_consumers, "consumer_sources", lambda slot=None, exclude=None: sources):
        rc, out, err = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0)
    row = hk.ledger()["issued"]["alpha/prod/K"]
    check("Heroku: the rotation succeeds", rc == 0, out + err)
    check("Heroku: no regenerate (it would kill the copy's value at once)",
          not any("regenerate" in e[2] for e in events), str(events))
    check("Heroku: the slot and the copy hold the successor",
          slots["alpha/prod/K"] == NEW and fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] == NEW, "")
    patch = events.index(("app", "PATCH", "/apps/app-1/config-vars"))
    delete = events.index(("door", "DELETE", "/oauth/authorizations/auth-1"))
    check("Heroku: the old authorization is revoked only after the copy was written and read back",
          patch < delete and any(e[:2] == ("app", "GET") for e in events[patch:delete]), str(events))
    check("Heroku: the ledger names the successor", row["authorization_id"] == "auth-2" and row["rotations"] == 1
          and "predecessor" not in row, str(row))
    check("Heroku: the delivery is journaled by name",
          any(j["event"] == "consumer-delivered" for j in journal) and any(j["event"] == "rotate" for j in journal),
          str(journal))
    check("Heroku: no value in output, journal or ledger",
          clean(out + err + json.dumps(journal) + json.dumps(hk.ledger())), "")


def test_heroku_a_copy_not_delivered_holds_the_revoke_until_the_next_rotate() -> None:
    fake = heroku_with_copy()
    fake.refuse_patch.add("app-1")
    hk, journal, slots, wire, events = heroku_door_with_copy(fake)
    sources = [heroku_source(fake)]
    with mock.patch.object(key_consumers, "vault_value", lambda *slot: slots.get("/".join(slot))), \
            mock.patch.object(key_consumers, "consumer_sources", lambda slot=None, exclude=None: sources):
        rc, out, err = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0)
        row = hk.ledger()["issued"]["alpha/prod/K"]
        check("Heroku: a refused delivery holds, exit 1", rc == 1 and "holds it" in err and "alpha-web" in err, err)
        check("Heroku: the old authorization stays alive", not wire.deleted, str(wire.deleted))
        check("Heroku: the row is held and names the copy and the old authorization",
              row["rotation_status"] == hk.HELD and row["predecessor"]["predecessor"] == "auth-1"
              and "alpha-web" in json.dumps(row["predecessor"]["held"]), str(row))
        rc, out2, err2 = run(hk.cmd_revoke, "alpha/prod/K")
        check("Heroku: revoke refuses a held row", rc == 1 and "held" in err2 and not wire.deleted, err2)
        fake.refuse_patch.clear()
        posts = sum(1 for e in events if e[1] == "POST")
        rc, out3, err3 = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0)
    row = hk.ledger()["issued"]["alpha/prod/K"]
    check("Heroku: the next rotate finishes it: the copy gets the successor, the old authorization is revoked",
          rc == 0 and fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] == NEW
          and wire.deleted == ["/oauth/authorizations/auth-1"], out3 + err3)
    check("Heroku: finishing mints nothing new", sum(1 for e in events if e[1] == "POST") == posts, str(events))
    check("Heroku: the row is complete again", "predecessor" not in row and row["rotation_status"] == "complete",
          str(row))
    check("Heroku: no value anywhere", clean(out + err + out2 + err2 + out3 + err3 + json.dumps(journal)
                                            + json.dumps(hk.ledger())), "")


class FakeCfDoor:
    """The Cloudflare door's provider calls for a successor rotation, recorded."""

    def __init__(self, cf, events: list):
        self.events = events
        cf.read_admin = lambda label: ("admin", "fake-admin")
        cf.existing_token = lambda admin, acc, name: "tok-old" if name == cf.PRESETS["analytics"]["name"] else None
        cf.mint_answered = self.mint
        cf.policies_for = lambda *a, **k: [{"effect": "allow"}]
        cf._request = self.request
        self.deleted: list = []
        self.renamed: list = []

    def mint(self, admin, acc, preset, resources=None, name=None, expires_on=None):
        self.events.append(("door", "mint", name))
        return "tok-new", NEW, {"expires_on": "2027-02-16T00:00:00Z"}

    def request(self, path, token, payload=None, method=None, headers=None):
        self.events.append(("door", method or "GET", path))
        if method == "DELETE":
            self.deleted.append(path)
        if method == "PUT":
            self.renamed.append((path, payload.get("name")))
        return {"result": {}}


def cloudflare_door_with_copy(fake: FakeHeroku):
    sys.path.insert(0, str(ROOT / "plugins"))
    cf = load("tools/cloudflare.py", "cloudflare_delivery_door")
    base = pathlib.Path(tmpdir.mkdtemp()).resolve()
    token = base / "example"
    private_io.write(token, OLD + "\n")
    metas = {token: {"account_id": "acc", "preset": "analytics", "account_name": "Example", "expire_days": 90}}
    cf.issued = lambda: [token]
    cf.read_meta = lambda p: dict(metas.get(p, {}))
    cf.write_meta = lambda p, text, encoding="utf-8": metas.__setitem__(p, json.loads(text))
    journal: list = []
    cf._journal = lambda event, secret, **d: journal.append({"event": event, "secret": secret, **d})

    def install(value, label, account, preset, project, rolled, **k):
        private_io.write(token, value + "\n")
        return 0
    cf.install_issued = install
    events: list = []
    door = FakeCfDoor(cf, events)
    real_request = fake.request

    def logged(method, path, tok, payload=None):
        events.append(("app", method, path))
        return real_request(method, path, tok, payload)
    fake.request = logged
    return cf, token, metas, journal, door, events


def test_cloudflare_delivers_to_a_copy_then_revokes_and_renames() -> None:
    fake = heroku_with_copy()
    cf, token, metas, journal, door, events = cloudflare_door_with_copy(fake)
    sources = [heroku_source(fake)]
    with mock.patch.object(key_consumers, "consumer_sources", lambda slot=None, exclude=None: sources):
        rc, out, err = run(cf.cmd_rotate, "example", False)
    check("Cloudflare: the rotation succeeds", rc == 0, out + err)
    check("Cloudflare: a successor is minted beside the old token, never a roll of it",
          ("door", "mint", cf.PRESETS["analytics"]["name"] + cf.SUCCESSOR_SUFFIX) in events, str(events))
    check("Cloudflare: the token file and the copy hold the successor",
          private_io.read(token).strip() == NEW and fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] == NEW, "")
    patch = events.index(("app", "PATCH", "/apps/app-1/config-vars"))
    delete = events.index(("door", "DELETE", "/accounts/acc/tokens/tok-old"))
    check("Cloudflare: the old token is deleted after the copy was written and read back, then the successor renamed",
          patch < delete and door.renamed == [("/accounts/acc/tokens/tok-new", cf.PRESETS["analytics"]["name"])],
          str(events))
    check("Cloudflare: the record holds no hold", "predecessor" not in metas[token], str(metas[token]))
    check("Cloudflare: no value in output or journal", clean(out + err + json.dumps(journal)), "")


def test_cloudflare_a_copy_not_delivered_holds_the_revoke_until_the_next_rotate() -> None:
    fake = heroku_with_copy()
    fake.ignore_patch.add("app-1")
    cf, token, metas, journal, door, events = cloudflare_door_with_copy(fake)
    sources = [heroku_source(fake)]
    with mock.patch.object(key_consumers, "consumer_sources", lambda slot=None, exclude=None: sources):
        rc, out, err = run(cf.cmd_rotate, "example", False)
        check("Cloudflare: a copy that did not read back holds, exit 1", rc == 1 and "alpha-web" in err, err)
        check("Cloudflare: the old token stays alive", not door.deleted, str(door.deleted))
        held = metas[token]
        check("Cloudflare: the record is held, naming the old and the new token and the copy",
              held.get("rotation_status") == cf.HELD and held["predecessor"]["predecessor"] == "tok-old"
              and held.get("successor_id") == "tok-new", str(held))
        check("Cloudflare: the record holds a keyed digest, never the value",
              held["predecessor"]["digest"].startswith("hmac-sha256:") and clean(json.dumps(held)), "")
        rc, out2, err2 = run(cf.cmd_revoke, "example")
        check("Cloudflare: revoke refuses a held token", rc == 1 and "held" in err2 and not door.deleted, err2)
        fake.ignore_patch.clear()
        mints = sum(1 for e in events if e[1] == "mint")
        rc, out3, err3 = run(cf.cmd_rotate, "example", False)
    check("Cloudflare: the next rotate finishes it", rc == 0 and door.deleted == ["/accounts/acc/tokens/tok-old"]
          and fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] == NEW, out3 + err3)
    check("Cloudflare: finishing mints nothing new", sum(1 for e in events if e[1] == "mint") == mints, "")
    check("Cloudflare: the record is clean again", "predecessor" not in metas[token]
          and "rotation_status" not in metas[token], str(metas[token]))
    check("Cloudflare: no value anywhere", clean(out + err + out2 + err2 + out3 + err3 + json.dumps(journal)), "")


def test_a_held_rotation_whose_old_value_is_gone_stays_held_and_force_revokes() -> None:
    fake = heroku_with_copy()
    fake.refuse_patch.add("app-1")
    hk, journal, slots, wire, events = heroku_door_with_copy(fake)
    sources = [heroku_source(fake)]
    with mock.patch.object(key_consumers, "vault_value", lambda *slot: slots.get("/".join(slot))), \
            mock.patch.object(key_consumers, "consumer_sources", lambda slot=None, exclude=None: sources):
        run(hk.cmd_rotate, "alpha/prod/K", False, wait=0)
        fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] = OTHER     # changed by hand: the old value is nowhere now
        rc, out, err = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0)
        check("a predecessor whose value no copy holds stays held", rc == 1 and not wire.deleted, err)
        rc, out2, err2 = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0, force=True)
    check("--force revokes it and journals the cut", rc == 0 and wire.deleted == ["/oauth/authorizations/auth-1"]
          and any(j["event"] == "rotation-forced" for j in journal), out2 + err2)
    check("no value anywhere", clean(out + err + out2 + err2 + json.dumps(journal)), "")


def test_a_refused_revoke_holds_and_the_next_rotate_only_retries_it() -> None:
    fake = heroku_with_copy()
    hk, journal, slots, wire, events = heroku_door_with_copy(fake)
    refuse = [True]
    real = hk._request

    def wire_refusing(method, path, token, payload=None, accept=None):
        if method == "DELETE" and refuse[0]:
            raise hk.HerokuError(500, "heroku answered HTTP 500 on /oauth/authorizations/<id>; provider response "
                                      "withheld")
        return real(method, path, token, payload)
    hk._request = wire_refusing
    sources = [heroku_source(fake)]
    with mock.patch.object(key_consumers, "vault_value", lambda *slot: slots.get("/".join(slot))), \
            mock.patch.object(key_consumers, "consumer_sources", lambda slot=None, exclude=None: sources):
        rc, out, err = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0)
        row = hk.ledger()["issued"]["alpha/prod/K"]
        check("a refused revoke holds, the copy already delivered", rc == 1 and row["predecessor"]["code"]
              == "revoke-failed" and fake.apps["app-1"]["vars"]["OPENROUTER_API_KEY"] == NEW, err + str(row))
        refuse[0] = False
        rc, out2, err2 = run(hk.cmd_rotate, "alpha/prod/K", False, wait=0)
    row = hk.ledger()["issued"]["alpha/prod/K"]
    check("the next rotate retries only the revoke", rc == 0 and wire.deleted == ["/oauth/authorizations/auth-1"]
          and "predecessor" not in row, out2 + err2)
    check("no value anywhere", clean(out + err + out2 + err2 + json.dumps(journal) + json.dumps(hk.ledger())), "")


def test_the_revoke_runs_through_the_guard_after_delivery() -> None:
    fake = heroku_with_copy()
    sources = [heroku_source(fake)]
    found = key_consumers.find(OLD, sources)
    revoked: list = []
    # A second copy appears between the delivery and the revoke: the guard sees it and holds.
    real = fake.request

    def sneaky(method, path, tok, payload=None):
        answer = real(method, path, tok, payload)
        if method == "PATCH":
            fake.apps["app-2"]["vars"]["LATE_COPY"] = OLD
        return answer
    fake.request = sneaky
    result = key_consumers.deliver_then_revoke("alpha/prod/K", found, OLD, NEW, lambda: revoked.append(1), sources)
    check("a copy the guard still finds holds the revoke", not result["revoked"] and not revoked
          and result["code"] == key_consumers.IN_USE and "beta-worker" in json.dumps(result["held"]), str(result))
    check("no value in the answer", clean(json.dumps(result)), "")


if __name__ == "__main__":
    print("rotation never cuts a live consumer (OBS-561)\n")
    for fn in (test_the_finder_names_every_copy_and_every_unread_source,
               test_an_env_file_is_rewritten_in_place_keeping_its_mode,
               test_rotate_delivers_reads_back_then_revokes,
               test_a_failed_delivery_keeps_the_old_key_alive,
               test_force_cuts_and_journals_the_consumers,
               test_an_unreadable_source_holds_before_anything_is_minted,
               test_a_held_rotation_finishes_once_the_copy_is_reachable,
               test_issue_into_an_occupied_destination_delivers_to_its_copies,
               test_dry_run_acts_on_nothing,
               test_rolling_doors_refuse_while_a_copy_is_found,
               test_a_vault_slot_holding_the_value_is_a_copy_and_takes_the_successor,
               test_heroku_delivers_to_a_copy_then_revokes_the_old_authorization,
               test_heroku_a_copy_not_delivered_holds_the_revoke_until_the_next_rotate,
               test_cloudflare_delivers_to_a_copy_then_revokes_and_renames,
               test_cloudflare_a_copy_not_delivered_holds_the_revoke_until_the_next_rotate,
               test_a_held_rotation_whose_old_value_is_gone_stays_held_and_force_revokes,
               test_a_refused_revoke_holds_and_the_next_rotate_only_retries_it,
               test_the_revoke_runs_through_the_guard_after_delivery):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("rotation consumer checks passed")
