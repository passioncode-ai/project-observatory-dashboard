#!/usr/bin/env python3
"""What the machine's SSH keys owe the operator (OSS-66, docs/design/SSH-KEYS.md).

Reads the inventory `tools/ssh_keys.py list` saves to `store/raw/ssh-keys.json`. Silent when
it was never taken: an absent inventory is not a clean one, and `ssh-keys findings` says so.
One row per key, because each is one fix. A row names the key by fingerprint and path and
never quotes a private byte (the inventory holds none).

"Unused" is an ESTIMATE: ssh reads a private key when it uses it, which moves the file's access
time where the file system keeps one; that is the only local trace. "Unregistered" means no
place this machine can SEE: not on GitHub, in no Host block, not loaded. A server's
authorized_keys is invisible from here, so that row is info, never a warning.

NO TIMESTAMP INSIDE A FINDING beyond the inventory's own: the same inventory gives the same rows.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

import datetime
import json
import pathlib

CMD = "project-observatory full ssh-keys"
UNUSED_DAYS = 180
EVIDENCE = ["store/raw/ssh-keys.json"]
TYPES = ("ssh.key_no_passphrase", "ssh.key_weak", "ssh.key_readable", "ssh.folder_permissions",
         "ssh.key_unused", "ssh.github_orphan", "ssh.key_unregistered", "ssh.public_missing",
         "ssh.identity_missing")


def _when(text) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _subject(k: dict) -> str:
    return f"ssh:{k.get('fingerprint') or k.get('path')}"


def _weak(k: dict) -> str | None:
    """The severity of a weak key, or None. DSA is gone from OpenSSH; RSA under 3072 bits is
    below today's recommendation, under 2048 below what most servers still accept."""
    kind, bits = k.get("type"), k.get("bits") or 0
    if kind == "DSA":
        return "critical"
    if kind == "RSA" and bits and bits < 2048:
        return "critical"
    if kind == "RSA" and bits and bits < 3072:
        return "warning"
    return None


def _mode(text) -> int:
    try:
        return int(str(text), 8)
    except ValueError:
        return 0


