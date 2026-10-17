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
them, so a known issuer prefix (the grammar below) is decided by its prefix and
its length and never measured; entropy judges only an unprefixed named value. A false positive there costs one allowlist line with
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
import argparse, collections, json, math, os, pathlib, re, stat, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import safe_git  # noqa: E402  — the engine's one git door
import osprivacy  # noqa: E402  — opens that refuse a link on every OS
import credential_shape  # noqa: E402  — the engine's one list of issuer prefixes
sys.path.insert(0, str(ROOT / "collectors"))
import scan_env  # noqa: E402  — the env classifier's secret-shape test

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
#: 0.78 sits below every real base62 body's minimum over 4000 draws (0.796 at
#: 48 characters, 0.806 at 32) and above every placeholder measured. It does NOT
#: hold for hex: sixteen symbols cap a body of n characters at log2(16)/log2(n),
#: under 0.78 past 35 characters (review round 8), so the bare-word rule also
#: refuses a value that passes `scan_env.looks_secret`, as the named rule does.
NORMALISED_FLOOR = 0.78

#: THE LIMIT OF THE METHOD, and it is why prefixed tokens are never measured. Over 4000 draws
#: a real sixteen-character AWS body's minimum normalised entropy is 0.725 and
#: its first percentile 0.789 — which OVERLAPS the fixture at 0.72 and the
#: placeholder at 0.74. No entropy measure can separate those distributions,
#: because sixteen characters do not hold the information to. So a short,
#: structurally exact vendor shape is decided by its PREFIX and never measured.
MIN_BODY = 24

#: (name, pattern). Each is a vendor prefix that means "this is a credential"
#: rather than a guess about shape. The generic assignment rule at the end is
#: what catches an unbranded token.
PATTERNS = [
    # Any algorithm word, ENCRYPTED (PKCS#8) and DSA included (review round 6).
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY")),
    # An unbranded token assigned to a name that says what it is. The name is
    # what makes it worth measuring; the entropy is what decides.
    # A bare word as the whole name (`api_key = "…"`), measured by entropy. An
    # UPPER_SNAKE name is `named_secrets()`'s documented scope below.
    # A JSON member keyed by the bare word (`"api_key": "…"`) too: the name's own
    # closing quote may stand before the `:` (review round 7).
    # Leading underscores belong to the name (`_secret`, round 8); a JSON `\/` is
    # part of the value (round 8) and is read as `/`; a space may end the value
    # inside its quotes (round 8), and begin it after its opening quote (round 9).
    ("named secret", re.compile(
        r"(?i)(?<![A-Za-z0-9_])_*(?:api[_-]?key|secret|token|passwd|password)\b[\"']?\s*[:=]\s*"
        r"['\"][ \t]*([A-Za-z0-9_\-+/=\\]{20,})[ \t]*['\"]")),
]

# ── THE ISSUER-PREFIX GRAMMAR (review round 5; docs/design/AGENT-SECRETS.md) ──
#
# One tokenizer replaces the vendor patterns, whose narrower alphabets took the
# start position and hid the prefix rule (`ghp_<20>_<20>`). A TOKEN is a known
# issuer prefix at a token start, followed by the LONGEST run of `[A-Za-z0-9_-]`.
#
#   token start   the start of the text, a character other than an ASCII letter,
#                 an ASCII digit or `_` (a non-ASCII letter before a prefix is a
#                 token start), or a literal `\n`, `\t` or `\r` escape in a string
#   issuers       `credential_shape.PREFIXES`, `sk-or-v1-`, `sk-proj-`,
#                 `sk-ant-api03-`; the LONGEST prefix the token starts with is its issuer
#   refused       a body at or above the issuer's minimum (`credential_shape.BODY_MIN`
#                 where it names one, else 24), whatever its entropy, unless it is a
#                 placeholder or a name
#   a placeholder two or more lower-case words of 1–15 letters joined by `-` or
#                 `_`, or one repeated character: reported as a fixture, not refused
#                 (checked before the name rule, so `sk_your_key_goes_here` is reported)
#   a name        two or more `_`-separated segments of lower-case letters that is
#                 not a placeholder (a word longer than 15 letters): skipped
#   not judged    a token whose body is under its minimum, or a name; the scan then
#                 resumes right after its prefix, so an inner token is still judged
#   AWS           `AKIA`/`ASIA` at a token start, exactly sixteen `[0-9A-Z]`, then
#                 anything but an ASCII letter or digit (`-backup`, `_x`, end of text)
ISSUERS = tuple(sorted({"sk-or-v1-", "sk-proj-", "sk-ant-api03-", *credential_shape.PREFIXES},
                       key=len, reverse=True))
