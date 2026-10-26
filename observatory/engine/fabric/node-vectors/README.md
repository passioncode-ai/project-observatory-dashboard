# fabric-relay/0.1 node vectors (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`de8cec30a443421ae5603dcfda7e4ff16ed00c45`, the engine's contract pin (`observatory/engine/fabric-contract.lock.json`),
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
`de8cec30a443421ae5603dcfda7e4ff16ed00c45` (`fixtures/vectors/`, `schemas/`); `tests/test_node_org.py` runs the
receiver's check over every transport case, the node's derivation and sender gate over every `derive`, `grantId` and
`gate` case, and its enrollment request against the schema's `key` rules. One command re-vendors the whole folder at
another commit, removes an `estate-*` fixture the commit no longer has, and rewrites every commit and the digests here:
`python3 tools/vendor_org_vectors.py --contract <checkout> --commit <sha>`.

The node's harness summary (DEC-0101, DEC-0102; OBS-192) is pinned with them: `summary/node-summary.schema.json` with the two schemas it refers to (`summary/common.schema.json`, `summary/activity-common.schema.json`), the coding-agent catalogue its ids come from (`summary/coding-agents.json`, DEC-0093, `catalogue/coding-agents.json` there, which `collectors/scan_harness.py` reads), the policy that carries a baseline (`summary/device-policy-harness-baseline.json`) and every node-summary fixture (`summary/positive/`, `summary/negative/`, `summary/semantic/`) but `node-summary-path-in-name.json`, which names a home folder, a shape the public-release gate refuses: `tests/test_node_summary.py` rebuilds it from the positive fixture, as `test_node_pairing.py` rebuilds its own. `tests/test_node_summary.py` runs the port (`node/node_summary.py`) over them.

The re-pin of OBS-217 added, at the same commit: the DEC-0105 home-path fixtures (`summary/semantic/node-summary-home-after-dot.json`, `-home-after-at.json`), the nine baselines the policy schema refuses since DEC-0102 (`summary/negative/device-policy-baseline-*.json`, run by `node_summary.baseline_problems`), the seven check-in fixtures of DEC-0114 (`summary/semantic/credential-check-in-summary-*.json` and `device-attribution-*.json`, run by `check_in_summary_findings` and `check_in_attribution_findings`), DEC-0103's `device-organization-closed.schema.json` with its positive fixture and six of its seven negative ones in `device/` (`-path` names a home folder the public-release gate refuses; `tests/test_node_org.py` rebuilds it from the positive), and the batch acknowledgements and check-in responses of DEC-0100 and DEC-0102 (`device/activity-batch-ack-purpose.json`, `-before-purpose.json`, `device/device-check-in-response-acknowledged.json` and its negative). DEC-0111 to DEC-0113 (the credential exchange) are not vendored here: OBS-191 and OBS-194 vendor them with the code that reads them.

The repair runner (DEC-0101, DEC-0102, DEC-0117; OBS-193) is pinned with them in `repair/`: `repair-decision.json`, the contract's shared vectors for `repairDecision` (FAC-SEM-129, written by its `tools/vectors/repair-decision.ts`), the `repair-request/1` and `repair-result/1` schemas with `device-common.schema.json` and `fabric-package.schema.json` (the two they refer to that `summary/` does not hold), every repair fixture (`repair/positive/`, `repair/negative/`) and FAC-SEM-068's remote-execution case (`repair/semantic/device-action-expired-remote-execution.json`). `tests/test_node_repair.py` runs the port (`node/repair.py`) over them.

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit and
the digests below.

