#!/usr/bin/env python3
"""The one way the engine runs `git`: asking git a question runs nothing else.

WHY THIS EXISTS. Git executes programs named in configuration: `core.fsmonitor`
on every index read, hooks on every commit and ref update (and, since Git 2.5x,
hooks declared in config as `hook.<name>.command`), `gpg.program` whenever
`log.showSignature` is on, `diff.external` and textconv drivers on every diff,
clean/smudge filters whenever a worktree file is compared, credential helpers
and `git-remote-<scheme>` helpers on every network call. An observer that runs
bare `git` inherits all of that from the operator's `~/.gitconfig` and from each
watched repository: an audit with a planted configuration counted gpg, the
fsmonitor, a dozen hooks and a filter running from one ordinary tick, and the
Stop hook ran them on every agent turn. A pinentry or Keychain dialog in front
of the operator, a hook with side effects, or a filter that rewrites what git
reports are all things a watcher must not cause.

WHAT IT DOES, in three layers:

1. **The user's global and system configuration are not loaded.**
   `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=<file>`, where the file holds
   only an allowlist of *values* copied from the protected scopes
   (`FORWARD_KEYS`): `safe.directory` (repositories owned by another user stay
   readable), `core.excludesFile` (a `.env` ignored by the global excludes file
   stays "ignored"), line-ending settings (a CRLF checkout does not turn dirty),
   `url.<base>.insteadOf` (a remote rewritten to SSH is still probed the way the
   operator's own `git` would reach it) and HTTP proxy and CA settings. No key
   that names a program is ever copied, so no future program-running key in the
   global file can reach the engine either. The file is private (0600 in a 0700
   directory), lives for the process and is removed at exit: it can hold a
   rewrite base with a token in it, and a `-c` argument would have put that
   value in every process listing.
2. **A fixed set of `-c` overrides** (`OPTIONS`) turns off what the repository's
   own configuration can still name: fsmonitor, the hooks directory, signature
   display and signing, credential helpers, colour and automatic maintenance,
   and it allows only the https, http, ssh, git and file transports, so a
   `gcrypt::` or `ext::` URL is refused instead of starting a helper.
   `diff`, `log` and `show` also get `--no-ext-diff --no-textconv`.
3. **Driver-named keys are enumerated** from the repository's configuration
   before any command that can compare worktree content or update a ref:
   every `filter.<driver>.clean|smudge|process` is blanked (and `.required`
   set false) and every config-declared hook is disabled. Their names cannot be
   known in advance, so they are read first, under the same environment; a
   configuration that cannot be read refuses the command (`GitConfigUnreadable`)
   rather than running it unprotected.

The environment keeps `GIT_TERMINAL_PROMPT=0`, askpass programs that answer
nothing, `ssh -o BatchMode=yes`, and only an allowlist of the caller's `GIT_*`
variables: the author/committer identity, the CA bundle and the discovery
ceiling. `GIT_DIR`, `GIT_EXTERNAL_DIFF`, `GIT_CONFIG_PARAMETERS` and the rest
would reopen what the layers above close.

COMMITS (`write=True`) need an author. The identity is resolved by
`git var GIT_AUTHOR_IDENT` / `GIT_COMMITTER_IDENT` under the operator's full
configuration in that repository's context (reading configuration runs
nothing), so `includeIf`-scoped and repository-local identities keep working,
and is handed to the hardened commit as `GIT_AUTHOR_*` / `GIT_COMMITTER_*`.

WHAT IS LEFT OUTSIDE, said rather than implied: `ssh` reads its own
`~/.ssh/config`, which can name a `ProxyCommand`; a submodule's private
configuration can name a filter driver the parent repository does not, and only
the parent's are enumerated; Git LFS and other filters are not run, so a
modified LFS file is compared byte for byte with its pointer; values copied
from the global file under an `includeIf` condition are read outside any
repository and so are not copied.

Standard library only: the companion plugin's hooks import this module, and a
hook is a bad place to discover a missing dependency.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Mapping, Sequence

#: The transports git may use. Everything else — `ext::`, `fd::` and every
#: `git-remote-<scheme>` helper such as `gcrypt::` — is refused by git itself.
TRANSPORTS = ("https", "http", "ssh", "git", "file")

#: A program that answers every prompt with nothing.
_TRUE = "/usr/bin/true" if os.path.exists("/usr/bin/true") else (shutil.which("true") or os.devnull)

#: The overrides every invocation carries. Each one names a program-running or
#: output-changing setting that a repository's own configuration can still set.
OPTIONS: tuple[str, ...] = (
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.pager=cat",
    "-c", "credential.helper=",          # an empty value resets the list, URL-scoped helpers too
    "-c", "log.showSignature=false",     # otherwise `git log` runs gpg.program
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
    "-c", "tag.forceSignAnnotated=false",
    "-c", "color.ui=false",
    "-c", "gc.auto=0",                   # no background `gc --auto` started by an observer
    "-c", "maintenance.auto=false",
    "-c", "protocol.allow=never",
    *[arg for name in TRANSPORTS for arg in ("-c", f"protocol.{name}.allow=always")],
)

#: Subcommands that print diffs: external diff drivers and textconv are off.
DIFFING = frozenset({"diff", "log", "show"})

#: Subcommands that neither compare worktree content nor update a ref, so no
#: filter and no hook can fire: the driver enumeration is skipped for them.
#: Anything not listed here is enumerated — the safe default for a new call.
PURE = frozenset({
    "rev-parse", "rev-list", "log", "show", "cat-file", "for-each-ref", "symbolic-ref",
    "merge-base", "ls-remote", "remote", "show-ref", "cherry", "describe", "config",
    "var", "reflog", "check-ignore", "version", "--version",
})

#: Subcommands that write a commit or a tag object, and so need an author.
COMMITTING = frozenset({"commit", "tag", "merge", "cherry-pick", "revert", "am", "notes", "stash"})

#: Values copied from the operator's global and system configuration. Values
#: only — nothing here names a program — matched against git's lower-cased keys.
FORWARD_KEYS = (r"^(safe\.directory"
                r"|core\.(excludesfile|attributesfile|autocrlf|eol|safecrlf|precomposeunicode)"
                r"|url\..+\.insteadof"
                r"|http\.(.+\.)?(proxy|sslcainfo|sslcapath|sslbackend))$")

#: Program-running keys whose names include a user-chosen driver or hook name.
_DRIVER_KEYS = r"^(filter\..+\.(clean|smudge|process|required)|hook\..+\.(command|event))$"

#: The caller's `GIT_*` variables that pass through. Every other one is dropped.
KEEP_ENV = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
})

#: Where the operator's own configuration lives; read only to copy `FORWARD_KEYS`.
_LOCATION_ENV = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")

SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"


class GitConfigUnreadable(OSError):
    """The repository's configuration could not be read, so the drivers it
    names could not be neutralized and the command was not run."""

    def __init__(self, message: str, returncode: int, stderr: str):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def binary(preferred: str | None = None) -> str:
    """The git to run: the caller's choice, else PATH's, else the bare name
    (so a missing git raises FileNotFoundError exactly as before)."""
    return preferred or shutil.which("git") or "git"


def _user_env() -> dict[str, str]:
    """The caller's environment as git would read the operator's configuration
    from it: every `GIT_*` dropped except where that configuration lives."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("GIT_") or k in _LOCATION_ENV}


