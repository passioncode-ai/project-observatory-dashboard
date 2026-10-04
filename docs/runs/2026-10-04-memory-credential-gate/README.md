# Unknown credential access blocks workflow continuation

A handoff that declares a key must not report execution ready when its vault inventory cannot be read. Before this correction `unknown` was distinct from `missing`, but `credentialsMissing` was false. The shared gate now permits only verified `vault` states. Handoff ownership may transfer so the successor can repair access; a fresh verified read clears the gate. No keys are read or modified by this change.

Scope: OSS-28; `store/workflow.py::_blocking`; synthetic `Credentials` tests in `test_workflow_memory.py`. A workflow with no credential declarations still requires no vault read. Vault reader exception text stays out of the response.

## Evidence

[regression-red.txt](regression-red.txt): before the source change, the Credentials class failed three assertions with exit 1. The complete workflow suite subsequently passed 63 tests using a canonical temporary directory. A direct first run had an unrelated Stop-hook fixture failure from the host's symlinked temporary path; setting `TMPDIR=/private/tmp` resolved it without a source change.

[verification.json](verification.json) records executed checks and their exit status. Full hosted acceptance, merge, release and installed behavior are separate; this branch alone changes none of them.

## Next task

Finish the OBS-04 local-model decision and external-embedding privacy gate before enabling semantic handoff retrieval. Keep model/calibration selection provisional until held-out answer and refusal performance pass the declared criteria. Also reproduce the credential/checkpoint race independently; this correction does not claim to repair it.

Full local run: 204/210 suites passed; six hit the default timeout. All six passed isolated single-job retries. The original full-gate exit remains 1. Root unit suite: 124 passed; wheel and inventory gates passed. See verification receipt for exact coverage and limitations.
