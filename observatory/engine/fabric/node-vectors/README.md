# fabric-relay/0.1 node vectors (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`f33f4d8ad3a15e3f4ea06e51cff00c64f4af4f6e`, the engine's contract pin (`observatory/engine/fabric-contract.lock.json`),
every file here: `fixtures/vectors/`, `schemas/`, and the `estate-*` fixtures of `fixtures/semantic/` in `semantic/`, all
but one: `estate-secret-value-as-slot.json` names the AWS documentation's example access key id as a slot, a shape the
public-release gate refuses, so `test_node_pairing.py` rebuilds it from `estate-record-ok.json`. They are the contract
kit's own vectors for RFC 8785 (`canonical.json`), P-256 key validation (`p256.json`) and fabric-relay/0.1
(`relay.json`, DEC-0047, DEC-0066), written by the contract's generators, never by hand, plus identifier equality
(`identifiers.json`, DEC-0076), the estate record (`estate-record.json`, DEC-0055) the revocation delta with its
heartbeat (`revocation-delta.json`, DEC-0054) and the secret envelope (`secret-envelope.json`, DEC-0047 item 7).

The estate ceremonies (DEC-0084, contract pull request #113): `enroll-token.json` (the headless join), `pairing.json`
(commit, reveal and the comparison code), and the DEC-0084 revisions of `relay.json` (the admission set and open
sessions), `revocation-delta.json` (a device-key reference) and `secret-envelope.json` (revocations of verified
deltas). The partner channel (DEC-0090, contract pull request #130, OBS-171): the five `estate-partner-*` fixtures of
`semantic/` (FAC-SEM-122, a partner's publisher key trusted only in the organization contexts `publisherScopes[]`
names).

`tests/test_node_foundation.py`, `test_node_estate.py`, `test_node_revocation.py`, `test_node_envelope.py`,
`test_node_pairing.py` and `test_node_enroll.py` run the node's ports over every
case; the first checks the digests below.

`http-signature.json` (the device transport binding, DEC-0056), `share-registration.json` (DEC-0086) and
`device-enrollment.schema.json` (DEC-0031, DEC-0094), with the device fixtures in `device/` (`fixtures/positive/` and
`fixtures/negative/`), are copied byte for byte (OBS-161 pin) from the contract at commit
`f33f4d8ad3a15e3f4ea06e51cff00c64f4af4f6e` (`fixtures/vectors/`, `schemas/`); `tests/test_node_org.py` runs the
receiver's check over every transport case, the node's derivation and sender gate over every `derive`, `grantId` and
`gate` case, and its enrollment request against the schema's `key` rules. One command re-vendors the whole folder at
another commit, removes an `estate-*` fixture the commit no longer has, and rewrites every commit and the digests here:
`python3 tools/vendor_org_vectors.py --contract <checkout> --commit <sha>`.

The node's harness summary (DEC-0101, DEC-0102; OBS-192) is pinned with them: `summary/node-summary.schema.json` with the two schemas it refers to (`summary/common.schema.json`, `summary/activity-common.schema.json`), the coding-agent catalogue its ids come from (`summary/coding-agents.json`, DEC-0093, `catalogue/coding-agents.json` there, which `collectors/scan_harness.py` reads), the policy that carries a baseline (`summary/device-policy-harness-baseline.json`) and every node-summary fixture (`summary/positive/`, `summary/negative/`, `summary/semantic/`) but `node-summary-path-in-name.json`, which names a home folder, a shape the public-release gate refuses: `tests/test_node_summary.py` rebuilds it from the positive fixture, as `test_node_pairing.py` rebuilds its own. `tests/test_node_summary.py` runs the port (`node/node_summary.py`) over them.

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit and
the digests below.

| File | sha256 |
|---|---|
| `canonical.json` | `e63411c0e1b615773d20c8c82b34a0c5ef7fc367fffc04d106b537c2628e5663` |
| `enroll-token.json` | `475e8e54cb47ceeb8b7b478327b7f8b9e89cf8293521986c65a9f3ad437bbc71` |
| `estate-record.json` | `3c37bf7bf205fe14674954200ef14b8e733e70a7e350ef08293da3edee2f61c0` |
| `http-signature.json` | `baaedfde6b6edcc9155fe7cf739f3173ccbbd1404aa96064688f36be34d393f5` |
| `identifiers.json` | `ff8726a99db6475e3a28f14bead020118ba01a1ac5844d3fd62678e3d9369f8b` |
| `p256.json` | `8dbfdb7cf8f80dcda4bfcfd7c577860db6d4d18ad2d03975beef38df80c0313b` |
| `pairing.json` | `c5d99e2a54ba210a06b94350bebda635369bf50b470981c6d10aedbf914c498b` |
| `relay.json` | `47563a359dc876500bb0405689eaa8c43bd9e148ab09c77c1a0f74e5850d42bb` |
| `revocation-delta.json` | `cdb1fb989808bf0bc7843ef39571c1aa2a27d2f2dd2281572e921fc77bfb7217` |
| `share-registration.json` | `3e55a402cafc606e91aeb427ffca0e4d1241528c3a634aa4d0e580961126faa9` |
| `secret-envelope.json` | `3ed14f7f95857b10a8d7ec97ded697ea65ea564c9f4c731e11c221e9ef03a3b3` |
| `device-enrollment.schema.json` | `db895afa14641d93d2f677b2a02b0ac762ba3f10f57a14de5171018ac05893a5` |
| `device/device-enrollment-request-file-key.json` | `5ff4d130ad4ad406d730eb7b353581f9ab0058e4a5ecb2a674bda8a9c64a9091` |
| `device/device-enrollment-file-key-not-exportable.json` | `fd07f7ec3be7be88cfe175524a8d08afea88bf31f1136ff25fa492a94e69dbdc` |
| `device/device-enrollment-file-key-attested.json` | `27fec7d9be6b86f8e99b152c7025f6b9046006e77d949ae401ddf2df537e7a27` |
| `device/device-enrollment-hardware-key-exportable.json` | `b3641681253c75eb971e23b245187f3b16c8dad674d69834d8e9b13134e5ad64` |
| `device/device-check-in-response-protocols.json` | `f7d9a64a109c118d86b15d7b309fc880d595d4e2ea4d897add884d94b7ce6d41` |
| `device/device-enrollment-request-key-on-record.json` | `67932d96183358f4f977bdbef25b90159cea179fc9f3d9f0246ce15968b216c2` |
| `device/device-enrollment-key-on-record-with-token.json` | `df17dd6bb544c7f53fb7bb4be94bcec531daf4e9a9bcc3775745259d65e9b982` |
| `summary/node-summary.schema.json` | `48979db88e36ba994755e717f990d99e20153d27d7253860a2b7a735034a46b2` |
| `summary/common.schema.json` | `707a28c49e8a370d3ace2898406093ad9bac93952054f05bbffeb0b912dd8e99` |
| `summary/activity-common.schema.json` | `990418510b2f83b0560c31c739f347d64752d611812abb89e7d2ce7079b11017` |
| `summary/coding-agents.json` | `97c663b787a82881a94313a47b0bbf1cf9c4030ab86287f207ba4152b33c1d65` |
| `summary/device-policy-harness-baseline.json` | `21c73ba6abe991ea52d65fd6fa8d1ba4fcb2a76425cfacb554b5a448b784206a` |
| `summary/positive/node-summary.json` | `2c35d0e21a3a1853786fe569c30935c909831b4c701c2ac3303ba077ead01958` |
| `summary/negative/node-summary-dotted-suffix.json` | `aadab7849ad90e2683bbcb59b9f4e7aecf5c060053b58dcb78cfbbe93f486bfd` |
| `summary/negative/node-summary-drift-dotted-suffix.json` | `be4226f634d95ec51250a8aa9e15795e66263c0e7e163857904b3afa3331786e` |
| `summary/negative/node-summary-endpoint-query.json` | `5ba680ae2112b185f6ad2719910d5dca570a2074568a9e843263afdb7e3d64fa` |
| `summary/negative/node-summary-mcp-dotted-suffix.json` | `27c3756483ec3a54128ab3b74140e263e97c31f19dcc0a6cf72c27ae4172e8d9` |
| `summary/negative/node-summary-personal-context.json` | `6d97385461245b7611104669600caeec46273a3cf9792f25f2a417ec865f0732` |
| `summary/negative/node-summary-server-arguments.json` | `315c07551333bca454bfc7e7bdb2f9d2820289a35c55fdc236f3a2c59207f9b3` |
| `summary/negative/node-summary-skill-two-at.json` | `44d16379ccd3ba5add0a437791ef411148fae11714a92f7247aec753ec17ec53` |
| `summary/semantic/node-summary-drift-hidden.json` | `945d462451b4115b48293896030bb842f48165049c6b96f19118f632a7422ec1` |
| `summary/semantic/node-summary-drift-without-baseline.json` | `1df47297e64106ad810944210a2039e879c87012ce7bd7390e6de72e9e9f240a` |
| `summary/semantic/node-summary-duplicate.json` | `eff3a8f484cebc7ba0bb56afc9a71acecb876cd471ecd0582016c9ca867284e5` |
| `summary/semantic/node-summary-ok.json` | `a57896a4e94132b24583fc1ffb4b8849f388f425bc9bceab46e8a1494c9ad5f7` |
| `summary/semantic/node-summary-other-org.json` | `a1d03178d464f28e754d26fba2de2066a197556730efbce8d0001a877294cd8a` |
| `summary/semantic/node-summary-person.json` | `487479893660ca2e293328e55a15954081fd92350ce980837f82c64a6d4a3b48` |
