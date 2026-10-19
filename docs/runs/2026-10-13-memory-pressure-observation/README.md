# Independently dated macOS memory pressure

## State now

The lightweight memory collector adds the platform pressure level separately from
swap usage and process RSS. This is an observation-only follow-up to the existing
OBS-87 memory scope. No process or service lifecycle action is introduced.

The read-only kern.memorystatus_vm_pressure_level sysctl emits dispatch levels.
The mapping is supported by Apple's published XNU source:
[sysctl conversion](https://github.com/apple-oss-distributions/xnu/blob/xnu-10063.101.15/bsd/kern/kern_memorystatus_notify.c#L1759),
[normal/warn/critical conversion](https://github.com/apple-oss-distributions/xnu/blob/xnu-10063.101.15/bsd/kern/kern_memorystatus_notify.c#L1692),
[dispatch constants](https://github.com/apple-oss-distributions/xnu/blob/xnu-10063.101.15/bsd/sys/event_private.h#L301).
This private platform interface may be absent; unsupported codes or command failure
remain unknown. It does not read the internal kernel enum as a dispatch code.

The pressure record carries its own timestamps, source, reason and raw supported
code. Saved summaries project a whitelist and preserve unknown on stale/future,
inconsistent or malformed records. A pressure-only success cannot refresh the
RAM/swap timestamp. Unsupported platforms remain explicit unknown; no inferred
percentage or Linux/macOS numerical equivalence is invented.

Swap remains a distinct metric, including the existing swap-based finding and
wording. Normal pressure does not imply low swap, recovered disk space or complete
process ownership. The existing memory summary is shared by CLI and MCP.

## Verification

New tests first failed for absent pressure state and unsanitized saved fields.
The collector fixture initially rejected the newly added sysctl name; its explicit
read-only command map was extended, without weakening existing time/argv bounds.
Final focused receipts and source hashes are in verification.json.
Independent review, hosted compatibility and installed acceptance remain pending.

Next task: independently verify all3 valid pressure codes, unknown/failure, own
freshness, malformed saved state, RAM timestamp independence and unchanged
no-lifecycle behavior; then publish an observation-only draft stack.
