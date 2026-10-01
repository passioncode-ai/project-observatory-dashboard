# Client disconnects end a connection without a server traceback

A polling client or browser can close its socket during a response. Previously both
`BrokenPipeError` and `ConnectionResetError` escaped the request handler, filling
stderr with tracebacks even though the service remained available.

The connection handler now catches those two exceptions and closes the connection.
Other I/O failures still propagate. Source: [Handler.handle](../../../observatory/engine/tools/serverd.py);
regression: `test_disconnected_clients_are_not_server_failures` in
[test_serverd.py](../../../observatory/engine/tests/test_serverd.py).
The test drives the real HTTP parser and fails socket writes at headers and body.

The resource-tracking example now uses an explicitly synthetic analytics account
number, and service-event fixtures use `alpha-web` instead of a real product id.
These changes affect examples and test data only.

## Scope and checks

[verification.json](verification.json) records the test-first result: four failing
assertions before the change, zero after; 24 server assertions pass. Before the fixture-only cleanup, the complete
portable runner passed 197 suites (5,827 printed assertions and 529 unittest cases;
14 explicit skips). Root unittest discovery passes 84 cases. The affected suites were rerun after
the fixture cleanup; the final counts are in the receipt. These are separate
counters, not a claim that skipped or external-provider checks ran.

Commands: `python observatory/engine/tests/run_portable.py --suite serverd`,
`python observatory/engine/tests/run_portable.py --jobs 4`,
`python -m unittest discover -s tests -v`, `python tools/update_inventory.py --check`,
`python -m compileall -q observatory` on Python 3.11, wheel build plus
`python tools/check_package.py <wheel>`, the three strict plugin validations,
`python docs/site/check.py --self-test`, `python tools/build_article.py --check`,
and `node tools/check_site_interactions.cjs`.

## Delivery and next task

This is a source fix for review, not a released or installed runtime change.
Next: review this branch, obtain the contributor's CLA checkbox and required PR
checks, then merge by the repository policy. A tagged release and the normal
`full update` path are required before an installation includes the fix.

Prerequisites and contracts: [AGENTS.md](../../../AGENTS.md),
[CONTRIBUTING.md](../../../CONTRIBUTING.md),
[service contract](../../design/FABRIC-SERVICE.md).
No local workspace, logs, provider replies or private configuration belong here.

Privacy: the current tree passes the identifier and pattern check against a
private denylist generated from the reviewed `v0.10.0` baseline. The default
pattern/history scan is recorded separately. An all-ancestry identifier scan
can still flag earlier example data; this change does not rewrite Git history.