def findings(doc: dict | None) -> list[dict]:
    if not isinstance(doc, dict) or doc.get("schema") != "ssh-keys/1":
        return []
    out: list[dict] = []
    measured = _when(doc.get("measured_at"))
    github_read = isinstance(doc.get("github"), dict)
    folder = doc.get("folder") or {}
    if folder.get("exists") and _mode(folder.get("mode")) & 0o077:
        out.append({"type": "ssh.folder_permissions", "subject": "ssh:~/.ssh", "severity": "warning",
                    **titled("the SSH folder is open to other accounts"),
                    "detail": f"~/.ssh has mode {folder.get('mode')}: other accounts can list which keys "
                              f"exist, and sshd refuses keys in a folder others can write.",
                    "action": "`chmod 700 ~/.ssh`", "evidence": EVIDENCE})
    for k in doc.get("keys") or []:
        name, path, subject = k.get("name"), k.get("path"), _subject(k)
        if k.get("private") and k.get("passphrase") == "no":
            out.append({"type": "ssh.key_no_passphrase", "subject": subject, "severity": "warning",
                        **titled("{key} has no passphrase", key=name),
                        "detail": f"{path} is stored in clear: anyone who copies the file can use it. "
                                  f"Read from its header, without decrypting it.",
                        "action": f"`ssh-keygen -p -f {path}` adds one (it asks for it); on macOS then "
                                  f"`ssh-add --apple-use-keychain {path}` keeps it in the Keychain",
                        "evidence": EVIDENCE})
        severity = _weak(k)
        if severity:
            kind = "DSA" if k.get("type") == "DSA" else f"RSA {k.get('bits')}-bit"
            out.append({"type": "ssh.key_weak", "subject": subject, "severity": severity,
                        **titled("{key} is a weak {kind} key", key=name, kind=kind),
                        "detail": f"{path} is {kind}. Current OpenSSH no longer accepts DSA, and RSA "
                                  f"below 3072 bits is under the current recommendation.",
                        "action": f"rotate it: `{CMD} create NEW_NAME`, send the new key where this one is "
                                  f"registered, then remove this one (OSS-67)",
                        "evidence": EVIDENCE})
        if k.get("private") and k.get("private_mode") and _mode(k["private_mode"]) & 0o077:
            out.append({"type": "ssh.key_readable", "subject": subject, "severity": "critical",
                        **titled("{key} is readable by other accounts", key=name),
                        "detail": f"{path} has mode {k['private_mode']}; ssh refuses to use a private key "
                                  f"others can read, and others can copy it.",
                        "action": f"`chmod 600 {path}`", "evidence": EVIDENCE})
        last = _when(k.get("last_access"))
        if (k.get("private") and k.get("in_agent") is False and measured and last
                and (measured - last).days > UNUSED_DAYS):
            out.append({"type": "ssh.key_unused", "subject": subject, "severity": "info",
                        **titled("{key} has not been used for a long time", key=name),
                        "detail": f"{path} was last read on {k['last_access'][:10]} by the file's access "
                                  f"time, an estimate, and it is not loaded in the agent.",
                        "action": "if nothing needs it, remove its registrations and move the file aside",
                        "evidence": EVIDENCE})
        if (github_read and k.get("github") == [] and k.get("public") and not k.get("hosts")
                and k.get("in_agent") is False):
            out.append({"type": "ssh.key_unregistered", "subject": subject, "severity": "info",
                        **titled("{key} is registered nowhere this machine can see", key=name),
                        "detail": f"{path} is not on GitHub, in no Host block of ~/.ssh/config and not "
                                  f"loaded. A server's authorized_keys is not visible from here.",
                        "action": f"send it where it is needed (`{CMD} send {name} --to github`), or move "
                                  f"it aside if nothing uses it",
                        "evidence": EVIDENCE})
        if k.get("private") and not k.get("public"):
            out.append({"type": "ssh.public_missing", "subject": subject, "severity": "info",
                        **titled("{key} has no public half", key=name),
                        "detail": f"{path} has no {path}.pub, so its fingerprint is unknown and it cannot "
                                  f"be copied or sent. Observatory does not derive it: that decrypts the key.",
                        "action": f"`ssh-keygen -y -f {path} > {path}.pub` recreates it (it asks for the "
                                  f"passphrase)",
                        "evidence": EVIDENCE})
    for f in doc.get("identity_files") or []:
        if f.get("exists") is False:
            out.append({"type": "ssh.identity_missing", "subject": f"ssh:{f.get('path')}", "severity": "warning",
                        **titled("{host} names a missing key file", host=f.get("host") or "*"),
                        "detail": f"~/.ssh/config gives Host {f.get('host')} the IdentityFile {f.get('path')}, "
                                  f"which does not exist; ssh skips it and tries other keys.",
                        "action": "point the IdentityFile at an existing key, or remove the line",
                        "evidence": EVIDENCE})
    if github_read:
        for g in doc["github"].get("keys") or []:
            if g.get("on_machine") is False:
                out.append({"type": "ssh.github_orphan", "subject": f"ssh:{g.get('fingerprint')}",
                            "severity": "warning",
                            **titled("GitHub accepts a key this machine does not hold: {title}",
                                     title=g.get("title") or g.get("fingerprint")),
                            "detail": f"GitHub key {g.get('id')} ({g.get('use')}, added {g.get('added')}) "
                                      f"matches no key on this disk or in the agent.",
                            "action": f"`gh ssh-key delete {g.get('id')}` if no other machine uses it",
                            "evidence": EVIDENCE})
    return out


def from_file(path: pathlib.Path) -> list[dict]:
    try:
        return findings(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
