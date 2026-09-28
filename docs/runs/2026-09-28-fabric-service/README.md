# 2026-09-28 — the always-on server speaks fabric-service/0.1

Handoff for the next agent. Branch `agent/fabric-service`; no private names or ids here.
Design and the rule-by-rule proof: [docs/design/FABRIC-SERVICE.md](../../design/FABRIC-SERVICE.md).

## Objective

Bring `tools/serverd.py` under the Fabric Agent Contract's local service extension
(`fabric-service/0.1`, contract commit `cc9ed2d`), so a local host such as Fabric Dashboards can
find, identify, supervise and read it — without changing `/health` or the dashboard's open-local
read model, and without touching any installed service.

## What changed

| Area | Files |
|---|---|
| Vendored kit, probe, schemas (unedited, digests checked) | `observatory/engine/fabric_service.py`, `observatory/engine/tests/check_service.py`, `observatory/engine/fabric/service-schemas/` |
| Identity, build, descriptor | `observatory/engine/service_identity.py` |
| Well-known snapshot (degraded + tiles) | `observatory/engine/service_health.py` |
| Events feed as a view over the event store | `observatory/engine/service_events.py` |
| Lock, token, routes, SIGTERM, installer | `observatory/engine/tools/serverd.py` |
| `full open --serve` names the running address on exit 75 | `observatory/engine/tools/dashboard_open.py` |
| `service.lock` excluded from upgrade snapshots | `observatory/engine/workspace_upgrade.py` |
| Manifest revision 5 with the extension key | `observatory/engine/fabric-agent.json`, `observatory/engine/fabric/FABRIC-CONFORMANCE.md` |
| Tests (38 cases), portable runner registration | `observatory/engine/tests/test_fabric_service.py`, `observatory/engine/tests/run_portable.py` |
| Docs | README, SECURITY, CHANGELOG (Unreleased), both ONBOARDING copies, `docs/design/ACCESS.md`, `docs/design/FABRIC-SERVICE.md` |

## Checks actually run

- `project-observatory full check`: before 62/62 suites, 366 cases; after **63/63 suites, 404
  cases** (the new `fabric_service` suite, 38 cases), status PASS.
- Root `python -m unittest discover -s tests`: 66 OK before and after.
- `tools/check_package.py` on a freshly built wheel: passed. `tools/update_inventory.py --check`: 0.
  `tools/check_public_release.py`: passed, 0 findings. `tools/fabric_hash.py --check` and
  `tools/publish_contract.py --local`: pass. `python3.11 -m compileall`: 0.
- `tools/check_docs.py` (engine): the same three drifts as on `main` before this change
  (`store/scrub-watermark.json`, `docs/dashboard/index.html`, `docs/REGISTRY_SHAPE.md`); none new.
- **Probe, in the test** (temporary workspace, descriptor with `lifecycle.manager: none`):
  19 rules, 0 FAIL; NOT_RUN `login.single-use` (no login declared) and `lifecycle.launchd`.
- **Probe, installed wheel** in a fresh virtual environment: same 19 rules, 0 FAIL; the build
  reported the wheel's own sha256.
- **Probe, real launchd**, a throwaway workspace with its own label and port (never the installed
  job): 20 rules, 0 FAIL, 1 NOT_RUN (login); `lifecycle.plist` and `lifecycle.one-copy` PASS
  (launchd pid equals answering pid). A hand-started second copy exited 75 naming the launchd pid;
  `kill -9` of that pid → a new pid answered the well-known document within 6 s. `--uninstall`
  removed plist and descriptor; launchd then no longer listed the label.

Planted defects, each valid code, each watched failing its test and then restored:

| Plant | Caught by |
|---|---|
| lock taken after a heartbeat | `test_a_held_lock_stops_the_server_before_any_side_effect` (`['heartbeat'] != []`), `test_a_second_copy_exits_75_naming_the_holder_and_leaves_nothing` (receipt pid changed) |
| well-known route recomputes `/health` | `test_the_route_reads_memory_only` |
| events route skips the token | `test_events_need_the_token_in_the_header_and_only_there` (200 != 401) |
| event id is a group's first row | `test_cursor_paging_sees_every_row_once_and_then_the_new_ones` (5 ids instead of 3) |
| plist written before the port claim | `test_a_claimed_port_is_refused_before_any_plist` |
| any enclosing checkout counts as the build | `test_a_foreign_enclosing_repository_is_not_this_build` |

## Decisions

- `paths.data` is the workspace: lock and token at its root, derived from `OBSERVATORY_HOME`
  alone (the one variable the plist carries), ignored by the workspace's own `.gitignore`.
- Instance `default` for the standard workspace, `ws-<sha16>` otherwise (the protocol's instance
  must start with a letter); the digest is the launchd label's.
- The dashboard keeps `login: false`; no POST route and no login-code flow were added.
- Event ids are the event store's `rowid` (zero-padded); the view never writes and never migrates.
- The server's own lifecycle events (`service.started`, …) are not emitted: the feed is a view over
  the store, and a second log beside it would break the one-id-space rule. Stated, not skipped.

## Open work

1. **Next task — release and switch.** Review and merge the PR; cut the release (version bump,
   wheel, tag) per the repository's release procedure. Then, on the installed machine, upgrade the
   package in its virtual environment and re-run `tools/serverd.py --install` for the workspace;
   that writes the descriptor and restarts the job under the new plist. This handoff did neither.
2. After that install, run the kit's probe against the installed instance. On a workspace whose
   root is a Git repository (registry history) expect `state.outside-code` to FAIL — see "Limits"
   in the design; the fix (probe accepts an ignored path, or a data directory outside the
   workspace) is the protocol owners' decision.
3. The contract commit `cc9ed2d` and the kit commit `dfd11dad72fe` sit on unmerged branches of
   their repositories; when they merge, re-copy the vendored files if anything moved.
