# Access: who can reach what, and what grants it

Status: the model the code implements as of 0.3.10 (PB-015), with the dashboard server's
fabric-service routes added on branch `agent/fabric-service`. Each rule below names the test that
proves it. A rule without a test is written as a gap, not as a guarantee.

## The principle

Project Observatory runs on one machine for one operator. Reaching it at all means being local, and
**authority comes from something a remote party cannot obtain**:

- a loopback socket of this OS account (on a host with several accounts the servers check the
  peer's account, rule 10; loopback itself is one per machine, not one per account);
- a token file at mode 600 in the workspace;
- the operator's own terminal.

Nothing a client *says about itself* grants anything. Headers such as a caller name, a user agent or
a referer are labels at most.

## Surfaces

| Surface | Reached by | Authority | What it can do | Checks |
|---|---|---|---|---|
| Dashboard server (`tools/serverd.py`, 127.0.0.1) | a browser or `curl` on this machine; a local Fabric host | none for the pages and `/.well-known/fabric-service`; the workspace's `service.token` (600) as `Authorization: Bearer` for `/fabric/v1/events` | GET pages, `/health`, `/remote`, `/leaks`, `/skills`, the well-known document, the events feed | loopback bind; `Host` names this server; `Origin`, when sent, is this server; `Sec-Fetch-Site: cross-site` refused; POST answered 405; one copy per workspace (instance lock), see [FABRIC-SERVICE.md](FABRIC-SERVICE.md); before all of them, on a host with several OS accounts, the peer's account (rule 10) |
| Keyserver (`tools/keyserver.py`, 127.0.0.1) | the dashboard page it serves, or a local client | the workspace token (`X-Observatory-Token`), handed to a page the keyserver itself served, or read from the 600 token file | mint, limit, revoke, leak, reveal, annotate, disable, enable, rotate-key | loopback bind only; on a host with several OS accounts the peer's account (rule 10) before the handler runs; exact `Host`; exact same-origin `Origin` when present; one token header compared with `compare_digest`; bounded JSON body; `put` and `rotate` refused, because values travel only on stdin |
| Memory over HTTP (`mcp/http_service.py`, 127.0.0.1) | an agent or service holding an access binding | the binding's bearer, or a Fabric delegation token for the meter | the memory tools of the binding's scope | the peer's OS account (rule 10) first, then `Host`, `Origin`, the bearer, the rate |
| MCP server (`mcp/server.py`, stdio) | an agent the operator started | the process was started by the operator | the reads; `observatory_record` and `observatory_propose` append to the ledger; the workflow tools write checkpoints, handoff packs and executor leases ([AGENT-MEMORY.md](AGENT-MEMORY.md)) | every note or proposal lands `proposed` with confidence below 1, and nothing here can promote it; a checkpoint needs the workflow's lease token; every workflow write is redacted before it is stored |
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
9. **(Reserved for the client side, OBS-355 part 2: a bearer leaves only for a listener of this
   account and this workspace.)**
10. **On a host with several OS accounts another account's connection is refused before it is
    read.** Loopback is one per machine: every account's processes reach every port any account
    listens on. When the host has more than one person's account (`host_accounts.count()`: the
    local accounts that can log in, hidden administrators and an enabled Guest included, joined
    with the people's uids that run processes now; a host bound to a directory, or a count that
    cannot be read, counts as several) or `config/local.json` sets `{"loopback": {"peerCheck":
    "always"}}` (a value other than `auto` or `always`, or a file that does not read, reads as
    `always`), the local server, memory over HTTP and the keyserver find the OS account of each
    connecting peer from the host's socket table (`peer_owner.py`) and answer anything but this
    account with `403 {"error":"peer-foreign-account"}` (another account, root, launchd) or
    `403 {"error":"peer-unverified"}` (the owner could not be read), `Connection: close`, on every
    path, the dashboard pages and `/.well-known/fabric-service` included. Nothing of the request is
    parsed or logged: after the 403 is written, the bytes the peer had already sent are received
    and discarded unparsed (at most 64 KiB, half a second) so the close does not reset the answer
    away. Memory over HTTP gets the request line and headers parsed by uvicorn first and uses none
    of them; `X-Forwarded-For` cannot change the address it checks (`proxy_headers` off), and a
    WebSocket upgrade is refused. Any failure of the check is a `peer-unverified` refusal, never an
    admission. On a one-account host nothing changes and no table is read. Each refused connection
    writes one JSON line on the server's stderr (at most one per peer uid and code a minute). The
    local server's refusals are counted in its receipt `store/raw/serverd.json` (`peer`) and shown
    by `full doctor` (`loopback.refused`); memory over HTTP and the keyserver count in their own
    process only.
    - `test_foreign_and_unverified_are_refused_before_the_request_line`;
    - `test_foreign_refused_before_host_origin_and_bearer`,
      `test_x_forwarded_for_does_not_change_the_peer_address`, `test_a_websocket_is_refused`;
    - `test_any_failure_is_a_typed_peer_unverified`;
    - `test_foreign_is_refused_before_the_handler_exists`;
    - `test_one_account_serves_as_before`, `test_one_account_and_auto_never_reads_the_table`;
    - `test_peer_picks_the_client_socket`, `test_both_ends_of_a_real_pair_are_this_account`.

## Gaps, stated

- **Local processes of the same account are trusted.** Any process running as the operator's user can
  read the token file. The model separates this machine from the web and, since rule 10, one OS
  account from another; not one process of an account from another.
- **The relay is exempt from rule 10.** Its admission is cryptographic (the estate record's
  signatures), and behind a TLS terminator every peer is the terminator's process, which may run as
  another account or root.
- **Rule 10 is the servers' half.** A client of this engine still sends its bearer to whatever
  listens on the port it was given (OBS-355 part 2 checks the server end first).
- **The MCP server has no per-tool scope.** An agent that can start it can call all eighteen tools. Its
  writes are bounded by rule 5, not by who the agent is.
- **The dashboard server's GET data is visible to any local process.** It holds names and verdicts,
  never values (`tools/check_secrets.py` enforces this on every gate run).
