#!/usr/bin/env python3
"""The key inventory, and the confident wrong answer it gave on its first run.

Every check here is a defect this file shipped and the run caught, not a
hypothetical:

1. A listing's `label` shows THREE characters after the ellipsis, not four. The
   first version compared four, matched nothing, and reported all four
   destinations as "not in this account" — the opposite of the truth.
2. Three characters COLLIDE. Across 1200 keys the birthday bound makes a
   three-hex-character tail a near-certainty to repeat, and it did: a stranger's
   `PRODUCTION_user_…` key was labelled as this project's. Matching moved to the
   provider's own label — prefix and suffix — and a collision is now REPORTED
   rather than resolved by picking one.
3. A key value must never reach the written document, and neither must the
   provisioning hash: the raw file is gitignored, but it feeds a registry
   document that is in git, and a handle that acts does not belong there.
"""
from __future__ import annotations
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def scanner():
    import scan_openrouter
    return scan_openrouter


#: A key value of the shape the provider issues. NOT a credential: 64 zeros and
#: a marker, refused by `tools/check_secrets.py` as a fixture and by the entropy
#: rule behind it.
FAKE = "sk-or-v1-" + "abc" + "0" * 55 + "def"


def test_the_label_is_derivable_without_asking():
    """Matching needs an identifier, and asking for one per key costs a request.

    `sk-or-v1-838...0ad` is what the provider calls a key, and the same string
    is computable from the value — verified against `/api/v1/key` before this
    was relied on.
    """
    S = scanner()
    got = S.label_of(FAKE)
    check("the label is prefix, ellipsis and suffix", got == "sk-or-v1-abc...def", got)
    check("a value that is not a key yields nothing",
          S.label_of("not-a-key") == "" and S.label_of("") == "")
    check("a short value cannot produce a label", S.label_of("sk-or-v1-ab") == "")


def test_a_dotenv_is_read_as_a_file_not_as_a_key():
    """claude-mem's destination is a `.env`, and its key sits on one line of it."""
    S = scanner()
    env = f'OTHER=1\nOPENROUTER_API_KEY="{FAKE}"\nMORE=2\n'
    check("the key is found inside a .env", S._label(env) == "sk-or-v1-abc...def",
          S._label(env))
    check("a bare key file works too", S._label(FAKE + "\n") == "sk-or-v1-abc...def")
    check("a file with no key yields nothing", S._label("NOTHING=1\n") == "")


def test_three_characters_are_not_an_identifier():
    """The defect that shipped: a tail collides, a label does not.

    Not a claim about hashing — a claim about THIS population. The account holds
    1200 keys and three hex characters have 4096 values, so the first run put a
    stranger's key under this project's name.
    """
    S = scanner()
    a = "sk-or-v1-" + "111" + "0" * 55 + "xyz"
    b = "sk-or-v1-" + "222" + "0" * 55 + "xyz"
    check("two different keys can share a tail",
          a[-S.TAIL:] == b[-S.TAIL:], "the fixture no longer reproduces the collision")
    check("and their labels still differ", S.label_of(a) != S.label_of(b),
          f"{S.label_of(a)} == {S.label_of(b)}")


def test_a_shared_label_is_reported_rather_than_resolved():
    """Two keys with one label leaves the destination unattributable, and the
    only honest answer is to say so."""
    src = (ROOT / "collectors/scan_openrouter.py").read_text(encoding="utf-8")
    check("the collector computes a collision set", "collisions" in src)
    check("and excludes a colliding label from attribution",
          "if lab not in collisions" in src)
    check("and says why, rather than leaving the destination blank",
          "share the label" in src)


