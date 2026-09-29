# Access: who can reach what, and what grants it

Status: the model the code implements as of 0.3.10 (PB-015), with the dashboard server's
fabric-service routes added on branch `agent/fabric-service`. Each rule below names the test that
proves it. A rule without a test is written as a gap, not as a guarantee.

## The principle

Project Observatory runs on one machine for one operator. Reaching it at all means being local, and
**authority comes from something a remote party cannot obtain**:

- a loopback socket;
- a token file at mode 600 in the workspace;
- the operator's own terminal.

Nothing a client *says about itself* grants anything. Headers such as a caller name, a user agent or
a referer are labels at most.

## Surfaces

| Surface | Reached by | Authority | What it can do | Checks |
|---|---|---|---|---|
| Dashboard server (`tools/serverd.py`, 127.0.0.1) | a browser or `curl` on this machine; a local Fabric host | none for the pages and `/.well-known/fabric-service`; the workspace's `service.token` (600) as `Authorization: Bearer` for `/fabric/v1/events` | GET pages, `/health`, `/remote`, `/leaks`, `/skills`, the well-known document, the events feed | loopback bind; `Host` names this server; `Origin`, when sent, is this server; `Sec-Fetch-Site: cross-site` refused; POST answered 405; one copy per workspace (instance lock) — see [FABRIC-SERVICE.md](FABRIC-SERVICE.md) |
| Keyserver (`tools/keyserver.py`, 127.0.0.1) | the dashboard page it serves, or a local client | the workspace token (`X-Observatory-Token`), handed to a page the keyserver itself served, or read from the 600 token file | mint, limit, revoke, leak, reveal, annotate, disable, enable, rotate-key | loopback bind only; exact `Host`; exact same-origin `Origin` when present; one token header compared with `compare_digest`; bounded JSON body; `put` and `rotate` refused, because values travel only on stdin |
| MCP server (`mcp/server.py`, stdio) | an agent the operator started | the process was started by the operator | seven reads; `observatory_record` and `observatory_propose` append to the ledger | every write lands `proposed` with confidence below 1; nothing here can promote it |
| CLI (`observatory.py`, `tools/*.py`) | the operator's shell | the operator | everything the tools do | values only on stdin (`vault.py put`, `install_key.py`); destinations enumerated, not taken from input |

## Rules and their proof

1. **Rebinding and cross-origin requests get nothing.** A page on another origin that points a
   hostname at 127.0.0.1 is refused before any page, token or action.
   - keyserver: `test_rebinding_host_cannot_obtain_page_token` and
     `test_origin_prefix_and_different_port_refused_before_effects`;
   - dashboard server: `test_server_refuses_rebinding_and_cross_origin`.
2. **CSRF can't act.** An action needs the token in a custom header, and a cross-origin page can
   neither read the token nor send that header without a preflight the keyserver never answers. An
   `Origin` that isn't this server is refused first.
   - `test_same_origin_and_cli_reach_action`;
   - `test_a_cors_preflight_is_never_granted`;
   - `test_duplicate_host_origin_and_token_are_refused`;
   - `test_token_page_is_uncacheable_and_not_frameable`.
3. **A declared caller is a label, never an authority.** `X-Observatory-Caller` opens nothing without
   the token. With the token, every name has the same rights. The name is sanitised to one short token
   and written beside the action in the audit journal.
   - `test_a_declared_caller_is_a_label_never_an_authority`.
4. **Another workspace's credentials open nothing here.** Each workspace has its own token file, so a
   page or agent holding one workspace's token is refused by another's keyserver. `full open` refuses a
   port that already serves another workspace.
   - `test_another_workspaces_token_is_refused`;
   - `test_serve_starts_a_loopback_server_then_reuses_it`.
5. **An automated writer can't promote itself.** An MCP write lands `proposed`. Promotion is the
   operator's act or an independent corroboration.
   - `test_automated_writer_cannot_self_promote`;
   - `test_a_writer_may_only_correct_its_own`;
   - `test_proposal_never_touches_the_registry`.
6. **The token is private and never empty.** It is created at mode 600. An empty token or a
   publicly readable file is refused, and so is a bind to a routable address.
   - `test_token_created_private_reused_and_symlinks_refused`;
   - `test_empty_and_publicly_readable_token_files_refused`;
   - `test_empty_tokens_and_routable_bind_are_refused`.
7. **The events feed needs the service token, in the header only.** The dashboard server's
   pages stay open to local reads; its events feed does not. The token is created at mode 600
   after the instance lock, accepted only as `Authorization: Bearer`, refused in a query string,
   and never written to the plist or the descriptor (which names the file, not the value).
   - `test_events_need_the_token_in_the_header_and_only_there`;
   - `test_install_writes_the_descriptor_then_the_plist`.
8. **Errors don't reflect secrets.** An unexpected failure answers with a fixed sentence.
   - `test_unexpected_exception_does_not_reflect_sensitive_detail`.

## Gaps, stated

- **Local users are trusted.** Any process running as the operator's user can read the token file.
  The model separates this machine from the web, not one local process from another.
- **The MCP server has no per-tool scope.** An agent that can start it can call all ten tools. Its
  writes are bounded by rule 5, not by who the agent is.
- **The dashboard server's GET data is visible to any local process.** It holds names and verdicts,
  never values (`tools/check_secrets.py` enforces this on every gate run).
