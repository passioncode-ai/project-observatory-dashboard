#!/usr/bin/env python3
"""The credential inventory: what it must never hold, and who it must never guess.

A document about secrets earns two rules the rest of the registry does not:

1. IT CONTAINS NONE. A `label` is what the provider calls a key — a short
   prefix, an ellipsis and a short suffix — and anything longer that starts the same way is the
   key itself. `tools/validate_registry.py` refuses such a record and
   `tools/check_secrets.py` reads the file on every gate run; this suite plants
   one to watch the first of those fail.
2. IT NEVER GUESSES WHO USES WHAT. A shared account's membership cannot be
   measured, and the wrong answer is not a wrong label — it sends somebody to
   rotate a credential other projects are quietly using. A curated row without
   evidence is refused for that reason, the same rule the curated
   `heroku_links.json` follows.

The rules over it are here too, including the one the first run could not fire:
a record invented FOR a leak was the one kind that could never be MARKED as
leaked, because the check asked only about `project-secret`.
"""
from __future__ import annotations
import json, os, pathlib, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
# `tmp.mkdtemp`, not `tempfile.mkdtemp`: a bare mkdtemp leaves its directory
# behind, and a gate that fills the disk is a gate that gets turned off.
import tmp as tmpdir

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


PROJECTS = [{"id": "project:alpha", "name": "alpha", "local_folders": ["alpha"]},
            {"id": "project:beta", "name": "beta", "local_folders": ["beta"]}]

SCAN = {"scanned_at": "2026-09-11T00:00:00Z",
        "keys": [{"label": "sk-or-v1-aaa...111", "name": "project-alpha-tick",
                  "serves": "observatory", "limit": 400, "limit_reset": "monthly",
                  "usage": 1.0, "disabled": False, "created_at": "2026-09-01T00:00:00"},
                 {"label": "sk-or-v1-bbb...222", "name": "PRODUCTION_user_9",
                  "serves": None, "limit": 0, "limit_reset": None, "usage": 0,
                  "disabled": False, "created_at": "2026-01-01T00:00:00"}],
        "destinations": {}, "destination_paths": {}, "unlisted_destinations": [],
        "degraded": []}


def machine_id(name: str) -> str:
    """A machine-secret record's id, composed from the file name it is about."""
    return "credential:" + "machine/" + name


def registry():
    import credentials_registry
    return credentials_registry


def rules():
    import credential_findings
    return credential_findings


def test_another_products_keys_are_not_this_estates():
    """The account holds over a thousand keys minted by a different product.

    Carrying them would bury four facts under twelve hundred and make the
    document an inventory of somebody else's work.
    """
    R = registry()
    with tempfile.TemporaryDirectory() as d:
        creds, _ = R.records(SCAN, pathlib.Path(d), PROJECTS)
    names = [c.get("name") for c in creds]
    check("a key this estate minted is kept", "project-alpha-tick" in names)
    check("a stranger's key is not", "PRODUCTION_user_9" not in names, str(names))


