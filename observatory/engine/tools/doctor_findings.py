#!/usr/bin/env python3
"""What the daily doctor owes the operator (OBS-558, CP-15, `tools/daily_doctor.py`).

- `doctor.backup_stale`: the newest workspace backup is older than 48 hours (or none is recorded), with the last
  snapshot failure's reason. The doctor never takes a snapshot itself: one stops the tick and the server.
- `doctor.step_failing`: a maintenance step failed on its last passes in a row, one row per step.
- `doctor.vault_meta_unreadable`: vault slot metadata that does not parse; names the slots, never a value.
- `doctor.fix_failed`: a safe fix the doctor could not apply (a file it could not move aside).

A check an existing rule already raises (`scan.stale`, `lifecycle.stale_servers`, `harness.plugin_behind`) is named
by that rule in the doctor's record and gets no second row here. Silent until the doctor has run once.
"""
from __future__ import annotations
import json
import pathlib
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

TYPES = ("doctor.backup_stale", "doctor.step_failing", "doctor.vault_meta_unreadable", "doctor.fix_failed")
EVIDENCE = ["store/state/doctor.json"]
UNDO = "`project-observatory full doctor-daily --undo {journal_id}`"


def findings(doc: dict | None) -> list[dict]:
    if not isinstance(doc, dict):
        return []
    day = str(doc.get("at") or "")[:10]
    out: list[dict] = []
    for c in doc.get("checks") or []:
        if not isinstance(c, dict):
            continue
        kind = c.get("finding")
        if kind == "doctor.backup_stale":
            failure = c.get("last_failure")
            out.append({
                "type": "doctor.backup_stale", "subject": "doctor:backup", "severity": "warning",
                **(titled("The newest workspace backup is {hours} hours old", hours=c["hours"])
                   if c.get("hours") is not None else titled("No workspace backup is recorded")),
                "detail": f"Checked by the daily doctor on {day}: {c.get('detail')}."
                          + (f" The last snapshot attempt failed: {failure}." if failure else ""),
                "action": "`project-observatory full maintain status` shows why; `project-observatory full maintain "
                          "run` tries the daily snapshot now (it stops the tick and the server for the copy)",
                "evidence": EVIDENCE + ["store/maintenance.json"]})
        elif kind == "doctor.step_failing":
            for s in c.get("steps") or []:
                out.append({
                    "type": "doctor.step_failing", "subject": f"doctor:step/{s['step']}", "severity": "warning",
                    **titled("The maintenance step {step} failed on its last {n} passes",
                             step=s["step"], n=s["passes"]),
                    "detail": f"Failing since {s.get('since')}"
                              + (f"; the last reason: {s['detail']}" if s.get("detail") else "") + ".",
                    "action": "`project-observatory full maintain status` shows the step's last answer; fix its "
                              "cause, the next pass clears this row",
                    "evidence": EVIDENCE + ["store/maintenance.json"], "step": s["step"]})
        elif kind == "doctor.vault_meta_unreadable":
            names = ", ".join(c.get("items") or [])
            more = int(c.get("count") or 0) - len(c.get("items") or [])
            out.append({
                "type": "doctor.vault_meta_unreadable", "subject": "doctor:vault-meta", "severity": "warning",
                **titled("{n} vault metadata files cannot be read", n=int(c.get("count") or 0)),
                "detail": f"These slots' `.meta.json` does not parse ({day}): {names}"
                          + (f" and {more} more" if more > 0 else "")
                          + ". Their expiry, source and consumers are unknown to every reader.",
                "action": "record the slot's metadata again through its door (`vault.py put` with its source), or "
                          "move the broken file aside if the slot is retired",
                "evidence": EVIDENCE, "slots": c.get("items") or []})
        elif kind == "doctor.fix_failed":
            jid = (c.get("fix") or {}).get("journal_id")
            out.append({
                "type": "doctor.fix_failed", "subject": f"doctor:fix/{c['id']}", "severity": "info",
                **titled("The daily doctor could not apply the fix {check}", check=c["id"]),
                "detail": f"{c.get('title')} ({day}): "
                          + ", ".join(f"{f['file']} ({f['reason']})" for f in c.get("failed") or [])
                          + (f". What it did move is journalled as {jid}." if jid else "."),
                "action": "remove or move the named files by hand; " + (UNDO.format(journal_id=jid) + " puts the moved "
                                                                        "ones back" if jid else "nothing was moved"),
                "evidence": EVIDENCE + ["store/logs/doctor.jsonl"]})
    return out


def from_file(path: pathlib.Path) -> list[dict]:
    try:
        return findings(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