def planted_scan(provisioning: bool = True, collide: bool = False, answer=None,
                 partial: bool = False, prov_value: str = "fixture-provisioning-handle") -> dict:
    """Run the collector in process against planted destinations and a stubbed
    listing, and return the document it wrote.

    Nothing reaches the provider: `listing` is replaced, and every destination
    file lives in a scratch directory. The rows carry a `hash` the way the
    provider's listing does, so the test can watch where the hash lands.
    `answer(path, key)` stands in for every direct request (`GET /key`,
    `GET /keys/{hash}`); by default each one fails as if offline. `partial`
    makes the listing stop early with nothing further to walk, and records each
    walk in `planted_scan.walks`.
    """
    import configuration
    sys.path.insert(0, str(ROOT / "tests"))
    import tmp as tmpdir
    S = scanner()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-or-scan-"))
    observatory_key = d / "observatory-key"
    observatory_key.write_text(FAKE + "\n", encoding="utf-8")
    companion_env = d / "companion.env"
    companion_env.write_text("OTHER=1\nOPENROUTER_API_KEY=" + "sk-or-v1-" + "fed" + "0" * 55 + "cba\n",
                             encoding="utf-8")
    prov = d / "provisioning"
    if provisioning:
        prov.write_text(prov_value + "\n", encoding="utf-8")
    rows = [{"label": "sk-or-v1-abc...def", "name": "tick", "hash": "h-observatory", "usage": 1.5},
            {"label": "sk-or-v1-999...zzz", "name": "unused", "hash": "h-unused"}]
    if collide:
        rows.append({"label": "sk-or-v1-abc...def", "name": "a stranger", "hash": "h-stranger"})
    saved = (S.DESTINATIONS, S.listing, configuration.enabled, S._get)
    S.DESTINATIONS = {"observatory": observatory_key, "claude-mem": companion_env,
                      "gateway": d / "absent", "provisioning": prov}
    walks = planted_scan.walks = []

    def listing(_prov, offset=0, pages=S.MAX_PAGES, until=None):
        walks.append(offset)
        if not partial:
            return rows, None
        return (rows if offset == 0 else []), "stopped after 1 pages; the account holds more"
    S.listing = listing
    S._get = answer or (lambda path, key: (None, None, "offline"))
    configuration.enabled = lambda name: True
    out = d / "openrouter.json"
    try:
        S.main(["scan_openrouter.py", str(out)])
    finally:
        S.DESTINATIONS, S.listing, configuration.enabled, S._get = saved
    return json.loads(out.read_text(encoding="utf-8"))


def test_no_value_and_no_hash_reach_the_registry_half():
    """The raw file may hold a hash; the per-key records that feed the registry
    may not, and neither may hold a key."""
    src = (ROOT / "collectors/scan_openrouter.py").read_text(encoding="utf-8")
    body = src.split("keys.append(", 1)[1].split("})", 1)[0]
    check("no per-key record carries a hash", "hash" not in body, body[:160])
    doc = planted_scan()
    text = json.dumps(doc["keys"])
    check("no key record holds anything key-shaped",
          "..." in text and not any(len(str(v)) > 40 and str(v).startswith("sk-or-v1-")
                                    for k in doc["keys"] for v in k.values()), text[:200])
    check("the hashes live beside the keys, not inside them",
          "hashes" in doc and all("hash" not in k for k in doc["keys"]), str(doc.get("hashes")))
    check("and no key value reached the document anywhere", FAKE not in json.dumps(doc))
    served = {k["label"]: k["serves"] for k in doc["keys"]}
    check("the key a destination holds is attributed to it by label",
          served.get("sk-or-v1-abc...def") == "observatory", str(served))
    check("and a key no destination holds serves nothing",
          served.get("sk-or-v1-999...zzz") is None, str(served))


def test_a_collision_is_reported_on_a_real_run():
    doc = planted_scan(collide=True)
    served = [k["serves"] for k in doc["keys"] if k["label"] == "sk-or-v1-abc...def"]
    check("two keys sharing a label are both left unattributed",
          len(served) == 2 and served == [None, None], str(served))
    check("and the destination says why",
          any(d["source"] == "openrouter:observatory" and "share the label" in d["reason"]
              for d in doc["degraded"]), str(doc["degraded"]))