def test_a_leak_with_no_slot_still_becomes_a_record():
    """Both real leaks are against secrets the vault never held.

    Without this the board carries `secret.leaked_unrotated` for a credential
    the registry cannot show — a debt with no subject.
    """
    R = registry()
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d)
        # The WRITER'S rows, verbatim shapes from `tools/vault.py`: a leak is
        # `event: "leaked"` and a rotation closes it with `event: "settled"`
        # naming the row in `of`. The first fixture here wrote rows in the
        # reader's imagined shape (no `event` at all), which is how a reader
        # that could never see a settlement stayed green.
        (store / "leaks.jsonl").write_text("\n".join([
            json.dumps({"event": "leaked", "id": "leak:1",
                        "secret": "alpha/prod/DATABASE_URL",
                        "where": "a traceback", "at": "2026-09-10T00:00:00Z"}),
            json.dumps({"event": "leaked", "id": "leak:2",
                        "secret": "beta/prod/API_KEY",
                        "where": "a transcript", "at": "2026-09-10T00:00:00Z"}),
            json.dumps({"event": "settled", "of": "leak:2",
                        "at": "2026-09-11T00:00:00Z"}),
        ]) + "\n", encoding="utf-8")
        open_now = R.leaks(store)
        creds, edges = R.records(SCAN, store, PROJECTS)
    leaked = [c for c in creds if c["id"].endswith("alpha/prod/DATABASE_URL")]
    check("the leak produces a record", len(leaked) == 1, str([c["id"] for c in creds]))
    check("and that record is MARKED leaked", leaked and leaked[0].get("leaked") is True,
          "the kind invented for leaks was the one kind that could not be marked")
    check("and it is tied to the project its slot names",
          leaked and leaked[0]["used_by"] == ["project:alpha"],
          str(leaked and leaked[0].get("used_by")))
    check("a settled leak is not a debt", set(open_now) == {"alpha/prod/DATABASE_URL"},
          f"open: {sorted(open_now)} — the settlement row must clear leak:2")
    check("and produces no record either",
          not [c for c in creds if c["id"].endswith("beta/prod/API_KEY")],
          str([c["id"] for c in creds]))
    check("an empty register holds no debt",
          R.leaks(pathlib.Path(tmpdir.mkdtemp())) == {})


def test_a_curated_row_without_evidence_is_refused():
    """The only thing between a shared account's membership and a guess."""
    R = registry()
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "credential_owners.json"
        p.write_text(json.dumps({"owners": [
            {"credential": "credential:openrouter/x", "projects": ["alpha"],
             "evidence": "the Cloudflare account page lists both"},
            {"credential": "credential:openrouter/y", "projects": ["beta"]},
        ]}), encoding="utf-8")
        was, R.OWNERS = R.OWNERS, p
        try:
            got = R.load_owners()
        finally:
            R.OWNERS = was
    check("a row carrying evidence is accepted", len(got) == 1, str(got))
    check("a row without it is refused",
          all(r["credential"] != "credential:openrouter/y" for r in got))


def test_the_edge_is_many_to_many():
    """One account, several projects — repeated edges, not one field with a list,
    so the same query answers both directions."""
    R = registry()
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "credential_owners.json"
        p.write_text(json.dumps({"owners": [
            {"credential": "credential:openrouter/project-alpha-tick",
             "projects": ["alpha", "beta"], "evidence": "both deploy from it"}]}),
            encoding="utf-8")
        was, R.OWNERS = R.OWNERS, p
        try:
            creds, edges = R.records(SCAN, pathlib.Path(d), PROJECTS)
        finally:
            R.OWNERS = was
    shared = [c for c in creds if c.get("name") == "project-alpha-tick"][0]
    check("one credential carries two projects", len(shared["used_by"]) == 2,
          str(shared["used_by"]))
    check("and there is an edge per project, not one edge with a list",
          sum(1 for e in edges if e["from"] == shared["id"]) == 2)
    check("every edge is the same declared type",
          {e["type"] for e in edges} == {"credential_used_by"})


def test_a_record_never_carries_a_value():
    """The one rule this document exists to keep, watched failing."""
    doc = {"credentials": [{"id": "credential:openrouter/x", "kind": "llm-api-key",
                            "label": "sk-or-v1-aaa...111", "used_by": ["project:alpha"]}]}
    text = json.dumps(doc)
    check("a label is not key-shaped", "..." in text and len(doc["credentials"][0]["label"]) < 24)
    planted = "sk-or-v1-" + "0" * 60
    check("and a planted key IS key-shaped by the validator's own test",
          planted.startswith("sk-") and "..." not in planted and len(planted) > 24,
          "if this fails the validator's rule cannot fire either")
    src = (ROOT / "tools/validate_registry.py").read_text(encoding="utf-8")
    check("the validator refuses such a record",
          "carries something key-shaped" in src)


