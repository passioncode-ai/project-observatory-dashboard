#!/usr/bin/env python3
"""Run one job to its end: `run_job.py <job-id>`. Started detached by `jobs.start`.

The MCP server that accepted the job may exit at any moment — a stdio server
lives as long as one host session — so the work runs here, in its own session,
and reports only through the job's record: a heartbeat while it works, then the
result envelope or an error. A record that is already terminal (a caller
cancelled it) is never overwritten; SIGTERM, which is what a cancel sends to
this process group, ends the work and whatever it started.

The one job today is `machine.mcp.refresh`: take a new MCP inventory with the
collector the tick runs, then answer `machine.mcp.inventory` from it. The
collector receives the job's trace as `TRACEPARENT`/`TRACESTATE`, the
environment carrier OpenTelemetry defines for a child process, on a child span
of the job's (fabric-interop/0.1, C3.4 b).
"""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import interop                                                      # noqa: E402
import jobs                                                         # noqa: E402
import paths                                                        # noqa: E402

#: Upper bound for the whole refresh: the collector's own probe limit plus room to
#: read the configs. The collector enforces the probe's; this bounds the rest.
REFRESH_LIMIT_SECONDS = 3600 + 120


def _heartbeat(job_id: str, stop: threading.Event) -> None:
    while not stop.wait(jobs.HEARTBEAT_SECONDS):
        jobs.update(job_id, heartbeatAt=jobs.now_iso())


def refresh(job: dict) -> dict:
    """`machine.mcp.refresh`: scan, then read the new inventory back. Returns the envelope."""
    import mcp_inventory
    span = interop.Span.from_record(job["trace"])
    started = time.monotonic()
    out = paths.SCRATCH / "mcp.json"
    env = {**os.environ, **span.child().env()}
    import configuration
    if not configuration.enabled("mcp"):
        # The collector would print "not configured" and write nothing; the
        # inventory says the integration is disabled. No process to start.
        answer = mcp_inventory.inventory()
        return interop.envelope(job_id=job["id"], capability=job["capability"], span=span, output=answer,
                                done=[{"claimId": "MCP-INVENTORY", "statement": "took no inventory: the mcp "
                                                                                "integration is disabled"}],
                                proof=[], not_verified=[{"claim": f"{d['source']} is covered", "reason": d["reason"]}
                                                        for d in answer.get("degraded", [])],
                                write_scopes=[], wall_ms=int((time.monotonic() - started) * 1000),
                                created_at=jobs.now_iso())
    jobs.update(job["id"], statusMessage="reading agent configs and probing their MCP servers")
    try:
        proc = subprocess.run([sys.executable, str(ROOT / "collectors/scan_mcp.py"), str(out)],
                              cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=REFRESH_LIMIT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the MCP scan did not finish within {REFRESH_LIMIT_SECONDS} s") from None
    if proc.returncode != 0:
        # The collector's own output is names and counts, but a traceback could
        # quote anything: only the exit status leaves this process.
        raise RuntimeError(f"the MCP scan exited with status {proc.returncode}")
    answer = mcp_inventory.inventory()
    wall_ms = int((time.monotonic() - started) * 1000)
    agents = sum(1 for s in answer.get("sources", []) if s.get("state") == "read")
    done = [{"claimId": "MCP-INVENTORY",
             "statement": f"took a new MCP inventory: {len(answer['servers'])} server(s) declared "
                          f"across {agents} readable agent config(s)"}]
    proof = []
    if answer.get("inventoryAt"):
        proof.append({"claimIds": ["MCP-INVENTORY"], "kind": "observatory.mcp-inventory",
                      "uri": "observatory://machine/mcp-inventory",
                      "producer": interop.producer(), "capturedAt": answer["inventoryAt"],
                      "classification": "project-internal"})
    not_verified = [{"claim": f"{d['source']} is covered", "reason": d["reason"]}
                    for d in answer.get("degraded", [])]
    return interop.envelope(job_id=job["id"], capability=job["capability"], span=span, output=answer,
                            done=done, proof=proof, not_verified=not_verified,
                            write_scopes=["observatory:store/raw/mcp.json"], wall_ms=wall_ms,
                            created_at=jobs.now_iso())


from agent import assistant
WORK = {"machine.mcp.refresh": refresh, "agent.ask": assistant.run_question}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not jobs.JOB_ID.match(argv[1]):
        print("usage: run_job.py <job-id>", file=sys.stderr)
        return 2
    job_id = argv[1]
    job = jobs.get(job_id)
    if job is None or job.get("status") in jobs.TERMINAL:
        return 0
    work = WORK.get(job.get("capability"))
    if work is None:
        jobs.update(job_id, status="failed", statusMessage="no runner for this capability",
                    error={"code": "unknown-capability", "message": str(job.get("capability"))})
        return 1

    def stop(signum, frame):                                          # noqa: ARG001
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    def timeout(signum, frame):
        raise assistant.AssistantError("assistant-timeout")
    if job.get("capability") == "agent.ask":
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(300)
    beat = threading.Event()
    threading.Thread(target=_heartbeat, args=(job_id, beat), daemon=True).start()
    try:
        result = work(job)
    except SystemExit:
        # A cancel: the record already says `cancelled`, and `update` leaves it.
        jobs.update(job_id, status="cancelled", statusMessage="stopped")
        return 0
    except Exception as exc:                                          # noqa: BLE001
        # Our own RuntimeErrors are sentences written above; anything else is
        # named by its kind only, since a message can quote a path or a value.
        message = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        jobs.update(job_id, status="failed", statusMessage="the work failed",
                    error={"code": str(exc) if isinstance(exc, assistant.AssistantError) else "job-failed", "message": message[:300]})
        return 1
    finally:
        beat.set()
        signal.alarm(0)
    jobs.update(job_id, status="completed", statusMessage="completed", result=result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