def test_absent_provisioning_is_a_degradation_not_an_empty_estate():
    """No provisioning key means the account cannot be listed — which is a
    different claim from "this account has no keys"."""
    src = (ROOT / "collectors/scan_openrouter.py").read_text(encoding="utf-8")
    check("the collector writes a file rather than failing",
          "no provisioning key, so the account's" in src)
    check("and a truncated listing says it is partial",
          "this list is partial" in src)
    doc = planted_scan(provisioning=False)
    check("with no provisioning key the listing is empty and says so",
          doc["keys"] == [] and any(d["source"] == "openrouter:listing" for d in doc["degraded"]),
          str(doc["degraded"]))
    check("every destination not in the listing is named",
          isinstance(doc.get("unlisted_destinations"), list)
          and set(doc["unlisted_destinations"]) == {"observatory", "claude-mem"},
          str(doc.get("unlisted_destinations")))
    check("and a destination with no key file carries a reason on the degraded list",
          any(d["source"] == "openrouter:gateway" for d in doc["degraded"]), str(doc["degraded"]))
    doc = planted_scan()
    check("a destination whose key is not in the listing is named with a reason",
          doc["unlisted_destinations"] == ["claude-mem"]
          and all(any(d["source"].endswith(n) for d in doc["degraded"])
                  for n in doc["unlisted_destinations"]),
          str(doc["unlisted_destinations"]))


COMPANION = "sk-or-v1-" + "fed" + "0" * 55 + "cba"


PROV = "sk-or-v1-" + "ppp" + "0" * 55 + "p03"


def test_a_key_past_every_page_is_settled_by_asking_it_about_itself():
    """2026-10-21: on an account where another product mints a key per user, the
    gateway's key sat past the newest 21,200 keys, and every run reported its
    existence as unknown. Asking the key about itself settles it in one request,
    keeps its row, and never overrides a complete listing or another workspace."""
    asked = []

    def provider(workspace="W1", companion=None):
        def answer(path, key):
            asked.append((path, key))
            if path == "/key" and key == PROV:
                return {"label": "sk-or-v1-ppp...p03", "workspace_id": "W1"}, None, None
            if path == "/key" and key == COMPANION:
                return companion if companion else (
                    {"label": "sk-or-v1-fed...cba", "workspace_id": workspace, "usage": 2.5,
                     "limit": 10, "disabled": False}, None, None)
            return None, None, "offline"
        return answer

    doc = planted_scan(answer=provider(), partial=True, prov_value=PROV)
    row = [k for k in doc["keys"] if k["label"] == "sk-or-v1-fed...cba"]
    check("an unreached key that describes itself is verified",
          "claude-mem" in doc["verified"] and doc["unlisted_destinations"] == [],
          f"{doc['verified']} {doc['unlisted_destinations']}")
    check("and keeps its row: serves, usage and limit from its own answer",
          len(row) == 1 and row[0]["serves"] == "claude-mem" and row[0]["usage"] == 2.5
          and row[0]["limit"] == 10, str(row))
    check("so the 200-page walk did not run for it", planted_scan.walks == [0], str(planted_scan.walks))
    check("the consumer's key went only to GET /key",
          ("/key", COMPANION) in asked and all(k != COMPANION or p == "/key" for p, k in asked),
          str([p for p, _ in asked]))
    check("a settled partial listing is a note that says how it was settled",
          any(n["source"] == "openrouter:listing" and "asked about itself" in n["reason"]
              for n in doc.get("not_applicable") or []), str(doc.get("not_applicable")))

    doc = planted_scan(answer=provider(workspace="W2"), partial=True, prov_value=PROV)
    check("a key from another workspace is said so, never verified",
          "claude-mem" not in doc["verified"]
          and any(d["source"] == "openrouter:claude-mem" and "another workspace" in d["reason"]
                  for d in doc["degraded"]), f"{doc['verified']} {doc['degraded']}")

    asked.clear()
    doc = planted_scan(answer=provider(), prov_value=PROV)
    check("a COMPLETE listing that lacks the key is not overridden by its answer",
          "claude-mem" not in doc["verified"] and ("/key", COMPANION) not in asked
          and any(d["source"] == "openrouter:claude-mem" and "not in this account's listing" in d["reason"]
                  for d in doc["degraded"]), f"{doc['verified']} {doc['degraded']}")

    doc = planted_scan(answer=provider(companion=(None, 401, "HTTP 401")), partial=True, prov_value=PROV)
    check("a key the provider refuses is reported as invalid, disabled or deleted",
          any(d["source"] == "openrouter:claude-mem" and "invalid, disabled or deleted" in d["reason"]
              for d in doc["degraded"]) and "claude-mem" in doc["unlisted_destinations"],
          f"{doc['degraded']} {doc['unlisted_destinations']}")

    doc = planted_scan(partial=True, prov_value=PROV)
    check("a key that cannot be asked stays unknown and says why",
          any(d["source"] == "openrouter:claude-mem" and "could not describe itself" in d["reason"]
              for d in doc["degraded"]), str(doc["degraded"]))