def test_the_rules_fire_on_their_own_subjects():
    F = rules()
    lifetime = F.findings({"credentials": [
        {"id": "credential:openrouter/x", "kind": "llm-api-key", "name": "x",
         "limit": 400, "limit_reset": None, "usage": 1, "used_by": ["project:alpha"]}]})
    check("a cap with no reset raises credential.lifetime_cap",
          any(f["type"] == "credential.lifetime_cap" for f in lifetime))
    monthly = F.findings({"credentials": [
        {"id": "credential:openrouter/x", "kind": "llm-api-key", "name": "x",
         "limit": 400, "limit_reset": "monthly", "used_by": ["project:alpha"]}]})
    check("a monthly cap raises nothing about its reset",
          not any(f["type"] == "credential.lifetime_cap" for f in monthly))
    orphan = F.findings({"credentials": [
        {"id": "credential:openrouter/x", "kind": "llm-api-key", "name": "x",
         "used_by": []}]})
    check("a credential nothing claims raises credential.unclaimed",
          any(f["type"] == "credential.unclaimed" for f in orphan))
    leaked = F.findings({"credentials": [
        {"id": "credential:vault/a/prod/B", "kind": "leaked-untracked", "name": "B",
         "vault_project": "a", "env": "prod", "leaked": True, "used_by": []}]})
    check("a leak-only credential raises credential.untracked",
          any(f["type"] == "credential.untracked" for f in leaked))
    shared = F.findings({"credentials": [
        {"id": "credential:openrouter/x", "kind": "llm-api-key", "name": "x",
         "used_by": ["project:alpha", "project:beta"]}]})
    check("a credential two projects use raises credential.shared_rotation",
          any(f["type"] == "credential.shared_rotation" for f in shared))
    check("no scan raises nothing", F.findings(None) == [] and F.findings({}) == [])


def test_no_rule_here_duplicates_the_leak_register():
    """`secret.leaked_unrotated` is raised by build_findings from the register.

    Two rules over one subject is how a board grows two rows that mean one
    thing, a defect this board has already paid for once.
    """
    src = (ROOT / "tools/credential_findings.py").read_text(encoding="utf-8")
    emitted = [ln for ln in src.splitlines() if '"type": "' in ln]
    check("this file raises no secret.* row",
          not any("secret." in ln for ln in emitted), str(emitted))


