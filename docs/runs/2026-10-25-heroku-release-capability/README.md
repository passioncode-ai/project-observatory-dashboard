# Heroku release capability proof

Status: INITIAL_RED_OBSERVED_IMPLEMENTATION_NOT_STARTED. Runtime unchanged. The initial preparation below is retained as history; the terminal handoff at the end is current.

The existing deploy preset names releases in its purpose, but requests only read and write scopes and proves only app inventory. That proof cannot establish access to protected release resources. The proposed change keeps its scopes unchanged and corrects the claim. A separate explicit release-deploy preset would require an app, disclose protected read/write capabilities including configuration variables, and prove app and builds reads, one release and its exact slug-info read with the new value before vault delivery. The proof itself reads no configuration variables and performs no app writes.

The [official OAuth documentation](https://devcenter.heroku.com/articles/oauth) describes protected scope classes. Endpoint capability is checked at issuance rather than inferred from that description. These probes establish read access only, not a successful deployment or every required write permission. No provider operation or credential issuance belongs to this source preparation.

Prepared regression: `observatory/engine/tests/test_heroku_door.py::test_release_deploy_proves_protected_endpoints`. It covers app requirement, exact read probes, unchanged legacy scopes, protected-scope disclosure, no config reads, failed-proof cleanup and value-free output. All providers and stores are synthetic. The runtime does not yet implement the new preset, and the test has not run.

Next: run the prepared RED through the shared test gate, implement only the preset/probes and documentation, then rerun the focused provider-door suite and neighboring checks. Independent review, source inventory and privacy checks precede source delivery. Existing installed credentials are not automatically changed. A live operation needs its own concrete authorization and exact reviewed source.

Skills used: task-pipeline for the bounded repair, handling-secrets for provider-door and vault boundaries, evidence-docs for the distinction between proposed and verified capability.

Independent design review found that Slug List is not a documented endpoint. The corrected proof reads the latest release, validates its slug UUID and requests Slug Info for that app. Missing or malformed slug evidence refuses the existing-Cedar workflow before delivery. The protected probe uses a dedicated no-redirect transport with a one-MiB body limit and typed failures; both issuance and rotation use the same proof dispatch. Existing generic transport is unchanged by this scope. The initial test draft was corrected before any test run or runtime change.

## Terminal handoff

The [initial synthetic run](red-terminal.json) exited1 with the three expected missing-feature controls. The shared gate admitted the run; it is terminal and no test watcher remains. No provider request or credential operation occurred. Implementation has not started.

Before implementation acceptance, extend the prepared tests with these independently identified gaps:

1. Rotation of the new preset must execute the same app, builds, latest release and exact slug proof using the regenerated value before vault delivery. A protected403 and a vault failure retain explicit old-value-dead semantics.
2. Pin the intended descending one-release Range. The original fake checked only endpoint order and ignored Range.
3. Reject a UUID followed by a newline, non-string IDs and oversized IDs before any slug request or delivery.
4. Install a no-network tripwire for the new transport fixture. Prove the no-redirect handler is actually installed and map HTTP errors, malformed UTF8/JSON and a dropped connection into value-free typed failures. The original fixture only covered body size and called the redirect method directly.
5. A vault failure after successful proof deletes the freshly issued authorization without writing a successful ledger row. Do not weaken the existing rollback and retirement controls.

Next executor starts with these synthetic controls, then implements only the explicit protected preset and shared issue/rotate proof dispatch. A source delivery requires focused and neighboring checks, exact inventory, documentation, independent review and privacy validation. Full product release and any live protected authorization remain separate operations.
