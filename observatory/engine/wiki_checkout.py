#!/usr/bin/env python3
"""Whether this machine's wiki checkout takes the registry projection (OBS-516).

A wiki may be checked out on several machines with one of them writing: the
operator's wiki is written on one Mac and read on another, where a job
fast-forwards a read mirror from `origin` and stops updating it the moment its
tree turns dirty. A projection written there stops that mirror.

`features.wiki_projection` off in the workspace settings keeps the tick from
projecting on that machine. This is the second guard, set on the checkout
itself by whoever makes it a mirror, so a workspace whose settings forgot the
feature still leaves the mirror alone:

    git -C <wiki> config observatory.wikiMirror true
"""
from __future__ import annotations

import os

import safe_git

MIRROR_KEY = "observatory.wikiMirror"


def mirror(repo: str | os.PathLike) -> str | None:
    """The reason the checkout must not be written, or None. Reads the checkout's
    configuration only; an unreadable configuration is not a mirror, because the
    workspace setting is the guard that decides on its own."""
    try:
        p = safe_git.run(["config", "--bool", "--get", MIRROR_KEY], cwd=repo, timeout=30)
    except (OSError, ValueError):
        return None
    if p.returncode == 0 and p.stdout.strip() == "true":
        return (f"the wiki checkout is a read mirror ({MIRROR_KEY}=true): another machine "
                f"writes the wiki, and a projection here would stop the mirror")
    return None
