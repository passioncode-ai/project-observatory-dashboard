<sub>ssheleg skills — task-pipeline · evidence-docs · copywriting · agent-sync</sub>

# Explicit decrease-only repair: source candidate

Status: source candidate accepted for its bounded synthetic contract; full system
acceptance remains NOT_MET. See the independent review below. This follows
[the reviewed contract](README.md) and preserves the original full-model failures.
No provider, credential, installed-runtime or live consumer operation was run.
Source inventory and diff checks pass; brand lint exits zero with 2,404 warnings,
so this is not a claim that the repository is warning-free.

The new actual CLI door is `limit NAME --set USD --decrease-only`; the observation
door is `limit NAME --repair-status [--refresh]`. `NAME` is an exact issued ledger
name. Reset changes and mixed status/set modes refuse. General manual `limit --set`
keeps its previous semantics and does not become an automatically safe repair.
No original-model adapter or emitted policy remedy changed in this implementation.
Integration status for automatic policy guidance is **NOT_WIRED**. A separate
accepted source-to-parser-to-model seam is required before a full remeasurement.

## Durable ordering and evidence

`decrease_limit` reads a current matching identity and period under `_policy_lock`.
Only a supported positive finite ceiling can authorize an observed decrease;
a larger target refuses and an equal target performs no write. The door saves an
intent, requires a journal record, PATCHes once, then requires a result journal
before publishing a matching actual observation. A valid actual above target but
no greater than the fresh bound is `observed-unmet`, with nonzero exit. Unknown
receipt/effect keeps the intent or unknown state and never saves the requested
amount as fact. No operation retries PATCH.

`repair_status(refresh=True)` saves observation-pending before its required journal
and GET, including when its prior state was resolved. Failed GET, journal or final
publication cannot retain an old success as current. The bounded `effect` record
keeps the earlier operation's observed/unconfirmed status; a later read supplies
`subsequent-current-read` evidence and cannot rewrite an unknown historical effect.
Recorded output names its observation time; ledger-only status does not imply a
fresh measurement. Historical observations are bounded, not an accumulating array.

The canonical context is account, primary hash, destination, reset and a SHA256 of
the canonical existing policy-binding object (including its attestation time), or
of null if absent. Requested/saved amounts and repair fields are excluded, so
repair publication cannot invalidate its own context. No new provider label/tail,
credential value or raw error body is stored. `ceiling_repair` schema 1 validates
identity/context, supported amounts, timestamps, status/actual consistency and
observation basis; malformed presence fails closed.

## Writer and publication guard

The shared rotation admission guard checks repair presence before provider writes.
Aliases match exact primary/predecessor/successor hashes regardless of local account
label; historical repair identity is retained for mismatch detection. `_read_held`
returns unknown exposure for an unresolved repair. Policy re-attestation validates
all affected primaries before its local provenance publication.

`save_ledger` checks changed rows against old repair context before publication.
Ordinary writers cannot introduce or replace repair evidence. An authorized context
change can supersede only a well-formed, resolved, matching repair: a required
supersession journal precedes a single ledger publication containing both the new
context and historical repair. Its superseded-context binding is checked on later
writers. Unresolved or mismatched records cannot be repaired by silently changing
provenance. Repair's own small publisher uses a scoped context variable while
calling the same ledger save function, preserving its existing fault hooks. It also
rechecks the current context, other rows and attempt before each repair publication
after a provider call; detected concurrent changes are preserved and refused. This
is a local precheck under the lock, not a filesystem CAS against other writers.

The audited callers are issuance, claim, policy provenance, manual limit, toggle,
rotation, revoke, pending forget, internal forget/mint cleanup and the legacy
`_delivery_failed` helper. The last helper has no source call site, but its direct
same-identity delete path now also refuses an unresolved repair. Tests exercise
actual alias calls plus direct local publication, rather than only a predicate.

## Verification and next step

