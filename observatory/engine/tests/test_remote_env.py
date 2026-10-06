#!/usr/bin/env python3
"""What production is configured with — the comparison, and what never leaves it.

THE DANGEROUS PART IS NOT THE COMPARISON, it is the fetch: Heroku's config-vars
endpoint has no names-only form, so every production secret of every application
passes through one process. Everything here exists to keep that transit honest:

  * the fingerprint is the SAME derivation `collectors/scan_env.py` uses — if it
    ever drifts, every verdict silently becomes `differs` and the page reports a
    healthy estate; the same sentence is true of the SALT, which is selectable
    and can be minted again, so each scan names its salt and a comparison
    across two names is refused rather than answered;
  * the registry document carries no fingerprint and no value, only the verdict,
    because `registry/` is in git and a salted fingerprint is a lookup the
    moment the salt leaks;
  * `differs` produces NO finding. A production value that differs from a
    laptop's is the design; hundreds of rows saying so would be the last time
    anybody read the board.

Values used here are COMPOSED rather than written: `tools/check_secrets.py`
reports a secret-shaped literal as a finding and cannot tell a fixture from a
live credential, which is the right rule and the reason for `_k()`.
"""
from __future__ import annotations
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                            

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []



def enable_remote_env_in_sandbox() -> None:
    """Turn the remote_env integration on in the synthetic workspace only.

    Public workspaces ship with every integration off. These checks exercise the
    scanner's cache and salt gates, which run before any provider call, so they
    need the integration on — but never in a real workspace.
    """
    import configuration
    import tempfile as _tempfile
    base = configuration.home().resolve()
    temp = pathlib.Path(_tempfile.gettempdir()).resolve()
    if not (base.is_relative_to(temp) or "observatory-portable-" in str(base)):
        raise SystemExit(f"refusing to enable an integration outside a sandbox: {base}")
    f = base / "config" / "settings.json"
    doc = json.loads(f.read_text()) if f.exists() else {
        "schema_version": configuration.CONFIG_VERSION, "sources": {}, "integrations": {}, "features": {}}
    doc.setdefault("integrations", {})["remote_env"] = True
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc), encoding="utf-8")

