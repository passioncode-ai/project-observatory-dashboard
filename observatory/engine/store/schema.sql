-- Project Observatory — the store.
--
-- Three groups, and the separation is load-bearing. CANONICAL is authored and
-- append-only; MEASURED is deterministic and replayable from the machine itself;
-- DERIVED is rebuildable and carries no meaning. See docs/ARCHITECTURE.md.
--
-- Requires sqlite-vec for vec_notes. Everything else is plain SQLite.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─────────────────────────────── CANONICAL ───────────────────────────────
-- Append-only. A correction supersedes a revision; it never rewrites one.
CREATE TABLE IF NOT EXISTS ledger (
  memory_id         TEXT    NOT NULL,
  revision          INTEGER NOT NULL CHECK (revision >= 1),
  kind              TEXT    NOT NULL,
  project_id        TEXT,
  agent_id          TEXT,
  run_id            TEXT,
  session_id        TEXT,
  -- function × scope are independent axes, per the Fabric memory contract.
  function          TEXT    NOT NULL CHECK (function IN
                      ('working','episodic','semantic','experiential')),
  scope             TEXT    NOT NULL CHECK (scope IN
                      ('run','agent-private','project','global')),
  statement         TEXT    NOT NULL,
  why               TEXT,
  state             TEXT    NOT NULL CHECK (state IN
                      ('proposed','observed','supported','contested','stale',
                       'superseded','rejected','archived')),
  confidence        REAL    CHECK (confidence > 0 AND confidence <= 1),
  -- owner is NOT NULL on purpose: trap T7 was an unnamed writer defaulting to
  -- the highest authority in the system.
  owner             TEXT    NOT NULL,
  classification    TEXT    NOT NULL DEFAULT 'project-internal'
                      CHECK (classification IN ('public','project-internal','confidential')),
  -- `retention_policy` STOOD HERE and nothing ever read it: retention decides
  -- by state age and `owner_exempt` (store/retention.json), and `append` wrote
  -- the literal 'default' on every row. Dropped by migration
  -- 0007-drop-inert-retention-policy — a per-row policy is a flag at the call
  -- site, which this store's own retention doctrine refuses, and the exemption
  -- added on 2026-09-08 proved it: it protected rows written before it, which a
  -- column could not.
  valid_from        TEXT,                      -- real-world validity,
  valid_to          TEXT,                      -- distinct from created_at
  supersedes_json   TEXT    NOT NULL DEFAULT '[]',
  conflicts_with_json TEXT  NOT NULL DEFAULT '[]',
  provenance_json   TEXT    NOT NULL DEFAULT '[]',
  evidence_json     TEXT    NOT NULL DEFAULT '[]',
  created_at        TEXT    NOT NULL,
  -- AGENT MEMORY (migration 0008). Which workflow step a record came out of,
  -- who executed it ({provider, model, accountRef} — an opaque account handle,
  -- never an address or a token), and the structured body a checkpoint or a
  -- handoff pack carries. NULL on every record that is not part of a workflow.
  workflow_id       TEXT,
  step_id           TEXT,
  executor_json     TEXT,
  body_json         TEXT,
  PRIMARY KEY (memory_id, revision)
) STRICT;
CREATE INDEX IF NOT EXISTS ledger_project ON ledger(project_id, state);
CREATE INDEX IF NOT EXISTS ledger_state   ON ledger(state, created_at);
CREATE INDEX IF NOT EXISTS ledger_workflow ON ledger(workflow_id, kind)
  WHERE workflow_id IS NOT NULL;

-- A WORKFLOW outlives the sessions, accounts, models and providers that work on
-- it. Its state is the checkpoint chain in `ledger` (kind 'checkpoint'); this
-- row is only what has no revision history: whether it is still open.
CREATE TABLE IF NOT EXISTS workflows (
  workflow_id TEXT PRIMARY KEY,
  project_id  TEXT,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('open','closed')),
  closed_at   TEXT,
  closed_by   TEXT
) STRICT;

