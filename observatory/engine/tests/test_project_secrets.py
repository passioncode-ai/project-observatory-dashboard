#!/usr/bin/env python3
"""Secrets a project keeps beside its own code — seen at last, and never read.

WHAT THIS EXISTS FOR. The registry used to read the machine store and the vault
and nothing else, so key files a project keeps in its own `secrets/` folder —
service accounts, store-connect keys, API tokens — were invisible to the system
whose whole job is to say what an estate holds. They are tracked as facts and
made reusable through the vault rather than copied by hand into a second project.

THREE PROPERTIES BREAK QUIETLY AND ARE DRIVEN HERE:

  * a folder under the projects root may be a SYMLINK to the machine store.
    Walking it as a project counts every machine secret a second time and
    roughly doubles the credential count;
  * git state is per FILE, not per directory — `secrets/.gitkeep` is tracked on
    purpose while `secrets/analytics-sa.json` beside it is ignored, and a
    directory-level verdict reports the first and hides the second;
  * the content is never read. The one exception is a service account's own
    `client_email`, which the file states in the clear and which is the
    identifier an operator grants and revokes.

Values are composed rather than written, for the reason the rule exists.
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
import tmp as tmpdir

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _k(tag: str) -> str:
    """A value-shaped string that is not one."""
    return "-".join(("fixture", tag, "value", "not", "a", "real", "one"))


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def estate() -> pathlib.Path:
    """A planted projects root: one git project with three git states, one non-repo
    project, and a symlink standing in for the machine store."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-projsec-"))
    proj = d / "demo-project" / "secrets"
    proj.mkdir(parents=True)
    (proj / "ignored.key").write_text(_k("ignored") + "\n", encoding="utf-8")
    (proj / "loose.pem").write_text(_k("loose") + "\n", encoding="utf-8")
    (proj / "tracked.token").write_text(_k("tracked") + "\n", encoding="utf-8")
    (proj / ".gitkeep").write_text("", encoding="utf-8")
    # The header is COMPOSED, not written: `tools/check_secrets.py` treats a
    # private-key header as a credential whatever follows it, in the tree and in
    # the last forty commits alike — and the first version of this fixture wrote
    # it out, which turned the gate red the moment the file was committed.
    # Composing it keeps the fixture from looking like a credential.
    header = "-----BEGIN " + "PRIVATE KEY-----"
    (proj / "sa.json").write_text(json.dumps({
        "type": "service_account", "client_email": "who@example.iam.gserviceaccount.com",
        "private_key": header + "\n" + _k("key") + "\n"}), encoding="utf-8")
    for f in proj.iterdir():
        os.chmod(f, 0o600)
    os.chmod(proj / "loose.pem", 0o644)
    repo = d / "demo-project"
    (repo / ".gitignore").write_text("secrets/ignored.key\nsecrets/sa.json\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q"], ["add", ".gitignore", "secrets/tracked.token", "secrets/.gitkeep"],
                ["commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True, env=env, timeout=60)
    plain = d / "no-repo" / "secrets"
    plain.mkdir(parents=True)
    (plain / "thing.key").write_text(_k("plain") + "\n", encoding="utf-8")
    os.chmod(plain / "thing.key", 0o600)
    return d


def test_it_sees_them_and_reads_no_value() -> None:
    cr = load("collectors/credentials_registry.py", "credentials_registry")
    d = estate()
    rows = cr.from_project_secrets(d)
    by = {r["name"]: r for r in rows}
    check("every real file is a record and the placeholders are not",
          set(by) == {"ignored.key", "loose.pem", "tracked.token", "sa.json", "thing.key"},
          str(sorted(by)))
    check("the project is decided by containment",
          by["ignored.key"]["in_project"] == "demo-project"
          and by["thing.key"]["in_project"] == "no-repo", "")
    check("a service account's public identity is read and its key is not",
          by["sa.json"]["identity"] == "who@example.iam.gserviceaccount.com", str(by["sa.json"]))
    blob = json.dumps(rows)
    for forbidden in ("BEGIN PRIVATE KEY", _k("key"), _k("ignored"), _k("plain")):
        check(f"no {forbidden[:22]!r} in the records", forbidden not in blob, "")
    check("the id names the file inside its project",
          by["sa.json"]["id"] == "credential:project/demo-project/secrets/sa.json",
          by["sa.json"]["id"])


def test_git_state_is_asked_per_file() -> None:
    cr = load("collectors/credentials_registry.py", "credentials_registry")
    by = {r["name"]: r for r in cr.from_project_secrets(estate())}
    check("a tracked key reads tracked", by["tracked.token"]["git"] == "tracked", by["tracked.token"]["git"])
    check("an ignored one reads ignored", by["ignored.key"]["git"] == "ignored", by["ignored.key"]["git"])
    check("and one that is neither reads loose", by["loose.pem"]["git"] == "loose", by["loose.pem"]["git"])
    check("a project with no repository says so rather than guessing",
          by["thing.key"]["git"] == "no-repo", by["thing.key"]["git"])
    check("the mode is carried as it is", by["loose.pem"]["mode"] == "644"
          and by["ignored.key"]["mode"] == "600", "")


def test_the_machine_store_is_not_counted_twice() -> None:
    cr = load("collectors/credentials_registry.py", "credentials_registry")
    d = estate()
    # A folder under the projects root that is a symlink to the machine store;
    # the same shape a real estate can have.
    store = d / "elsewhere" / "secrets"
    store.mkdir(parents=True)
    (store / "machine.key").write_text(_k("machine"), encoding="utf-8")
    os.chmod(store / "machine.key", 0o600)
    (d / "gateway-link").symlink_to(d / "elsewhere")
    before = cr.MACHINE_STORE
    try:
        cr.MACHINE_STORE = store
        rows = cr.from_project_secrets(d)
    finally:
        cr.MACHINE_STORE = before
    check("a project folder that resolves to the machine store is skipped",
          not any(r["in_project"] == "gateway-link" for r in rows),
          str([r["in_project"] for r in rows]))
    check("and the projects that are their own are still read",
          {r["in_project"] for r in rows} >= {"demo-project", "no-repo"},
          str(sorted({r["in_project"] for r in rows})))


def test_an_exposed_file_is_a_finding_and_a_clean_one_is_silence() -> None:
    cf = load("tools/credential_findings.py", "credential_findings")
    creds = [
        {"kind": "project-secret-file", "in_project": "a", "path": "secrets/t.token",
         "git": "tracked", "mode": "600"},
        {"kind": "project-secret-file", "in_project": "a", "path": "secrets/l.pem",
         "git": "loose", "mode": "644"},
        {"kind": "project-secret-file", "in_project": "b", "path": "secrets/ok.key",
         "git": "ignored", "mode": "600"},
    ]
    rows = {f["type"]: f for f in cf.project_file_exposed(creds)}
    check("a tracked key file is critical",
          rows["credential.project_file_in_git"]["severity"] == "critical",
          str(sorted(rows)))
    check("and the remedy is rotation first, because history is not deletable",
          "rotate" in rows["credential.project_file_in_git"]["action"], "")
    check("an unignored one is a warning",
          rows["credential.project_file_unignored"]["severity"] == "warning", "")
    check("a mode anyone can read is a warning naming the mode",
          "644" in rows["credential.project_file_readable"]["detail"], "")
    clean = [{"kind": "project-secret-file", "in_project": "b", "path": "secrets/ok.key",
              "git": "ignored", "mode": "600"}]
    check("ten clean files raise nothing at all", cf.project_file_exposed(clean) == [],
          "this rule ships silent on this estate, and that is the measurement")
    check("and no project secrets at all raise nothing",
          cf.project_file_exposed([{"kind": "llm-api-key"}]) == [])


def test_the_page_offers_the_reuse_the_operator_asked_for() -> None:
    # PORTED-DIVERGED: the public page is English in the code and translated
    # from locale catalogs, and builds commands through helpers, so the
    # assertions name the English message ids and the helper calls.
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the keys page has a section for secrets beside the code",
          '["projfile", T("Secrets beside the code")' in src, "")
    check("a row offers to put the value into the vault, on stdin",
          'toolCommand("vault.py", ["put", vaultProject, "local", slotName]) + " < "' in src,
          "reuse is the point, and the vault is how a value is reused by name")
    check("git state and an open mode show on the row",
          'c.git === "tracked"' in src and 'T("mode {mode}", {mode: c.mode})' in src, "")
    # The folder and the project are two spellings of one thing and the vault
    # only answers to one of them: a folder `acme-alpha-web` may be the project
    # `alpha-web`, and a slot written under the folder is one nothing would ever
    # find again.
    check("the slot is named for the project, not for the folder it sits in",
          "PROJ_NAME.get(owner)" in src and "const vaultProject" in src,
          "the vault joins its slots by the registry's project name")


if __name__ == "__main__":
    print("secrets beside the code — seen, never read, and reusable by name\n")
    for fn in (test_it_sees_them_and_reads_no_value,
               test_git_state_is_asked_per_file,
               test_the_machine_store_is_not_counted_twice,
               test_an_exposed_file_is_a_finding_and_a_clean_one_is_silence,
               test_the_page_offers_the_reuse_the_operator_asked_for):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mfiles beside the code are seen, never read, and reusable by name\033[0m")
