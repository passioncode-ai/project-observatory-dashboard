# Control plane: the target, where it stands, and the refactor waves

Status: plan, 2026-11-17. Measured against `main` at `aa074af2` (0.41.0 plus wave 5, #575) by three read-only audits,
one per requirement band of [CONTROL-PLANE.md](../CONTROL-PLANE.md). Each row below names the requirement, what is
true now with its evidence, and the board row that closes the gap. The CP-15 to CP-20 band and the update path are
added when their audit lands (section "Still to measure").

## The target, in one paragraph

A person opens Home and sees, once each, whether the estate is fine and what needs them first; every page shows facts
from one function, never a command to copy; every act a page offers is also an MCP tool with the same effect and the
same gate; a key is classed by where it came from and can never be revoked while a consumer still uses it; every
detector of a secret shape and every redactor is one implementation; an update installs or says exactly why it did
not. Agents and people read the same numbers.

## Where it stands (CP-01 to CP-14)

All suites of these requirements pass (`run_portable.py`, 2026-11-17). Done: CP-04 (sessions outside a lifecycle
owner), CP-08 (key sweep), CP-14 (issues for findings, stricter than its text). Partial: the rest.

| Req | Gap that matters | Evidence | Row |
|---|---|---|---|
| CP-01 Home | "Each fact once" is tested on links, not on shown values; no viewport screenshots in tests | `tests/test_home_page.py:403` | OBS-608 |
| CP-02 Agents | Two session lists on one page with two meanings of "working" (30 min quiet vs 1 h turn); OSS-24 contradicts OSS-119 | `dashboard/agents_page.py:189,458`, `agents_view.py:36` | OBS-599 |
| CP-03 One count | "Active in the last hour" counts ended sessions too, so it is not a subset of "running" | `sessions_view.py:180-195` | OBS-597 |
| CP-05 Tooling | The page and `observatory_machine(section="skills")` compute two different inventories | `tools_page.py:3`, `mcp/server.py:994` | OBS-600 |
| CP-06 Inventory | A project's stack is still guessed from marker files; no page shows the packages | `collectors/scan_dependencies.py:4`, `survey.py:205` | OBS-603 |
| CP-07 Vuln watch | Text says osv-scanner is used; the decision taken was not to run it | `tools/vuln_watch.py:23`, `CONTROL-PLANE.md:49,128` | OBS-608 |
| CP-09 Supply chain | The remote-scanner switch has no client; remote and plugin-shipped servers are neither judged nor pinned | `mcp_supply.py:408`, `supply-chain.md` | OBS-606 |
| CP-10 Keys by source | `shared` means "same value in another folder", not an outward share; no personal or organization context; consumers read launchd only | `key_source.py:157,166` | OBS-604 |
| CP-11 Rotation | Only OpenRouter delivers to consumers; its revoke skips the consumer check when the scan file is missing; `tools/revoke_key.py` revokes with no check; DigitalOcean keeps its own finder | `tools/openrouter.py:2144`, `tools/keyserver.py:313`, `tools/revoke_key.py`, `tools/digitalocean.py:294` | OBS-594, OBS-607 |
| CP-12 Integration health | No token scope check; Heroku and Sentry keys are not asked | `tools/integration_health.py:235` | OBS-605 |
| CP-13 Untracked | `TRACKED` is hand-kept apart from the doors (`sentry` counts as tracked with no door) | `tools/integration_health.py:154` | OBS-605 |
| CP-14 Issues | Text turns key findings off only in public repositories; code keeps them off everywhere | `configuration.py:155` | OBS-608 |

## Cross-cutting refactors

- **One secret-shape detector** (OBS-595): `credential_shape.py` is the rule; four more detectors and three hard-coded
  `sk-or-v1-` checks live beside it (`collectors/scan_env.py:92`, `service_connections.py:68`, `ops_digest.py:83`,
  `tools/check_secrets.py`, `tools/revoke_key.py:53`, `collectors/scan_openrouter.py:91`, `agent/providers.py:742`).
  `ops_digest.py` keeps its JavaScript-compatible reading, expressed as a mode of the one rule.
- **One redactor** (OBS-602): `memory_redact.credential_like` wraps `credential_shape`; five others
  (`maintenance.py:533`, `engine_update.py:173`, `ops_acts.py:104`, `retrieval_audit.py:68`,
  `collectors/scan_lifecycle.py:72`) become calls to one function.
- **Door helpers** (OBS-601): `consumer_sources` three times and `_roll_guard` twice in the doors move into
  `tools/key_consumers.py`; every `degraded` entry carries a `code`; the key server answers typed refusals like the
  MCP tools.
- **Page helpers** (OBS-598): `_e` in six page modules and `_stamp` four ways (one time renders differently on Home and
  Agents) become one `dashboard/page_util.py`; `_now` and `_iso` likewise.
- **No em dash on a page** (OBS-596): the Agents page renders U+2014 in four places.
- **Long functions** (OBS-609): `tools/vault.py` `copy_slot` (226 lines), `tools/digitalocean.py` `cmd_rotate` (175),
  `tools/scan_leaks.py` `main` (347) and `scan_sqlite` (249), `tools/ssh_keys.py` `inventory` (192) and `rekey_plan`
  (184) are split into named steps with their tests unchanged.

## Waves

Each wave is one integration PR (main requires an up-to-date branch, so one integration costs one check run), merged
when the root `unittest discover -s tests` and every touched suite pass, then released.

1. **R1, correctness and safety**: OBS-594 (every revoke through one consumer guard, refusing when it cannot read),
   OBS-595 (one detector), OBS-596, OBS-597.
2. **R2, one truth**: OBS-598, OBS-599, OBS-600, OBS-601, OBS-602.
3. **R3, the missing halves**: OBS-603, OBS-604, OBS-605, OBS-606, OBS-607 (delivery to consumers for Cloudflare and
   Heroku).
4. **R4, the documents tell the truth**: OBS-608 (CP-01 tests, CP-07, D2, CP-14, the M1 artefact name, `rotation.md`,
   OSS-24).
5. **R5, the CP-15 to CP-20 band and the update path**: rows filed when that audit lands; OBS-593 (who reloads the
   server during an update) belongs here.
6. **R6, long functions**: OBS-609.

Incoming board requests are taken between waves, or folded into the running wave when they touch its files.

## Still to measure

CP-15 (daily doctor), CP-16 (disk), CP-17 (navigation), CP-18 (parity: 72 CLI verbs without a tool, grouped into
families), CP-19 (agent panel), CP-20 (configuration door), the update and install path, and the MCP surface (tool
docstrings, the decision check implemented in several places, the instructions length).