-- ONE EXECUTOR AT A TIME. A lease is the right to write the workflow's next
-- checkpoint, proven by its token. `offered` is a handoff waiting to be
-- accepted, and lapses at `expires_at`; `active` is the current executor and
-- has no expiry, because the session it would expire away from is gone and the
-- only way to the next one is a handoff. The partial unique indexes make "one
-- active, one offered" a property of the table rather than of the code.
CREATE TABLE IF NOT EXISTS workflow_leases (
  lease_ref     TEXT PRIMARY KEY,
  workflow_id   TEXT NOT NULL REFERENCES workflows(workflow_id),
  state         TEXT NOT NULL CHECK (state IN ('offered','active','ended')),
  token         TEXT UNIQUE,
  holder        TEXT,
  executor_json TEXT NOT NULL DEFAULT '{}',
  handoff_id    TEXT,
  granted_at    TEXT NOT NULL,
  expires_at    TEXT,
  accepted_at   TEXT,
  ended_at      TEXT,
  ended_reason  TEXT
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS workflow_one_active
  ON workflow_leases(workflow_id) WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS workflow_one_offer
  ON workflow_leases(workflow_id) WHERE state = 'offered';
CREATE UNIQUE INDEX IF NOT EXISTS workflow_lease_handoff
  ON workflow_leases(handoff_id) WHERE handoff_id IS NOT NULL;

-- A RETRY IS NOT A SECOND WRITE. A write carries a key; the same key with the
-- same request returns the answer recorded the first time, in the transaction
-- that made it, and the same key with a different request is refused.
CREATE TABLE IF NOT EXISTS idempotency (
  principal      TEXT NOT NULL,
  operation      TEXT NOT NULL,
  key            TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  response_json  TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (principal, operation, key)
) STRICT;

-- Erasure leaves a trace. Retention writes here; it never DELETEs from ledger.
CREATE TABLE IF NOT EXISTS tombstones (
  memory_id   TEXT    NOT NULL,
  revision    INTEGER NOT NULL,
  reason      TEXT    NOT NULL,
  approved_by TEXT    NOT NULL,
  created_at  TEXT    NOT NULL,
  PRIMARY KEY (memory_id, revision)
) STRICT;

-- The ledger append and this row are ONE transaction. No index write may happen
-- without a committed revision behind it.
CREATE TABLE IF NOT EXISTS outbox (
  seq                INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id          TEXT    NOT NULL,
  revision           INTEGER NOT NULL,
  projection_version INTEGER NOT NULL DEFAULT 1,
  consumed_at        TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(consumed_at) WHERE consumed_at IS NULL;

-- A proposed change to registry/*.json. It NEVER mutates the registry: that is
-- written by collectors and by the operator. A proposal is a row awaiting a
-- decision, because a writer that could edit the fact base directly would
-- poison it one plausible sentence at a time.
CREATE TABLE IF NOT EXISTS proposals (
  id            TEXT PRIMARY KEY,
  target_id     TEXT NOT NULL,          -- project:… | repository:… | domain:…
  patch_json    TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed','accepted','rejected','superseded')),
  decided_by    TEXT,
  decided_note  TEXT,
  created_at    TEXT NOT NULL,
  decided_at    TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS proposals_pending ON proposals(status, created_at);

-- ──────────────────────────────── MEASURED ───────────────────────────────
CREATE TABLE IF NOT EXISTS scans (
  id                TEXT PRIMARY KEY,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  collector_version TEXT NOT NULL,
  counts_json       TEXT NOT NULL DEFAULT '{}',
  degraded_json     TEXT NOT NULL DEFAULT '[]'   -- never omitted; empty means full coverage
) STRICT;

CREATE TABLE IF NOT EXISTS observations (
  id          TEXT PRIMARY KEY,
  scan_id     TEXT NOT NULL REFERENCES scans(id),
  subject_id  TEXT NOT NULL,
  kind        TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  observed_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS observations_subject ON observations(subject_id, observed_at);

CREATE TABLE IF NOT EXISTS events (
  id           TEXT PRIMARY KEY,
  project_id   TEXT,
  repo_id      TEXT,
  kind         TEXT NOT NULL,     -- commit | session | deploy | scan | …
  ref          TEXT,              -- sha, session id, release tag
  actor        TEXT,
  occurred_at  TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
) STRICT;
CREATE INDEX IF NOT EXISTS events_project_time ON events(project_id, occurred_at);
CREATE UNIQUE INDEX IF NOT EXISTS events_dedup ON events(kind, ref) WHERE ref IS NOT NULL;

-- What the agent reads, and the only thing it reads. Dropped once consumed:
-- function 'working' expires and is never promoted directly.
CREATE TABLE IF NOT EXISTS deltas (
  id          TEXT PRIMARY KEY,
  from_scan   TEXT REFERENCES scans(id),
  to_scan     TEXT NOT NULL REFERENCES scans(id),
  subject_id  TEXT NOT NULL,
  kind        TEXT NOT NULL,
  before_json TEXT,
  after_json  TEXT,
  consumed_at TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS deltas_pending ON deltas(consumed_at) WHERE consumed_at IS NULL;

-- ──────────────────────── DERIVED PROJECTIONS ────────────────────────────
-- Rebuildable from CANONICAL. Losing these is a rebuild, never a data loss.
-- embedding_version and indexed_at are receipts, not meaning.
-- WHERE A COLLECTOR GOT TO. One row per named cursor, never pruned, and it is
-- STATE rather than a measurement: nothing here is a fact about the estate, so
-- nothing here belongs in `observations`, which retention keeps only three of.
--
-- The first cursor exists because `compute_deltas diff` decided what to compare
-- from timestamps alone and therefore could not tell what it had already
-- compared. Two live consequences, both measured 2026-09-07: a repeated `diff`
-- wrote the same delta twice (three such rows in the live store, all still
-- pending, so the agent would have read one change twice), and a SKIPPED diff
-- lost a generation outright — three snapshots and one diff produced deltas for
-- the last pair only, and the command printed a confident "2 delta(s) written"
-- while a rename in the middle generation became nothing at all.
--
-- Inferring it from `deltas.to_scan` instead would have worked until retention
-- deleted the consumed rows it was inferred from. A cursor is small, durable,
-- and says what it means.
CREATE TABLE IF NOT EXISTS cursors (
  name       TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS vec_meta (
  projection_version INTEGER NOT NULL,
  embedding_provider TEXT    NOT NULL,
  embedding_model    TEXT    NOT NULL,
  dims               INTEGER NOT NULL,
  checkpointed_seq   INTEGER NOT NULL DEFAULT 0
) STRICT;
-- The pinned estate contract. Boot MUST refuse a mismatch, not warn (trap T9).
INSERT INTO vec_meta (projection_version, embedding_provider, embedding_model, dims)
  SELECT 1, 'openai', 'text-embedding-3-small', 1536
  WHERE NOT EXISTS (SELECT 1 FROM vec_meta);

-- vec_notes is created by store/indexer.py once sqlite-vec is loaded, so this
-- schema stays applicable on a machine without the extension:
--
--   CREATE VIRTUAL TABLE vec_notes USING vec0(
--     memory_id TEXT PARTITION KEY, revision INTEGER, embedding float[1536]);
--
-- It carries no meaning. Losing it is a rebuild — `indexer.py rebuild` — not a
-- data loss, and that is the invariant the whole separation exists to protect.

-- `stems`: every word's search key (textkeys.py) — Snowball stems, Russian cut
-- to six characters — so a question finds a record in another word form.
-- Written in the same transaction as the ledger revision (ledger.insert_revision),
-- so a record is searchable the moment it exists; the indexer rewrites it.
CREATE VIRTUAL TABLE IF NOT EXISTS search_notes USING fts5(
  memory_id UNINDEXED, revision UNINDEXED, statement, why, stems);

-- ROLLUPS: small, permanent, and NOT rebuildable once the raw events age out.
--
-- Every "statistic" in this system used to be a `sum(1 for ...)` over the
-- current registry, so the only real time series was the raw commit stream —
-- 365 days by `retention.json`, after which "was this project active in Q1"
-- stopped having an answer. An aggregate is three orders of magnitude smaller
-- than what it summarises (156 projects x 52 weeks is ~8,000 rows a year), so it
-- is kept for ever while the rows under it are still pruned.
--
-- `frozen_at` is the load-bearing column. A week is recomputed only while its
-- WHOLE span lies inside the event window; once retention's cutoff passes the
-- week's start the row is frozen, because recomputing it would silently replace
-- a measurement with a zero — destroying exactly what this table exists to keep.
CREATE TABLE IF NOT EXISTS project_week (
  project_id   TEXT NOT NULL,
  week         TEXT NOT NULL,          -- ISO year-week in UTC, e.g. 2026-W36
  week_start   TEXT NOT NULL,          -- the Monday, so a reader can order by time
  commits      INTEGER NOT NULL,
  active_days  INTEGER NOT NULL,       -- days with a COMMIT. Its meaning is frozen:
                                       -- widening it would make every existing row
                                       -- incomparable with every new one.
  authors      INTEGER NOT NULL,       -- distinct commit actors
  -- WORK WITHOUT A COMMIT. `last_activity_on` learned this the hard way; the
  -- weekly series, which is the one table that outlives its source, had not.
  -- Measured 2026-09-07: 45 (project, week) pairs held claude-mem sessions and
  -- no commit, so they produced no row at all — and the freeze rule would have
  -- made each of them a permanent zero once its week passed the 365-day cutoff.
  --
  -- NULLABLE on purpose. NULL means "this row was computed before the measure
  -- existed", which for a FROZEN row can never be completed; 0 would claim it
  -- was measured and found empty. The same distinction `resolves: None` carries
  -- for a domain probe.
  sessions     INTEGER,                -- session events in the week
  session_days INTEGER,                -- days with a session
  worked_days  INTEGER,                -- days with EITHER, as a set union: it cannot
                                       -- be derived from the two counts above
  first_at     TEXT,
  last_at      TEXT,
  computed_at  TEXT NOT NULL,
  frozen_at    TEXT,                   -- set once the raw events fell out of the window
  PRIMARY KEY (project_id, week)
);
CREATE INDEX IF NOT EXISTS project_week_by_week ON project_week(week_start);

-- METRICS: sampled values about a project, from a plugin, over time.
--
-- NOT registry facts. The registry answers what EXISTS — this project lives
-- here, is implemented by that repository — and it is validated, in git, and
-- rewritten whole on every emit. A traffic figure, an error rate, an uptime
-- percentage or a byte count is none of those things: it is a measurement taken
-- at an instant, it accumulates, and it belongs beside `events` rather than
-- inside a typed fact base that a rebuild replaces.
--
-- That distinction is what collapses "adding an analytics source means editing
-- six core files" into one contract. A plugin declares itself in
-- `plugins/<id>.json` and writes rows here; nothing in the merge, the emitter,
-- the validator, the findings builder or the dashboard has to learn its name.
--
-- `(project_id, metric, at)` is the key, so re-running a plugin over a period it
-- has already measured replaces rather than duplicates — the same idempotence
-- `events` gets from UNIQUE(kind, ref).
CREATE TABLE IF NOT EXISTS metrics (
  project_id  TEXT NOT NULL,
  metric      TEXT NOT NULL,          -- e.g. disk.bytes, traffic.sessions
  at          TEXT NOT NULL,          -- UTC Z, the instant the sample describes
  value       REAL NOT NULL,
  unit        TEXT NOT NULL DEFAULT '',
  source      TEXT NOT NULL,          -- the plugin id that measured it
  payload_json TEXT,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (project_id, metric, at)
);
CREATE INDEX IF NOT EXISTS metrics_by_metric ON metrics(metric, at);
CREATE INDEX IF NOT EXISTS metrics_by_source ON metrics(source);
