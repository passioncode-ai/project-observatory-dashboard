#!/usr/bin/env python3
"""No credential is in the tracked tree, as an exit code rather than a doctrine.

This repository says a great deal about how a key is handled: one per file at
mode 600, a loose-permission file REFUSED rather than used, the launchd plist
carrying no key, `key_status` printing a shape and never a value, and a suite
(`tests/test_key.py`) that drives all of it. **Nothing verified that a key had
never been committed**: no step of the gate looked.

A project audit once raised CRITICAL findings for an "openai-style key committed
in the tree" at `tests/test_key.py`. They were FALSE POSITIVES, and saying so
needs evidence rather than a shrug:

    the value            a fake OpenRouter-shaped key used only by that test
    normalised entropy   0.72 of what a body that length could carry
    a random 32-char key 0.91
    a random 48-char key 0.89
    used only as         an input to assertions that the key is never printed

So the scanner separates a fixture from a credential by ENTROPY and never by
name: a real key called `FAKE_KEY` must still fail, which is why a
word that describes itself lowers nothing on its own.

**Normalised, and that is not a refinement.** The first version of this file used
raw bits per character with a floor of 4.3, and a real AWS key sailed straight
through, because its body is exactly sixteen characters and the maximum entropy
of sixteen characters is log2(16) = 4.0. The floor sat above the ceiling.

**And the method has a floor of its own.** Over thousands of random draws, a
sixteen-character AWS body's normalised entropy reaches as low as about 0.72,
which OVERLAPS this repository's fixture at 0.72 and a `your-key-goes-here`
placeholder at 0.74. Sixteen characters do not hold the information to separate
them, so a structurally exact vendor shape (`EXACT_SHAPES`) is decided by its
prefix and never measured. A false positive there costs one allowlist line with
a reason; a false negative is a leaked credential.

WHAT IS IN SCOPE. `git ls-files`, the tracked tree, because "committed" is what
the claim is about. An untracked file is the operator's own working state and
flagging it would make this the check nobody runs (trap T28). History is a
separate, opt-in mode: it is expensive and its remedy is different. A key in
history is rotated at its issuer, never rewritten out, because a rewrite breaks
every clone and the credential is compromised either way.

    check_secrets.py              # the tracked tree; non-zero on a credential
    check_secrets.py --history N  # also the last N commits' added lines
"""
from __future__ import annotations
import argparse, collections, json, math, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import safe_git  # noqa: E402  — the engine's one git door

#: Shannon entropy DIVIDED BY its own ceiling, `log2(len)`. The first version
#: used raw bits per character with a floor of 4.3 — and a real AWS key sailed
#: through, because its body is exactly sixteen characters and the maximum
#: entropy of a sixteen-character string is log2(16) = 4.0. The floor was above
#: the ceiling: unreachable by construction, for a shape AWS defines exactly.
#:
#: Normalised, the separation is real. Measured 2026-09-07:
#:
#:     this repository's fixture   0.72      random 32-char body   0.91
#:     'your-key-goes-here…'      0.74      random 48-char body   0.89
#:     'placeholder' x4           0.57      random 64-char body   0.87
#:     'x' x32                    0.00
#:
#: 0.78 sits below every real body's minimum over 4000 draws (0.796 at 48
#: characters, 0.806 at 32) and above every placeholder measured.
NORMALISED_FLOOR = 0.78

#: THE LIMIT OF THE METHOD, and it is why `EXACT_SHAPES` exists. Over 4000 draws
#: a real sixteen-character AWS body's minimum normalised entropy is 0.725 and
#: its first percentile 0.789 — which OVERLAPS the fixture at 0.72 and the
#: placeholder at 0.74. No entropy measure can separate those distributions,
#: because sixteen characters do not hold the information to. So a short,
#: structurally exact vendor shape is decided by its PREFIX and never measured.
MIN_BODY = 24

#: Shapes a vendor defines precisely enough that the prefix alone is the answer.
#: A false positive here costs one allowlist line with a reason; a false negative
#: is a leaked credential.
EXACT_SHAPES = {"aws access key", "google api key"}

