"""The one environment every test's git runs under.

Two things, together:

1. **The operator's configuration is shut out.** `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL`
   (`os.devnull` unless the test names a file of its own, or `None` to leave it to the test's
   temporary `HOME`), so a hook, signer or filter on the machine running the suite never runs.
2. **Git starts no background maintenance.** Git runs `gc --auto` and `maintenance run --auto`
   by itself after `commit`, `fetch`, `merge`, `am` and `rebase`, detached; the work outlives
   the command, and in a temporary repository it races the test's cleanup. On 2026-10-20
   check.yml failed with "OSError: directory not empty" while a test removed its `.git` that a
   detached gc was still writing, and the release guard refused v0.27.0. `gc.auto=0` and
   `maintenance.auto=false` are set through `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` /
   `GIT_CONFIG_VALUE_n` (git 2.31 and later; an older git ignores them), appended after any
   entries the caller already set, so a caller's own entries are kept and these two are the
   last of them on their keys. Git ranks `-c` above `GIT_CONFIG_COUNT`, and a git started by
   another git (a hook, say) inherits that one's `-c` options as `GIT_CONFIG_PARAMETERS`: it is
   dropped here, so only a `-c` written on the test's own command line can still override these
   two, and that is the test's own choice.

The portable runner's `clean_env` uses it, so a suite inheriting the runner's environment is
covered, and check.yml's workflow-level `env:` carries the same two entries for every CI step,
the root tests included (`unittest discover -s tests` runs outside the runner). A test that
builds its own environment calls `git_env(...)`. The repository's root test
`tests/test_git_background_maintenance.py` refuses a test that sets or removes a `GIT_CONFIG_*`
variable itself, strips every `GIT_*` variable by hand, or hands git an environment built as a
literal dict. Standard library only: `run_portable.py` loads this file by its path.
"""
from __future__ import annotations

import os
from typing import Mapping

#: The settings, in the order they are appended.
QUIET: tuple[tuple[str, str], ...] = (("gc.auto", "0"), ("maintenance.auto", "false"))

_UNSET = object()


def quiet_maintenance(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of `env` whose `GIT_CONFIG_COUNT` entries end with `QUIET`.

    Entries already there are kept. A key whose last entry already holds our value is not
    repeated, so composing twice adds nothing. A count git itself would refuse (not a
    non-negative integer) raises ValueError rather than being overwritten. An inherited
    `GIT_CONFIG_PARAMETERS` (an outer git's `-c` options, which git ranks above these entries)
    is dropped.
    """
    out = dict(env)
    out.pop("GIT_CONFIG_PARAMETERS", None)
    raw = out.get("GIT_CONFIG_COUNT", "").strip()
    try:
        count = int(raw) if raw else 0
    except ValueError:
        raise ValueError(f"GIT_CONFIG_COUNT is not a number: {raw!r}") from None
    if count < 0:
        raise ValueError(f"GIT_CONFIG_COUNT is negative: {raw!r}")
    last: dict[str, str | None] = {}
    for i in range(count):
        key = out.get(f"GIT_CONFIG_KEY_{i}")
        if key is not None:
            last[key.lower()] = out.get(f"GIT_CONFIG_VALUE_{i}")
    for key, value in QUIET:
        if last.get(key) == value:
            continue
        out[f"GIT_CONFIG_KEY_{count}"] = key
        out[f"GIT_CONFIG_VALUE_{count}"] = value
        last[key] = value
        count += 1
    out["GIT_CONFIG_COUNT"] = str(count)
    return out


def git_env(base: Mapping[str, str] | None = None, *, global_config: str | None | object = _UNSET,
            drop_git: bool = False, **extra: str) -> dict[str, str]:
    """`base` (the process environment by default) ready for a test's git.

    `global_config`: the file git reads as the global configuration; `os.devnull` by default,
    a path for a test that plants one, `None` to leave `GIT_CONFIG_GLOBAL` as `base` has it (a
    test whose temporary `HOME` holds the file). `drop_git` removes every inherited `GIT_*`
    variable first. `extra` is added last, before the maintenance settings are composed.
    """
    env = dict(os.environ if base is None else base)
    if drop_git:
        env = {k: v for k, v in env.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if global_config is _UNSET:
        env["GIT_CONFIG_GLOBAL"] = os.devnull
    elif global_config is not None:
        env["GIT_CONFIG_GLOBAL"] = str(global_config)
    env.update(extra)
    return quiet_maintenance(env)
