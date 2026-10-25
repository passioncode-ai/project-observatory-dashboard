# Heroku release capability proof

Status: TEST_PREPARATION_NOT_RUN. Runtime unchanged.

The existing deploy preset names releases in its purpose, but requests only read and write scopes and proves only app inventory. That proof cannot establish access to protected release resources. The proposed change keeps its scopes unchanged and corrects the claim. A separate explicit release-deploy preset would require an app, disclose protected read/write capabilities including configuration variables, and prove app and builds reads, one release and its exact slug-info read with the new value before vault delivery. The proof itself reads no configuration variables and performs no app writes.

The [official OAuth documentation](https://devcenter.heroku.com/articles/oauth) describes protected scope classes. Endpoint capability is checked at issuance rather than inferred from that description. These probes establish read access only, not a successful deployment or every required write permission. No provider operation or credential issuance belongs to this source preparation.

Prepared regression: `observatory/engine/tests/test_heroku_door.py::test_release_deploy_proves_protected_endpoints`. It covers app requirement, exact read probes, unchanged legacy scopes, protected-scope disclosure, no config reads, failed-proof cleanup and value-free output. All providers and stores are synthetic. The runtime does not yet implement the new preset, and the test has not run.

Next: run the prepared RED through the shared test gate, implement only the preset/probes and documentation, then rerun the focused provider-door suite and neighboring checks. Independent review, source inventory and privacy checks precede source delivery. Existing installed credentials are not automatically changed. A live operation needs its own concrete authorization and exact reviewed source.

Skills used: task-pipeline for the bounded repair, handling-secrets for provider-door and vault boundaries, evidence-docs for the distinction between proposed and verified capability.

Independent design review found that Slug List is not a documented endpoint. The corrected proof reads the latest release, validates its slug UUID and requests Slug Info for that app. Missing or malformed slug evidence refuses the existing-Cedar workflow before delivery. The protected probe uses a dedicated no-redirect transport with a one-MiB body limit and typed failures; both issuance and rotation use the same proof dispatch. Existing generic transport is unchanged by this scope. The initial test draft was corrected before any test run or runtime change.
