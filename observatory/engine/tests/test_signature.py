#!/usr/bin/env python3
"""Signing a credential — the half of credential hygiene no scan can measure.

THE FILE IS IN GIT, which decides most of what is asserted here. A purpose is a
sentence an operator types, and the one sentence that must never be typed into
it is a credential: an edit does not remove it from the history afterwards. So
the writer refuses a value-shaped purpose, and it refuses it in the ONE place
every path goes through — the CLI, the keyserver route and the page's button all
call `write()`, or a refusal would be true at a terminal and false in a browser.

`credential.rotation_due` is asserted for what it does NOT do as much as for
what it does: a global age policy would have put twenty-one rows on the board
the day it was written, so the policy is per credential and opt-in, and the
rule is silent for every credential nobody set one on.

Values here are composed rather than written, for the reason the writer exists.
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
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _k(tag: str) -> str:
    """A value-SHAPED string that is not a value: forty-plus characters with no
    spaces, which is exactly what the writer is built to refuse."""
    return ("x" + tag) * 16


def sandbox() -> tuple[dict, pathlib.Path]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sign-"))
    reg = d / "registry"
    reg.mkdir()
    (reg / "credentials.json").write_text(json.dumps({
        "credentials": [
            {"id": "credential:openrouter/demo", "kind": "llm-api-key",
             "name": "demo", "created_on": "2026-01-01"},
            {"id": "credential:vault/demo/prod/API_TOKEN", "kind": "project-secret",
             "name": "API_TOKEN", "rotated_on": "2026-08-01"},
        ]}), encoding="utf-8")
    ann = d / "annotations.json"
    ann.write_text(json.dumps({"schema_version": 1, "note": "fixture",
                               "annotations": {}}), encoding="utf-8")
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(reg),
               OBSERVATORY_ANNOTATIONS=str(ann))
    return env, ann


def sign(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "tools/sign_credential.py"), *args],
                          env=env, capture_output=True, text=True, timeout=120)


def test_the_writer_refuses_what_would_make_the_file_worse() -> None:
    env, ann = sandbox()
    cid = "credential:openrouter/demo"
    p = sign(env, "set", cid, "--evidence", "a note")
    check("no purpose is refused", p.returncode == 2 and "purpose is required" in p.stderr,
          p.stderr[-120:])
    p = sign(env, "set", cid, "--purpose", "for the thing")
    check("no evidence is refused", p.returncode == 2 and "evidence is required" in p.stderr,
          p.stderr[-120:])
    p = sign(env, "set", cid, "--purpose", f"the key is {_k('ab')}", "--evidence", "a note")
    check("a value-shaped PURPOSE is refused, and the refusal says why the file matters",
          p.returncode == 2 and "in git" in p.stderr, p.stderr[-160:])
    p = sign(env, "set", cid, "--purpose", "for the thing", "--evidence", f"see {_k('cd')}")
    check("a value-shaped EVIDENCE is refused too", p.returncode == 2 and "value" in p.stderr,
          p.stderr[-120:])
    p = sign(env, "set", "credential:openrouter/typo", "--purpose", "x", "--evidence", "y")
    check("an id the board does not carry is refused, with the near ones named",
          p.returncode == 2 and "carries no credential" in p.stderr, p.stderr[-160:])
    p = sign(env, "set", cid, "--purpose", "x", "--evidence", "y", "--rotation-days", "0")
    check("a rotation policy of zero is refused",
          p.returncode == 2 and "at least 1" in p.stderr, p.stderr[-120:])
    check("and none of that wrote a row",
          json.loads(ann.read_text(encoding="utf-8"))["annotations"] == {})


def test_every_free_text_field_and_the_id_refuse_a_value() -> None:
    """Only purpose and evidence were checked, and only for `sk-…` or 40+ runs:
    a token as `owner` landed in the annotation file and on two pages, a
    38-character token passed as a purpose, and UUID or 32-hex keys passed
    everything. One shared heuristic now checks every field the writer stores."""
    env, ann = sandbox()
    cid = "credential:openrouter/demo"
    mixed = "Fake" + "_" + "Ab3" * 11                # 38 chars, three classes
    uuid = "-".join(("0f" * 4, "1a2b", "3c4d", "5e6f", "7a" * 6))
    hex32 = "0123456789abcdef" * 2
    attempts = (("owner", mixed, ("--purpose", "p", "--evidence", "e", "--owner", mixed)),
                ("tag", hex32, ("--purpose", "p", "--evidence", "e", "--tag", hex32)),
                ("38-char purpose", mixed, ("--purpose", f"key {mixed}", "--evidence", "e")),
                ("uuid evidence", uuid, ("--purpose", "p", "--evidence", f"see {uuid}")))
    for label, bad, args in attempts:
        p = sign(env, "set", cid, *args)
        check(f"a value-shaped {label} is refused without being echoed",
              p.returncode == 2 and bad not in p.stderr + p.stdout, (p.stdout + p.stderr)[-200:])
    empty_env = dict(env, OBSERVATORY_REGISTRY=str(pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sign-empty-"))))
    p = sign(empty_env, "set", mixed, "--purpose", "p", "--evidence", "e")
    check("a value-shaped id is refused even when no board can be read to compare it",
          p.returncode == 2 and mixed not in p.stderr + p.stdout, (p.stdout + p.stderr)[-200:])
    check("and none of that wrote a row",
          json.loads(ann.read_text(encoding="utf-8"))["annotations"] == {})
    p = sign(env, "set", cid, "--purpose", "DNS edits for alpha-web", "--evidence",
             "issued 2026-10-03 to project:local-alpha-web", "--owner", "ops-team", "--tag", "dns")
    check("ordinary words, dates and ids still sign", p.returncode == 0, p.stderr[-200:])


def test_a_signature_lands_and_a_re_signing_keeps_the_doors_half() -> None:
    env, ann = sandbox()
    cid = "credential:openrouter/demo"
    p = sign(env, "set", cid, "--purpose", "what the tick pays a model with",
             "--evidence", "issued by the door to the observatory destination",
             "--owner", "operator", "--rotation-days", "180", "--tag", "tick")
    check("the signature is recorded", p.returncode == 0 and "signed:" in p.stdout,
          (p.stdout + p.stderr)[-200:])
    rows = json.loads(ann.read_text(encoding="utf-8"))["annotations"]
    row = rows.get(cid, {})
    check("with purpose, evidence, owner, policy, tags and the date",
          row.get("owner") == "operator" and row.get("rotation_days") == 180
          and row.get("tags") == ["tick"] and len(row.get("signed_on", "")) == 10,
          str(row))
    # the door's half, as `openrouter.py issue` writes it
    rows[cid]["auto"] = {"provider": "openrouter", "ceiling": 400}
    doc = json.loads(ann.read_text(encoding="utf-8"))
    doc["annotations"] = rows
    ann.write_text(json.dumps(doc), encoding="utf-8")
    p = sign(env, "set", cid, "--purpose", "a better sentence", "--evidence", "the same note")
    after = json.loads(ann.read_text(encoding="utf-8"))["annotations"][cid]
    check("re-signing replaces the human half and keeps the door's",
          after["purpose"] == "a better sentence"
          and after.get("auto") == {"provider": "openrouter", "ceiling": 400},
          str(after))
    check("and keeps the policy it was given", after.get("rotation_days") == 180, str(after))
    p = sign(env, "show")
    check("`show` names it and counts what is still unsigned",
          cid in p.stdout and "1 signed, 1 not" in p.stdout, p.stdout[-160:])


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_board_reports_the_unsigned_and_only_a_chosen_policy() -> None:
    cf = load("tools/credential_findings.py", "credential_findings")
    creds = [
        {"id": "credential:openrouter/a", "kind": "llm-api-key", "name": "a",
         "created_on": "2026-01-01"},
        {"id": "credential:openrouter/b", "kind": "llm-api-key", "name": "b",
         "created_on": "2026-01-01",
         "signature": {"purpose": "signed", "evidence": "e"}},
    ]
    rows = cf.unsigned(creds)
    check("one aggregate row, not one per credential", len(rows) == 1, str(rows))
    check("of type `credential.unsigned`",
          rows[0]["type"] == "credential.unsigned", rows[0]["type"])
    check("which counts only the unsigned and names them",
          "1 credential carries" in rows[0]["title"] and "openrouter/a" in rows[0]["detail"],
          rows[0]["title"])
    check("and the remedy is the verb, not a file to edit",
          'full-path)/tools/sign_credential.py" set' in rows[0]["action"], rows[0]["action"])
    check("everything signed raises nothing", cf.unsigned([creds[1]]) == [])

    due = cf.rotation_due(creds, "2026-09-14")
    check("a credential with no policy is never due", due == [],
          "a global age policy would put every key on the board the day it was written")
    creds[0]["signature"] = {"purpose": "p", "evidence": "e", "rotation_days": 30}
    due = cf.rotation_due(creds, "2026-09-14")
    check("one past its own policy is a warning", len(due) == 1
          and due[0]["severity"] == "warning", str(due))
    check("of type `credential.rotation_due`",
          due[0]["type"] == "credential.rotation_due", due[0]["type"])
    check("and the row names the age and the policy",
          "256 days ago" in due[0]["title"] and "every 30" in due[0]["title"],
          due[0]["title"])
    check("it is not due the day before", cf.rotation_due(creds, "2026-01-30") == [],
          "thirty days after 2026-01-01 is not past thirty days")
    check("and the age is measured against the DOCUMENT's date, never a clock",
          "scanned_on" in (ROOT / "tools/credential_findings.py").read_text(encoding="utf-8"),
          "a rule keeps no clock of its own inside a finding, or the board would differ every tick")


def test_one_writer_serves_the_page_and_the_terminal() -> None:
    ks = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    check("the keyserver has an annotate route", '"annotate": act_annotate' in ks)
    body = ks.split("def act_annotate(", 1)[1].split("\ndef ", 1)[0]
    check("which calls the CLI's own writer rather than assembling JSON",
          "sign_credential.write(" in body,
          "two writers is two sets of refusals, and one of them will be weaker")
    # THE DOCSTRING IS NOT CODE, and it names the writer — comparing raw offsets
    # put the "call" four hundred characters before the audit line and reported
    # an ordering defect in a function that has none.
    code = body.split('"""', 2)[-1] if body.count('"""') >= 2 else body
    check("and audits before it writes",
          code.index("audit(") < code.index("sign_credential.write("),
          "a log written after the call loses the one case that matters")
    # The page's text is English message ids translated from a catalog, so the
    # verb and the "unsigned" wording are asserted as ids and as translations.
    page = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    ru = json.loads((ROOT / "dashboard/locales/ru.json").read_text(encoding="utf-8"))
    check("the page offers the verb on every credential row",
          'T("sign…")' in page and 'act === "annotate"' in page, "")
    check("and shows the signature, or says plainly that there is none",
          'T("not signed — nobody said what it is for")' in page,
          "an empty space reads as «no purpose needed»")
    check("and both strings are translated",
          bool(ru.get("sign…")) and bool(ru.get("not signed — nobody said what it is for")))


if __name__ == "__main__":
    print("signing a credential — one writer, and a file that stays clean\n")
    for fn in (test_the_writer_refuses_what_would_make_the_file_worse,
               test_every_free_text_field_and_the_id_refuse_a_value,
               test_a_signature_lands_and_a_re_signing_keeps_the_doors_half,
               test_the_board_reports_the_unsigned_and_only_a_chosen_policy,
               test_one_writer_serves_the_page_and_the_terminal):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma credential with no purpose is now a row, and a purpose with no source is refused\033[0m")