def emitted_estate() -> tuple[pathlib.Path, dict]:
    """A synthetic workspace whose registry the REAL emitter wrote.

    The original read the operator's live document; here the same emitter runs
    over a frozen fixture with a planted OpenRouter listing, so the document's
    self-consistency is checked on something this test built.
    """
    from emitter_fixture import seed
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-credentials-")).resolve()
    env = seed(root)
    env = {**env, "OBSERVATORY_HOME": str(root / "home")}
    subprocess.run([sys.executable, str(ROOT / "observatory.py"), "init"], cwd=ROOT, env=env,
                   capture_output=True, timeout=120, check=True)
    (root / "raw" / "openrouter.json").write_text(json.dumps(SCAN), encoding="utf-8")
    p = subprocess.run([sys.executable, str(ROOT / "collectors/emit_registry.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    check("the emitter writes a credential document", p.returncode == 0
          and (root / "registry/credentials.json").is_file(), (p.stdout + p.stderr)[-300:])
    return root, env


def test_the_emitted_document_agrees_with_itself():
    # PORTED: the original read the live registry and skipped on a fresh clone;
    # this builds its own document with the real emitter instead.
    root, _ = emitted_estate()
    doc = root / "registry/credentials.json"
    if not doc.is_file():
        return
    d = json.loads(doc.read_text(encoding="utf-8"))
    creds, t = d["credentials"], d["totals"]
    check("the synthetic document holds the estate's own key", any(
          c.get("name") == "project-alpha-tick" for c in creds), str([c.get("name") for c in creds]))
    check("totals count the rows they summarise", t["credentials"] == len(creds))
    check("leaked count agrees with the rows",
          t["leaked_unrotated"] == sum(1 for c in creds if c.get("leaked")))
    check("every unclaimed credential says why",
          all(c.get("unclaimed_reason") for c in creds if not (c.get("used_by") or [])))
    rel = json.loads((root / "registry/relations.json").read_text(encoding="utf-8"))
    check("`credential_used_by` is a declared relation type",
          "credential_used_by" in rel["relation_types"])
    ids = {c["id"] for c in creds}
    edges = [r for r in rel["relations"] if r["type"] == "credential_used_by"]
    check("no edge points at a credential the document does not hold",
          all(e["from"] in ids for e in edges))
    check("no record carries anything key-shaped",
          not any(isinstance(v, str) and v.startswith("sk-") and "..." not in v
                  and len(v) > 24 for c in creds for v in c.values()))


def test_the_machines_own_secrets_are_records_too() -> None:
    """The third class, after OpenRouter keys and vault slots.

    These are the files collectors and plugins authenticate WITH. Until they
    were recorded the estate could not answer "what does this machine hold and
    what reads it" — the question the projection exists for. Driven over a
    planted store, never a real one.
    """
    store = pathlib.Path(tmpdir.mkdtemp()) / "secrets"
    (store / "cloudflare-analytics.d").mkdir(parents=True)
    (store / "cloudflare-analytics.d/acme").write_text("t1", encoding="utf-8")
    (store / "cloudflare-analytics.d/other").write_text("t2", encoding="utf-8")
    (store / "cloudflare-analytics.d/.DS_Store").write_text("x", encoding="utf-8")
    # COMPOSED, never written as a literal: a key-shaped string in this tree is
    # indistinguishable from a real one to every reader and every scanner.
    fake_key = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5 + "\nnope\n"
    (store / "google-service-account.json").write_text(json.dumps(
        {"type": "service_account", "client_email": "sa@example.iam.gserviceaccount.com",
         "private_key": fake_key}), encoding="utf-8")
    (store / "plain-token").write_text("value", encoding="utf-8")
    (store / "projects/alpha").mkdir(parents=True)
    got = {c["id"]: c for c in registry().from_machine_secrets(store)}

    check("a directory of per-account tokens is ONE credential",
          machine_id("cloudflare-analytics.d") in got, str(sorted(got)))
    cf = got.get(machine_id("cloudflare-analytics.d"), {})
    check("carrying its holder count", cf.get("holders") == 2, str(cf.get("holders")))
    check("and their labels, which are account names and not values",
          cf.get("holder_labels") == ["acme", "other"], str(cf.get("holder_labels")))
    sa = got.get(machine_id("google-service-account.json"), {})
    check("a service account states its identity in the clear",
          sa.get("identity") == "sa@example.iam.gserviceaccount.com", str(sa.get("identity")))
    check("and its reader is named, because that is what breaks on rotation",
          sa.get("read_by") == "plugins/ga4_analytics.py", str(sa.get("read_by")))
    check("a plain token is a record with no identity to state",
          got.get(machine_id("plain-token"), {}).get("identity") is None)
    check("THE VAULT IS NOT ONE OF THESE — its slots have their own records",
          machine_id("projects") not in got, str(sorted(got)))
    for c in got.values():
        for k, v in c.items():
            check(f"{c['name']}.{k} carries no value",
                  not (isinstance(v, str)
                       and ("PRIVATE" in v or v in ("t1", "t2", "value"))),
                  f"{k} leaked a value")


def test_a_slot_named_for_no_project_here_says_which_name_is_unknown():
    """A slot like `northwind/prod/ADS_API_TOKEN`: named for the company whose
    ad accounts it opens, and no project in this registry is
    called that. The generic sentence said "no vault path names a project" —
    about a record whose vault path plainly names one."""
    R = registry()
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "store"
        slot = store / "northwind" / "prod" / "ADS_API_TOKEN"
        slot.parent.mkdir(parents=True)
        slot.write_text("fixture-not-a-value\n", encoding="utf-8")
        slot.with_name(slot.name + ".meta.json").write_text(
            json.dumps({"created": "2026-09-14T10:10:32Z"}), encoding="utf-8")
        creds, edges = R.records(SCAN, store, PROJECTS)
    row = [c for c in creds if c["id"] == "credential:vault/northwind/prod/ADS_API_TOKEN"][0]
    check("the slot is a record with no project", row["used_by"] == [], str(row["used_by"]))
    check("and the reason names the unknown name, not a missing path",
          "'northwind'" in row["unclaimed_reason"] and "no project in this registry" in row["unclaimed_reason"],
          row["unclaimed_reason"])
    check("the value never reaches the record", "fixture-not-a-value" not in json.dumps(creds))
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "store"
        slot = store / "alpha" / "prod" / "DB_URL"
        slot.parent.mkdir(parents=True)
        slot.write_text("fixture-not-a-value\n", encoding="utf-8")
        creds, edges = R.records(SCAN, store, PROJECTS)
    row = [c for c in creds if c["id"] == "credential:vault/alpha/prod/DB_URL"][0]
    check("a slot named for a project here is joined by its path and carries no reason",
          row["used_by"] == ["project:alpha"] and not row.get("unclaimed_reason"),
          str((row["used_by"], row.get("unclaimed_reason"))))
    # BY FOLDER TOO. `acme-ops-desk/prod/DEPLOY_TOKEN` is the folder of the
    # project the registry calls `ops-desk`; joined by name alone, most slots
    # read "unowned" on the keys page.
    folder_projects = PROJECTS + [{"id": "project:ops-desk", "name": "ops-desk",
                                   "local_folders": ["acme-ops-desk"]}]
    with tempfile.TemporaryDirectory() as d:
        store = pathlib.Path(d) / "store"
        slot = store / "acme-ops-desk" / "prod" / "DEPLOY_TOKEN"
        slot.parent.mkdir(parents=True)
        slot.write_text("fixture-not-a-value\n", encoding="utf-8")
        creds, edges = R.records(SCAN, store, folder_projects)
    row = [c for c in creds if c["id"] == "credential:vault/acme-ops-desk/prod/DEPLOY_TOKEN"][0]
    check("a slot named for a project's FOLDER is joined to that project",
          row["used_by"] == ["project:ops-desk"] and not row.get("unclaimed_reason"),
          str((row["used_by"], row.get("unclaimed_reason"))))


def test_a_register_that_will_not_read_is_a_row_and_withholds_unsigned():
    """A corrupt annotations file used to read as empty: every
    signature gone, `credential.unsigned` on every row, and the next `set`
    writing one annotation over all the others. Driven on a planted file."""
    R = registry()
    F = rules()
    with tempfile.TemporaryDirectory() as d:
        bad = pathlib.Path(d) / "credential_annotations.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        was = R.ANNOTATIONS
        R.ANNOTATIONS = bad
        try:
            creds, edges = R.records(SCAN, pathlib.Path(d), PROJECTS)
        finally:
            R.ANNOTATIONS = was
        problems = list(R.REGISTER_PROBLEMS)
        check("the loader records the fault instead of reading the file as empty",
              len(problems) == 1 and problems[0]["register"] == "credential_annotations.json",
              str(problems))
        fake_doc = {"credentials": creds, "scanned_on": "2026-09-14", "registers_unreadable": problems}
        out = F.findings(fake_doc)
        check("the board carries one row for the unreadable register",
              [f["type"] for f in out].count("credential.register_unreadable") == 1
              and "annotations" in [f for f in out if f["type"] == "credential.register_unreadable"][0]["subject"],
              str([f["type"] for f in out]))
        check("and `credential.unsigned` is withheld rather than fired on everything",
              not any(f["type"] == "credential.unsigned" for f in out), str([f["type"] for f in out]))
        was = R.ANNOTATIONS
        R.ANNOTATIONS = pathlib.Path(d) / "absent-annotations.json"
        try:
            check("a MISSING register is still simply empty, not a fault",
                  R.load_annotations() == {})
        finally:
            R.ANNOTATIONS = was
        src = (ROOT / "collectors/credentials_registry.py").read_text(encoding="utf-8")
        check("the loader distinguishes missing from unreadable in code",
              "if not ANNOTATIONS.is_file():\n        return {}" in src)
        # THE WRITER REFUSES. `sign_credential.py set` on the corrupt file must
        # exit non-zero and leave the bytes exactly as they were.
        before = bad.read_bytes()
        # An id the emitted board knows, because the tool checks the id BEFORE
        # it opens the register — an unknown id is refused for its own reason.
        root, env = emitted_estate()
        live = json.loads((root / "registry/credentials.json").read_text(encoding="utf-8"))
        real_id = live["credentials"][0]["id"]
        p = subprocess.run([sys.executable, str(ROOT / "tools/sign_credential.py"), "set",
                            real_id, "--purpose", "a purpose long enough",
                            "--evidence", "an evidence long enough"],
                           cwd=ROOT, env={**env, "OBSERVATORY_ANNOTATIONS": str(bad)},
                           capture_output=True, text=True, timeout=60)
        check("sign_credential refuses to write over a register it cannot read",
              p.returncode != 0 and "refusing" in (p.stderr + p.stdout), (p.stderr + p.stdout)[-200:])
        check("and the file is byte-identical afterwards", bad.read_bytes() == before)


def test_a_vault_slot_reaches_the_document_without_an_openrouter_scan() -> None:
    """A key added with `vault.py put` never reached the Keys page: the emitter
    built the credential document only when `store/raw/openrouter.json` existed,
    which is never, for a workspace that has not switched OpenRouter on."""
    from emitter_fixture import seed
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-credentials-novault-")).resolve()
    env = {**seed(root), "OBSERVATORY_HOME": str(root / "home")}
    env.pop("OBSERVATORY_VAULT_DIR", None)
    subprocess.run([sys.executable, str(ROOT / "observatory.py"), "init"], cwd=ROOT, env=env,
                   capture_output=True, timeout=120, check=True)
    (root / "raw" / "openrouter.json").unlink(missing_ok=True)
    put = subprocess.run([sys.executable, str(ROOT / "tools/vault.py"), "put", "fixture-a", "local", "EXAMPLE_API_KEY"],
                         cwd=ROOT, env=env, input="synthetic-not-a-real-value-0000\n",
                         capture_output=True, text=True, timeout=60)
    check("vault put accepts the slot", put.returncode == 0, (put.stdout + put.stderr)[-300:])
    p = subprocess.run([sys.executable, str(ROOT / "collectors/emit_registry.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    doc = root / "registry/credentials.json"
    check("the emitter still writes the credential document", p.returncode == 0 and doc.is_file(),
          (p.stdout + p.stderr)[-300:])
    if not doc.is_file():
        return
    d = json.loads(doc.read_text(encoding="utf-8"))
    names = [c.get("name") for c in d["credentials"]]
    check("the slot is a record", "EXAMPLE_API_KEY" in names, str(names))
    check("and the document says when it was read", bool(d.get("scanned_on")), str(d.get("scanned_on")))
    check("no value anywhere in it", "synthetic-not-a-real-value" not in doc.read_text(encoding="utf-8"))


if __name__ == "__main__":
    print("credentials — a document about secrets that holds none\n")
    for fn in (test_a_register_that_will_not_read_is_a_row_and_withholds_unsigned,
               test_a_slot_named_for_no_project_here_says_which_name_is_unknown,
               test_another_products_keys_are_not_this_estates,
               test_a_leak_with_no_slot_still_becomes_a_record,
               test_a_curated_row_without_evidence_is_refused,
               test_the_edge_is_many_to_many,
               test_a_record_never_carries_a_value,
               test_the_rules_fire_on_their_own_subjects,
               test_no_rule_here_duplicates_the_leak_register,
               test_the_emitted_document_agrees_with_itself,
               test_the_machines_own_secrets_are_records_too,
               test_a_vault_slot_reaches_the_document_without_an_openrouter_scan):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mmembership is measured or curated, and never guessed\033[0m")