def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _k(tag: str) -> str:
    """A value that LOOKS like a credential without being a literal one."""
    return "-".join(("fixture", tag, "value", "not", "a", "real", "one"))


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _minted_state() -> tuple[pathlib.Path, str]:
    """A scratch STATE with its own salt, and the name that salt derives.

    Hermetic on purpose: a test that read the operator's own salt would pass or
    fail by ambient state, and the thing under test here is precisely what
    happens when there are two salts.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-state-"))
    d.chmod(0o700)
    code = ("import sys, importlib.util;"
            "spec=importlib.util.spec_from_file_location('scan_env','collectors/scan_env.py');"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            "import runtime_identity as ri;"
            "ri.load(m.SALT_FILE,'env-fingerprint-salt',initialize=True);"
            "print(m.namespace(m.salt()))")
    p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=120, env={**os.environ, "OBSERVATORY_STATE": str(d)})
    if p.returncode != 0:
        raise AssertionError((p.stdout + p.stderr)[-400:])
    return d, p.stdout.strip()


def test_the_two_inventories_fingerprint_the_same_way() -> None:
    """One derivation, or every comparison silently reads as `differs`."""
    sre = load("collectors/scan_remote_env.py", "scan_remote_env")
    env = load("collectors/scan_env.py", "scan_env")
    pepper, value = _k("pepper"), _k("database-url")
    import hashlib
    expected = hashlib.sha256((pepper + value).encode("utf-8")).hexdigest()[:16]
    check("the remote scan's fingerprint is a salted SHA-256, sixteen characters",
          sre.fingerprint(pepper, value) == expected, sre.fingerprint(pepper, value))
    src = (ROOT / "collectors/scan_env.py").read_text(encoding="utf-8")
    check("and `scan_env` computes it the same way in the same place",
          'hashlib.sha256(\n                    (pepper + value).encode("utf-8")).hexdigest()[:16]' in src
          or '(pepper + value).encode("utf-8")).hexdigest()[:16]' in src,
          "if the two derivations drift, every verdict becomes `differs` and nothing says so")
    check("and the two agree on one value", sre.fingerprint(pepper, value)
          == hashlib.sha256((pepper + value).encode("utf-8")).hexdigest()[:16])


def test_the_verdicts_are_the_four_the_scenario_asks_for() -> None:
    rr = load("collectors/remote_registry.py", "remote_registry")
    app = {"app": "demo", "folders": ["/srv/projects/demo"], "error": None, "vars": [
        {"name": "SAME", "class": "secret", "fingerprint": "aaaa"},
        {"name": "OTHER", "class": "secret", "fingerprint": "bbbb"},
        {"name": "ONLY_THERE", "class": "secret", "fingerprint": "cccc"},
        {"name": "PLAIN", "class": "config"},
    ]}
    env_scan = {"files": [
        {"kind": "env", "project": "demo", "path": "demo/.env", "variables": [
            {"name": "SAME", "class": "secret", "fingerprint": "aaaa"},
            {"name": "OTHER", "class": "secret", "fingerprint": "zzzz"},
            {"name": "PLAIN", "class": "config"},
            {"name": "ONLY_HERE", "class": "secret", "fingerprint": "dddd"},
        ]},
        # A TEMPLATE MUST NOT BE COMPARED: `.env.example` holds a placeholder by
        # construction, and comparing against one reports every application as
        # differing from itself.
        {"kind": "template", "project": "demo", "path": "demo/.env.example", "variables": [
            {"name": "SAME", "class": "secret", "fingerprint": "ffff"}]},
    ]}
    got = {v["name"]: v["verdict"] for v in rr.compare(app, env_scan, [])["vars"]}
    check("an equal value reads as the same", got["SAME"] == "same_as_local", str(got))
    check("a different one reads as differing", got["OTHER"] == "differs", str(got))
    check("a variable only production has", got["ONLY_THERE"] == "remote_only", str(got))
    check("a variable only the checkout has", got["ONLY_HERE"] == "local_only", str(got))
    check("and a non-secret is not guessed at",
          got["PLAIN"] == "not_compared",
          "the local inventory fingerprints secrets only; `same` there would be a guess")
    check("the template was not used as the comparison",
          got["SAME"] == "same_as_local",
          "a placeholder must never decide a verdict")
    nolocal = rr.compare({**app, "folders": []}, env_scan, [])
    check("an application with no checkout here is not reported as differing",
          {v["verdict"] for v in nolocal["vars"]} == {"no_local_checkout"},
          "nineteen applications' worth of variables would otherwise be a "
          "difference nobody can act on")


def test_a_retired_value_still_deployed_is_found_and_is_critical() -> None:
    rr = load("collectors/remote_registry.py", "remote_registry")
    rf = load("tools/remote_findings.py", "remote_findings")
    app = {"app": "demo", "folders": ["/x/demo"], "error": None, "vars": [
        {"name": "API_TOKEN", "class": "secret", "fingerprint": "old1"}]}
    retired = [{"project": "demo", "env": "prod", "name": "API_TOKEN",
                "retired_on": "2026-09-01", "fingerprint": "old1"}]
    row = rr.compare(app, {"files": []}, retired)
    check("production holding a retired value is named with its slot and date",
          row["retired_in_use"] == [{"name": "API_TOKEN", "retired_on": "2026-09-01",
                                     "slot": "demo/prod/API_TOKEN"}],
          str(row["retired_in_use"]))
    doc = {"apps": [row]}
    rows = rf.findings(doc)
    crit = [f for f in rows if f["type"] == "remote.retired_still_deployed"]
    check("and it is a critical, one per slot", len(crit) == 1
          and crit[0]["severity"] == "critical", str(rows))
    check("whose remedy names the application and the variable",
          "demo" in crit[0]["action"] and "API_TOKEN" in crit[0]["action"], crit[0]["action"])


def test_differing_is_not_a_finding_and_sharing_is_one() -> None:
    rf = load("tools/remote_findings.py", "remote_findings")
    only_differs = {"apps": [{"app": "a", "compared_with": ["/x/a"], "error": None,
                              "retired_in_use": [], "counts": {"differs": 400},
                              "vars": [{"name": f"V{i}", "class": "secret",
                                        "verdict": "differs"} for i in range(400)]}]}
    check("four hundred differing values raise nothing",
          rf.findings(only_differs) == [],
          "a production value that differs from a laptop's is the design")
    shared = {"apps": [
        {"app": "a", "compared_with": ["/x/a"], "error": None, "retired_in_use": [],
         "counts": {}, "vars": [{"name": "DATABASE_URL", "class": "secret",
                                 "verdict": "same_as_local"},
                                {"name": "API_KEY", "class": "secret",
                                 "verdict": "same_as_local"}]},
        {"app": "b", "compared_with": ["/x/b"], "error": None, "retired_in_use": [],
         "counts": {}, "vars": [{"name": "TOKEN", "class": "secret",
                                 "verdict": "same_as_local"},
                                # a CONFIG value equal on both sides is normal
                                {"name": "APP_ENV", "class": "config",
                                 "verdict": "same_as_local"}]}]}
    rows = rf.findings(shared)
    same = [f for f in rows if f["type"] == "remote.same_as_local"]
    check("a secret equal on both sides raises exactly one aggregate row",
          len(same) == 1 and same[0]["severity"] == "warning", str(rows))
    check("which counts the secrets and not the configuration",
          "3 production secret" in same[0]["title"], same[0]["title"])
    check("and names the worst application first",
          same[0]["detail"].index("a (2)") < same[0]["detail"].index("b (1)"),
          same[0]["detail"][:160])
    unread = {"apps": [{"app": "c", "compared_with": [], "error": "403", "vars": [],
                        "counts": {}, "retired_in_use": []}]}
    rows = rf.findings(unread)
    check("an application that would not answer is info, and says unknown is not empty",
          len(rows) == 1 and rows[0]["severity"] == "info"
          and "not the same as" in rows[0]["detail"], str(rows))


def test_a_secret_that_exists_only_at_the_provider_is_counted_not_copied() -> None:
    """Production configuration is not backed up by copying its values here —
    holding them is forbidden. So the backup is measured instead: a
    secret-class variable nothing on this machine fingerprints to is one a
    lost application loses — a count with the worst application first, and
    the vault, by name, as the remedy."""
    rf = load("tools/remote_findings.py", "remote_findings")
    doc = {"apps": [
        {"app": "a", "compared_with": ["/x/a"], "error": None, "retired_in_use": [], "counts": {},
         "vars": [{"name": "DB", "class": "secret", "verdict": "remote_only"},
                  {"name": "KEY", "class": "secret", "verdict": "remote_only"},
                  {"name": "REGION", "class": "config", "verdict": "remote_only"},                          
                  {"name": "SHARED", "class": "secret", "verdict": "same_as_local"}]},                            
        {"app": "b", "compared_with": [], "error": None, "retired_in_use": [], "counts": {},
         "vars": [{"name": "TOKEN", "class": "secret", "verdict": "no_local_checkout"}]},
        {"app": "c", "compared_with": [], "error": "403", "retired_in_use": [], "counts": {},
         "vars": [{"name": "X", "class": "secret", "verdict": "remote_only"}]},                                   
    ]}
    rows = [f for f in rf.findings(doc) if f["type"] == "remote.unbacked"]
    check("one aggregate row, info", len(rows) == 1 and rows[0]["severity"] == "info", str(rows))
    check("it counts secrets with no copy here — and not config, not twins, not unreadable apps",
          rows[0]["title"].startswith("3 production secrets exist only at the provider (applications: 2)"), rows[0]["title"])
    check("worst application first", rows[0]["detail"].index("a (2)") < rows[0]["detail"].index("b (1)"), rows[0]["detail"][:120])
    check("the remedy is the vault, by name, on stdin", 'vault.py" put' in rows[0]["action"] and "config:get" in rows[0]["action"])
    check("no value and no fingerprint in the row", "sk-" not in json.dumps(rows) and "fingerprint" not in rows[0]["title"])


def test_the_document_carries_no_fingerprint_and_no_value() -> None:
    rr = load("collectors/remote_registry.py", "remote_registry")
    # ONE salt on both sides, or the comparison is refused before it is made
    # and this test would be measuring the refusal instead.
    ns = "fp1:feedfacefeedface"
    scan = {"scanned_at": "2026-09-14T00:00:00Z", "provider": "heroku", "retired": [],
            "fingerprint_namespace": ns,
            "apps": [{"app": "demo", "folders": ["/x/demo"], "error": None, "vars": [
                {"name": "TOKEN", "class": "secret", "fingerprint": "deadbeefdeadbeef"}]}]}
    env_scan = {"fingerprint_namespace": ns,
                "files": [{"kind": "env", "project": "demo", "path": "demo/.env",
                           "variables": [{"name": "TOKEN", "class": "secret",
                                          "fingerprint": "deadbeefdeadbeef"}]}]}
    doc = rr.document(scan, env_scan, "2026-09-14")
    blob = json.dumps(doc)

    def keys(node) -> set:
        """Every key name in the document, at any depth. The prose explains that
        the comparison is made FROM fingerprints, which is why this walks the
        structure instead of searching the text for the word."""
        if isinstance(node, dict):
            return set(node) | {k for v in node.values() for k in keys(v)}
        if isinstance(node, list):
            return {k for v in node for k in keys(v)}
        return set()

    check("no fingerprint reaches the registry document",
          "deadbeefdeadbeef" not in blob and "fingerprint" not in keys(doc),
          "registry/ is in git, and a salted fingerprint is a lookup once the salt leaks")
    check("and neither does the NAME of the salt, only what it permitted",
          ns not in blob and "feedfacefeedface" not in blob
          and doc["fingerprint_namespace"] == {"state": "matched", "withheld": 0},
          "the name is derived from the salt; the document carries the verdict instead")
    check("the verdict does", '"same_as_local"' in blob)
    check("the totals count every verdict", doc["totals"]["same_as_local"] == 1
          and doc["totals"]["apps"] == 1, str(doc["totals"]))


def test_the_scan_is_gated_to_once_a_day() -> None:
    """Every run pulls every production secret into one process."""
    state, ns = _minted_state()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-remote-"))
    out = d / "remote-env.json"
    out.write_text(json.dumps({"scanned_at": __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Stamped with the salt this run will read: the age gate is what is
        # under test, and a scan from another salt is refused for a different
        # reason entirely.
        "fingerprint_namespace": ns,
        "apps": [], "retired": [], "degraded": []}), encoding="utf-8")
    before = out.read_text(encoding="utf-8")
    p = subprocess.run([PY, str(ROOT / "collectors/scan_remote_env.py"), str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300,
                       env={**os.environ, "OBSERVATORY_STATE": str(state)})
    check("a fresh scan is not re-fetched", p.returncode == 0 and "not re-fetched" in p.stdout,
          (p.stdout + p.stderr)[-200:])
    check("and the file is left exactly as it was", out.read_text(encoding="utf-8") == before)
    src = (ROOT / "collectors/scan_remote_env.py").read_text(encoding="utf-8")
    check("`--force` exists for the moment after a rotation", "--force" in src)
    check("and the reason for the gate is written down, not just the number",
          "every production secret" in src, "a constant with no reason is a constant nobody keeps")


def test_a_retired_archive_is_read_from_the_vault_shape() -> None:
    sre = load("collectors/scan_remote_env.py", "scan_remote_env")
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-vault-"))
    slot = d / "demo" / "prod"
    slot.mkdir(parents=True)
    # The stamp `tools/vault.py` actually writes: its own `now()` with the
    # colons removed, so the date keeps its hyphens.
    (slot / "API_TOKEN.retired-2026-09-01T100000Z").write_text(_k("old") + "\n", encoding="utf-8")
    (slot / "API_TOKEN").write_text(_k("new") + "\n", encoding="utf-8")
    (slot / "API_TOKEN.meta.json").write_text("{}", encoding="utf-8")
    rows = sre.retired_values(_k("pepper"), d)
    check("only the archive is read, and it is read as a slot",
          len(rows) == 1 and rows[0]["project"] == "demo" and rows[0]["env"] == "prod"
          and rows[0]["name"] == "API_TOKEN" and rows[0]["retired_on"] == "2026-09-01",
          str(rows))
    check("its fingerprint is the archive's value, not the live one",
          rows[0]["fingerprint"] == sre.fingerprint(_k("pepper"), _k("old")), str(rows))
    check("and no value is carried", _k("old") not in json.dumps(rows))




def test_a_scan_names_the_salt_its_fingerprints_belong_to() -> None:
    """The name is derived from the salt and does not lead back to it."""
    env = load("collectors/scan_env.py", "scan_env")
    one, two = _k("salt-one"), _k("salt-two")
    a, b = env.namespace(one), env.namespace(two)
    check("the same salt always names itself the same way", env.namespace(one) == a, a)
    check("two salts get two names", a != b, f"{a} / {b}")
    check("the name carries the derivation's version, so a changed hash is a changed name",
          a.startswith(env.FINGERPRINT_VERSION + ":"), a)
    check("and the salt is not in its own name", one not in a and two not in b,
          "the name travels beside fingerprints; the salt must not travel at all")
    import hashlib
    check("a fingerprint is not recoverable from the name",
          hashlib.sha256((one + _k("v")).encode("utf-8")).hexdigest()[:16] not in a,
          "the two derivations must not share an output")


def _sides(remote_ns, local_ns):
    """One application holding one secret the checkout also holds, equal."""
    scan = {"scanned_at": "2026-09-18T00:00:00Z", "provider": "heroku", "retired": [],
            "apps": [{"app": "demo", "folders": ["/x/demo"], "error": None, "vars": [
                {"name": "TOKEN", "class": "secret", "fingerprint": "aaaa"},
                {"name": "ONLY_THERE", "class": "secret", "fingerprint": "cccc"},
                {"name": "PLAIN", "class": "config"}]}]}
    env_scan = {"files": [{"kind": "env", "project": "demo", "path": "demo/.env",
                           "variables": [
                               {"name": "TOKEN", "class": "secret", "fingerprint": "aaaa"},
                               {"name": "PLAIN", "class": "config"}]}]}
    if remote_ns:
        scan["fingerprint_namespace"] = remote_ns
    if local_ns:
        env_scan["fingerprint_namespace"] = local_ns
    return scan, env_scan


def test_two_salts_never_produce_a_measured_verdict() -> None:
    """A new salt beside an old cache.

    `differs` is the ORDINARY verdict here, so an estate whose salt changed and
    an estate in perfect health would render identically. Both must stop being
    said: the comparison is withheld and the reason is named.
    """
    rr = load("collectors/remote_registry.py", "remote_registry")
    for label, remote_ns, local_ns, state in (
            ("two different salts", "fp1:aaaaaaaaaaaaaaaa", "fp1:bbbbbbbbbbbbbbbb", "mismatched"),
            ("a scan from before the namespace existed", None, "fp1:bbbbbbbbbbbbbbbb", "absent"),
            ("a local inventory from before it existed", "fp1:aaaaaaaaaaaaaaaa", None, "absent"),
            ("neither side naming its salt", None, None, "absent")):
        doc = rr.document(*_sides(remote_ns, local_ns), "2026-09-18")
        got = {v["name"]: v for v in doc["apps"][0]["vars"]}
        check(f"{label}: an equal fingerprint is not called the same",
              got["TOKEN"]["verdict"] == "not_compared", str(got["TOKEN"]))
        check(f"{label}: and it is not called differing either",
              doc["totals"]["differs"] == 0 and doc["totals"]["same_as_local"] == 0,
              str(doc["totals"]))
        check(f"{label}: the row says WHY it was not compared",
              got["TOKEN"].get("why") == rr.NAMESPACE_WHY, str(got["TOKEN"]))
        check(f"{label}: the document names the state and counts what it withheld",
              doc["fingerprint_namespace"]["state"] == state
              and doc["fingerprint_namespace"]["withheld"] == 1
              and doc["totals"]["withheld_for_namespace"] == 1,
              str(doc["fingerprint_namespace"]))
        check(f"{label}: and hands over the command that restores the comparison",
              "--force" in doc["fingerprint_namespace"]["action"],
              str(doc["fingerprint_namespace"]))
        check(f"{label}: a name only production has is still measured",
              got["ONLY_THERE"]["verdict"] == "remote_only",
              "presence of a NAME is decided without any fingerprint, so no salt touches it")
        rows = [d for d in doc["degraded"] if d["source"] == "fingerprint namespace"]
        check(f"{label}: the refusal is a degradation with its reason and effect",
              len(rows) == 1 and rows[0]["reason"] and "--force" in rows[0]["effect"],
              str(doc["degraded"]))


def test_one_salt_compares_exactly_as_before() -> None:
    rr = load("collectors/remote_registry.py", "remote_registry")
    ns = "fp1:aaaaaaaaaaaaaaaa"
    doc = rr.document(*_sides(ns, ns), "2026-09-18")
    got = {v["name"]: v for v in doc["apps"][0]["vars"]}
    check("one salt, and the equal value reads as the same again",
          got["TOKEN"]["verdict"] == "same_as_local", str(got["TOKEN"]))
    check("no row claims a reason it does not have",
          "why" not in got["TOKEN"] and "why" not in got["ONLY_THERE"], str(got))
    check("the document says the namespaces matched and withheld nothing",
          doc["fingerprint_namespace"] == {"state": "matched", "withheld": 0},
          str(doc["fingerprint_namespace"]))
    check("and no namespace degradation is invented",
          [d for d in doc["degraded"] if d["source"] == "fingerprint namespace"] == [],
          str(doc["degraded"]))


def test_nothing_to_compare_is_not_a_namespace_complaint() -> None:
    """A fresh clone has no local fingerprints at all — and no loss to report."""
    rr = load("collectors/remote_registry.py", "remote_registry")
    scan, _ = _sides(None, None)
    doc = rr.document(scan, {"files": [], "degraded": []}, "2026-09-18")
    check("no comparison was possible, so none was withheld",
          doc["fingerprint_namespace"]["withheld"] == 0, str(doc["fingerprint_namespace"]))
    check("and the operator is not asked to refresh a comparison they never had",
          [d for d in doc["degraded"] if d["source"] == "fingerprint namespace"] == []
          and "action" not in doc["fingerprint_namespace"],
          "a row that cries wolf is how the row that matters gets skipped")


def test_a_retired_value_is_still_found_across_a_namespace_refusal() -> None:
    """The archive and production are fingerprinted in ONE process, by one
    pepper, into one file. The local inventory's salt cannot make them
    incomparable, and withholding the estate's only critical here would be a
    refusal with no measurement behind it."""
    rr = load("collectors/remote_registry.py", "remote_registry")
    scan = {"scanned_at": "2026-09-18T00:00:00Z", "provider": "heroku",
            "fingerprint_namespace": "fp1:aaaaaaaaaaaaaaaa",
            "retired": [{"project": "demo", "env": "prod", "name": "API_TOKEN",
                         "retired_on": "2026-09-01", "fingerprint": "old1"}],
            "apps": [{"app": "demo", "folders": ["/x/demo"], "error": None, "vars": [
                {"name": "API_TOKEN", "class": "secret", "fingerprint": "old1"}]}]}
    env_scan = {"files": [{"kind": "env", "project": "demo", "path": "demo/.env",
                           "variables": [{"name": "API_TOKEN", "class": "secret",
                                          "fingerprint": "zzzz"}]}],
                "fingerprint_namespace": "fp1:bbbbbbbbbbbbbbbb"}
    doc = rr.document(scan, env_scan, "2026-09-18")
    check("the local comparison is refused",
          doc["fingerprint_namespace"]["state"] == "mismatched"
          and doc["totals"]["withheld_for_namespace"] == 1, str(doc["totals"]))
    check("and production is still reported as running the retired value",
          doc["totals"]["retired_still_deployed"] == 1
          and doc["apps"][0]["retired_in_use"][0]["slot"] == "demo/prod/API_TOKEN",
          str(doc["apps"][0]["retired_in_use"]))


def test_the_refusal_reaches_the_board_with_its_remedy() -> None:
    rr = load("collectors/remote_registry.py", "remote_registry")
    rf = load("tools/remote_findings.py", "remote_findings")
    doc = rr.document(*_sides("fp1:aaaaaaaaaaaaaaaa", "fp1:bbbbbbbbbbbbbbbb"), "2026-09-18")
    rows = [f for f in rf.findings(doc) if f["type"] == "remote.namespace_withheld"]
    check("one row, a warning: a question the board answered has stopped being answerable",
          len(rows) == 1 and rows[0]["severity"] == "warning", str(rows))
    check("it counts what was withheld", rows[0]["title"].startswith("1 production secret"),
          rows[0]["title"])
    check("it says production did not change", "Nothing about production changed" in rows[0]["detail"],
          rows[0]["detail"][:120])
    check("and its remedy is the re-read, not a guess", "--force" in rows[0]["action"],
          rows[0]["action"])
    check("a matched pair raises nothing",
          [f for f in rf.findings(rr.document(*_sides("fp1:cccccccccccccccc",
                                                      "fp1:cccccccccccccccc"), "2026-09-18"))
           if f["type"] == "remote.namespace_withheld"] == [])
    check("and no fingerprint or namespace value reaches the row",
          "aaaaaaaaaaaaaaaa" not in json.dumps(rows) and "bbbbbbbbbbbbbbbb" not in json.dumps(rows),
          "the name is derived from the salt, and findings are rendered in a page")


def test_a_cached_scan_from_another_salt_is_not_served_as_current() -> None:
    """The gate used to answer `recent enough` without ever reading the salt."""
    import datetime as _dt
    fresh = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state, here = _minted_state()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-remote-ns-"))
    for label, stamped, served in (("the salt it was taken under", here, True),
                                   ("a different salt", "fp1:0000000000000000", False),
                                   ("no namespace at all", None, False)):
        out = d / f"remote-env-{'none' if stamped is None else stamped[-4:]}.json"
        doc = {"scanned_at": fresh, "apps": [], "retired": [], "degraded": []}
        if stamped:
            doc["fingerprint_namespace"] = stamped
        out.write_text(json.dumps(doc), encoding="utf-8")
        before = out.read_text(encoding="utf-8")
        p = subprocess.run([PY, str(ROOT / "collectors/scan_remote_env.py"), str(out)],
                           cwd=ROOT, capture_output=True, text=True, timeout=300,
                           env={**os.environ, "OBSERVATORY_STATE": str(state)})
        said = (p.stdout + p.stderr)
        check(f"a cached scan under {label}: the tick is not failed", p.returncode == 0,
              said[-200:])
        check(f"a cached scan under {label}: it is left byte for byte",
              out.read_text(encoding="utf-8") == before,
              "refusing to compare must never cost the operator the scan itself")
        if served:
            check("the matching one is served as it always was",
                  "not re-fetched" in said, said[-200:])
        else:
            check(f"the one under {label} is not offered as an answer",
                  "not re-fetched" not in said, said[-200:])
            check(f"and the refusal names the way back for {label}",
                  "--force" in said, said[-200:])



def test_a_failed_heroku_scan_is_not_an_empty_production() -> None:
    """Audit A26, seen live: the Heroku scan timed out, `heroku.json` held no
    application, and the ENV page's "Prod" column was "—" on every row with nothing
    saying why. The comparison now carries the scan's reason as `degraded`."""
    m = load("collectors/scan_remote_env.py", "scan_remote_env_a26")
    import paths
    raw = pathlib.Path(paths.SCRATCH) / "heroku.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    before = raw.read_text(encoding="utf-8") if raw.exists() else None
    raw.write_text(json.dumps({"apps": [], "degraded": [
        {"source": "heroku", "reason": "heroku auth:token did not finish in 30s"}]}), encoding="utf-8")

    class FakeHeroku:
        API = "https://api.heroku.invalid"

        @staticmethod
        def token():
            return "synthetic", None

        @staticmethod
        def get(url, tok):
            raise AssertionError("no application to ask about")
    real = m._load
    m._load = lambda rel, name: FakeHeroku if "scan_heroku" in rel else real(rel, name)
    try:
        rows, degraded = m.scan_heroku_apps("pepper")
    finally:
        m._load = real
        if before is None:
            raw.unlink()
        else:
            raw.write_text(before, encoding="utf-8")
    check("no application row is invented", rows == [], str(rows))
    check("the Heroku scan's own reason is carried",
          degraded and "did not finish in 30s" in degraded[0]["reason"], str(degraded))
    check("and it says unknown, not empty", degraded and "unknown, not empty" in degraded[0]["effect"],
          str(degraded))


if __name__ == "__main__":
    enable_remote_env_in_sandbox()
    print("what production holds — the comparison, and what never leaves it\n")
    for fn in (test_the_two_inventories_fingerprint_the_same_way,
               test_the_verdicts_are_the_four_the_scenario_asks_for,
               test_a_retired_value_still_deployed_is_found_and_is_critical,
               test_differing_is_not_a_finding_and_sharing_is_one,
               test_a_secret_that_exists_only_at_the_provider_is_counted_not_copied,
               test_the_document_carries_no_fingerprint_and_no_value,
               test_the_scan_is_gated_to_once_a_day,
               test_a_retired_archive_is_read_from_the_vault_shape,
               test_a_scan_names_the_salt_its_fingerprints_belong_to,
               test_two_salts_never_produce_a_measured_verdict,
               test_one_salt_compares_exactly_as_before,
               test_nothing_to_compare_is_not_a_namespace_complaint,
               test_a_retired_value_is_still_found_across_a_namespace_refusal,
               test_the_refusal_reaches_the_board_with_its_remedy,
               test_a_cached_scan_from_another_salt_is_not_served_as_current,
               test_a_failed_heroku_scan_is_not_an_empty_production):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mproduction's configuration is a verdict here, never a value\033[0m")