def test_a_key_is_never_carried_to_a_redirect():
    """urllib copies `Authorization` to a redirect's new location; the opener
    every request goes through refuses a redirect instead."""
    import urllib.error
    S = scanner()
    try:
        S._NoRedirect().redirect_request(None if False else type("R", (), {"full_url": "x"})(),
                                         None, 302, "Found", {}, "http://elsewhere.invalid/")
        check("a redirect is refused", False, "followed")
    except urllib.error.HTTPError as e:
        check("a redirect is refused", e.code == 302, str(e.code))


def test_only_the_exact_variable_is_read_from_a_dotenv():
    S = scanner()
    key = "sk-or-v1-" + "abc" + "0" * 55 + "def"
    for text in ("OPENROUTER_API_KEY_OLD=stale\nOPENROUTER_API_KEY=" + key,
                 'export OPENROUTER_API_KEY="' + key + '" # the tick',
                 "OPENROUTER_API_KEY=" + key + " # the tick"):
        check(f"read {text[:24]!r}", S.label_of(S._key_in(text)) == "sk-or-v1-abc...def", S._key_in(text)[:12])
    check("a .env without the variable holds no key", S._key_in("OPENROUTER_API_KEY_OLD=x") == "")


def test_the_step_is_reachable():
    import observatory
    check("the collector has a step", "openrouter" in observatory.STEPS)
    check("and a group reaches it",
          any("openrouter" in s for s in observatory.GROUPS.values()),
          "a step no group runs is a step that never runs")