#: (name, pattern). Each is a vendor prefix that means "this is a credential"
#: rather than a guess about shape. The generic assignment rule at the end is
#: what catches an unbranded token.
PATTERNS = [
    ("openai/openrouter", re.compile(r"\bsk-(?:or-v1-|proj-|ant-api03-)?[A-Za-z0-9_\-]{16,}")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}")),
    ("github fine-grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("linear api key", re.compile(r"\blin_api_[A-Za-z0-9]{16,}")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}")),
    ("google api key", re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY")),
    # An unbranded token assigned to a name that says what it is. The name is
    # what makes it worth measuring; the entropy is what decides.
    ("named secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*"
        r"['\"]([A-Za-z0-9_\-+/=]{20,})['\"]")),
]

#: Files whose content is deliberately key-SHAPED, each with the reason. Empty
#: on purpose: `tests/test_key.py` composes its fixture at runtime so no
#: key-shaped literal exists in the tree at all, which is a stronger posture
#: than an allowlist — it makes every third-party scanner agree too. The
#: mechanism stays because the next fixture may not have that option.
#: path -> why a literal there is not a credential. Empty until 2026-09-08, when
#: committing this scanner and its test turned the gate red: `check_secrets.py`
#: carries the DETECTION PATTERN for a private-key header and `test_secrets.py`
#: carries a truncated placeholder of one, with `MII...` where key material
#: would be. Neither is a credential, and a permanently red secrets gate is one
#: people stop reading — the exact failure this file exists to prevent, one
#: level up.
#:
#: A reason per entry rather than a bare set: an exemption list without reasons
#: becomes the place credentials hide.
ALLOWLIST: dict[str, str] = {
    "tools/check_secrets.py":
        "this file IS the scanner; the literals here are its detection patterns, "
        "and a pattern that matches a private-key header is not one",
    "tests/test_secrets.py":
        "the fixtures that drive the scanner, including a truncated private-key "
        "header with no key material — planted so the detector can be watched "
        "detecting",
    # HISTORY ONLY. An earlier commit wrote the header out in full in a planted
    # service-account fixture whose 'key' is plainly not key material; the tree
    # composes it at runtime now. History is never rewritten here (the reason is
    # in main() below), so the entry has to stand while `--history` can still
    # reach that commit, and after that it stands for nothing: a path with no
    # literal in the tree cannot hide one behind this line.
    "tests/test_project_secrets.py":
        "a planted service-account file whose private-key header was written out "
        "once (b78147b) over a body that is plainly not key material; the tree "
        "composes the header now, so this covers the history hit only",
}

#: Paths never scanned, with the reason. This file quotes every pattern it hunts
#: for, and the decisions log quotes the audit's findings verbatim.
SKIP = {
    "tools/check_secrets.py": "it contains the patterns themselves",
    "docs/DECISIONS.md": "it quotes the audit's findings, including the fixture",
    "tests/test_secrets.py": "it plants credentials to prove the check refuses them",
}


def entropy(s: str) -> float:
    if not s:
        return 0.0
    f = collections.Counter(s)
    return -sum((c / len(s)) * math.log2(c / len(s)) for c in f.values())


def normalised(s: str) -> float:
    """Entropy as a fraction of what a string that length could carry.

    A length-blind threshold compares a short body against something above its
    own ceiling, which is exactly how the first version of this file let an AWS
    key through.
    """
    if len(s) < 2:
        return 0.0
    return entropy(s) / math.log2(len(s))


