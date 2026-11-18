#!/usr/bin/env python3
"""Keys by source (OBS-556, CP-10): every vault slot is classed by where its value came from.

`issued` (a door minted it: its ledger, its record, the movements journal, or a credential request's provenance),
`received` (a person, an organization or an agent handed it over: `vault.py put --from LABEL`), `shared` (handed to
another party: a sent peer share, a credential request in an organization's context, a hand-over in an applied
organization selection; OBS-604), `copied` (the same value sits in another project's folder of this vault, measured
from the keyed digests of this machine's manifest; `shared` before OBS-604) or `unknown`. What is asserted, with no
network:

- each class, with its context, and a slot put without a source reads `unknown`, never `issued` (`test_key_classes`);
- `put --from` round trip: the label lands beside the slot's metadata and the journal, never in the value file; a put
  or a rotation without it forgets it; a label shaped like a credential, too long, or equal to the value is refused
  and nothing is written;
- a slot whose metadata does not read is `unknown`, and `degraded` names it;
- `observatory_credentials` and the keys page's registry rows carry the class, its context and the consumers;
- each row says personal or which organization (`context`), read the way `full org keys` reads it (OBS-604);
- consumers come from every source the revoke guard reads (`key_consumers.GUARD_SOURCES`), not launchd only;
- no value ever appears in an answer, a row, the journal, the metadata or the command's output.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402  sandboxes HOME first

VAULT_DIR = pathlib.Path(tmpdir.mkdtemp(prefix="source-vault-")).resolve() / "projects"
os.environ["OBSERVATORY_VAULT_DIR"] = str(VAULT_DIR)
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
import private_io                                                   # noqa: E402
import vault                                                        # noqa: E402
import vault_digests                                                # noqa: E402
import key_source                                                   # noqa: E402
import org_view                                                     # noqa: E402

#: Fixture values, not credentials: one letter repeated.
VALUE = "fixture-value-" + "v" * 32
SHARED = "fixture-value-" + "s" * 32
OTHER = "fixture-value-" + "o" * 32
MINTED = "fixture-value-" + "m" * 32
HAND = "fixture-value-" + "h" * 32


def pin_vault() -> None:
    os.environ["OBSERVATORY_VAULT_DIR"] = str(VAULT_DIR)
    vault.STORE, vault.LEAKS, vault.MOVES = VAULT_DIR, VAULT_DIR / "leaks.jsonl", VAULT_DIR / "movements.jsonl"


def cli(*args: str, value: str = VALUE) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), *args], input=value + "\n",
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "OBSERVATORY_VAULT_DIR": str(VAULT_DIR)})


def put(project: str, env: str, name: str, value: str = VALUE, *extra: str) -> subprocess.CompletedProcess:
    r = cli("put", project, env, name, "--force", *extra, value=value)
    assert r.returncode == 0, r.stderr
    return r


def meta_of(project: str, env: str, name: str) -> dict:
    return json.loads((VAULT_DIR / project / env / f"{name}.meta.json").read_text())


def everything_written() -> str:
    """Every file of the scratch vault and the secret store that is not a value file, as one text."""
    out = []
    for p in sorted(VAULT_DIR.rglob("*")):
        if p.is_file() and (p.name.endswith(".json") or p.name.endswith(".jsonl")):
            out.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


def clean(text: str) -> bool:
    return "fixture-value-" not in text and all(c * 32 not in text for c in "vsomh")


def classify(*slots: str) -> tuple[dict, list]:
    degraded: list = []
    return key_source.classify(list(slots), degraded), degraded


class Pinned(unittest.TestCase):
    def setUp(self):
        pin_vault()


class KeyClasses(Pinned):

    def test_key_classes(self):
        """The four classes, each with its context; a slot put without a source is `unknown`, never `issued`."""
        # issued: the OpenRouter door's ledger names the slot as its destination.
        put("alpha-web", "prod", "OPENROUTER_API_KEY")
        private_io.write(org_view._secret_store() / "openrouter-issued.json", json.dumps({"issued": {
            "alpha-web-prod": {"destination": "vault:alpha-web/prod/OPENROUTER_API_KEY", "account": "acct-a"}}}))
        # issued on a credential request: the provenance names the door and its account.
        put("alpha-web", "prod", "REQUESTED_KEY")
        vault.record_provenance("alpha-web", "prod", "REQUESTED_KEY", {
            "requestId": "req-1", "issuer": {"door": "cloudflare", "account": "acct-c"},
            "recipient": {"context": "personal"}})
        # received: a person handed it over.
        put("alpha-web", "prod", "PARTNER_TOKEN", VALUE, "--from", "example-org support desk")
        # copied: one value under two project folders, measured from the keyed manifest.
        put("alpha-web", "prod", "SHARED_KEY", SHARED)
        put("beta-api", "prod", "SHARED_KEY", SHARED)
        # unknown: put by hand, no record of where it came from.
        put("alpha-web", "prod", "BY_HAND", HAND)
        vault_digests.write_manifest("test-machine")
        rows, degraded = classify("alpha-web/prod/OPENROUTER_API_KEY", "alpha-web/prod/REQUESTED_KEY",
                                  "alpha-web/prod/PARTNER_TOKEN", "alpha-web/prod/SHARED_KEY",
                                  "alpha-web/prod/BY_HAND")
        issued = rows["alpha-web/prod/OPENROUTER_API_KEY"]
        self.assertEqual(issued["source"], "issued")
        self.assertEqual((issued["sourceContext"]["door"], issued["sourceContext"]["account"]),
                         ("openrouter", "acct-a"))
        requested = rows["alpha-web/prod/REQUESTED_KEY"]
        self.assertEqual(requested["source"], "issued")
        self.assertEqual((requested["sourceContext"]["door"], requested["sourceContext"]["account"],
                          requested["sourceContext"]["requestId"]), ("cloudflare", "acct-c", "req-1"))
        received = rows["alpha-web/prod/PARTNER_TOKEN"]
        self.assertEqual(received["source"], "received")
        self.assertEqual(received["sourceContext"]["from"], "example-org support desk")
        copied = rows["alpha-web/prod/SHARED_KEY"]
        self.assertEqual(copied["source"], "copied", "a copy inside this vault is not an outward share (OBS-604)")
        self.assertEqual(copied["sourceContext"]["with"], ["beta-api/prod/SHARED_KEY"])
        self.assertEqual(copied["copiedWith"], ["beta-api/prod/SHARED_KEY"])
        self.assertEqual(copied["sharedWith"], ["beta-api/prod/SHARED_KEY"], "the published field is kept")
        self.assertNotIn("sharedTo", copied)
        by_hand = rows["alpha-web/prod/BY_HAND"]
        self.assertEqual(by_hand["source"], "unknown")
        self.assertNotIn("sharedWith", by_hand)
        self.assertEqual(degraded, [])
        self.assertTrue(set(r["source"] for r in rows.values()) <= set(key_source.CLASSES))
        self.assertTrue(clean(json.dumps(rows)))

    def test_a_shared_value_a_door_issued_stays_issued_and_names_the_other_copy(self):
        put("gamma-site", "prod", "MINTED", MINTED)
        put("delta-site", "prod", "MINTED", MINTED)
        private_io.write(org_view._secret_store() / "openrouter-issued.json", json.dumps({"issued": {
            "g": {"destination": "vault:gamma-site/prod/MINTED", "account": "acct-g"}}}))
        vault_digests.write_manifest("test-machine")
        rows, _ = classify("gamma-site/prod/MINTED", "delta-site/prod/MINTED")
        self.assertEqual(rows["gamma-site/prod/MINTED"]["source"], "issued")
        self.assertEqual(rows["gamma-site/prod/MINTED"]["sharedWith"], ["delta-site/prod/MINTED"])
        self.assertEqual(rows["gamma-site/prod/MINTED"]["copiedWith"], ["delta-site/prod/MINTED"])
        self.assertEqual(rows["delta-site/prod/MINTED"]["source"], "copied")

    def test_without_a_manifest_copies_are_not_measured_and_say_so(self):
        put("epsilon", "prod", "ALONE")
        manifest = vault_digests.local_manifest_path()
        if manifest.exists():
            manifest.unlink()
        rows, degraded = classify("epsilon/prod/ALONE")
        self.assertEqual(rows["epsilon/prod/ALONE"]["source"], "unknown")
        self.assertIn("shared-unmeasured", [d.get("code") for d in degraded])


class FromRoundTrip(Pinned):

    def test_from_lands_beside_the_metadata_never_in_the_value(self):
        r = put("zeta-app", "prod", "HANDED", VALUE, "--from", "a colleague, by hand")
        self.assertEqual(meta_of("zeta-app", "prod", "HANDED")["source"]["class"], "received")
        self.assertEqual(meta_of("zeta-app", "prod", "HANDED")["source"]["from"], "a colleague, by hand")
        self.assertEqual((VAULT_DIR / "zeta-app" / "prod" / "HANDED").read_text().strip(), VALUE)
        journal = [json.loads(line) for line in (VAULT_DIR / "movements.jsonl").read_text().splitlines()]
        last = [row for row in journal if row.get("secret") == "zeta-app/prod/HANDED"][-1]
        self.assertEqual(last.get("received_from"), "a colleague, by hand")
        self.assertTrue(clean(r.stdout + r.stderr))
        self.assertTrue(clean(everything_written()))

    def test_a_put_or_rotation_without_from_forgets_it(self):
        put("eta-app", "prod", "HANDED", VALUE, "--from", "example-org")
        put("eta-app", "prod", "HANDED", OTHER)
        self.assertNotIn("source", meta_of("eta-app", "prod", "HANDED"))
        put("eta-app", "prod", "ROTATED", VALUE, "--from", "example-org")
        r = cli("rotate", "eta-app", "prod", "ROTATED", value=OTHER)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("source", meta_of("eta-app", "prod", "ROTATED"))
        rows, _ = classify("eta-app/prod/HANDED", "eta-app/prod/ROTATED")
        self.assertEqual({r["source"] for r in rows.values()}, {"unknown"})

    def test_a_label_that_cannot_name_a_sender_is_refused_and_nothing_is_written(self):
        for label in ("sk-or-v1-" + "a" * 48, "x" * 81, "line\nbreak", "", VALUE):
            r = cli("put", "theta-app", "prod", "REFUSED", "--from", label)
            self.assertEqual(r.returncode, 2, label)
            self.assertFalse((VAULT_DIR / "theta-app" / "prod" / "REFUSED").exists(), label)
            self.assertTrue(clean(r.stdout + r.stderr))


class Degraded(Pinned):

    def test_unreadable_metadata_is_unknown_with_a_degraded_entry(self):
        put("iota-app", "prod", "BROKEN", VALUE, "--from", "example-org")
        broken = VAULT_DIR / "iota-app" / "prod" / "BROKEN.meta.json"
        broken.write_text("{not json", encoding="utf-8")
        # The other suites' manifest refuses to read a vault holding it, as it should: gone after this case.
        self.addCleanup(lambda: [p.unlink() for p in broken.parent.iterdir()])
        rows, degraded = classify("iota-app/prod/BROKEN")
        self.assertEqual(rows["iota-app/prod/BROKEN"]["source"], "unknown")
        self.assertTrue(any(d.get("code") == "meta-unreadable" and d.get("slot") == "iota-app/prod/BROKEN"
                            for d in degraded), degraded)

    def test_a_tampered_label_is_not_echoed(self):
        put("kappa-app", "prod", "TAMPERED")
        meta = meta_of("kappa-app", "prod", "TAMPERED")
        meta["source"] = {"class": "received", "from": "sk-or-v1-" + "b" * 48}
        private_io.write(VAULT_DIR / "kappa-app" / "prod" / "TAMPERED.meta.json", json.dumps(meta))
        rows, degraded = classify("kappa-app/prod/TAMPERED")
        self.assertEqual(rows["kappa-app/prod/TAMPERED"]["source"], "unknown")
        self.assertNotIn("b" * 48, json.dumps(rows) + json.dumps(degraded))


class Surfaces(Pinned):

    def test_observatory_credentials_carries_the_class_context_and_consumers(self):
        import survey
        put("lambda-web", "prod", "HANDED", VALUE, "--from", "example-org")
        put("lambda-web", "prod", "PLAIN", OTHER)
        answer = survey.credentials("lambda-web")
        rows = {r["name"]: r for r in answer["vault"]}
        self.assertEqual(rows["HANDED"]["source"], "received")
        self.assertEqual(rows["HANDED"]["sourceContext"]["from"], "example-org")
        self.assertEqual(rows["PLAIN"]["source"], "unknown")
        self.assertIsInstance(rows["PLAIN"]["consumers"], list)
        self.assertEqual(rows["PLAIN"]["context"]["kind"], "personal")
        self.assertEqual(rows["PLAIN"]["consumersTool"], "observatory_key_consumers")
        for kept in ("name", "env", "project", "expiry", "use", "machines"):
            self.assertIn(kept, rows["PLAIN"])
        self.assertIn("degraded", answer)
        self.assertTrue(clean(json.dumps(answer)))

    def test_the_keys_page_rows_carry_the_class(self):
        import credentials_registry
        put("mu-web", "prod", "HANDED", VALUE, "--from", "example-org")
        creds, _ = credentials_registry.records({"keys": [], "destinations": {}, "destination_paths": {}},
                                                VAULT_DIR, [{"id": "project:mu-web", "name": "mu-web",
                                                             "local_folders": ["mu-web"]}])
        row = next(c for c in creds if c["id"] == "credential:vault/mu-web/prod/HANDED")
        self.assertEqual(row["key_source"], "received")
        self.assertEqual(row["key_source_context"]["from"], "example-org")
        self.assertEqual(row["source"], "vault")                      # the record's own source is unchanged
        self.assertEqual(row["key_context"]["kind"], "personal")
        self.assertTrue(clean(json.dumps(creds)))


# ---- OBS-604: copied versus shared, the key's context, consumers from every guard source ----------------------------

def node_root() -> pathlib.Path:
    import paths
    return paths.setting_path("OBSERVATORY_NODE", paths.HOME / "node")


def sent_share(share_id: str, project: str, env: str, slots: list[str], label: str = "example-org partner",
               state: str = "sealed", revocation=None) -> None:
    """A sender's share record as `node/shares.py` writes it: names, keys and states, never a value."""
    folder = node_root() / "shares" / "sent" / share_id
    folder.mkdir(parents=True, exist_ok=True)
    private_io.write(folder / "share.json", json.dumps({
        "schema": "observatory-share-sent/1", "shareId": share_id, "label": label, "project": project, "env": env,
        "state": state, "revocation": revocation,
        "slots": [{"slot": s, "provider": "openrouter", "sequence": 1} for s in slots]}))