_START = r"(?:(?<![A-Za-z0-9_])|(?<=\\[nrt]))"
TOKEN = re.compile(_START + r"(?:" + "|".join(re.escape(p) for p in ISSUERS) + r")[A-Za-z0-9_\-]*")
AWS_ID = re.compile(_START + r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")
NAME_BODY = re.compile(r"[a-z]+(?:_[a-z]+)+")
PLACEHOLDER_BODY = re.compile(r"[a-z]{1,15}(?:[-_][a-z]{1,15})+")
#: The bare-word rule's measured minimum: the table says 20+, and it is 20.
BARE_MIN = 20
#: A slot address — WORD segments (letters, joined by `-`, `_` or `.`), then an
#: UPPER_SNAKE name, optionally `_`-led, of two or more characters after its
#: underscores (`alpha-web/prod/STRIPE_KEY`, `Alpha-Web/prod/_STRIPE_KEY`): the
#: vault's reference to a secret, not a value. Review round 8: a segment with
#: digits (`8gru7kmv…/EOX8…`, `hf_rtd399…/X`) is a value, and is judged. Review
#: round 9: a one-character name (`…/X`) is not a slot name, and the vault's own
#: project segments may carry upper-case letters. Review round 10: the name is
#: as wide as the vault's own (`vault.py`: `[A-Z_][A-Z0-9_]{0,127}`) — `_` runs
#: anywhere (`APP__DB_PASSWORD`, `KEY_`), a digit first after a leading `_`
#: (`_2FA_SECRET`) — and still two or more characters after its leading underscores.
#: Review round 11: at most 128 characters in all, leading underscores included,
#: as the vault allows (the lookahead bounds the whole name; the body after it
#: only shapes it).
_WORD_SEGMENT = r"[A-Za-z]+(?:[-_.][A-Za-z]+)*"
_SLOT_NAME = r"(?=[A-Z0-9_]{2,128}(?![A-Z0-9_]))(?:[A-Z]|_+[A-Z0-9])[A-Z0-9_]+"
SLOT_ADDRESS = re.compile(_WORD_SEGMENT + r"(?:/" + _WORD_SEGMENT + r")*/" + _SLOT_NAME)

#: THE NAMED-SECRET SCOPE (review round 4), stated once: an UPPER_SNAKE name of
#: at least two words ending in `_TOKEN`, `_KEY`, `_SECRET` or `_PASSWORD`, in
#: any of four forms — a quoted assignment (`NAME = "v"`, `NAME: 'v'`), an
#: unquoted `.env` line (`[export ]NAME=v`), a YAML line (`NAME: v`) and a JSON
#: member (`"NAME": "v"`) — whose value passes the env classifier's secret-shape
#: test (`scan_env.looks_secret`) or is 32 or more hex characters (`is_hex_secret`,
#: review round 13: a hex value under `looks_secret`'s entropy floor used to pass).
#: OUT OF SCOPE, on purpose: a lower-case name (a code argument such as
#: `idempotency_key=`) and a bare `KEY`/`_KEY`.
#: Leading underscores are part of the name (`_API_KEY`, `self._API_KEY`; review
#: round 8); a bare `_KEY` or `__KEY` is still one word and out of scope.
#: Review round 9: words may be joined by more than one `_` (a nested-config name
#: such as `APP__DB_PASSWORD` or `MYAPP__STRIPE__API_KEY`), and the first word may
#: start with a digit (`_2FA_SECRET`).
NAMED = r"_*[A-Z0-9][A-Z0-9]*(?:_+[A-Z0-9]+)*_+(?:TOKEN|KEY|SECRET|PASSWORD)"
#: A literal value: a key's alphabet (a JSON-escaped `\/` included), sixteen
#: characters or more. An expression (`{…}`, `$(…)`, `${{ … }}`) is not a
#: literal and is never judged.
_VALUE = r"([A-Za-z0-9_\-+/=.~\\]{16,})"
#: Spaces or tabs may stand after the opening quote and before the closing one
#: (review rounds 8–9): the value is what lies between them.
NAMED_FORMS = (
    re.compile(r'"(' + NAMED + r')"\s*:\s*"[ \t]*' + _VALUE + r'[ \t]*"'),
    re.compile(r"(?<![A-Za-z0-9_\"'])(" + NAMED + r")\s*[:=]\s*[\"'][ \t]*" + _VALUE + r"[ \t]*[\"']"),
    # A `.env` or YAML line: an optional `- ` list item (a compose `environment:`
    # list) and an optional quoted name (`"NAME": value`), value quoted or not.
    # `\r?` before the end: a CRLF file's lines end in `\r` (review round 6).
    # A no-break space may indent the line (review round 9).
    re.compile(r"^[ \t ]*(?:-[ \t]+)?(?:export[ \t]+)?[\"']?(" + NAMED + r")[\"']?[ \t]*[:=][ \t]*[\"']?"
               + r"[ \t]*" + _VALUE + r"[ \t]*[\"']?[ \t]*(?:#[^\r\n]*)?\r?$", re.M),
)

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
    "fabric/node-vectors/secret-envelope.json":
        "the contract's secret-envelope vector, vendored byte for byte with its digest "
        "checked (fabric/node-vectors/README.md): its sealed `secret` is the placeholder "
        "'example-secret-value-not-real' and its keys are RFC 9180 A.3.1's published test keys",
}