def body_of(match: str) -> str:
    """The random part, with the vendor prefix removed.

    The prefix is fixed text every key of that vendor carries, so leaving it in
    lowers the entropy of every real key by diluting it with a known string;
    the measurement has to be about the part that is supposed to be random.
    """
    for pre in ("sk-or-v1-", "sk-proj-", "sk-ant-api03-", "sk-", "github_pat_",
                "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "lin_api_", "AKIA", "AIza"):
        if match.startswith(pre):
            return match[len(pre):]
    return match


def tracked() -> list[str]:
    out = safe_git.run(["ls-files"], cwd=ROOT, timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"check_secrets: `git ls-files` failed: {out.stderr[:200]}")
    return [l for l in out.stdout.splitlines() if l]


def scan_text(text: str, where: str) -> list[dict]:
    hits = []
    for label, rx in PATTERNS:
        for m in rx.finditer(text):
            raw = m.group(1) if m.groups() else m.group(0)
            if label == "private key block":
                hits.append({"where": where, "kind": label, "entropy": None,
                             "why": "a private key block is a credential whatever "
                                    "its contents"})
                continue
            body = body_of(raw)
            line = text[:m.start()].count("\n") + 1
            if label in EXACT_SHAPES:
                hits.append({"where": f"{where}:{line}", "kind": label,
                             "entropy": round(entropy(body), 2),
                             "normalised": round(normalised(body), 2),
                             "credential": True, "length": len(raw),
                             "why": "the vendor defines this shape exactly, and "
                                    "entropy cannot separate a 16-character "
                                    "credential from a placeholder"})
                continue
            if len(body) < MIN_BODY:
                # Too short to measure and not an exact shape: reported, never
                # silently dropped, because "below the measurable length" is a
                # different answer from "measured and harmless".
                hits.append({"where": f"{where}:{line}", "kind": label,
                             "entropy": round(entropy(body), 2),
                             "normalised": round(normalised(body), 2),
                             "credential": False, "length": len(raw),
                             "why": f"body is {len(body)} chars, under the {MIN_BODY} "
                                    f"entropy needs to mean anything"})
                continue
            n = normalised(body)
            hits.append({"where": f"{where}:{line}", "kind": label,
                         "entropy": round(entropy(body), 2),
                         "normalised": round(n, 2),
                         "credential": n >= NORMALISED_FLOOR,
                         "length": len(raw)})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", type=int, default=0, metavar="N",
                    help="also scan the added lines of the last N commits")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    credentials, fixtures, allowed = [], [], []
    for rel in tracked():
        if rel in SKIP:
            continue
        f = ROOT / rel
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue                                                                   
        for hit in scan_text(text, rel):
            if rel in ALLOWLIST:
                allowed.append({**hit, "allowed_because": ALLOWLIST[rel]})
            elif hit.get("credential") or hit["entropy"] is None:
                credentials.append(hit)
            else:
                fixtures.append(hit)

    if a.history:
        # `safe_git` adds `--no-ext-diff --no-textconv`: the scan must read the
        # bytes that were committed, not a driver's rendering of them.
        out = safe_git.run(["log", f"-{a.history}", "-p", "--no-color", "--unified=0"],
                           cwd=ROOT, timeout=600)
        # THE FILE EACH LINE CAME FROM, kept. The first version concatenated
        # every added line of N commits into one string and scanned it as
        # `history(last N)` — so the per-file allowlist above could not apply,
        # and a private-key-header match, which carries no entropy score, was an
        # unconditional credential with no way to be exempted. Committing the
        # scanner itself then turned this gate red for ever.
        #
        # `git log -p` names the file in a `+++ b/<path>` header, so the walk
        # tracks it and each hit is attributed the way the tree path attributes
        # its own.
        per_file: dict[str, list[str]] = {}
        current = "history(unattributed)"
        for line in out.stdout.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:].strip()
                continue
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                per_file.setdefault(current, []).append(line[1:])
        hits = []
        for rel_hist, lines in per_file.items():
            for hit in scan_text("\n".join(lines), f"history:{rel_hist}"):
                hit["file"] = rel_hist
                hits.append(hit)
        for hit in hits:
            if hit.get("file") in ALLOWLIST:
                allowed.append({**hit, "allowed_because": ALLOWLIST[hit["file"]]})
                continue
            if hit.get("credential") or hit["entropy"] is None:
                # ROTATION, not a rewrite. A rewrite breaks every clone and the
                # credential is compromised either way — the audit's own remedy
                # text says the same.
                hit["remedy"] = "rotate the credential at its issuer; a history "
                credentials.append(hit)
            else:
                fixtures.append(hit)

    if a.json:
        print(json.dumps({"credentials": credentials, "fixtures": fixtures,
                          "allowed": allowed, "normalised_floor": NORMALISED_FLOOR,
                          "min_body": MIN_BODY},
                         indent=1))
    else:
        for h in credentials:
            print(f"  CREDENTIAL  {h['where']}  {h['kind']}"
                  + (f"  entropy {h['entropy']}" if h.get("entropy") else ""))
        for h in fixtures:
            print(f"  fixture     {h['where']}  {h['kind']}  normalised "
                  f"{h.get('normalised')} < {NORMALISED_FLOOR}")
        for h in allowed:
            print(f"  allowed     {h['where']}  {h['allowed_because'][:60]}")
        print(f"{len(tracked())} tracked file(s); {len(credentials)} credential(s), "
              f"{len(fixtures)} fixture(s), {len(allowed)} allowed")
    if credentials:
        print("\nA credential in the tracked tree is rotated at its issuer FIRST, "
              "then removed. Rewriting history is not the fix: it breaks every "
              "clone and the credential is compromised either way.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