[Focused receipt](implementation-focused.json) binds production, new tests, original
adversary and credential tests by SHA256. It includes the new fault/alias/CLI suite
and every function from the preceding 115-assertion focused safety set. The [initial red receipt](implementation-red.json) records nine tests and fourteen
missing-door errors on the base. Tests were observed red before implementation; additional red controls exposed contradictory
resolved metadata, missing superseded-context binding and direct legacy cleanup
helper deletion. Two more red controls caught context changes during the fresh read
and during the provider effect; publication now preserves and refuses that drift.
All are retained as regression assertions.

The original 5200 model and credential suite sources are unchanged. Their latest
[full failed measurement](../2026-10-13-held-ceiling-remedy/measurement.md) remains
NOT_MET. Full matrix and broad suite were not repeated for this candidate. Main
advanced independently while this isolated implementation ran; compatibility with
that newer main is not established. Independent review of this exact source must
precede integration, then the integration owner must reconcile those changes.

Next: independently probe every durable fault seam and alias/context writer,
including resolved supersession. Only after acceptance may a separate packet change
policy guidance or its model adapter to invoke this actual door. No global atomic
cap, external-writer exclusion, complete consumer discovery or full convergence is
claimed. The provider may change state between GET and PATCH despite the local lock.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- `task-pipeline`: bounded source implementation.
- `evidence-docs`: source-bound checks.
- `copywriting`: CLI copy.
- `agent-sync`: shared strings lease.

Portable registration: `decrease_repair` is in `run_portable.BOUNDARY`, so the
source-copy selector includes its test module and the existing adversary helper.
Parser and membership checks pass without copying a workspace. The future combined
runner must include the independently owned reporting correction `476d392`; no
portable execution against that composition is claimed here.


## Independent review correction

The first source candidate `5abd295` was rejected for a real hashless-alias bypass:
manual limit and toggle could discover a protected provider identity after their
initial ledger guard. The correction checks the actual discovered row before
set/toggle/rotate/revoke mutation. A reproduced adjacent local-publication seam
now resolves a selected hashless legacy row before policy re-attestation whenever
repair evidence exists; unknown discovery refuses publication. Distinct known
identities retain general manual and attestation behavior.

The equal-target no-op now rechecks the ledger snapshot and repair aliases after
GET before reporting success. It still writes nothing. Evidence validation also
requires a `same-operation-response` observation to match its historical effect's
status, amount and timestamp; a later current read can legitimately retain an
unknown historical effect. All findings were observed red before these changes.

[Correction checks](correction-focused.json) contain 27 repair tests plus the
same 115 prior safety assertions. The [initial receipt](implementation-focused.json)
remains the original source-bound snapshot, not a current verdict. Independent
rereview of the correction is pending; no integration, push or full-model outcome
is implied by these focused checks.

A second independent pass confirmed those corrections and found a read-only alias
seam: when policy provenance already matched, a hashless row could discover the
protected key and report `in-place`. `_read_held` now guards the discovered hash as
well, returning unknown exposure rather than a served slot. The counterexample
was observed red and is retained. [Current focused receipt](held-alias-focused.json):
28 repair tests and 115 prior safety assertions pass. Original receipt snapshots
remain unchanged; independent rereview of this final correction is pending.


## Accepted source handoff

Independent review accepted exact runtime/test head
`b4fbc94cbefbfb16632ee8b30073a05f6328d6a9` for this bounded source contract.
[Public review summary](independent-review.json) binds the source and closes four
findings. It distinguishes 18 controls at the preceding correction from seven
additional controls on the final four-line delta and publication seams. The
latest developer receipt remains 28 repair tests plus 115 prior assertions.

The runtime is ready for a separate source-to-parser-to-model guidance unit, not
for a full-system acceptance claim. Guidance remains NOT_WIRED, current-main
composition and portable runner composition remain untested, and original failed
matrices are retained. No provider, release, installed or live consumer acceptance
is implied. The exact next task is the reviewed guidance seam: emit the existing
flagged command, preserve its target exactly, dispatch through the actual parser,
and keep all original model invariants and failure outcomes.
