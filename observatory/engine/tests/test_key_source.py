#!/usr/bin/env python3
"""Keys by source (OBS-556, CP-10): every vault slot is classed by where its value came from.

`issued` (a door minted it: its ledger, its record, the movements journal, or a credential request's provenance),
`received` (a person, an organization or an agent handed it over: `vault.py put --from LABEL`), `shared` (the same
value sits in another project's folder, measured from the keyed digests of this machine's manifest) or `unknown`.
What is asserted, with no network:

- each class, with its context, and a slot put without a source reads `unknown`, never `issued` (`test_key_classes`);
- `put --from` round trip: the label lands beside the slot's metadata and the journal, never in the value file; a put
  or a rotation without it forgets it; a label shaped like a credential, too long, or equal to the value is refused
  and nothing is written;
- a slot whose metadata does not read is `unknown`, and `degraded` names it;
- `observatory_credentials` and the keys page's registry rows carry the class, its context and the consumers;
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
        # shared: one value under two project folders, measured from the keyed manifest.
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
        shared = rows["alpha-web/prod/SHARED_KEY"]
        self.assertEqual(shared["source"], "shared")
        self.assertEqual(shared["sourceContext"]["with"], ["beta-api/prod/SHARED_KEY"])
        self.assertEqual(shared["sharedWith"], ["beta-api/prod/SHARED_KEY"])
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
        self.assertEqual(rows["delta-site/prod/MINTED"]["source"], "shared")

    def test_without_a_manifest_shared_is_not_measured_and_says_so(self):
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
        self.assertTrue(clean(json.dumps(creds)))


if __name__ == "__main__":
    unittest.main()