def write_config(name: str, doc: dict) -> None:
    import paths
    path = paths.config_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def write_registry(name: str, doc: dict) -> None:
    import paths
    paths.REGISTRY.mkdir(parents=True, exist_ok=True)
    (paths.REGISTRY / name).write_text(json.dumps(doc), encoding="utf-8")


ORGS = {"organizations": {
    "acme": {"label": "Acme Example", "match": {"github_owners": ["acme-org"]}},
    "self": {"label": "Me", "default": True}}, "projects": {}}
PROJECTS = {"projects": [
    {"id": "project:acme-web", "name": "acme-web", "local_folders": ["acme-web"], "owners": ["acme-org"],
     "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-org"},
    {"id": "project:own-app", "name": "own-app", "local_folders": ["own-app"], "owners": ["me-org"],
     "organization": "self", "organization_source": "default", "organization_why": "no rule matched"},
    {"id": "project:torn", "name": "torn", "local_folders": ["torn"], "organization": None,
     "organization_source": "conflict", "organization_why": "acme vs self"},
    {"id": "project:clone", "name": "clone", "local_folders": ["clone"], "organization": "external",
     "organization_source": "external", "organization_why": "outside"}]}


class OutwardShares(Pinned):
    """`shared` is handed to another party, from the share and hand-over records; `copied` stays inside the vault."""

    def setUp(self):
        super().setUp()
        import shutil
        shutil.rmtree(node_root() / "shares", ignore_errors=True)
        self.addCleanup(lambda: shutil.rmtree(node_root() / "shares", ignore_errors=True))

    def test_a_sent_peer_share_makes_a_slot_shared_and_names_the_party(self):
        put("nu-app", "prod", "OUT_BY_SHARE", HAND)
        sent_share("a" * 32, "nu-app", "prod", ["OUT_BY_SHARE"], label="example-org partner")
        rows, degraded = classify("nu-app/prod/OUT_BY_SHARE")
        row = rows["nu-app/prod/OUT_BY_SHARE"]
        self.assertEqual(row["source"], "shared")
        self.assertEqual(row["sharedTo"], [{"kind": "peer-share", "to": "example-org partner",
                                            "record": "peer share " + "a" * 8, "state": "sealed",
                                            "takenBack": False}])
        self.assertEqual(row["sourceContext"]["to"], ["example-org partner"])
        self.assertFalse([d for d in degraded if d.get("source") == "shares"], degraded)
        self.assertTrue(clean(json.dumps(rows)))

    def test_an_issued_key_shared_out_stays_issued_and_names_where_it_went(self):
        put("xi-app", "prod", "ISSUED_OUT")
        private_io.write(org_view._secret_store() / "openrouter-issued.json", json.dumps({"issued": {
            "xi": {"destination": "vault:xi-app/prod/ISSUED_OUT", "account": "acct-x"}}}))
        sent_share("b" * 32, "xi-app", "prod", ["ISSUED_OUT"], revocation={"at": "2026-11-01"})
        rows, _ = classify("xi-app/prod/ISSUED_OUT")
        row = rows["xi-app/prod/ISSUED_OUT"]
        self.assertEqual(row["source"], "issued", "where it came from wins; the share is named beside it")
        self.assertEqual([(s["kind"], s["takenBack"]) for s in row["sharedTo"]], [("peer-share", True)])

    def test_a_credential_request_into_an_organization_context_is_shared_out(self):
        put("omicron", "prod", "ORG_AGENT_KEY")
        vault.record_provenance("omicron", "prod", "ORG_AGENT_KEY", {
            "requestId": "req-9", "issuer": {"door": "openrouter", "account": "acct-o"},
            "recipient": {"agentRef": "example-agent", "context": "org:acme", "slot": "ORG_AGENT_KEY"}})
        put("omicron", "prod", "OWN_AGENT_KEY")
        vault.record_provenance("omicron", "prod", "OWN_AGENT_KEY", {
            "requestId": "req-10", "issuer": {"door": "openrouter", "account": "acct-o"},
            "recipient": {"agentRef": "example-agent", "context": "personal", "slot": "OWN_AGENT_KEY"}})
        rows, _ = classify("omicron/prod/ORG_AGENT_KEY", "omicron/prod/OWN_AGENT_KEY")
        out = rows["omicron/prod/ORG_AGENT_KEY"]
        self.assertEqual(out["source"], "issued")
        self.assertEqual(out["sharedTo"], [{"kind": "credential-request", "to": "org:acme",
                                            "agent": "example-agent", "record": "credential request req-9"}])
        self.assertNotIn("sharedTo", rows["omicron/prod/OWN_AGENT_KEY"], "a personal request stays here")

    def test_a_hand_over_in_an_applied_selection_is_shared_out(self):
        put("pi-app", "prod", "HANDED_KEY", HAND)
        write_config("organizations.json", ORGS)
        self.addCleanup(lambda: write_config("organizations.json", {"organizations": {}, "projects": {}}))
        import org_selection
        record = {"revision": 3, "items": [
            {"id": "vault:pi-app/prod/HANDED_KEY", "kind": "key", "decision": "hand-over-revoke",
             "slot": "pi-app/prod/HANDED_KEY"},
            {"id": "vault:pi-app/prod/KEPT", "kind": "key", "decision": "keep", "slot": "pi-app/prod/KEPT"}]}
        from unittest import mock
        with mock.patch.object(org_selection, "_revisions", lambda org: [pathlib.Path("1.json")] if org == "acme"
                               else []), \
                mock.patch.object(org_selection, "current", lambda org: record if org == "acme" else None):
            rows, degraded = classify("pi-app/prod/HANDED_KEY")
        row = rows["pi-app/prod/HANDED_KEY"]
        self.assertEqual(row["source"], "shared")
        self.assertEqual(row["sharedTo"], [{"kind": "org-hand-over", "to": "acme", "decision": "hand-over-revoke",
                                            "record": "organization selection revision 3"}])

    def test_an_unreadable_share_record_is_named_never_read_as_no_share(self):
        put("rho-app", "prod", "ANY")
        folder = node_root() / "shares" / "sent" / ("c" * 32)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "share.json").write_text("{not json", encoding="utf-8")
        rows, degraded = classify("rho-app/prod/ANY")
        self.assertEqual(rows["rho-app/prod/ANY"]["source"], "unknown")
        self.assertIn(("shares", "share-unreadable"), [(d.get("source"), d.get("code")) for d in degraded])


