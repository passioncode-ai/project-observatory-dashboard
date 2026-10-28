"""`observatory_secret_request`, the agent's half (OBS-260; docs/design/PERSON-SURFACES.md D4; scenario OSS-84).

The stdio MCP server is a child of the agent's process, so it shows nothing and never touches a value. It files the
request with the node (`POST /mcp` on the engine's local server, tool `secret.request`), presenting the bearer of
the agent's binding (`--agent NAME` on the server's command line, `config/agent-bindings/NAME.token`), and turns
the answer into a `fabric.job.*` job that follows the request to its outcome. The node's refusals come back at once
as typed answers, before any job exists; a node that does not answer is "Observatory is not running", and no window
is shown by anyone.

The job's runner (`tools/run_job.py`, capability `secret.request`) polls `secret.get` until the request is final,
then completes with `output.outcome`: `stored`, `declined`, `no-answer`, `unconfirmed` or `no-client`, never a value.
`fabric.job.cancel` stops the runner, and the runner withdraws the request first (`secret.cancel`), so the person is
not left with a window nobody waits for.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

import paths  # noqa: F401  (resolves the workspace before secret_prompt reads its folders)
import private_io
import secret_prompt as SP

CAPABILITY = "secret.request"
#: How long the stdio server waits for the node to answer one call.
CALL_TIMEOUT_SECONDS = 10
#: The runner's poll, and how long past the request's deadline it keeps asking an unreachable node.
POLL_SECONDS = 2
GRACE_SECONDS = 60
NOT_RUNNING = "Observatory is not running"


class ClientRefused(Exception):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(code)
        self.answer = {"error": code, "message": message, **detail}


def node_url() -> str:
    return f"http://127.0.0.1:{int(os.environ.get('OBSERVATORY_SERVER_PORT', '47311'))}/mcp"


def agent_name() -> str | None:
    name = os.environ.get("OBSERVATORY_AGENT") or None
    return name if name and SP.AGENT_NAME.match(name) else None


def _token(name: str) -> str:
    try:
        return private_io.read(SP.token_path(name)).strip()
    except FileNotFoundError:
        raise ClientRefused("no-agent-binding", f"the agent binding {name!r} has no token file; the person "
                            f"reconnects it with `project-observatory full agent-binding issue {name}`") from None
    except (OSError, RuntimeError):
        raise ClientRefused("no-agent-binding", "the agent binding's token file must be owner-only and reached "
                            "through no link") from None


def call(tool: str, arguments: dict, *, timeout: float = CALL_TIMEOUT_SECONDS) -> dict:
    """One `tools/call` on the node; the structured answer. Raises ClientRefused when the node cannot be asked."""
    name = agent_name()
    if name is None:
        raise ClientRefused("no-agent-binding", "this MCP server was started without an agent binding, so the node "
                            "cannot name who asks; the person connects the agent with `project-observatory full "
                            "agent-binding issue NAME` and adds `--agent NAME` to this server's command line")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode("utf-8")
    req = urllib.request.Request(node_url(), data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _token(name)})
    # Loopback only, never through a proxy an environment variable names.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            doc = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ClientRefused("binding-refused", f"the node does not know the agent binding {name!r}, or it was "
                                f"revoked; the person reconnects it with `project-observatory full agent-binding "
                                f"issue {name}`") from None
        raise ClientRefused("node-error", f"the node answered HTTP {exc.code}") from None
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        raise ClientRefused("not-running", NOT_RUNNING, step="the person starts the local server: python "
                                 "\"$(project-observatory full-path)/tools/serverd.py\" --install; nothing was "
                                 "filed and no window was shown") from None
    except ValueError:
        raise ClientRefused("node-error", "the node's answer is not JSON") from None
    result = doc.get("result") if isinstance(doc, dict) else None
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if not isinstance(structured, dict):
        raise ClientRefused("node-error", "the node's answer carries no structured content")
    return structured


def request(arguments: dict, span_record: dict) -> dict:
    """File the request; a refusal is returned as a typed answer, a filing as a job handle."""
    import jobs
    try:
        answer = call("secret.request", arguments)
    except ClientRefused as exc:
        return exc.answer
    if "error" in answer:
        return answer
    filed = answer["request"]
    try:
        doc, _ = jobs.start(CAPABILITY, span_record, exclusive=False,
                            arguments={"requestId": filed["requestId"], "slot": filed["slot"],
                                       "agent": filed["agent"]})
    except BaseException:
        # No job will follow this request, so it is withdrawn before the error goes on: the person is never shown a
        # window nobody waits for (the review of #378). A joined request belongs to the job already following it.
        if not answer.get("joined"):
            withdraw(filed["requestId"])
        raise
    return {"job": jobs.view(doc), "requestId": filed["requestId"], "agent": filed["agent"],
            "slot": filed["slot"], "replaces": filed["replaces"], "joined": bool(answer.get("joined")),
            "deadline": filed["deadline"]}


def withdraw(request_id: str) -> None:
    """Best effort: `secret.cancel` on a request this server filed; a node that cannot be asked keeps its deadline."""
    try:
        call("secret.cancel", {"requestId": request_id}, timeout=3)
    except ClientRefused:
        pass


def follow(job: dict) -> dict:
    """The runner's work: poll the request to its outcome and return the result envelope."""
    import interop
    import jobs
    span = interop.Span.from_record(job["trace"])
    args = job["arguments"]
    started = time.monotonic()
    give_up = None
    jobs.update(job["id"], statusMessage="waiting for the person to answer in Observatory's window")
    try:
        while True:
            try:
                answer = call("secret.get", {"requestId": args["requestId"]})
            except ClientRefused as exc:
                if exc.answer["error"] != "not-running":
                    # A refused binding or a node error ends this runner: the request is withdrawn first, so the
                    # window does not wait for a job that is gone (the review of #378).
                    withdraw(args["requestId"])
                    raise RuntimeError(exc.answer["message"]) from None
                give_up = give_up or (time.monotonic() + SP.MAX_DEADLINE_SECONDS + GRACE_SECONDS)
                if time.monotonic() > give_up:
                    raise RuntimeError("Observatory stopped answering before the request ended") from None
                time.sleep(POLL_SECONDS)
                continue
            if "error" in answer:
                raise RuntimeError(answer.get("message") or answer["error"])
            r = answer["request"]
            if r["state"] != SP.OPEN:
                break
            time.sleep(POLL_SECONDS)
    except SystemExit:
        # fabric.job.cancel: withdraw the request before this runner stops, so no window waits for nobody.
        withdraw(args["requestId"])
        raise
    output = {"outcome": r["outcome"], "message": r["message"], "requestId": r["requestId"], "agent": r["agent"],
              "slot": r["slot"], "replaces": r["replaces"], "answeredAt": r.get("answeredAt")}
    for key in ("why", "step"):
        if r.get(key):
            output[key] = r[key]
    if r["outcome"] == "stored":
        project, env, name = r["slot"].split("/")
        output["use"] = (f"python \"$(project-observatory full-path)/tools/use_secret.py\" run --vault-only "
                         f"--env {env} {project} {name} -- COMMAND")
    return interop.envelope(
        job_id=job["id"], capability=CAPABILITY, span=span, output=output,
        done=[{"claimId": "SECRET-REQUEST", "statement": f"the request for {r['slot']} ended: {r['outcome']}"}],
        proof=[], not_verified=([] if r["outcome"] == "stored" else
                                [{"claim": f"{r['slot']} holds a value", "reason": r["message"]}]),
        write_scopes=[], wall_ms=int((time.monotonic() - started) * 1000), created_at=jobs.now_iso())
