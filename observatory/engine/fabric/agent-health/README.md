# Agent health (vendored, DEC-0179)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit `db0d8f15994822724356d0a7e7ccc98dbcca604d`
(DEC-0179, the contract's CT-336, its pull request #333), never from a branch. SOURCE: that commit's
`schemas/agent-health.schema.json`, `schemas/common.schema.json` (the schema's `$ref` target),
`fixtures/vectors/agent-health.json` and the `agent-health*` files of `fixtures/positive/`, `fixtures/negative/` and
`fixtures/semantic/`. The commit is not the engine's contract pin (`fabric-contract.lock.json`): the pin moves with
its own review, so this folder is listed in `contract_lock.PIN_CHECK_EXCLUDES` and
`contract_lock.VENDORED_AT_OTHER_COMMITS` (OBS-577).

`machine_health.py` ports `worstState`, `readHealth` and the report rules of `FAC-SEM-181` from the contract's
`src/agent-health.ts` at that commit; `tests/test_machine_health.py` runs every vector and fixture through them.

Do not edit these files here. When the contract changes, copy them again from a new commit and update the commit and
the digests below; `tests/test_machine_health.py` checks the digests.

| File | sha256 |
|---|---|
| `agent-health.json` | `b0fda54b1576ac699c90265a7ff79450ac5d56685cf580cf116ea29f53ce85a3` |
| `agent-health.schema.json` | `f46cfaeacc330fbfef9a5094de92f90277bff99d9177515d9f30ed3065be1472` |
| `common.schema.json` | `707a28c49e8a370d3ace2898406093ad9bac93952054f05bbffeb0b912dd8e99` |
| `negative/agent-health-area.json` | `b1c0de0b92b2ec8c6f51ff45ad06c79444289d277f03a3c254d991ca1a125666` |
| `negative/agent-health-at-iso.json` | `bb242ea655673f0030125f9db2bff3686d290e6568c0e18eacd634b75f015267` |
| `negative/agent-health-check-id-camel.json` | `83558c4c4f0abba12c543c30ea79b86f201105bbb361bbbf2d4769f77af61f28` |
| `negative/agent-health-count-negative.json` | `61c8ef2f681ab7ec7573e65175cb56c6e0ed119eba4b79b4e075df7bb4200ee5` |
| `negative/agent-health-fix-both.json` | `623705337ac31006420336f6e9260335eca6d987afefcf9577b94fd89a1dcff8` |
| `negative/agent-health-free-text.json` | `b0fc7fcf1bc8e2295847c16d39beb426f76bb9bc070b4052a777f897f61e2c86` |
| `negative/agent-health-path.json` | `e52300cb3615d3acd3c5e3d05e2d95c6b9fac8f8b1a75d45ae87f92b9ca13453` |
| `negative/agent-health-state-healthy.json` | `169c9a71e17634cf76371293d4d293b594f31a7aa34322d39ac823a9752abf7f` |
| `positive/agent-health-empty.json` | `39ff684c94eebad73fc935b360f96387d62adacbc9ba0d40c105fd9fc1f0d479` |
| `positive/agent-health.json` | `d1c9b43accad8af1a3e3a7c4eb8636e74988e6f36f5e23499c86e09e0fa58830` |
| `semantic/agent-health-empty-ok.json` | `4fd38215ec74478b989d03c76243c4a1cec6e5770f5d5a1c6e282844b5cb42db` |
| `semantic/agent-health-fix-credential.json` | `0479af70613cc6b4b86619e97b2f59a2f36fe4dafaa54d59193eae7da79fc68c` |
| `semantic/agent-health-id-twice.json` | `e4ed40f0442e4fff082a88c875fe242efd892e2caccc3a7563d757c4590b232b` |
| `semantic/agent-health-not-worst.json` | `d8505fd343b23ac59112c3a7bb18e6bdf5d3eb16257a292900d2c86ffa1e0872` |
| `semantic/agent-health-ok.json` | `cb0d1ab665f7e9918b81335510c05ef752cd552ca339eb4f45853a1cb4496562` |
| `semantic/agent-health-service-degraded-ok.json` | `ce35a4ce4a1a5333cd2a313528b85accdeb70d1d04e7c997b35439fc5fc78bda` |
| `semantic/agent-health-service-setup-warn.json` | `5b43541fdba9e4a8538ae3663dc38054f13aee0f2714e659865b14b32caa5e10` |
| `semantic/agent-health-unknown-as-ok.json` | `7b42ecc685fad0942c5cbe2759601e27dde7311c30b4f3163d8c5b76b74f9c33` |