class KeyContext(Pinned):
    """Each row says personal or which organization, read as `full org keys` reads it (OBS-604)."""

    def tearDown(self):
        write_config("organizations.json", {"organizations": {}, "projects": {}})
        import paths
        (paths.REGISTRY / "projects.json").unlink(missing_ok=True)

    def test_without_organizations_every_key_is_personal(self):
        write_config("organizations.json", {"organizations": {}, "projects": {}})
        put("sigma", "prod", "K")
        rows, _ = classify("sigma/prod/K")
        self.assertEqual(rows["sigma/prod/K"]["context"]["kind"], "personal")

    def test_each_project_s_organization_is_the_key_s_context(self):
        write_config("organizations.json", ORGS)
        write_registry("projects.json", PROJECTS)
        for folder in ("acme-web", "own-app", "torn", "clone", "loose-folder"):
            put(folder, "prod", "K")
        rows, degraded = classify(*(f"{f}/prod/K" for f in ("acme-web", "own-app", "torn", "clone", "loose-folder")))
        ctx = {slot.split("/")[0]: r["context"] for slot, r in rows.items()}
        self.assertEqual((ctx["acme-web"]["kind"], ctx["acme-web"]["organization"], ctx["acme-web"]["label"],
                          ctx["acme-web"]["project"]), ("organization", "acme", "Acme Example", "project:acme-web"))
        self.assertEqual((ctx["own-app"]["kind"], ctx["own-app"]["organization"]), ("personal", "self"))
        self.assertEqual(ctx["torn"]["kind"], "undecided")
        self.assertEqual(ctx["clone"]["kind"], "not-ours")
        self.assertEqual((ctx["loose-folder"]["kind"], ctx["loose-folder"]["project"]), ("personal", None))
        self.assertTrue(clean(json.dumps(rows)))

    def test_a_missing_registry_is_degraded_and_the_context_undecided(self):
        write_config("organizations.json", ORGS)
        import paths
        (paths.REGISTRY / "projects.json").unlink(missing_ok=True)
        put("tau", "prod", "K")
        rows, degraded = classify("tau/prod/K")
        self.assertEqual(rows["tau/prod/K"]["context"]["kind"], "undecided")
        self.assertIn("registry/projects.json", [d.get("source") for d in degraded])