def test_the_door_reads_every_page_of_keys():
    """The fourth defect: `find_key` and `ping` read only the provider's first page.

    The provisioning account holds more than 500 keys, the provider pages them by 100, and a
    key issued later sits past the first page. The door then called three live keys "NOT at
    the provider" and refused `limit` (2026-10-04) — the same confident wrong answer, one page
    deep. Pages are read until one comes back short; a runaway listing is an error, not a
    silent truncation.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib
    door = importlib.import_module("openrouter")
    pages = {0: [{"name": f"user_{i}", "hash": f"h{i}"} for i in range(100)],
             100: [{"name": f"user_{i}", "hash": f"h{i}"} for i in range(100, 200)],
             200: [{"name": "example-agent", "hash": "hx"}]}
    asked = []
    def fake(path, key, payload=None, method=None):
        asked.append(path)
        off = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
        return {"data": pages.get(off, [])}
    real, real_ledger = door._request, door.ledger
    door._request = fake
    door.ledger = lambda: {"issued": {}}  # no recorded hash: the page-by-page fallback
    try:
        row = door.find_key("admin-value", "example-agent")
        check("a key on the third page is found", bool(row) and row.get("hash") == "hx", f"got {row!r}")
        check("every page was asked, offset by what came back",
              [p for p in asked if "offset=" in p] == ["/keys?include_disabled=true&offset=0",
                                                       "/keys?include_disabled=true&offset=100",
                                                       "/keys?include_disabled=true&offset=200"], f"asked {asked}")
        rows = door._all_keys("admin-value")
        check("all 201 keys are listed", len(rows) == 201, f"{len(rows)}")
        endless = lambda path, key, payload=None, method=None: {"data": [{"name": "x", "hash": "y"}] * 100}
        door._request = endless
        try:
            door._all_keys("admin-value")
            check("a listing that never ends is refused", False, "no error raised")
        except RuntimeError as exc:
            check("a listing that never ends is refused", "pages" in str(exc), str(exc))
        # With the hash the ledger recorded at issue, the key is read directly — one request.
        door.ledger = lambda: {"issued": {"example-agent": {"hash": "hx", "account": "a"}}}
        asked.clear()
        door._request = lambda path, key, payload=None, method=None: (asked.append(path) or
            ({"data": {"name": "example-agent", "hash": "hx"}} if path == "/keys/hx" else {"data": []}))
        row = door.find_key("admin-value", "example-agent")
        check("a key with a recorded hash is read by hash, in one request",
              bool(row) and asked == ["/keys/hx"], f"asked {asked}")
        def gone(path, key, payload=None, method=None):
            raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
        door._request = gone
        check("a recorded key the provider no longer has is reported absent",
              door.find_key("admin-value", "example-agent") is None)
    finally:
        door._request, door.ledger = real, real_ledger


def test_a_key_minted_elsewhere_is_claimed_by_its_hash_and_never_guessed():
    """`claim` (2026-10-11): plaintext copies of keys made by hand in the dashboard could be
    replaced through the door but not revoked, because `revoke` reads the ledger. The value
    is hashed, the provider row is read by that hash, and a label alone — prefix plus three
    characters — is accepted only when it is the one label in every listing and the ceiling
    agrees. A claimed key is revoked through the door; it is never "rotated" to nowhere."""
    sys.path.insert(0, str(ROOT / "tools"))
    import hashlib, importlib
    door = importlib.import_module("openrouter")
    value = "sk-or-v1-" + "abc" + "0" * 55 + "def"
    digest = hashlib.sha256(value.encode()).hexdigest()
    label = "sk-or-v1-abc...def"
    state = {"issued": {}}
    saved = {}
    real = {k: getattr(door, k) for k in ("_request", "ledger", "save_ledger", "admins", "read_admin", "_journal")}
    door.ledger = lambda: json.loads(json.dumps(state))
    door.save_ledger = lambda doc: (state.clear(), state.update(doc), saved.update(doc))
    door.admins = lambda: [("acct-a", pathlib.Path("/nonexistent/a")), ("acct-b", pathlib.Path("/nonexistent/b"))]
    door.read_admin = lambda label=None: (label or "acct-a", "admin-" + (label or "acct-a"))
    door._journal = lambda *a, **k: None
    try:
        # 1. By hash: only acct-b holds it.
        def by_hash(path, key, payload=None, method=None):
            if path == f"/keys/{digest}" and key == "admin-acct-b":
                return {"data": {"hash": digest, "label": label, "name": "made-by-hand", "limit": 500}}
            if path.startswith("/keys/"):
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            return {"data": []}
        door._request = by_hash
        r = door.claim_key("old-tool", value, where="a plaintext file")
        rec = state["issued"].get("old-tool") or {}
        check("a key is claimed by its hash in the account that holds it",
              r["account"] == "acct-b" and r["found_by"] == "hash" and rec.get("hash") == digest, f"{r} {rec}")
        check("the ledger never holds the value", value not in json.dumps(state))
        check("the provider's own name is kept, so the door finds the key again",
              door.find_key("admin-acct-b", "old-tool") is not None)
        try:
            door.claim_key("old-tool-again", value)
            check("the same key cannot be claimed twice", False, "no refusal")
        except ValueError:
            check("the same key cannot be claimed twice", True)
        try:
            door.rotate_one("old-tool")
            check("a claimed key is not rotated to nowhere", False, "no refusal")
        except ValueError as exc:
            check("a claimed key is not rotated to nowhere", "claimed" in str(exc), str(exc))
        # 2. No hash match; two keys share the label in the listing: refused.
        state["issued"].clear()
        twin = [{"hash": "h1", "label": label, "name": "x", "limit": 500},
                {"hash": "h2", "label": label, "name": "y", "limit": 500}]
        def shared(path, key, payload=None, method=None):
            if path.startswith("/keys/"):
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            if path == "/key":
                return {"data": {"label": label, "limit": 500}}
            return {"data": twin if key == "admin-acct-a" else []}
        door._request = shared
        try:
            door.claim_key("old-tool", value)
            check("a label two keys share is refused", False, "claimed one of two")
        except ValueError as exc:
            check("a label two keys share is refused", "2 keys" in str(exc), str(exc))
        # 3. One label match whose ceiling disagrees with what the key says of itself: refused.
        def wrong_limit(path, key, payload=None, method=None):
            if path.startswith("/keys/"):
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            if path == "/key":
                return {"data": {"label": label, "limit": 1000}}
            return {"data": twin[:1] if key == "admin-acct-a" else []}
        door._request = wrong_limit
        try:
            door.claim_key("old-tool", value)
            check("a single label match with another ceiling is refused", False, "claimed")
        except ValueError:
            check("a single label match with another ceiling is refused", True)
        check("nothing was written by a refusal", "old-tool" not in state["issued"])
        try:
            door.claim_key("x", "not-a-key")
            check("a value that is not a key is refused", False, "claimed")
        except ValueError:
            check("a value that is not a key is refused", True)
    finally:
        for k, v in real.items():
            setattr(door, k, v)


def test_a_claimed_key_is_never_lost_skipped_or_guessed():
    """Review of `claim` (2026-10-12), five defects:
    A a bulk `rotate` died at the first claimed key; B `revoke` missed the consumer reading a
    claimed key; C the label path accepted a match while a listing was incomplete, and two
    unlimited keys "agreed" on a ceiling of None; D a rename at the provider read as a
    deletion, so `revoke` dropped the row and left the key live; E two claims overwrote each
    other."""
    sys.path.insert(0, str(ROOT / "tools"))
    import hashlib, importlib, io, contextlib, tempfile
    door = importlib.import_module("openrouter")
    value = "sk-or-v1-" + "abc" + "0" * 55 + "def"
    digest = hashlib.sha256(value.encode()).hexdigest()
    label = "sk-or-v1-abc...def"
    state = {"issued": {}}
    real = {k: getattr(door, k) for k in ("_request", "ledger", "save_ledger", "admins", "read_admin", "_journal",
                                         "_all_keys")}
    door.ledger = lambda: json.loads(json.dumps(state))
    door.save_ledger = lambda doc: (state.clear(), state.update(json.loads(json.dumps(doc))))
    door.admins = lambda: [("acct-a", pathlib.Path("/nonexistent/a"))]
    door.read_admin = lambda label=None: (label or "acct-a", "admin-acct-a")
    door._journal = lambda *a, **k: None
    deleted = []
    def by_hash(path, key, payload=None, method=None):
        if method == "DELETE":
            deleted.append(path)
            return {}
        if path == f"/keys/{digest}":
            return {"data": {"hash": digest, "label": label, "name": state.get("_provider_name", "made-by-hand"),
                             "limit": 500}}
        raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
    try:
        door._request = by_hash
        door.claim_key("old-tool", value, where="a plaintext file")
        # D: renamed at the provider, still found by its hash, and revoke deletes it there.
        state["_provider_name"] = "renamed-in-dashboard"
        check("D: a claimed key renamed at the provider is still found",
              door.find_key("admin-acct-a", "old-tool") is not None)
        # A: a bulk rotation names the claimed key and does not die.
        err, code = io.StringIO(), None
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = door.cmd_rotate(None, False)
        except Exception as exc:                                       # noqa: BLE001
            err.write(repr(exc))
        check("A: a bulk rotation survives a claimed key", code == 1 and "claimed" in err.getvalue(),
              err.getvalue())
        # B: a consumer reading the claimed key (by its label) stops revoke.
        with tempfile.TemporaryDirectory() as tmp:
            real_scratch = door.paths.SCRATCH
            door.paths.SCRATCH = pathlib.Path(tmp)
            try:
                (pathlib.Path(tmp) / "openrouter.json").write_text(json.dumps(
                    {"keys": [{"label": label, "name": "renamed-in-dashboard", "serves": "an agent"}]}))
                try:
                    door.revoke_key("old-tool")
                    check("B: revoke refuses a claimed key a consumer reads", False, "revoked")
                except ValueError as exc:
                    check("B: revoke refuses a claimed key a consumer reads", "reading right now" in str(exc), str(exc))
                (pathlib.Path(tmp) / "openrouter.json").write_text(json.dumps({"keys": []}))
                door.revoke_key("old-tool")
                check("D: revoking a renamed claimed key deletes it at the provider",
                      deleted == [f"/keys/{digest}"] and "old-tool" not in state["issued"], f"{deleted}")
            finally:
                door.paths.SCRATCH = real_scratch
        # C: a listing that is not whole refuses the label path; so does a key with no ceiling.
        state["issued"].clear()
        def label_only(path, key, payload=None, method=None):
            if path.startswith("/keys/"):
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            if path == "/key":
                return {"data": {"label": label, "limit": state.get("_own_limit", 500)}}
            return {"data": []}
        door._request = label_only
        def too_long(admin, include_disabled=True):
            raise door.ListingTooLong("too many", [{"hash": "h0", "label": "other", "limit": 500}])
        door._all_keys = too_long
        try:
            door.claim_key("old-tool", value)
            check("C: an incomplete listing refuses the label path", False, "claimed")
        except ValueError as exc:
            check("C: an incomplete listing refuses the label path", "more keys" in str(exc), str(exc))
        door._all_keys = lambda admin, include_disabled=True: [{"hash": "h1", "label": label, "limit": None}]
        state["_own_limit"] = None
        try:
            door.claim_key("old-tool", value)
            check("C: no ceiling, no label claim", False, "claimed an unlimited key by label")
        except ValueError as exc:
            check("C: no ceiling, no label claim", "no ceiling" in str(exc), str(exc))
        # E: a claim that lands while this one talks to the provider is not overwritten.
        state.pop("_own_limit", None)
        door._all_keys = lambda admin, include_disabled=True: [{"hash": "h1", "label": label, "limit": 500,
                                                                 "name": "x"}]
        def racing(path, key, payload=None, method=None):
            if path == "/key":
                state["issued"]["old-tool"] = {"hash": "someone-else", "account": "acct-a"}
            return label_only(path, key, payload, method)
        door._request = racing
        try:
            door.claim_key("old-tool", value)
            check("E: a concurrent claim is not overwritten", False, "overwrote")
        except ValueError:
            check("E: a concurrent claim is not overwritten", state["issued"]["old-tool"]["hash"] == "someone-else")
    finally:
        for k, v in real.items():
            setattr(door, k, v)


if __name__ == "__main__":
    print("openrouter keys — three characters were not an identifier\n")
    for fn in (test_the_label_is_derivable_without_asking,
               test_a_dotenv_is_read_as_a_file_not_as_a_key,
               test_three_characters_are_not_an_identifier,
               test_a_shared_label_is_reported_rather_than_resolved,
               test_no_value_and_no_hash_reach_the_registry_half,
               test_a_collision_is_reported_on_a_real_run,
               test_absent_provisioning_is_a_degradation_not_an_empty_estate,
               test_a_key_past_every_page_is_settled_by_asking_it_about_itself,
               test_a_key_is_never_carried_to_a_redirect,
               test_only_the_exact_variable_is_read_from_a_dotenv,
               test_the_step_is_reachable,
               test_the_door_reads_every_page_of_keys,
               test_a_key_minted_elsewhere_is_claimed_by_its_hash_and_never_guessed,
               test_a_claimed_key_is_never_lost_skipped_or_guessed):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma destination is attributed by what the provider calls the key\033[0m")
