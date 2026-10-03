#!/usr/bin/env python3
"""Redaction on the way INTO agent memory: nothing credential-shaped is stored.

Agent memory is the one place in the engine that stores what agents say about
their work, and agents paste what they see — a connection string out of an
error, a token out of a header. A value stored here would be served back to
every later session that reads the workflow, copied into each handoff pack and
exported with the ledger, so it is replaced before the first write, by two
filters:

1. **Shapes** (`credential_shape.redact`): provider prefixes, AWS ids, JSON web
   tokens, key blocks, passwords in URLs, long hexadecimal and random runs.
   Always on, needs nothing, and generous on purpose: a false redaction costs a
   rewording, a stored key costs a rotation.
2. **Known values**: the workspace's own secrets — the env inventory's
   secret-class variables and the vault's slots (`tools/scan_leaks.py`). This is
   the only filter that catches a value with no recognisable shape. The values
   are held in this process for a few minutes, are never written, and a match
   is replaced by the slot's NAME, which says what was there without being it.

When the known values cannot be read, shapes still run and the report says
`knownValuesChecked: false` — an honest partial, not a silent skip.

A full commit id is 40 hexadecimal characters and is redacted as a shape: cite
a commit by its short form (12 characters), as the handoff's git snapshot does.
"""
from __future__ import annotations

import pathlib
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import credential_shape                                                             # noqa: E402

MARKER = "[redacted]"

#: How long known values stay in memory before they are read again. Long
#: enough that a workflow writing a checkpoint per step does not re-read the
#: vault each time; short enough that a rotated key is known within minutes.
KNOWN_TTL_SECONDS = 300


def credential_like(text: str) -> bool:
    """True when `text` carries any credential shape, UUIDs included."""
    return credential_shape.shaped(text)


def credential_kind(text: str) -> str | None:
    """The kind of the first credential shape in `text` (`uuid`, `hex`, `prefixed`, …)."""
    return credential_shape.find(text)


#: References a shape filter would destroy and that carry no value: a session
#: is named by its UUID, and a commit by a short id. They are kept whole; known
#: secret values inside them are still replaced.
KEEP = re.compile(r"session:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                  r"|commit:[0-9a-f]{7,12}\b")


@dataclass
class Report:
    """What was replaced, by count, kind and field. Never the replaced text."""
    shapes: int = 0
    known: int = 0
    known_checked: bool = False
    known_names: set[str] = field(default_factory=set)
    #: Where text was replaced, as paths (`done[].result`), so the writer knows
    #: what it lost and can restate it without the value.
    fields: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {"shapes": self.shapes, "knownValues": self.known,
                "knownValuesChecked": self.known_checked,
                "knownNames": sorted(self.known_names), "fields": sorted(self.fields)}


def _load_known() -> dict[str, str]:
    """value → name, from the same source the leak scan reads."""
    root = pathlib.Path(__file__).resolve().parent
    for sub in ("tools", "collectors"):
        if str(root / sub) not in sys.path:
            sys.path.insert(0, str(root / sub))
    import scan_leaks                                                               # noqa: E402
    values, _homes, _skipped = scan_leaks.known_values()
    return {v: n for v, n in values.items() if v}


def _sources_fingerprint() -> tuple:
    """What the known values are read from, as (path, mtime, size) stamps.

    A rotation writes a new slot file, and the env inventory is rewritten when
    a scan finds a new value: either changes this, and the cache reloads on the
    next write instead of serving the old values for up to `KNOWN_TTL_SECONDS`.
    Stats only — tens of files, no contents."""
    root = pathlib.Path(__file__).resolve().parent
    for sub in ("tools", "collectors"):
        if str(root / sub) not in sys.path:
            sys.path.insert(0, str(root / sub))
    import paths
    import scan_leaks                                                               # noqa: E402
    stamps = []
    for f in [paths.SCRATCH / "env.json", *sorted(pathlib.Path(scan_leaks.VAULT).glob("*/*/*"))]:
        try:
            st = f.stat()
        except OSError:
            continue
        stamps.append((str(f), st.st_mtime_ns, st.st_size))
    return tuple(stamps)


class _KnownCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = 0.0
        self._values: dict[str, str] | None = None
        self._error: str | None = None
        self._stamp: tuple | None = None

    def get(self, loader: Callable[[], dict[str, str]],
            fingerprint: Callable[[], tuple] | None = None
            ) -> tuple[dict[str, str] | None, str | None]:
        with self._lock:
            try:
                stamp = fingerprint() if fingerprint else None
            except Exception:                                                   # noqa: BLE001
                stamp = None            # unknown sources: fall back to the TTL alone
            if (self._values is None or time.monotonic() - self._at > KNOWN_TTL_SECONDS
                    or (stamp is not None and stamp != self._stamp)):
                self._stamp = stamp
                try:
                    self._values, self._error = loader(), None
                except Exception as exc:                                        # noqa: BLE001
                    # Any failure to read the values is a degradation, not a
                    # crash: the write still gets shape redaction and the
                    # answer says the values were not checked.
                    self._values, self._error = None, type(exc).__name__
                self._at = time.monotonic()
            return self._values, self._error


_CACHE = _KnownCache()


class Redactor:
    """Scrub a JSON-shaped value. One instance per operation, so its report
    counts that operation's replacements alone."""

    def __init__(self, known_loader: Callable[[], dict[str, str]] | None = None,
                 cache: _KnownCache | None = None,
                 fingerprint: Callable[[], tuple] | None = None) -> None:
        # An injected loader gets a private cache: a test's fixture values must
        # never be served to another caller from the process-wide one.
        self._cache = cache or (_KnownCache() if known_loader else _CACHE)
        self._loader = known_loader or _load_known
        self._fingerprint = fingerprint if known_loader else (fingerprint or _sources_fingerprint)

    def scrub(self, value: Any) -> tuple[Any, Report]:
        report = Report()
        known, _error = self._cache.get(self._loader, self._fingerprint)
        report.known_checked = known is not None
        # Longest first, so a value that contains another is replaced whole.
        needles = sorted((known or {}).items(), key=lambda kv: -len(kv[0]))

        def text(s: str, where: str) -> str:
            original = s
            for needle, name in needles:
                if needle in s:
                    report.known += s.count(needle)
                    report.known_names.add(name)
                    s = s.replace(needle, f"[redacted:{name}]")
            # Kept references are set aside while the shape filter runs, then
            # put back: `\x00` never occurs in a value an agent sends as JSON
            # text, and no shape matches it.
            kept: list[str] = []

            def hold(m: re.Match) -> str:
                kept.append(m.group(0))
                return f"\x00{len(kept) - 1}\x00"

            held = KEEP.sub(hold, s)
            cleaned = credential_shape.redact(held, marker=MARKER)
            if cleaned != held:
                report.shapes += cleaned.count(MARKER) - held.count(MARKER)
            cleaned = re.sub("\x00(\\d+)\x00", lambda m: kept[int(m.group(1))], cleaned)
            if cleaned != original:
                report.fields.add(where or "(value)")
            return cleaned

        def walk(v: Any, where: str) -> Any:
            if isinstance(v, str):
                return text(v, where)
            if isinstance(v, list):
                return [walk(x, f"{where}[]") for x in v]
            if isinstance(v, dict):
                return {k: walk(x, f"{where}.{k}" if where else k) for k, x in v.items()}
            return v

        return walk(value, ""), report