class FakeValueSource:
    """A consumer source that compares the value in memory, as the value sources do."""

    def __init__(self, name, value, row, degraded=()):
        self.name, self._value, self._row, self._degraded, self.asked = name, value, row, list(degraded), 0

    def find(self, old):
        self.asked += 1
        import hmac
        hit = isinstance(old, str) and hmac.compare_digest(old.encode(), self._value.encode())
        return ([dict(self._row)] if hit else []), [dict(d) for d in self._degraded], []


class FakeScan:
    name = "openrouter-scan"

    def __init__(self):
        self.keys = []

    def find_key(self, key):
        self.keys.append(key)
        return [{"kind": "openrouter-serves", "serves": "example-gateway"}], [], []


class Consumers(Pinned):
    """Consumers come from every source the revoke guard reads, the value held in memory only."""

    def test_the_default_sources_are_the_guard_s(self):
        import key_consumers
        self.assertEqual(set(key_source.GUARD_NAMES) | set(key_source.SHOWN_AS_COPIED), set(key_consumers.GUARD_SOURCES))

    def test_every_source_is_read_and_no_value_is_shown(self):
        put("upsilon", "prod", "OPENROUTER_API_KEY", VALUE)
        private_io.write(org_view._secret_store() / "openrouter-issued.json", json.dumps({"issued": {
            "upsilon-llm": {"destination": "vault:upsilon/prod/OPENROUTER_API_KEY", "account": "acct-u",
                            "hash": "h-upsilon", "label": "sk-or-v1-abc...xyz"}}}))
        env = FakeValueSource("env-file", VALUE, {"kind": "env-file", "path": "/srv/example-app/.env", "var": "LLM"})
        heroku = FakeValueSource("heroku", VALUE, {"kind": "heroku", "app": "example-app", "var": "LLM_KEY"},
                                 degraded=[{"source": "heroku", "app": "other-app", "code": "config-unreadable",
                                            "reason": "config vars of other-app could not be read"}])
        launchd = FakeValueSource("launchd", VALUE, {"kind": "launchd", "label": "dev.example.bot"})
        scan = FakeScan()
        degraded: list = []
        got = key_source.consumers("upsilon", "prod", "OPENROUTER_API_KEY", degraded,
                                   sources=[scan, heroku, env, launchd])
        self.assertEqual(sorted(c["kind"] for c in got),
                         ["env-file", "heroku", "launchd", "openrouter-serves"])
        self.assertEqual(scan.keys[0]["hash"], "h-upsilon", "the OpenRouter scan is asked by the ledger's hash")
        self.assertIn(("heroku", "config-unreadable"), [(d["source"], d["code"]) for d in degraded])
        self.assertTrue(clean(json.dumps(got) + json.dumps(degraded)))

    def test_a_key_no_ledger_names_skips_the_scan_and_a_slot_without_value_reads_nothing(self):
        put("phi", "prod", "PLAIN", OTHER)
        scan, env = FakeScan(), FakeValueSource("env-file", VALUE, {"kind": "env-file", "path": "/x", "var": "V"})
        degraded: list = []
        self.assertEqual(key_source.consumers("phi", "prod", "PLAIN", degraded, sources=[scan, env]), [])
        self.assertEqual((scan.keys, env.asked), ([], 1))
        self.assertEqual(key_source.consumers("phi", "prod", "ABSENT", degraded, sources=[env]), [])
        self.assertEqual(env.asked, 1, "no value, no comparison")
        self.assertEqual(degraded, [])

    def test_one_reader_for_many_slots_reads_each_source_once_per_slot_and_names_a_failure_once(self):
        put("chi", "prod", "A", VALUE)
        put("chi", "prod", "B", OTHER)
        bad = FakeValueSource("heroku", "none", {}, degraded=[{"source": "heroku", "code": "token-unreadable",
                                                               "reason": "no Heroku token"}])
        degraded: list = []
        reader = key_source.ConsumerReader(degraded, sources=[bad])
        reader.of("chi", "prod", "A")
        reader.of("chi", "prod", "B")
        self.assertEqual(bad.asked, 2)
        self.assertEqual([d["code"] for d in degraded], ["token-unreadable"])


if __name__ == "__main__":
    unittest.main()
