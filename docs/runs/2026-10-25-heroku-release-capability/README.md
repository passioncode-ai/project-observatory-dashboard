# Heroku release capability proof

Status: IMPLEMENTED_SYNTHETIC_GREEN_REVIEW_PENDING. The initial preparation and the terminal handoff below are retained as history; the implementation section at the end is current.

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

## Implementation (2026-10-26)

Extended controls first. The prepared regression gained the five gaps named in the terminal handoff, all against the synthetic provider and stores:

- `test_release_deploy_rotation_proves_with_the_regenerated_value`: rotation runs the same app, builds, newest release and exact slug proof with the regenerated value, every proof read before the vault is handed the value; a protected 403 on the release or slug read, and a vault failure after a full proof, each say the old value is dead, deliver nothing and count no rotation.
- `test_release_read_pins_the_newest_release_range`: the door pins `version ..; order=desc, max=1`, the release read carries exactly it, and the fake answers two releases when it is dropped.
- `test_release_proof_refuses_a_slug_id_that_is_not_exactly_a_uuid`: a UUID with a trailing or leading newline, numbers, booleans, lists, objects, oversized strings and near-UUIDs are refused before any slug request or delivery, and the fresh authorization is deleted.
- `test_protected_release_wire_installs_its_guards_and_types_every_failure`: a no-network tripwire, proof that every protected open installs the no-redirect handler, typed value-free failures for HTTP errors (including a refused redirect), malformed UTF-8, malformed JSON, an empty body, dropped and reset connections and a timeout, and refusal of any path outside builds, releases and slug info before a request exists.
- `test_release_vault_failure_after_proof_deletes_the_fresh_authorization`: the full proof runs, the vault refuses, the fresh authorization is deleted and no ledger row is written; over an existing slot the previous authorization and its row survive.
- `test_legacy_deploy_claims_no_release_access`: `deploy` keeps `read` and `write` and no longer claims releases it never proves.

The [extended RED](red-extended.json) ran on the unchanged runtime and exited 1 with the nine expected missing-feature controls (109 PASS lines). A first gated attempt was cancelled while the gate still waited, before the test command started; no test ran in it.

Implementation, in `observatory/engine/tools/heroku.py` only: the explicit `release-deploy` preset (`read-protected` and `write-protected`, needs `--app`, discloses config var reach); `prove_value`, the one proof dispatch `issue` and `rotate` share; `release_proof` (builds, the newest release by the pinned Range, a strict UUID slug id, then the exact slug info whose id must match); and `_release_read`, the protected transport with a fixed origin, a three-shape path allowlist, `_NoReleaseRedirect`, a one-MiB body bound and typed value-free failures. The `deploy` preset keeps its scopes; only its claim text changed. The generic transport is unchanged except that `builds`, `releases` and `slugs` are kept in error endpoint shapes. The proof reads no config vars and makes no app write.

[GREEN](green.json): the focused suite passed with 219 PASS lines; the neighbouring engine suites (`heroku_door`, `heroku_scoped`, `heroku`, `credential_doors`, `handed_commands`, `vault`, `credentials`) passed through the portable runner. The root documentation tests failed once (the new preset was named only inside a longer code span in ONBOARDING, so the door-preset rule could not find it); that result is kept in the receipt, the paragraph was corrected and the rerun passed 18 of 18. Source inventory, engine document copies and a Python 3.11 compile were checked. Documentation updated in the same change: ONBOARDING (and its engine copy), the agent secrets design, and scenario OSS-68.

No provider request, credential issuance or rotation, vault value read or live Heroku call happened. Existing installed credentials are not changed. Next: independent review of this source delivery; a product release and any live protected authorization remain separate operations with their own authorization.

## Independent review (2026-10-26)

Two blind readings of `7e1c9054`. The unit reading passed: the gated focused run gave 219 PASS, and nine of twelve deliberate mutations made the suite fail. The seam reading failed on two blocking points, both fixed on this branch:

- The module docstring, which is the `--help` text, said presets with `-protected` scopes are named after config, not deploys, and that every protected read uses the no-redirect transport. Commit `23be449f` makes both sentences true: three presets reach config vars and say so, and the first proof read (the app) still uses the shared transport.
- The branch conflicted with `main` in `SOURCE-INVENTORY.json`. Commit `9c8dd9d1` merges `main` and regenerates the inventory; the inventory check and the engine document copy check pass on the merged tree.

Non-blocking findings, proposed as follow-up rows (none was fixed here):

1. The release proof regenerates before it proves. When the proof fails for a stable reason (the newest release has no slug), rotate and a repeated issue leave the slot holding a dead value, and "run rotate again" cannot help. Read the newest release shape with the admin token before regenerating.
2. The first proof read (`/apps/{app}`) carries the new protected value through the shared transport, which follows redirects and keeps `Authorization`. The origin is fixed and HTTPS, and `config-read` and `config-write` already behave this way. Route protected proofs through an opener without redirects.
3. `_release_read` has no retries while `prove()` retries up to six times, so a single 5xx or 429 during rotate kills the old value. Retry non-final statuses the same way.
4. Tests replace `_release_read` with a stub, so narrowing the path allowlist to releases only stays green; the slug id match and the release proof without an app have no test.
5. The app segment of the allowlist accepts `%` and `.`; protection rests on quoting and on Heroku not decoding `%2F`. Restrict it to the app name grammar or a UUID.
6. Engines 0.26.0 to 0.29.1 rotate a `release-deploy` ledger row with the `read` proof (unknown preset fallback); `docs/COMPATIBILITY.md` does not say so.
7. The agent skill `handling-secrets` still lists `read|deploy|identity` and does not warn that `deploy` is not a release credential; changing it needs an `observatory-log` version bump.
8. Minor: the `why` text is never shown at runtime, the `list` preset column is 12 wide for a 14-character name, and deeply nested JSON raises an untyped `RecursionError`.

On the merged tree (`9c8dd9d1` plus this note) the seven neighbouring engine suites passed through the portable runner and the root documentation and release boundary tests ran 42, OK ([receipt](merged-check.json)).
