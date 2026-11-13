# The device's activity collector: usage lines and session intervals from transcripts (OBS-76, OE-01a and OE-01b)

**Status:** design, 2026-11-13. Code follows in OE-01a (target 2026-11-24) and OE-01b (target 2026-11-27).

**Asked by:** Vesta's production stores no activity event at all (board thread `9ad1f8d0`, 2026-11-13). The node's
outbox has one producer today, `grant.usage` (`node/org.py`, the only `enqueue` caller): a device enrolled in an
organization sends nothing about the work done on it. The event shapes are the contract's
(`fabric-agent-contract` `docs/specification/activity.md`, `fabric-activity/0.1`, `schemas/telemetry-event.schema.json`);
this document is Observatory's half: where the collector reads, what it keeps, what it emits and where it sends it.

## A1. What it reads

| Runtime | Where | One model call | Session | Folder |
|---|---|---|---|---|
| Claude Code CLI | `~/.claude/projects/<folder>/<session>.jsonl` (and `CLAUDE_CONFIG_DIR`'s, `paths.transcript_homes`) | a line of `type: assistant` with `message.usage`; its `requestId` (else `message.id`) is the call | `sessionId` | `cwd` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/*.jsonl` | a `token_usage_record` line, its `usage` and `response_id` | `session_meta.payload.id` | `session_meta`'s and `turn_context`'s `cwd` |

Measured on this machine on 2026-11-13 (record keys only, no content read). Nothing else of a line is kept: no
`content`, no tool input or output, no prompt; the contract's `FAC-SEM-037` refuses every key that names content, and
the collector never reads one into memory beyond the line it is parsing.

## A2. How it keeps its place

- **A cursor per file**: its device and inode, the byte offset read so far, and the SHA-256 of its first 4 KB. A file
  whose first 4 KB changed, or whose size fell below the offset, is a new file read from the start; a known call is
  not counted twice (A3).
- **Lines only up to the last newline**: a line still being written is read next pass.
- **A budget per pass** (bytes and seconds, as the disk guard's): a large backlog is read over several passes, oldest
  file first, and the receipt says how much is left (`degraded`, never a silent stop).
- The cursor lives in the node's state (`node/activity/cursors.sqlite`, owner-only), beside the outbox.

## A3. What it emits

- **`usage.line`**, one per call: `model`, `tokens` (`input`, `output`, `cacheRead`, `cacheWrite` from the usage's
  `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`), `cost` and `dedupeKey`.
  `dedupeKey` is the provider's request id, so a call read from two sources (a transcript and OpenTelemetry later) is
  counted once. **Cost:** `null` with no basis unless a price is known; a price list (the OpenRouter catalogue the engine keeps, `store/openrouter-catalogue.json`) gives
  `price-list`; a zero estimate for a call that used tokens is `null` (`FAC-SEM-038`).
- **`session.interval`**, by `method: transcript`: the session's lines in time order make stretches of
  `agent_working` (an assistant line within `idleThresholdSeconds`, 300 by default, of the line before) and `idle`
  (a longer silence); `awaiting_input` is not told apart by a transcript and is not claimed. Intervals of one session
  never overlap, and one session has one collector (`FAC-SEM-100`). An interval is emitted once it is closed (the next
  line, or the idle threshold passed); the last of a session that stopped carries `outcome: unknown`.
- `source` is `claude_code.transcript` or `codex.rollout`; `runtime.name` `claude_code` or `codex`; `session.id` the
  runtime's; `git.branch` per the policy key (`hash` under the device's telemetry key by default, never the plain name
  before a policy says so).
- `sourceKey` and `eventId`: the line's own `uuid` or request id where it has one, else `hmac-sha256:` of the file's
  cursor identity and offset under the device's telemetry key; never a path (the schema refuses one).

## A4. Where it goes: routing by context

A session's events go **only** to the organization server of the enrollment whose organization is the session's
context, and **never** an event of a personal session (`fabric-activity/0.1`, routing by context, DEC-0082). The
context of a session is its folder's (`cwd`) attribution: `folder_attribution.py` and `fabric_attribution.py` resolve a
folder to a Fabric project and its context; a folder in no organization's project is personal, and so is one the
attribution cannot tell (the safe reading: nothing leaves). `project.id` is the project as the organization names it,
from the same attribution. `account.class` comes from Switchboard's provenance for the session's account when it is
known, else `unknown` (OE-02 refines it).

A personal session's events stay on the device, in the local journal the owner reads (`full activity`, OE-03), and are
counted in the receipt as `kept-personal`; they are never put in an outbox.

## A5. The outbox and the stream

`node/org.py`'s `Enrollment.enqueue` writes each event with its stream position (`collector.id`
`observatory-node`, the enrollment's epoch, the next `seq`) and its `bootId` and `monotonicNs` (`CLOCK_BOOTTIME`,
`mach_continuous_time`, `QueryInterruptTimePrecise`). The existing sender (`flush`, signed batches, range acks)
carries them; nothing new is sent another way. A burst past the outbox bound ends in one `buffer.overflow`, as today.

## A6. Proof

- OE-01a: tests over synthetic transcripts of both runtimes, the contract's telemetry fixtures (schema and the
  semantic rules `FAC-SEM-037`, `-038`, `-039`, `-100`), a personal folder's session never enqueued, a line written in
  two halves read once, a rotated file read from the start without counting a call twice.
- OE-01b: `full activity reconcile --day DATE` rebuilds the day's interval sums from the transcripts' raw timestamps
  and compares them with what was emitted; on the operator's machine the difference is at most 5 %, the command's
  output kept in the pilot's report (the plan's gate).
- Then Vesta's `events_raw` holds the pilot device's first events (VES-FIRST-EVENTS).

## A7. Not here

OpenTelemetry (OE-21, a second source deduplicated by `dedupeKey`), the account class from the signed policy's tenants
(OE-02 with OE-06), the owner's page (OE-03), the export (OE-13), and the unit-of-work event (OBS-378, after the
contract's DEC on CT-259).