# --- the forwarded values ------------------------------------------------------

_lock = threading.Lock()
_forward: dict[tuple, str] = {}


def _quote(value: str) -> str:
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + out.replace("\n", "\\n").replace("\t", "\\t") + '"'


def render(entries: Sequence[tuple[str, str | None]]) -> str:
    """`(key, value)` pairs as a config file, grouped per section in order.

    A key's subsection is everything between its first and last dot, kept
    verbatim (it is case sensitive); a value of None is a bare boolean key.
    Entries git could not write back faithfully — a newline in a subsection —
    are dropped rather than mangled.
    """
    lines: list[str] = []
    current = None
    for key, value in entries:
        first, last = key.find("."), key.rfind(".")
        if first <= 0 or last == len(key) - 1:
            continue
        section, var = key[:first], key[last + 1:]
        sub = key[first + 1:last] if last > first else None
        if sub is not None and "\n" in sub:
            continue
        header = (section, sub)
        if header != current:
            lines.append(f"[{section}]" if sub is None else
                         f'[{section} "{sub.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"]')
            current = header
        lines.append(f"\t{var}" if value is None else f"\t{var} = {_quote(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def parse_null(raw: bytes) -> list[tuple[str, str | None]]:
    """`git config --null --get-regexp` output: `key\\nvalue\\0`, or `key\\0`
    for a key with no value."""
    out = []
    for item in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not item:
            continue
        key, sep, value = item.partition("\n")
        out.append((key, value if sep else None))
    return out


def _read_protected(git: str) -> list[tuple[str, str | None]]:
    """The allowlisted values from the operator's system and global files.

    Read outside any repository (`cwd=/`) so a repository's own file cannot
    masquerade as protected configuration. Reading configuration runs nothing.
    A file that cannot be read forwards nothing: the hardened call then runs
    without those values, as bare git would have failed on the same file.
    """
    env = _user_env()
    scopes = ["--global"]
    if not env.get("GIT_CONFIG_NOSYSTEM"):
        scopes.insert(0, "--system")
    entries: list[tuple[str, str | None]] = []
    for scope in scopes:
        try:
            p = subprocess.run([git, "config", scope, "--includes", "--null", "--get-regexp", FORWARD_KEYS],
                               cwd="/", env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if p.returncode == 0:
            entries += parse_null(p.stdout)
    return entries


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass


def forward_file(git: str | None = None) -> str:
    """The private file that stands in for the operator's global configuration.

    One per process and per configuration location: tests and long-running
    servers that change HOME get a fresh one. `os.devnull` when there is
    nothing to forward, which is the common case and writes nothing.
    """
    git = binary(git)
    key = (git, os.environ.get("HOME"), os.environ.get("XDG_CONFIG_HOME"),
           *(os.environ.get(name) for name in _LOCATION_ENV))
    with _lock:
        cached = _forward.get(key)
        if cached and (cached == os.devnull or os.path.exists(cached)):
            return cached
        text = render(_read_protected(git))
        if not text:
            _forward[key] = os.devnull
            return os.devnull
        directory = tempfile.mkdtemp(prefix="observatory-git-")  # paths-check: allow — removed at exit by _cleanup, and at once if the write fails
        path = os.path.join(directory, "forwarded.gitconfig")
        try:
            os.chmod(directory, 0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write(text)
        except BaseException:
            # A full disk fails the write; without this the folder stayed behind on
            # every call of a long-running server, since _cleanup was not yet registered.
            shutil.rmtree(directory, ignore_errors=True)
            raise
        atexit.register(_cleanup, path)
        _forward[key] = path
        return path


# --- identity for commits ------------------------------------------------------

_IDENT = re.compile(r"^(.*) <(.*)> \d+ [+-]\d{4}$")


def identity(repo: str | os.PathLike | None, git: str | None = None) -> dict[str, str]:
    """`GIT_AUTHOR_*` / `GIT_COMMITTER_*` as the operator's git would commit them
    in `repo`, or the subset it could resolve. Variables the caller already set
    are left alone, as git itself would honour them first."""
    git = binary(git)
    out: dict[str, str] = {}
    for role in ("AUTHOR", "COMMITTER"):
        if os.environ.get(f"GIT_{role}_NAME") and os.environ.get(f"GIT_{role}_EMAIL"):
            continue
        argv = [git, *(["-C", str(repo)] if repo is not None else []), "var", f"GIT_{role}_IDENT"]
        try:
            p = subprocess.run(argv, env=_user_env(), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        m = _IDENT.match(p.stdout.strip()) if p.returncode == 0 else None
        if m:
            out.setdefault(f"GIT_{role}_NAME", m.group(1))
            out.setdefault(f"GIT_{role}_EMAIL", m.group(2))
    return out


# --- composing a call ----------------------------------------------------------

def environment(*, write: bool = False, extra: Mapping[str, str] | None = None,
                git: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in KEEP_ENV}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": forward_file(git),
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": _TRUE,
        "SSH_ASKPASS": _TRUE,
        "SSH_ASKPASS_REQUIRE": "never",
        "GIT_SSH_COMMAND": SSH_COMMAND,
        "GIT_PAGER": "cat",
        "GIT_EDITOR": ":",
        "GIT_SEQUENCE_EDITOR": ":",
    })
    if not write:
        # The same as `--no-optional-locks`: a read never rewrites the index.
        env["GIT_OPTIONAL_LOCKS"] = "0"
    if extra:
        env.update(extra)
    return env


def driver_overrides(location: Sequence[str], env: Mapping[str, str], git: str,
                     timeout: float | None, cwd: str | os.PathLike | None = None) -> list[str]:
    """`-c` arguments blanking every filter and disabling every config hook the
    repository at `location` (`["-C", path]` or `[]`) names.

    Raises GitConfigUnreadable when the configuration cannot be read: a command
    whose drivers are unknown is not run.
    """
    # Keys AND values are read (no `--name-only`, which Git before 2.22 lacks)
    # and only the keys are used.
    p = subprocess.run([git, *location, "config", "--null", "--get-regexp", _DRIVER_KEYS],
                       cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=timeout)
    if p.returncode not in (0, 1):
        stderr = p.stderr.decode("utf-8", "replace").strip()
        raise GitConfigUnreadable(f"git configuration could not be read: {stderr[:200] or p.returncode}",
                                  p.returncode, stderr)
    options: list[str] = []
    hooks: list[str] = []
    for key, _ in parse_null(p.stdout):
        if key.startswith("filter."):
            options += ["-c", key + ("=false" if key.endswith(".required") else "=")]
        else:
            name = key[len("hook."):key.rfind(".")]
            if name not in hooks:
                hooks.append(name)
    for name in hooks:
        # `enabled=false` where Git knows it; an empty `event` resets the list of
        # events the hook is attached to, which covers a Git that does not.
        options += ["-c", f"hook.{name}.enabled=false", "-c", f"hook.{name}.event="]
    return options


def command(args: Sequence[str], *, repo: str | os.PathLike | None = None,
            cwd: str | os.PathLike | None = None, write: bool = False,
            extra_env: Mapping[str, str] | None = None, git: str | None = None,
            timeout: float | None = 60) -> tuple[list[str], dict[str, str]]:
    """`(argv, env)` for one hardened git call, for callers that run it their own
    way (with retries, say). `args[0]` is the subcommand. `repo` becomes `-C`;
    `cwd` is used only to read the configuration from the same place."""
    git = binary(git)
    args = list(args)
    env = environment(write=write, extra=extra_env, git=git)
    sub = args[0] if args else ""
    if write and sub in COMMITTING:
        for k, v in identity(repo if repo is not None else cwd, git).items():
            env.setdefault(k, v)
    location = ["-C", str(repo)] if repo is not None else []
    drivers: list[str] = []
    if sub not in PURE:
        drivers = driver_overrides(location, env, git, timeout, cwd=cwd)
    if sub in DIFFING:
        args = [sub, "--no-ext-diff", "--no-textconv", *args[1:]]
    return [git, *location, *OPTIONS, *drivers, *args], env


def run(args: Sequence[str], *, repo: str | os.PathLike | None = None,
        cwd: str | os.PathLike | None = None, write: bool = False, timeout: float | None = 60,
        text: bool = True, input: str | bytes | None = None, check: bool = False,
        extra_env: Mapping[str, str] | None = None, git: str | None = None) -> subprocess.CompletedProcess:
    """`subprocess.run` for git, hardened; output is always captured.

    Raises what `subprocess.run` raises (OSError, TimeoutExpired, and
    CalledProcessError with `check=True`). A configuration that cannot be read
    comes back as a failed CompletedProcess carrying git's own message, the same
    shape as any other git failure, so callers keep a single failure path.
    """
    try:
        argv, env = command(args, repo=repo, cwd=cwd, write=write, extra_env=extra_env,
                            git=git, timeout=timeout)
    except GitConfigUnreadable as exc:
        empty: str | bytes = "" if text else b""
        result = subprocess.CompletedProcess([binary(git), *args], exc.returncode, empty,
                                             exc.stderr if text else exc.stderr.encode())
        if check:
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        return result
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=text,
                          input=input, timeout=timeout, check=check,
                          **({} if input is not None else {"stdin": subprocess.DEVNULL}))