#: Paths never scanned, with the reason (listed in AGENT-SECRETS's table). The
#: engine folder holds no decisions log, so that entry is gone (review round 6).
SKIP = {
    "tools/check_secrets.py": "it contains the patterns themselves",
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



def is_hex_secret(value: str) -> bool:
    """32 or more hex characters and nothing else — a credential whatever its
    entropy (review round 13). `looks_secret` needs entropy 3.4, which about 1% of
    40-hex and 6.5% of 32-hex values miss, and a hex value can never reach the
    0.78 normalised floor; `credential_shape` already counts such a run as a
    credential, and this is its own pattern (`credential_shape._HEX`)."""
    return credential_shape._HEX.fullmatch(value) is not None


def placeholder_body(body: str) -> bool:
    """EXACTLY the documented placeholder (review round 5): two or more lower-case
    words of 1–15 letters joined by `-` or `_`, or one repeated character. No
    lead or end word excuses a body: `scan_env.is_placeholder` let `…-here` through."""
    return bool(PLACEHOLDER_BODY.fullmatch(body)) or (len(body) > 0 and len(set(body)) == 1)


def issuer_of(raw: str) -> str | None:
    """The known issuer prefix `raw` starts with (longest first), or None."""
    for pre in ISSUERS:
        if raw.startswith(pre):
            return pre
    return None


def judge_token(raw: str) -> tuple[str, str] | None:
    """(verdict, why) for one TOKEN under the grammar, or None when it is not one.

    verdict is "refused" or "fixture"; None means the token is below its issuer's
    minimum or is a name, and the gate says nothing about it."""
    issuer = issuer_of(raw)
    if issuer is None:
        return None
    body = raw[len(issuer):]
    # The vendor's own minimum where `credential_shape.BODY_MIN` names one, above
    # 24 as well as below it (review round 6: a `min(24, …)` cap ignored it).
    if len(body) < credential_shape.BODY_MIN.get(issuer, MIN_BODY):
        return None
    # The placeholder first: `sk_your_key_goes_here_please` is reported, not silenced
    # by the name rule (review round 6).
    if placeholder_body(body):
        return ("fixture", "a placeholder body: lower-case words, or one repeated character")
    if NAME_BODY.fullmatch(body):
        return None
    return ("refused", "a known issuer prefix with its body is refused whatever its entropy")


def decode(raw: bytes) -> str:
    """A file's bytes as text the grammar can read: UTF-8, any other byte kept as a
    surrogate (review round 6: a non-UTF-8 file used to be skipped in silence)."""
    return raw.decode("utf-8", errors="surrogateescape")


class _NotRegular(Exception):
    """A tracked path that is neither a regular file nor a symbolic link."""


class _ParentLink(Exception):
    """A folder on a tracked path is a symbolic link; `args[0]` is that folder's path."""


#: One file's read is capped (review round 13): a larger file is unreadable, named
#: so, rather than read into memory whole.
MAX_FILE_BYTES = 32 * 1024 * 1024


def _too_large() -> _NotRegular:
    return _NotRegular(f"larger than {MAX_FILE_BYTES // (1 << 20)} MB; not read")


def _read_checked(fd: int, st: os.stat_result) -> bytes:
    """Read an opened regular file: it must still be the file `st` described, and
    no larger than `MAX_FILE_BYTES`."""
    with os.fdopen(fd, "rb") as fh:
        now = os.fstat(fh.fileno())
        if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            raise _NotRegular("changed between lstat and open; not read")
        data = fh.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise _too_large()
        return data


def _is_link(st: os.stat_result) -> bool:
    """A symbolic link, or on Windows any reparse point (a junction too)."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _tracked_bytes(root: pathlib.Path, rel: str) -> bytes:
    """The WORKING-TREE bytes at `rel` under `root`, read without following anything.

    Review round 13: every folder on the path is opened with
    `O_NOFOLLOW|O_DIRECTORY` relative to the one before it (`dir_fd`), so a folder
    swapped for a symbolic link is never read through; round 11 opened only the
    last component that way. A folder that is a link raises `_ParentLink`.

    Review round 14: every open goes through `osprivacy`, the engine's Windows/Linux
    boundary. Windows has no folder descriptor (`osprivacy.open` raises
    `NotImplementedError` for `DIRECTORY` and `dir_fd`), so there each component is
    `lstat`ed and a link or junction refused before the file is opened by path with
    `NOFOLLOW`; the window between a look and the open that `dir_fd` closes on POSIX
    stays open there, and `fstat` still checks the file is the one `lstat` saw.

    The last component: a symbolic link is judged by its TARGET TEXT (what git
    stores for a link) and never followed (review round 11: a link to a FIFO
    blocked the scan, one to `/dev/zero` would read for ever, one to an outside
    file was judged under the link's name). Anything else that is not a regular
    file is unreadable. A regular file is opened `NOFOLLOW|NONBLOCK`, checked
    with `fstat` to be the file `lstat` saw, and read up to `MAX_FILE_BYTES`."""
    parts = rel.split("/")
    try:
        dfd = osprivacy.open(root, os.O_RDONLY | osprivacy.DIRECTORY)
    except NotImplementedError:
        return _tracked_bytes_by_path(root, parts)
    try:
        for i, comp in enumerate(parts[:-1]):
            try:
                nfd = osprivacy.open(comp, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.DIRECTORY, dir_fd=dfd)
            except OSError:
                st = os.stat(comp, dir_fd=dfd, follow_symlinks=False)  # FileNotFoundError propagates
                if stat.S_ISLNK(st.st_mode):
                    raise _ParentLink("/".join(parts[:i + 1])) from None
                raise _NotRegular("a folder on its path is not a folder; not read") from None
            os.close(dfd)
            dfd = nfd
        name = parts[-1]
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            return os.fsencode(os.readlink(name, dir_fd=dfd))
        if not stat.S_ISREG(st.st_mode):
            raise _NotRegular("not a regular file or a symbolic link; not read")
        if st.st_size > MAX_FILE_BYTES:
            raise _too_large()
        return _read_checked(osprivacy.open(name, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK,
                                            dir_fd=dfd), st)
    finally:
        os.close(dfd)


def _tracked_bytes_by_path(root: pathlib.Path, parts: list[str]) -> bytes:
    """`_tracked_bytes` where a folder has no descriptor (Windows): `lstat` each
    folder on the path and refuse a link or junction, then the same last-component
    rules, the file opened by path through `osprivacy` (which refuses a link)."""
    here = pathlib.Path(root)
    for i, comp in enumerate(parts[:-1]):
        here = here / comp
        st = os.lstat(here)  # FileNotFoundError propagates
        if _is_link(st):
            raise _ParentLink("/".join(parts[:i + 1]))
        if not stat.S_ISDIR(st.st_mode):
            raise _NotRegular("a folder on its path is not a folder; not read")
    path = here / parts[-1]
    st = os.lstat(path)
    if _is_link(st):
        return os.fsencode(os.readlink(path))
    if not stat.S_ISREG(st.st_mode):
        raise _NotRegular("not a regular file or a symbolic link; not read")
    if st.st_size > MAX_FILE_BYTES:
        raise _too_large()
    return _read_checked(osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK), st)


def index_entries(root: pathlib.Path = ROOT) -> dict[str, tuple[str, str, bool]]:
    """path -> (mode, object id, skip-worktree) for every index entry under `root`
    (`git ls-files -s -t -z`: `<tag> <mode> <oid> <stage>\t<path>`; tag `S` is
    skip-worktree). An unmerged path keeps its first stage."""
    out = safe_git.run(["ls-files", "-s", "-t", "-z"], cwd=root, timeout=120, text=False)
    if out.returncode != 0:
        raise SystemExit("check_secrets: `git ls-files -s` failed: "
                         + out.stderr.decode("utf-8", "replace")[:200])
    entries: dict[str, tuple[str, str, bool]] = {}
    for rec in out.stdout.split(b"\0"):
        if not rec:
            continue
        head, _, path = rec.partition(b"\t")
        tag, mode, oid, _stage = head.decode("ascii").split(" ")
        entries.setdefault(os.fsdecode(path), (mode, oid, tag == "S"))
    return entries


def _index_blob(root: pathlib.Path, oid: str) -> bytes:
    """An index entry's committed bytes (`git cat-file blob`), capped like a file.
    Review round 14: with `GIT_NO_REPLACE_OBJECTS=1`, so a `refs/replace/` stand-in
    for the blob is never read in its place."""
    real = {"GIT_NO_REPLACE_OBJECTS": "1"}
    size = safe_git.run(["cat-file", "-s", oid], cwd=root, timeout=60, extra_env=real)
    if size.returncode != 0:
        raise OSError(f"git cat-file -s failed: {size.stderr.strip()[:120]}")
    if int(size.stdout.strip()) > MAX_FILE_BYTES:
        raise _too_large()
    blob = safe_git.run(["cat-file", "blob", oid], cwd=root, timeout=120, text=False, extra_env=real)
    if blob.returncode != 0:
        raise OSError("git cat-file blob failed")
    return blob.stdout


def scan_tree(root: pathlib.Path, rels: list[str],
              index: dict[str, tuple[str, str, bool]] | None = None) -> tuple[list, list, list, list]:
    """(credentials, fixtures, allowed, unreadable) over `rels` under `root`.

    The WORKING TREE is read, not the index (review round 13 corrected "what git
    committed"). With `index` (from `index_entries`): a skip-worktree entry (a
    sparse checkout), or a tracked file missing from the working tree, is read
    from its index blob instead. A path under a folder that is a symbolic link is
    never read through it: the link is judged at its own path when that path is
    in `rels`, and otherwise the path is UNREADABLE — so a committed key whose
    folder was swapped for a link to a harmless copy fails the gate rather than
    passing on the copy. A file that will not read is UNREADABLE — a failure the
    caller reports and exits on, never a skip. File NAMES are never judged, only
    contents."""
    credentials, fixtures, allowed, unreadable = [], [], [], []
    listed = set(rels)
    for rel in rels:
        if rel in SKIP:
            continue
        entry = (index or {}).get(rel)
        try:
            if entry is not None and entry[2]:
                raw = _index_blob(root, entry[1])
            else:
                try:
                    raw = _tracked_bytes(root, rel)
                except FileNotFoundError:
                    if entry is None:
                        raise
                    raw = _index_blob(root, entry[1])
        except _ParentLink as exc:
            if exc.args[0] not in listed:
                unreadable.append({"path": rel, "why": f"its folder {exc.args[0]} is a symbolic link; "
                                                       "not followed, not read"})
            continue
        except _NotRegular as exc:
            unreadable.append({"path": rel, "why": str(exc)})
            continue
        except OSError as exc:
            unreadable.append({"path": rel, "why": type(exc).__name__})
            continue
        text = decode(raw)
        for hit in scan_text(text, rel):
            if rel in ALLOWLIST:
                allowed.append({**hit, "allowed_because": ALLOWLIST[rel]})
            elif hit.get("credential") or hit["entropy"] is None:
                credentials.append(hit)
            else:
                fixtures.append(hit)
    return credentials, fixtures, allowed, unreadable


def scan_repo(root: pathlib.Path = ROOT) -> dict:
    """The tree scan as the gate runs it: every index entry under `root`; a gitlink
    (a submodule, mode 160000) is SKIPPED and named — its own repository is not
    this gate's — rather than failed as unreadable (review round 13)."""
    entries = index_entries(root)
    skipped = [{"path": rel, "why": "a submodule (gitlink); its repository is not scanned here"}
               for rel, (mode, _oid, _skip) in entries.items() if mode == "160000"]
    rels = [rel for rel, (mode, _oid, _skip) in entries.items() if mode != "160000"]
    credentials, fixtures, allowed, unreadable = scan_tree(root, rels, entries)
    return {"credentials": credentials, "fixtures": fixtures, "allowed": allowed,
            "unreadable": unreadable, "skipped": skipped, "files": len(rels)}


def body_of(match: str) -> str:
    """The random part, with the vendor prefix removed.

    The prefix is fixed text every key of that vendor carries, so leaving it in
    lowers the entropy of every real key by diluting it with a known string;
    the measurement has to be about the part that is supposed to be random.
    """
    for pre in sorted({"sk-or-v1-", "sk-proj-", "sk-ant-api03-", "AKIA", *credential_shape.PREFIXES},
                      key=len, reverse=True):
        if match.startswith(pre):
            return match[len(pre):]
    return match


def tracked(root: pathlib.Path = ROOT) -> list[str]:
    """The tracked paths under `root`, NUL-separated (review round 9): without `-z`
    git C-quotes a name with a non-ASCII byte or a control character, and the
    quoted string names no file, so the gate reported it unreadable. Names are
    decoded as the file system decodes them, so a non-UTF-8 name opens too."""
    out = safe_git.run(["ls-files", "-z"], cwd=root, timeout=120, text=False)
    if out.returncode != 0:
        raise SystemExit("check_secrets: `git ls-files` failed: "
                         + out.stderr.decode("utf-8", "replace")[:200])
    return [os.fsdecode(n) for n in out.stdout.split(b"\0") if n]


#: Verdict severity: at one position the most severe verdict wins (review round 7).
_SEVERITY = {True: 2, False: 1}


def scan_text(text: str, where: str) -> list[dict]:
    """Every rule judges; at each position the MOST SEVERE verdict wins.

    Review round 7: a fixture verdict that claimed a position first (a bare-word
    entropy measure, a placeholder-bodied token at the start of a value) hid a
    refusal another rule would have made of the same value. Now every rule is
    evaluated and only the strongest verdict per position is reported:
    refused > fixture > not judged."""
    # A BOM at the start of the file, and a lone CR line ending, are read as the
    # line structure they are (review round 7); a lone CR becomes LF, which keeps
    # every offset.
    if text.startswith("﻿"):
        text = text[1:]
    text = re.sub(r"\r(?!\n)", "\n", text)
    # Round 8: every other line separator a file may carry — U+2028, U+2029, NEL,
    # FF, VT — is a line end too, and a BOM at a line start is a space; each is one
    # character for one, so every offset holds.
    text = re.sub("[  \u0085\x0c\x0b]", "\n", text)
    text = re.sub("(?m)^﻿", " ", text)
    best: dict[int, dict] = {}

    def offer(at: int, hit: dict) -> None:
        held = best.get(at)
        if held is None or _SEVERITY[bool(hit.get("credential") or hit.get("entropy") is None)] > \
                _SEVERITY[bool(held.get("credential") or held.get("entropy") is None)]:
            best[at] = hit

    hits = _scan_all(text, where, offer)
    return hits + list(best.values())


def _hit(text: str, where: str, at: int, **fields) -> dict:
    return {"where": f"{where}:{text[:at].count(chr(10)) + 1}", **fields}


def _measure_bare(value: str) -> tuple[bool, str]:
    """(refused, why) for a bare-word value, measured WHOLE (review round 7: an
    issuer prefix was stripped first, which the table never said).

    Review round 11: a value that starts with an issuer prefix and is not a
    placeholder is refused by `looks_secret`, because every issuer prefix is one
    of `scan_env.KNOWN_PREFIX` (pinned by `test_round11_issuers_are_known_prefixes`).
    The arm that measured "the rest after a leading token" could never decide,
    so it is gone, from the code and from the table."""
    if SLOT_ADDRESS.fullmatch(value):
        # `project/env/NAME`, the vault's own reference form: it names a secret.
        return False, "a slot address names a secret; it is not one"
    issuer = issuer_of(value)
    if placeholder_body(value[len(issuer):] if issuer else value):
        # Before the shape test, as in the named rule (review round 9: the table
        # states the order, and the code now has it in both rules).
        return False, "a placeholder value"
    if scan_env.looks_secret(value):
        # The named rule's own shape test (review round 8): forty hex characters
        # can never reach the entropy floor, and are a secret's shape all the same.
        return True, "a bare-word value of a secret's shape (scan_env.looks_secret)"
    if is_hex_secret(value):
        return True, "a bare-word value of 32 or more hex characters"
    if len(value) >= BARE_MIN and normalised(value) >= NORMALISED_FLOOR:
        return True, "a bare-word value of a secret's entropy"
    return False, "measured under the entropy floor"


def _scan_all(text: str, where: str, offer) -> list[dict]:
    """Every rule offers a verdict at the position it judged; `scan_text` keeps
    the most severe per position. Nothing here skips a position another rule took."""
    # 1. THE ISSUER-PREFIX GRAMMAR (review round 5).
    pos = 0
    while (m := TOKEN.search(text, pos)) is not None:
        raw = m.group(0)
        issuer = issuer_of(raw)
        verdict = judge_token(raw)
        if verdict is None:
            # NOT JUDGED: resume right after the prefix, so a token inside this
            # run (`sk-short-ghp_<36>`) is judged on its own (review round 6).
            pos = m.start() + len(issuer)
            continue
        pos = m.end()
        body = raw[len(issuer):]
        offer(m.start(), _hit(text, where, m.start(), kind="issuer prefix", issuer=issuer,
                              entropy=round(entropy(body), 2), normalised=round(normalised(body), 2),
                              credential=verdict[0] == "refused", length=len(raw), why=verdict[1]))
    # The AWS id has its own exact pattern (review round 6): `AKIA-ghp_…` is not an
    # AWS id, and its `ghp_` token is judged above; `AKIA<16>-ghp_<36>` is both.
    for m in AWS_ID.finditer(text):
        offer(m.start(), _hit(text, where, m.start(), kind="aws access key", issuer=m.group(0)[:4],
                              entropy=round(entropy(m.group(0)[4:]), 2),
                              normalised=round(normalised(m.group(0)[4:]), 2), credential=True,
                              length=len(m.group(0)), why="an AWS access key id: the prefix and sixteen [0-9A-Z]"))
    for label, rx in PATTERNS:
        for m in rx.finditer(text):
            if label == "private key block":
                offer(m.start(), {"where": where, "kind": label, "entropy": None,
                                  "why": "a private key block is a credential whatever its contents"})
                continue
            value = m.group(1).replace("\\/", "/")
            if SLOT_ADDRESS.fullmatch(value):
                continue                 # names a secret; not judged, not reported
            issuer = issuer_of(value)
            if placeholder_body(value[len(issuer):] if issuer else value):
                # A PLACEHOLDER IS A FIXTURE (review round 5); the most severe
                # verdict at this position still wins (round 7).
                offer(m.start(1), _hit(text, where, m.start(), kind=label, entropy=round(entropy(value), 2),
                                       normalised=round(normalised(value), 2), credential=False,
                                       length=len(value), why="a placeholder value"))
                continue
            refused, why = _measure_bare(value)
            offer(m.start(1), _hit(text, where, m.start(), kind=label, entropy=round(entropy(value), 2),
                                   normalised=round(normalised(value), 2), credential=refused,
                                   length=len(value), why=why))
    for rx in NAMED_FORMS:
        for m in rx.finditer(text):
            name, value = m.group(1), m.group(2).strip().replace("\\/", "/")
            issuer = issuer_of(value)
            if placeholder_body(value[len(issuer):] if issuer else value):
                offer(m.start(2), _hit(text, where, m.start(), kind="named secret", name=name,
                                       entropy=round(entropy(value), 2), normalised=round(normalised(value), 2),
                                       credential=False, length=len(value), why="a placeholder value"))
                continue
            if not (scan_env.looks_secret(value) or is_hex_secret(value)):
                continue
            offer(m.start(2), _hit(text, where, m.start(), kind="named secret", name=name,
                                   entropy=round(entropy(value), 2), normalised=round(normalised(value), 2),
                                   credential=True, length=len(value),
                                   why="an UPPER_SNAKE secret name with a value of a secret's shape"))
    return []

class HistoryIncomplete(Exception):
    """The history mode cannot see every commit it was asked for (a shallow repository)."""


def history_log(root: pathlib.Path, n: int) -> bytes:
    """The patches of the last `n` commits under `root`, as bytes.

    `safe_git` adds `--no-ext-diff --no-textconv`: the scan must read the bytes
    that were committed, not a driver's rendering of them. Review round 9:
    `--relative` and `-- .` name each file as the tree scan names it (relative to
    the engine folder), so SKIP and ALLOWLIST apply to history by the same keys;
    `core.quotePath=false` leaves a non-ASCII name as it is (a control character
    is still C-quoted, and `_header_path` unquotes it).

    Review round 10: `--text`, because git otherwise prints "Binary files differ"
    for a file holding a NUL or marked `binary`/`-diff` in `.gitattributes`, and
    its added lines were never scanned; `--diff-merges=first-parent`, because a
    merge's own resolution was never diffed (kept in the `diff --git` format the
    header state machine reads, not `--cc`); `--src-prefix=a/ --dst-prefix=b/`,
    because a repository's `diff.noprefix` or `diff.mnemonicPrefix` would make a
    real `b/…` folder indistinguishable from git's prefix.

    Review round 11: `--root`, so `log.showRoot=false` cannot hide the root
    commit's diff; `GIT_NO_REPLACE_OBJECTS=1`, so a `refs/replace/` stand-in cannot
    show another commit in place of the one that added a key; `--full-history`,
    stated outright: with the `-- .` pathspec git would otherwise simplify history
    and drop a side branch that added a key and removed it before its merge
    (`--diff-merges=first-parent` happens to imply it today; the gate does not
    rely on that). `-N` counts commits in git's date order, side branches
    included.

    Review round 13: `GIT_GRAFT_FILE` names an empty file, so `.git/info/grafts`
    cannot re-parent a commit past the one that added a key; `--no-renames`, so a
    file renamed or copied (`diff.renames=copies`) shows its whole content as added
    lines, even when it leaves a SKIP path."""
    # Review round 14: a shallow repository's `.git/shallow` boundary hides every
    # commit behind it — at the commit that removed a key, the one that added it.
    # History is then INCOMPLETE, and the gate says so rather than reporting clean.
    shallow = safe_git.run(["rev-parse", "--is-shallow-repository"], cwd=root, timeout=60)
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        raise HistoryIncomplete("a shallow repository: commits behind its boundary are not in the history; "
                                "fetch the whole history (`git fetch --unshallow`) and run again")
    out = safe_git.run(["log", f"-{n}", "-p", "--root", "--full-history", "--no-renames", "--text", "--diff-merges=first-parent",
                        "--src-prefix=a/", "--dst-prefix=b/", "--relative", "--no-color", "--unified=0",
                        "--", "."],
                       cwd=root, timeout=600, text=False,
                       extra_env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.quotePath",
                                  "GIT_CONFIG_VALUE_0": "false", "GIT_NO_REPLACE_OBJECTS": "1",
                                  "GIT_GRAFT_FILE": os.devnull})
    if out.returncode != 0:
        raise SystemExit("check_secrets: `git log` failed: " + out.stderr.decode("utf-8", "replace")[:200])
    return out.stdout


def history_files(raw: bytes) -> dict[str, list[str]]:
    """The added lines of a `git log -p` per file, read as BYTES (review round 8):
    decoded like a tree file (`surrogateescape`) and split on `\\n` only, so a
    lone CR inside an added line stays in it — `str.splitlines()` cut the line
    there and the `+` filter dropped the rest — and a non-UTF-8 byte never raises.

    A STATE MACHINE (review round 9). A file's header is the lines from its
    `diff --git` to its first `@@`, and only there is `+++ <path>` a header: in a
    hunk an added line reading `++ b/x` arrives as `+++ b/x` and is a line of the
    file, as is `++i;` (arriving as `+++i;`). The first version took either for a
    header, so the line after it was filed under a file that does not exist, or
    the line was dropped. A C-quoted path (`"b/sp ace\\ttab.txt"`) is unquoted."""
    per_file: dict[str, list[str]] = {}
    current: str | None = "history(unattributed)"
    in_header = False
    for line in decode(raw).split("\n"):
        if line.startswith("diff --git "):
            in_header, current = True, None
            continue
        if line.startswith("commit "):
            # A body line starts with `+`, `-`, ` `, `\` or `@`; this is the next commit.
            in_header, current = False, "history(unattributed)"
            continue
        if in_header:
            if line.startswith("@@"):
                in_header = False
            elif line.startswith("+++ "):
                current = _header_path(line[4:])
            continue
        if line.startswith("+") and current is not None:
            per_file.setdefault(current, []).append(line[1:])
    return per_file


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _header_path(field: str) -> str | None:
    """The path of a `+++` header field: `/dev/null` is no file; a C-quoted field
    (`"b/…"`, git's quoting of a control character, `"` or `\\`) is unquoted, its
    octal escapes read as bytes; an unquoted one loses the TAB git appends to a
    name holding a space. The `b/` prefix is dropped.

    Review round 11: that TAB follows a QUOTED name too (`"b/a \\"q\\" c.txt"<TAB>`),
    so it is stripped before the quote test, not after."""
    if field.endswith("\t"):
        field = field[:-1]
    if field.startswith('"') and field.endswith('"') and len(field) >= 2:
        out = bytearray()
        body, i = field[1:-1], 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if re.fullmatch(r"[0-7]{3}", body[i + 1:i + 4]):
                    out.append(int(body[i + 1:i + 4], 8) & 0xFF)
                    i += 4
                    continue
                if nxt in _C_ESCAPES:
                    out.append(_C_ESCAPES[nxt])
                    i += 2
                    continue
            out += ch.encode("utf-8", "surrogateescape")
            i += 1
        path = out.decode("utf-8", "surrogateescape")
    else:
        path = field
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith("b/") else path


def text_line(kind: str, h: dict) -> str:
    """One text-output line, with the hit's OWN reason (review round 13: every
    fixture used to print `normalised X < 0.78`, placeholders included)."""
    label = "CREDENTIAL" if kind == "credential" else "fixture   "
    why = h.get("why")
    if not why:
        why = (f"entropy {h['entropy']}" if kind == "credential" and h.get("entropy") else
               f"normalised {h.get('normalised')} < {NORMALISED_FLOOR}" if kind == "fixture" else "")
    return f"  {label}  {h['where']}  {h['kind']}" + (f"  — {why}" if why else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", type=int, default=0, metavar="N",
                    help="also scan the added lines of the last N commits")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    tree = scan_repo(ROOT)
    credentials, fixtures, allowed, unreadable = (tree["credentials"], tree["fixtures"], tree["allowed"],
                                                  tree["unreadable"])

    if a.history:
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
        try:
            per_file = history_files(history_log(ROOT, a.history))
        except HistoryIncomplete as exc:
            print(f"check_secrets: history incomplete — {exc}", file=sys.stderr)
            return 1
        hits = []
        for rel_hist, lines in per_file.items():
            if rel_hist in SKIP:
                continue
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
                hit["remedy"] = ("rotate the credential at its issuer; a history rewrite does not "
                                 "un-leak it, and breaks every clone")
                credentials.append(hit)
            else:
                fixtures.append(hit)

    if a.json:
        print(json.dumps({"credentials": credentials, "fixtures": fixtures,
                          "allowed": allowed, "unreadable": unreadable, "skipped": tree["skipped"],
                          "normalised_floor": NORMALISED_FLOOR, "min_body": MIN_BODY},
                         indent=1))
    else:
        for h in credentials:
            print(text_line("credential", h))
        for h in fixtures:
            print(text_line("fixture", h))
        for h in allowed:
            print(f"  allowed     {h['where']}  {h['allowed_because'][:60]}")
        for u in unreadable:
            print(f"  UNREADABLE  {u['path']}  {u['why']}")
        for s in tree["skipped"]:
            print(f"  skipped     {s['path']}  {s['why']}")
        print(f"{tree['files']} tracked file(s); {len(credentials)} credential(s), "
              f"{len(fixtures)} fixture(s), {len(allowed)} allowed, {len(unreadable)} unreadable, "
              f"{len(tree['skipped'])} skipped")
    if unreadable:
        print("\nA tracked file the gate could not read is a failure, not a pass: "
              "it was not judged.", file=sys.stderr)
        return 1
    if credentials:
        print("\nA credential in the tracked tree is rotated at its issuer FIRST, "
              "then removed. Rewriting history is not the fix: it breaks every "
              "clone and the credential is compromised either way.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
