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
    m.consumer_sources = lambda to: sources
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
    rc, out, err = run(m.cmd_revoke, "alpha")
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
    hk._roll_guard = lambda slot: key_consumers.roll_guard(OLD, sources)
    hk.read_admin = lambda label: ("admin", "fake-admin")
    calls = []
    hk._request = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError("stop here"))
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
    cf.consumer_sources = lambda path: sources
    cf.read_admin = lambda label: ("admin", "fake-admin")
    minted = []
    cf.mint_answered = lambda *a, **k: minted.append(a) or (_ for _ in ()).throw(RuntimeError("stop here"))
    rc, out, err = run(cf.cmd_rotate, "example", False)
    check("Cloudflare: held, nothing rolled", rc == 1 and "HELD" in err and not minted, err)
    rc, out, err = run(cf.cmd_rotate, "example", False, True)
    check("Cloudflare: --force rolls", minted, err)
    check("no value in either door's output or journal", clean(out + err + json.dumps(journal)), "")


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
               test_rolling_doors_refuse_while_a_copy_is_found):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("rotation consumer checks passed")
