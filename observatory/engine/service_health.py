#!/usr/bin/env python3
"""The cheap half of `/health`: what the well-known document says about the estate.

`GET /.well-known/fabric-service` must answer from memory in under 100 ms
(fabric-service/0.1). `/health` recomputes the remote summary, the leak
register, the skill versions and the tick verdict on every request and answers
about 14 KB, so the well-known route never calls it. Instead the server's
heartbeat thread calls `snapshot()` once per cycle and the route serves the
result it left behind.

What a snapshot holds:

- `degraded` — every reason the estate's picture is partial, read the way every
  other surface reads it: `degradations.every_collector()` for the collectors
  (plus `model.json`, whose rows have a tailored reader on the board but are
  still a gap in coverage), `tick_health.degraded()` for a tick that died or
  went quiet, and this module's own reads (a registry or leak register that
  cannot be parsed). One row per source, so a collector that failed on forty
  owners is one line, not forty, and the protocol's 64-row ceiling holds;
- `summary` — at most six tiles for a host's card: projects, open findings,
  open leaks and the last tick. A tile with `attention: true` asks for the
  operator.

Absent is not zero. A registry that has never been built shows "not built", not
0, and a leak register that does not exist shows "not kept"; neither is a
degradation, because nothing failed — nothing has run yet.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading

import configuration
import degradations
import paths
import tick_health

#: The severities that ask for a person. `info` findings are read on purpose,
#: never pushed — the same line `tools/notify_findings.py` draws.
ATTENTION_SEVERITIES = ("critical", "warning")
#: Tick verdicts that mean the data may be older than it looks.
TICK_ATTENTION = ("interrupted", "stale", "running-long")
MAX_DEGRADED = 64
SOURCE_LIMIT = 80
REASON_LIMIT = 300


def _clip(text: object, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _JsonCache:
    """Parse a registry document only when its (mtime, size) moved.

    `projects.json` on a large estate is megabytes; re-reading it every twenty
    seconds to count its rows would make the cheap route the expensive one.
    """

    def __init__(self) -> None:
        self._seen: dict[Path, tuple[tuple[int, int], object]] = {}
        self._lock = threading.Lock()

    def load(self, path: Path):
        """(document, None), (None, None) when absent, or (None, reason) when unreadable."""
        try:
            st = path.stat()
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"cannot be read: {type(exc).__name__}"
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._seen.get(path)
            if hit and hit[0] == key:
                return hit[1], None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, f"cannot be parsed: {type(exc).__name__}"
        with self._lock:
            self._seen[path] = (key, doc)
        return doc, None


CACHE = _JsonCache()


def project_labels() -> dict[str, str]:
    """project id -> its name, for the events feed. Empty when the registry is absent."""
    doc, _why = CACHE.load(paths.REGISTRY / "projects.json")
    out: dict[str, str] = {}
    if isinstance(doc, dict):
        for p in doc.get("projects") or []:
            if isinstance(p, dict) and p.get("id"):
                out[str(p["id"])] = str(p.get("name") or p["id"])
    return out


def _age(stamp: str | None, now: datetime) -> str:
    if not stamp:
        return "never"
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{seconds // 60} min ago"
    if seconds < 36 * 3600:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _collector_rows() -> list[dict]:
    per_file = dict(degradations.every_collector())
    # The merge records every input it did not read, switched off or not; an input
    # whose integration is off was never asked for, and is no gap (0.19.3).
    own = [d for d in degradations.collector("model.json")
           if not (isinstance(d, dict) and degradations.merge_source_off(str(d.get("source") or "")))]
    if own:
        per_file["model.json"] = own
    rows = []
    for name, found in sorted(per_file.items()):
        first = next((r for r in found if isinstance(r, dict)), {})
        reason = str(first.get("reason") or "no reason given")
        where = str(first.get("source") or "").strip()
        head = f"{where}: {reason}" if where and where != name else reason
        more = f" (+{len(found) - 1} more)" if len(found) > 1 else ""
        rows.append({"source": _clip("collector:" + name.removesuffix(".json"), SOURCE_LIMIT),
                     "reason": _clip(f"{len(found)} source(s) not fully read — {head}{more}",
                                     REASON_LIMIT)})
    return rows


def snapshot(leaks: dict, *, extra_degraded: list[dict] | None = None,
             now: datetime | None = None) -> dict:
    """{"degraded": [...], "summary": [...], "tick": {...}, "at": iso}.

    `leaks` is the dict `tools/serverd.py:refresh_leaks` builds for `/health`
    (passed in, so the register is read one way). Never raises: a source that
    fails to read becomes a degraded row, because the well-known document must
    stay truthful rather than absent.
    """
    now = now or datetime.now(timezone.utc)
    degraded: list[dict] = list(extra_degraded or [])
    summary: list[dict] = []

    try:
        degraded += _collector_rows()
    except Exception as exc:  # noqa: BLE001 — a reader failure is itself a gap
        degraded.append({"source": "collectors", "reason": _clip(
            f"collector receipts could not be read: {type(exc).__name__}", REASON_LIMIT)})

    try:
        scheduler = configuration.enabled("scheduler", "features")
        tick = tick_health.health(paths.STATE, paths.SCRATCH, scheduler_enabled=scheduler)
    except Exception as exc:  # noqa: BLE001
        tick = {"verdict": "unknown", "why": f"{type(exc).__name__}: {exc}", "last_finished": None}
    if tick.get("verdict") in TICK_ATTENTION:
        degraded.append({"source": "tick", "reason": _clip(f"{tick['verdict']}: {tick.get('why')}",
                                                           REASON_LIMIT)})
    elif tick.get("verdict") == "unknown":
        degraded.append({"source": "tick", "reason": _clip(f"tick health unknown: {tick.get('why')}",
                                                           REASON_LIMIT)})
    if tick.get("history_warning"):
        # Ticks that keep stopping at their ceiling while the last one happened to finish:
        # the same alert the MCP survey raises, on the card (0.20.1).
        degraded.append({"source": "tick-history", "reason": _clip(tick["history_warning"], REASON_LIMIT)})

    projects, why = CACHE.load(paths.REGISTRY / "projects.json")
    if why:
        degraded.append({"source": "registry", "reason": f"registry/projects.json {why}; projects are not counted"})
        summary.append({"label": "Projects", "value": "unreadable", "attention": True})
    elif isinstance(projects, dict):
        summary.append({"label": "Projects", "value": len(projects.get("projects") or [])})
    else:
        summary.append({"label": "Projects", "value": "not built"})

    findings, why = CACHE.load(paths.REGISTRY / "findings.json")
    if why:
        degraded.append({"source": "findings", "reason": f"registry/findings.json {why}; open findings are not counted"})
        summary.append({"label": "Open findings", "value": "unreadable", "attention": True})
    elif isinstance(findings, dict) and isinstance(findings.get("findings"), list):
        open_ = [f for f in findings["findings"] if isinstance(f, dict)
                 and f.get("severity") in ATTENTION_SEVERITIES and not f.get("acked")]
        summary.append({"label": "Open findings", "value": len(open_), "attention": bool(open_)})
    else:
        summary.append({"label": "Open findings", "value": "not built"})

    if not leaks.get("register"):
        summary.append({"label": "Open leaks", "value": "not kept"})
    elif not leaks.get("readable", True):
        degraded.append({"source": "leak-register", "reason": "the leak register cannot be parsed; open leaks are not counted"})
        summary.append({"label": "Open leaks", "value": "unreadable", "attention": True})
    else:
        count = int(leaks.get("open") or 0)
        summary.append({"label": "Open leaks", "value": count, "attention": count > 0})

    verdict = tick.get("verdict")
    last = "running" if verdict in ("running", "running-long") else _age(tick.get("last_finished"), now)
    if verdict == "disabled":
        last = "scheduler off"
    summary.append({"label": "Last tick", "value": last, "attention": verdict in TICK_ATTENTION})

    if len(degraded) > MAX_DEGRADED:
        kept = degraded[: MAX_DEGRADED - 1]
        kept.append({"source": "degraded", "reason": f"{len(degraded) - len(kept)} more degraded source(s) not listed"})
        degraded = kept
    return {"degraded": degraded, "summary": summary[:6], "tick": tick,
            "at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