| File | sha256 |
|---|---|
| `canonical.json` | `e63411c0e1b615773d20c8c82b34a0c5ef7fc367fffc04d106b537c2628e5663` |
| `enroll-token.json` | `475e8e54cb47ceeb8b7b478327b7f8b9e89cf8293521986c65a9f3ad437bbc71` |
| `estate-record.json` | `3c37bf7bf205fe14674954200ef14b8e733e70a7e350ef08293da3edee2f61c0` |
| `http-signature.json` | `d749bc6e6251beb764ddead064b8a1bc8b54518908fe9adf81e4e542adc7cfb7` |
| `identifiers.json` | `ff8726a99db6475e3a28f14bead020118ba01a1ac5844d3fd62678e3d9369f8b` |
| `p256.json` | `8dbfdb7cf8f80dcda4bfcfd7c577860db6d4d18ad2d03975beef38df80c0313b` |
| `pairing.json` | `c5d99e2a54ba210a06b94350bebda635369bf50b470981c6d10aedbf914c498b` |
| `relay.json` | `47563a359dc876500bb0405689eaa8c43bd9e148ab09c77c1a0f74e5850d42bb` |
| `revocation-delta.json` | `cdb1fb989808bf0bc7843ef39571c1aa2a27d2f2dd2281572e921fc77bfb7217` |
| `share-registration.json` | `50b21647253d8e440a404165365e459ae01e52cf658d2f57986101921b1c8f16` |
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
| `summary/activity-common.schema.json` | `82ffe926bb82c2d78525c947389c24aa718bf3a0d3f8367117c0e3fbefef7bec` |
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
| `summary/semantic/node-summary-home-after-at.json` | `b4caead312a22d107e25a6ee7cbed9f28d121a43926ddd38e6b1c3de97b112c0` |
| `summary/semantic/node-summary-home-after-dot.json` | `eaa8226991ea6f9bb7ef84f4405dab25c4cfab01e10fbdadd03d84f3ca6f8c75` |
| `summary/negative/device-policy-baseline-coding-agent-digest.json` | `11b378ae3ab3925732f30655f63c2d7e002a18535bad2a169abd79409fd22523` |
| `summary/negative/device-policy-baseline-coding-agent-name.json` | `b6b2046347339ca361dc138df3b431aad8ccecf36e1403d1f67854d3018930f8` |
| `summary/negative/device-policy-baseline-dotted-suffix.json` | `2220d7b8bb4d830a006974948a94c5e3ae56de42dc8211f558c26a72e22de0a4` |
| `summary/negative/device-policy-baseline-mcp-server-digest.json` | `61314d13fd71c83afc7e3c9902bc089f71ee9c4854ff5a8fedafec7eaa6eb9cd` |
| `summary/negative/device-policy-baseline-mcp-server-min-version.json` | `e6632cd052b8504727ad63f0621321d4e0ffd0eb93068525f09306e4642a0a60` |
| `summary/negative/device-policy-baseline-service-digest.json` | `d7285fd83aa2aef6c2deabdb554de555bb52ee62ac792d7c694503587e420037` |
| `summary/negative/device-policy-baseline-service-min-version.json` | `95bd2830ab912ff244745ba09135e93daf42c431d5ef353cb32eefd8c1465c4b` |
| `summary/negative/device-policy-baseline-service-name.json` | `29067abbbcf33a3e66b1d6e9876d0d9ae54f9311da1fcc4bcc4c3f883739212d` |
| `summary/negative/device-policy-baseline-skill-min-version.json` | `5dc08179e89e9c45ef42e8634edfdb35a79f0001b97a0721aaf1e04626e27afc` |
| `summary/semantic/credential-check-in-summary-ok.json` | `0cd618e17ccce7a4aa5e1c5bf7721a2150a58b9b010f47f0fac161307983cbf5` |
| `summary/semantic/credential-check-in-summary-other-device.json` | `1e9c175c6cfbd1398b4a7589de465c7528c1eff3d92df05b14b4216e4fea880d` |
| `summary/semantic/credential-check-in-summary-duplicate.json` | `fe0a30a7aa3a8fff371125c9544a06a5bd2fa97def6e35213e2501cf2c77c2b0` |
| `summary/semantic/device-attribution-records-ok.json` | `288144709d989bc1d468ec4e34a794ebf387934945a18dd11268d0aaa41b8b0a` |
| `summary/semantic/device-attribution-records-other-org.json` | `e80afb2e46b9f9f050b0ba0a486becead709409b1519eee55e047ab874f7396e` |
| `summary/semantic/device-attribution-summary-other-device.json` | `7a081d108fd5a6f8c1fd64964aa41d67e663ea59dc243c7708ae2d020ee80977` |
| `summary/semantic/device-attribution-summary-other-org.json` | `7c38d553c88b189f132683d99150d596c7a2bb1a908a839b424f9885fbf8502e` |
| `device-organization-closed.schema.json` | `cefd215be5c2d06ac6120cbad6c6a764e9b139b88933d221eb6043a72aa88f1d` |
| `device/device-organization-closed.json` | `b68feedba79415d0c0868554f933add150a44726fc9322e422f31b99627b2cc6` |
| `device/device-organization-closed-bad-time.json` | `a038af0de0de08f6071fd08fda56b941bb9c077b7794d8fca3ef5a39b4f225b0` |
| `device/device-organization-closed-error-extra.json` | `9f57943f6c2817bad4e7064bb6c01095fbe98355c04461bf5ddbf0fbd1692522` |
| `device/device-organization-closed-extra.json` | `f0aa3b6956ef5798c2b872032a988f8203bc64c3fb530706b71a882af586c32a` |
| `device/device-organization-closed-flat.json` | `104e1c985fda3f0e37133dec59bc37b458284b909d2d696390880b1d378e12d3` |
| `device/device-organization-closed-no-time.json` | `f021f764a0194e943b07426501ea7afb13961b78cd565a85783332a13654f750` |
| `device/device-organization-closed-other-code.json` | `d70de17d7011c4150323aa043433df39f7d3ded28bd6b1444c819dcfb1ca1f40` |
| `device/activity-batch-ack-purpose.json` | `7405cdaa9fd10ebb0bf206e8f1b7dad7d12f1d8144d592a318198c1c4fb7c482` |
| `device/activity-batch-ack-before-purpose.json` | `0791d78443303a890db6dd8bcabfceb0069bba4b95998d66756dc97a6505057d` |
| `device/device-check-in-response-acknowledged.json` | `4aef11b8c8ff31c001f44e492553d79c083180170874abd3311b212a40fca1f1` |
| `device/device-check-in-response-acknowledged-bad.json` | `f17998c87e9a41fa5bc8cd47914bc343620f035e9794d60b9a9ab95c2b567bb5` |
| `summary/device-policy-organization.json` | `1b645475054a87cadd8679a6e03f36071f1a2e7b23e2da36a49f6078fc4ae57f` |
| `summary/device-policy-mdm-managed.json` | `14a1062781993cf469a99bb3d5ce30baa165640b5ba8a74b2f36e9774e5db0bc` |
| `summary/negative/device-policy-workspace-folder-absolute.json` | `8597d106c9ddefcaeba3bba760e5fe5c77eadc14f64cf9e59ecd0f7e3298d308` |
| `summary/negative/device-policy-workspace-folder-home.json` | `0bb59e634126b1e6edb302e7f905f49fc2fb7bb44da60a2e5296d3025b871592` |
| `repair/repair-decision.json` | `ee3b71c2aa3a7fe67193a67cdedc4c4571bb04f3760979ad6bb772a355d2f39d` |
| `repair/repair-request.schema.json` | `46188f4fa2866857dd2457765f321470dfea494ea2ec4a33255adca62c921833` |
| `repair/repair-result.schema.json` | `166d5397dbb984f55162805114e202b17c8aaa54ac9b8b67d8acc676af2760be` |
| `repair/device-common.schema.json` | `54a640bf7469f5dca331c03e589bbb05b059f401275d0b13a96b39c391c48230` |
| `repair/fabric-package.schema.json` | `a413ab4884dff154a81034e50d74303c4af8ca7b5902deaeda11a77c275bed43` |
| `repair/positive/repair-request.json` | `a09b84af8688e9ec97289e6b97f246e45db196dae2a1caa12d92d173b9ad15bd` |
| `repair/positive/repair-result.json` | `0a4d02f9342987cf73083dd01d24445852ef2f9236b76c18415a66c3f6d7f573` |
| `repair/positive/repair-result-refused.json` | `7fec7fb4505de9e23b5009fa285cb9587880ec09a857ff9e493cba4c2e65b316` |
| `repair/positive/repair-result-org-cloud.json` | `347af54396c474c03216f8987e1477a1af5891abbc76bc1d5fd24c913d9d6368` |
| `repair/negative/repair-request-task-two-lines.json` | `1789b30acb0b4f31f83fbcdcdc32f5d78e1081777041f0d26cbb75671c926719` |
| `repair/negative/repair-result-already-answered.json` | `b738982e5258a9a27aab1b199fe745560840ce5ce06e28f43ed4d0c7cade86a3` |
| `repair/negative/repair-result-refused-without-reason.json` | `fe6d7663891683d791d6b09f2d1947efec2d8c6eef7be8ff0add50a7c800760b` |
| `repair/negative/repair-result-succeeded-unnotified.json` | `329567488c644013d73e70a12fba879ef86497f8046b205870a180a75c16468b` |
| `repair/semantic/device-action-expired-remote-execution.json` | `5638ed124984182c3ebf9062697a080a885c6aa40ec0b01671930ccd57d13b51` |
