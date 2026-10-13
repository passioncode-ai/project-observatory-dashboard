# Why the remaining24 convergence cases still fail

Source: `532b70911ec39fcb4590a6632283dc63dfcaecf5`. Every failing seed was
replayed with passive capture of actual plans, revoke exception types/reasons and
trace. The [seed partition](raw/residual-classification.json) covers24 distinct seeds
exactly once. It is pending independent review; all original failures remain FAIL.

| Cases | Evidence and next prerequisite |
|---:|---|
|3|External sub-cent drift remains inside `_same` tolerance; the final plan explicitly names `limit NAME --set TARGET`. The harness never executes limit remedies. Add an exact documented-remedy fixture with negative controls.|
|10|Persistent positive metadata echo makes conservative cents-up totals over-limit while the fake observer's requested-capacity total fits. Repeating the same PATCH may retain the discrepancy. Authority semantics must be resolved; requested values cannot silently replace observed exposure.|
|3|The same positive echo blocks replacement issuance after a missing key was cleared. Current total fits but proposed rounded-up exposure does not. A blocked plan does not supply an executable headroom proof.|
|6|Four consumers retain externally deleted values and degrade the producer; two still use a key removed from policy. Revoke correctly refuses. Consumer replacement/removal needs an explicit model transition and authorization.|
|2|Hashless pending rows exceed the listing bound. The CLI's attested `--forget-pending --reason` remedy is never called even after the harness's synthetic deletion. A fixture must prove absence before exercising that branch.|

Bindings: `observatory/engine/tools/openrouter.py:622` (tolerance), `:1056`
(in-place and explicit limit hint), `:1082` (admission), `:1113` (over-limit guidance),
`:1579` (attested forget), `:1724` (CLI hint); producer
`observatory/engine/collectors/scan_openrouter.py:341` (unresolved destination), and
model `observatory/engine/tests/adv_j1_property.py` convergence loop. These refer to
the source commit above. Counterexamples7000008,7000013,7000025,7000045,7000083 and
7000093 identify distinct observable outcomes; the JSON lists every seed.

This classification establishes no additional unsafe production mutation. It also
does not establish complete remediation: the original convergence property remains
red, and unmet consumer/provider assumptions cannot be removed from its denominator.
No production correction or automatic remedy is included. Exact next task: review
this partition, then separately test source-backed limit and attestation remedies,
keeping missing/unknown/live-holder negative controls and all original assertions.
