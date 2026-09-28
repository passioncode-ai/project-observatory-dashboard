#!/usr/bin/env python3
"""Project Observatory — the deterministic pipeline.

    setup      create .venv (the MCP server's shebang points at it)
    deps       install the pinned MCP SDK and sqlite-vec — mcp==2.1.1, the only line that
               speaks protocol revision 2026-07-28. The LLM provider needs no
               SDK: OpenRouter is HTTP and urllib reaches it.
    scan       collect: filesystem, GitHub, the wiki
    env        every .env under the estate, as names and never as values
    leaks      hunt the estate's OWN values in transcripts, logs and the tree
    use        tools/use_secret.py run <project> <NAME> -- <cmd>: a secret reaches
               a command's environment and never the agent's transcript
    merge      build the model from what was collected
    emit       write the typed registry
    validate   gate the registry
    dashboard  render the HTML view
    project    write the read-only projection into the wiki
    commit-projection  commit it there under a lease, or explain why not
    links      audit the wiki's wikilinks; non-zero if any resolves to nothing
    design     check the dashboard against the SHELEG Workbench pack
    smoke      execute the dashboard's script against a stub DOM; blank page = non-zero
    findings   rebuild registry/findings.json from the typed registry
    notify     notify once about findings that are new or have got worse
    corroborate  promote a proposal whose claim a machine can independently re-check
    export-ledger  write registry/ledger.jsonl — the only copy outside the store
    ledger-current  non-zero if that export is behind the store
    review     the operator's queue; writes need a terminal (tools/review.py --help)
    scan-events  read commits out of every local checkout into the store
    probes     run the declared Fabric probes against the live MCP server
    test       planted trap fixtures
    test-ledger  the write path's invariants — owner guard, CAS, lifecycle, tombstones
    test-skill   the companion plugin, including the SessionStart hook it must NOT ship
    test-agent   the agent's deterministic half: the client-side ceiling and its three
                 degradations. No model is called.
    snapshot     fingerprint the registry
    deltas       write what moved since the previous fingerprint
    pending      what the agent has not consumed yet
    agent        interpret the deltas — one of two steps that can spend anything
    index        build the derived projections from the outbox (the other one)
    reindex      drop both projections and rebuild them from canon
    index-status what is indexed, what is pending
    chain        which models would be used, with live prices and health
    wallet       what has been spent, against all three guardrails
    key          whether a key is resolvable, and where it was looked for
    revoke       list the account's keys; tools/revoke_key.py --tail <t> revokes one
    models       refresh the provider catalogue now
    tick         one full cycle, the way launchd runs it
    validate-plugin  both `claude plugin validate --strict` runs
    test-wire  drive mcp/server.py over real stdio
    check      validate + fabric + test + test-wire + probes
    all        every step above, in order

Two steps can SPEND: `agent` interprets deltas through a model, and `index`
embeds what it wrote. Everything else is deterministic and free. This paragraph
said the opposite — "no model participates" and "the LLM layer is not built yet"
— for three days after the agent was scheduled and spending, fifty lines above
the table that lists it. `tools/check_docs.py` now fails on both sentences.
"""
from __future__ import annotations
import hashlib
import json, os, re, sqlite3, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# `paths` for the artefacts a NON_MUTATING group must not write. Imported here
# rather than inside the function so a typo is a startup error rather than a
# failure in the middle of a gate.
sys.path.insert(0, str(ROOT))
import configuration
try:
    import paths              
except configuration.ConfigurationError as _exc:
    print(f"Observatory: {_exc}", file=sys.stderr)
    raise SystemExit(2) from None

# ONE interpreter for the whole pipeline. Until 2026-09-05 the steps were split:
# `sys.executable` for merge, emit, projection and validation — which under
# `./observatory.py` is Homebrew's python3.14 — and `.venv/bin/python` (3.12) for
# everything else, while tools/tick.sh ran ALL of them under the venv. The same
# code therefore ran on two interpreters depending on who started it. It worked
# only because those four files are stdlib-only; the day one of them needs a
# third-party import it breaks on one path and not the other, and the difference
# is invisible from either.
PY = str(ROOT / ".venv" / "bin" / "python")
if not Path(PY).exists():                                                       
    PY = sys.executable
STEPS = {
    "setup":     ["uv", "venv", ".venv"],
    "deps":      ["uv", "pip", "install", "--python", ".venv/bin/python",
                  "mcp==2.1.1", "jsonschema", "sqlite-vec",
                  # google-auth signs the service-account JWTs the GSC and GA4
                  # plugins need; a launchd tick cannot do an OAuth browser dance
                  #. Pure-python, no build step.
                  "google-auth"],
    "chain":     [PY, "agent/providers.py", "chain"],
    "wallet":    [PY, "agent/providers.py", "wallet"],
    "key":       [PY, "agent/providers.py", "key"],
    "revoke":    [PY, "tools/revoke_key.py", "--list"],
    "models":    [PY, "agent/providers.py", "refresh"],
    "scan-events": [PY, "collectors/scan_events.py"],
    "remotes":   [PY, "collectors/scan_remotes.py", "store/raw/remotes.json"],
    "scan-bb":   [PY, "collectors/scan_bitbucket.py", "store/raw/bitbucket.json"],
    "domains":   [PY, "collectors/scan_domains.py", "store/raw/domains_live.json"],
    "heroku":    [PY, "collectors/scan_heroku.py", "store/raw/heroku.json"],
    "scan-cloudflare": [PY, "collectors/scan_cloudflare.py", "store/raw/cloudflare_zones.json"],
    "scan-mcp":  [PY, "collectors/scan_mcp.py", "store/raw/mcp.json"],
    "openrouter": [PY, "collectors/scan_openrouter.py", "store/raw/openrouter.json"],
    "env":       [PY, "collectors/scan_env.py", "store/raw/env.json"],
    # Gated to once a day inside the scan itself: every run pulls every
    # production secret of every application into one process.
    "remote-env": [PY, "collectors/scan_remote_env.py", "store/raw/remote-env.json"],
    # Cached for twelve hours inside the scan: thirty-eight properties is
    # seventy-six calls and a minute, and analytics settle daily.
    "google":    [PY, "collectors/scan_google.py", "store/raw/google.json"],
    "leaks":     [PY, "tools/scan_leaks.py"],
    "use":       [PY, "tools/use_secret.py", "--help"],
    "probes":    [PY, "tools/run_probes.py"],
    "probes-check": [PY, "tools/run_probes.py", "--check"],
    "design":    [PY, "dashboard/audit_pack.py"],
    "smoke":     ["node", "dashboard/smoke.js", "docs/projects-dashboard.html"],
    "smoke-pages": [PY, "dashboard/smoke_pages.py"],
    "findings":  [PY, "tools/build_findings.py"],
    "notify":    [PY, "tools/notify_findings.py"],
    "corroborate": [PY, "tools/corroborate.py"],
    "export-ledger": [PY, "tools/export_ledger.py"],
    "ledger-current": [PY, "tools/export_ledger.py", "--check"],
    "review":    [PY, "tools/review.py", "list"],
    "digest":    [PY, "tools/review.py", "digest"],
    "links":     [PY, "tools/audit_vault_links.py"],
    "lost":      [PY, "tools/record_lost_projects.py"],
    "sweep":     [PY, "tools/sweep_fixtures.py"],
    "integrity":  [PY, "tools/check_store.py", "--print"],
    "backup":    [PY, "tools/backup_store.py"],
    "docs-current": [PY, "tools/check_docs.py"],
    "paths-current": [PY, "tools/check_paths.py"],
    "secrets": [PY, "tools/check_secrets.py"],
    "test-queue": [PY, "tests/test_review_queue.py"],
    "test-wire-contract": [PY, "tests/test_wire_contract.py"],
    "test-docs": [PY, "tests/test_docs_current.py"],
    "test-dead-data": [PY, "tests/test_dead_data.py"],
    "test-projection": [PY, "tests/test_projection.py"],
    "test-tick-repo": [PY, "tests/test_tick_repo.py"],
    "test-identity": [PY, "tests/test_caller_identity.py"],
    "test-render": [PY, "tests/test_render_surface.py"],
    "test-sessions": [PY, "tests/test_sessions.py"],
    "test-indexer-load": [PY, "tests/test_indexer_load.py"],
    "test-corroboration": [PY, "tests/test_corroboration.py"],
    "test-tick-failures": [PY, "tests/test_tick_failures.py"],
    "test-notification": [PY, "tests/test_notification.py"],
    "test-hook": [PY, "tests/test_companion_hook.py"],
    "test-interpretation": [PY, "tests/test_interpretation.py"],
    "test-render-page": [PY, "tests/test_dashboard_render.py"],
    "test-recall": [PY, "tests/test_recall.py"],
    "test-validator": [PY, "tests/test_validator_rules.py"],
    "test-domain-probe": [PY, "tests/test_domain_probe.py"],
    "test-listings": [PY, "tests/test_api_listings.py"],
    "test-merge": [PY, "tests/test_merge_membership.py"],
    "test-erasure": [PY, "tests/test_erasure_bytes.py"],
    "test-erasure-scope": [PY, "tests/test_export_scope.py"],
    "test-collector-state": [PY, "tests/test_collector_state.py"],
    "test-delivery": [PY, "tests/test_delivery.py"],
    "test-freshness": [PY, "tests/test_freshness.py"],
    "test-project-surface": [PY, "tests/test_project_surface.py"],
    "test-metric-series": [PY, "tests/test_metric_series.py"],
    "test-companion": [PY, "tests/test_companion.py"],
    "test-footprint": [PY, "tests/test_footprint.py"],
    "test-budget": [PY, "tests/test_budget_subject.py"],
    "test-agent-queue": [PY, "tests/test_agent_queue.py"],
    "test-lost-projects": [PY, "tests/test_lost_projects.py"],
    "test-estate-history": [PY, "tests/test_estate_history.py"],
    "test-cause": [PY, "tests/test_cause_and_symptom.py"],
    "test-clone-sync": [PY, "tests/test_clone_sync.py"],
    "test-fixture-sweep": [PY, "tests/test_fixture_sweep.py"],
    "test-unpublished": [PY, "tests/test_unpublished_work.py"],
    "test-checkout": [PY, "tests/test_checkout_subject.py"],
    "test-truncation": [PY, "tests/test_no_silent_truncation.py"],
    "test-key-shape": [PY, "tests/test_key_shape.py"],
    "test-identity-drift": [PY, "tests/test_project_identity.py"],
    "test-confidence": [PY, "tests/test_mechanical_confidence.py"],
    "test-standdown": [PY, "tests/test_tick_standdown.py"],
    "test-dark-cost": [PY, "tests/test_dark_domain_cost.py"],
    "test-stale-expected": [PY, "tests/test_stale_is_expected.py"],
    "test-agent-faults": [PY, "tests/test_agent_faults.py"],
    "test-integrity": [PY, "tests/test_store_integrity.py"],
    "test-release": [PY, "tests/test_release_cadence.py"],
    "test-portfolio": [PY, "tests/test_portfolio_review.py"],
    "test-omission": [PY, "tests/test_page_omission.py"],
    "test-ledger-ids": [PY, "tests/test_ledger_pointer.py"],
    "test-provider-health": [PY, "tests/test_provider_health.py"],
    "test-heroku": [PY, "tests/test_heroku.py"],
    "test-openrouter": [PY, "tests/test_openrouter_keys.py"],
    "test-credentials": [PY, "tests/test_credentials.py"],
    "test-remote-env": [PY, "tests/test_remote_env.py"],
    "test-google": [PY, "tests/test_google.py"],
    "test-project-secrets": [PY, "tests/test_project_secrets.py"],
    "test-reveals": [PY, "tests/test_reveals.py"],
    "test-store-modes": [PY, "tests/test_store_modes.py"],
    # THE COMPANION'S MEMORY, scrubbed of the estate's values:
    # claude-mem's own redactor knows key SHAPES, not this estate's values, and
    # 26 of them sat in its summaries. Runs after `leaks`, which measures them.
    "scrub-companion": [PY, "tools/scrub_companion.py"],
    "test-scrub": [PY, "tests/test_scrub.py"],
    "test-session-start": [PY, "tests/test_session_start.py"],
    "test-backup-store": [PY, "tests/test_backup_store.py"],
    "test-plugin-pair": [PY, "tests/test_plugin_pair.py"],
    "test-publish-contract": [PY, "tests/test_publish_contract.py"],
    "test-recovery": [PY, "tests/test_recovery.py"],
    "test-signature": [PY, "tests/test_signature.py"],
    "test-env": [PY, "tests/test_env_inventory.py"],
    "test-use-secret": [PY, "tests/test_use_secret.py"],
    # NODE, like `smoke`, and for the same reason: the only honest check of a
    # tab is to render it, and the page's script is JavaScript. It drives the
    # built page rather than the source.
    "test-env-tab": ["node", "tests/env_tab_check.js", "docs/projects-dashboard.html"],
    "test-keyserver": [PY, "tests/test_keyserver.py"],
    "test-store-faults": [PY, "tests/test_store_faults.py"],
    "test-gate-skips": [PY, "tests/test_gate_skips.py"],
    "test-block": [PY, "tests/test_temporary_block.py"],
    "test-lever": [PY, "tests/test_reclaimable_pressure.py"],
    "test-queue-order": [PY, "tests/test_queue_order.py"],
    "test-residue": [PY, "tests/test_policy_residue.py"],
    "test-stale-collapse": [PY, "tests/test_stale_collapse.py"],
    "test-labels": [PY, "tests/test_metric_labels.py"],
    "test-work-tiles": [PY, "tests/test_work_tiles.py"],
    "test-attribution": [PY, "tests/test_side_effect_attribution.py"],
    "test-handoff": [PY, "tests/test_handoff.py"],
    "test-site-kinds": [PY, "tests/test_skip_site_kinds.py"],
    "test-at-stake": [PY, "tests/test_at_stake.py"],
    "shape": [PY, "tools/registry_shape.py"],
    "test-shape": [PY, "tests/test_registry_shape.py"],
    "test-unobservable": [PY, "tests/test_unobservable.py"],
    "test-blank-page": [PY, "tests/test_blank_page.py"],
    "test-witness": [PY, "tests/test_witness.py"],
    "test-horizon": [PY, "tests/test_horizon.py"],
    "test-recount": [PY, "tests/test_recount.py"],
    "test-git-locale": [PY, "tests/test_git_locale.py"],
    "test-foreign": [PY, "tests/test_foreign_vocabulary.py"],
    "test-rdap-keys": [PY, "tests/test_rdap_keys.py"],
    "test-wire-alive": [PY, "tests/test_wire_survives.py"],
    "test-absent-fields": [PY, "tests/test_absent_fields.py"],
    "test-wire-inputs": [PY, "tests/test_wire_inputs.py"],
    "test-atomic": [PY, "tests/test_atomic_writers.py"],
    "test-two-surfaces": [PY, "tests/test_two_surfaces.py"],
    "test-companion-faults": [PY, "tests/test_companion_faults.py"],
    "test-fold": [PY, "tests/test_delta_fold.py"],
    "test-remedy": [PY, "tests/test_remedy_fits.py"],
    "skip-sites": [PY, "tools/skip_sites.py"],
    "test-rollup-sessions": [PY, "tests/test_rollup_sessions.py"],
    "test-plugin-report": [PY, "tests/test_plugin_reporting.py"],
    "test-fs-scan": [PY, "tests/test_filesystem_scan.py"],
    "test-secrets": [PY, "tests/test_secrets.py"],
    "test-write-surface": [PY, "tests/test_write_surface.py"],
    "test-receipt": [PY, "tests/test_conformance_receipt.py"],
    "test-interp-contract": [PY, "tests/test_interpretation_contract.py"],
    "test-gate-purity": [PY, "tests/test_gate_purity.py"],
    "test-provider": [PY, "tests/test_provider_boundary.py"],
    "test-search": [PY, "tests/test_search_path.py"],
    "test-mirror": [PY, "tests/test_projection_mirror.py"],
    "plugins-check": [PY, "collectors/run_plugins.py", "--check"],
    "test":      [PY, "tests/test_traps.py"],
    "test-scan-ids": [PY, "tests/test_scan_ids.py"],
    "trap-map":  [PY, "tools/trap_map.py", "--check"],
    "test-trap-map": [PY, "tests/test_trap_map.py"],
    "test-trap-efficacy": [PY, "tests/test_trap_efficacy.py"],
    "test-finding-rules": [PY, "tests/test_finding_rules.py"],
    "test-tracer": [PY, "tests/test_trace_opens.py"],
    "test-ledger": [PY, "tests/test_ledger.py"],
    "test-skill": [PY, "tests/test_skill.py"],
    "test-agent": [PY, "tests/test_agent.py"],
    "test-key":  [PY, "tests/test_key.py"],
    "test-install-key": [PY, "tests/test_install_key.py"],
    "test-vault": [PY, "tests/test_vault.py"],
    "test-analytics": [PY, "tests/test_analytics_plugins.py"],
    "test-doors": [PY, "tests/test_credential_doors.py"],
    "test-surfaces": [PY, "tests/test_estate_surfaces.py"],
    "test-mcp-inventory": [PY, "tests/test_mcp_inventory.py"],
    "test-ack": [PY, "tests/test_ack.py"],
    "test-pages": [PY, "tests/test_pages.py"],
    "test-serverd": [PY, "tests/test_serverd.py"],
    "test-skill-check": [PY, "tests/test_skill_check.py"],
    "server-status": [PY, "tools/serverd.py", "--status"],
    "test-index": [PY, "tests/test_index.py"],
    "test-retention": [PY, "tests/test_retention.py"],
    "retention":  [PY, "store/retention.py", "plan"],
    "retention-apply": [PY, "store/retention.py", "apply"],
    "snapshot":  [PY, "collectors/compute_deltas.py", "snapshot"],
    "deltas":    [PY, "collectors/compute_deltas.py", "diff"],
    "pending":   [PY, "collectors/compute_deltas.py", "pending"],
    "agent":     [PY, "agent/observe.py"],
    "index":     [PY, "store/indexer.py", "index"],
    "reindex":   [PY, "store/indexer.py", "rebuild"],
    "index-status": [PY, "store/indexer.py", "status"],
    "tick":      ["bash", "tools/tick.sh"],
    "validate-plugin": ["bash", "-c",
                        "claude plugin validate skill --strict && "
                        "claude plugin validate skill/plugins/observatory-log --strict"],
    "test-wire": [PY, "tests/test_mcp_wire.py"],
    "scan-fs":   [PY, "collectors/scan_filesystem.py", "store/raw/local.json"],
    "scan-gh":   [PY, "collectors/scan_github.py", "store/raw/gh"],
    "scan-vault":[PY, "collectors/scan_vault.py", "store/raw/vault.json"],
    "scan-sessions": [PY, "collectors/scan_sessions.py",
                      "store/raw/sessions.json"],
    "merge":     [PY, "collectors/merge.py", "store/raw"],
    "emit":      [PY, "collectors/emit_registry.py", "store/raw"],
    "validate":  [PY, "tools/validate_registry.py"],
    "dashboard": [PY, "dashboard/build_dashboard.py"],
    "project":   [PY, "tools/project_into_vault.py"],
    "commit-projection": [PY, "tools/commit_projection.py"],
    "commit-registry": [PY, "tools/commit_registry.py"],
    "lease":     [PY, "tools/tick_lease.py", "status"],
    "test-tick": [PY, "tests/test_tick.py"],
    "test-pipeline": [PY, "tests/test_pipeline.py"],
    "test-time": [PY, "tests/test_time.py"],
    "test-events": [PY, "tests/test_events.py"],
    "test-fingerprints": [PY, "tests/test_fingerprints.py"],
    "test-export": [PY, "tests/test_ledger_export.py"],
    "test-degradations": [PY, "tests/test_degradations.py"],
    "test-emit": [PY, "tests/test_emit_purity.py"],
    "test-ancestry": [PY, "tests/test_shared_ancestry.py"],
    "test-provenance": [PY, "tests/test_provenance.py"],
    "test-activity": [PY, "tests/test_activity.py"],
    "test-rollup": [PY, "tests/test_rollup.py"],
    "plugins":   [PY, "collectors/run_plugins.py"],
    "test-plugins": [PY, "tests/test_plugins.py"],
    "test-dashboard": [PY, "tests/test_dashboard_store.py"],
    "rollup":    [PY, "store/rollup.py", "refresh"],
    "stats":     [PY, "store/rollup.py", "status"],
    "activity":  [PY, "activity.py"],
    "migrate":   [PY, "store/migrate.py"],
    "fabric":    [PY, "tools/fabric_hash.py", "--check"],
    "contract":  [PY, "tools/publish_contract.py", "--check"],
}
GROUPS = {
    "scan":  ["scan-fs", "scan-gh", "scan-vault", "scan-sessions"],
    "local": ["scan-fs", "merge", "emit", "validate", "scan-events",
              "findings", "dashboard", "smoke-pages"],
    # `dashboard` is IN the gate, not assumed before it: `design` and `smoke` both
    # read docs/projects-dashboard.html, which git ignores — so on a fresh clone
    # the gate failed on an artefact no step of it built. Building an ignored file
    # is not a tree change; the gate proves that itself (NON_MUTATING below).
    # `contract` runs LAST. It verifies a REMOTE state — the published contract
    # surface — and the only remedy is a human running `--publish`, so sitting it
    # third meant one pending human step hid 648 local assertions behind an early
    # exit. A gate should report everything it can before it reports what only
    # somebody else can fix.
    "check": ["validate", "fabric", "ledger-current", "dashboard", "design", "smoke", "smoke-pages", "test-pages",
              "test", "test-ledger", "test-skill", "test-agent", "test-key", "test-install-key", "test-vault", "test-analytics", "test-doors", "test-surfaces", "test-mcp-inventory", "test-ack", "test-serverd", "test-skill-check", "test-index",
              "test-retention", "test-tick", "test-pipeline", "test-time", "test-events", "test-fingerprints", "test-export", "test-degradations", "test-emit", "test-ancestry", "test-provenance", "test-activity", "test-rollup", "test-plugins", "test-dashboard", "test-queue", "test-wire-contract", "docs-current", "test-docs", "test-dead-data", "test-projection", "test-tick-repo", "test-identity", "test-render", "test-sessions", "test-indexer-load", "test-corroboration", "test-tick-failures", "test-notification", "test-hook", "test-interpretation", "test-render-page", "test-recall", "test-validator", "test-domain-probe", "paths-current", "secrets", "test-secrets", "test-write-surface", "test-receipt", "test-interp-contract", "test-provider", "test-search", "test-mirror", "test-erasure-scope", "test-collector-state", "test-delivery", "test-freshness", "test-project-surface", "test-metric-series", "test-companion", "test-footprint", "test-budget", "test-agent-queue", "test-lost-projects", "test-estate-history", "test-cause", "test-clone-sync", "test-fixture-sweep", "test-unpublished", "test-checkout", "test-truncation", "test-key-shape", "test-identity-drift", "test-confidence", "test-standdown", "test-dark-cost", "test-stale-expected", "test-agent-faults", "test-integrity", "test-release", "test-portfolio", "test-omission", "test-ledger-ids", "test-provider-health", "test-store-faults", "test-heroku", "test-openrouter", "test-credentials", "test-keyserver", "test-env", "test-remote-env", "test-google", "test-project-secrets", "test-reveals", "test-store-modes", "test-backup-store", "test-plugin-pair", "test-publish-contract", "test-scrub", "test-session-start", "test-recovery", "test-signature", "test-env-tab", "test-use-secret", "use", "test-gate-skips", "test-block", "test-lever", "test-queue-order", "test-residue", "test-stale-collapse", "test-labels", "test-work-tiles", "test-attribution", "test-handoff", "test-site-kinds", "test-at-stake", "test-shape", "test-unobservable", "test-blank-page", "test-witness", "test-horizon", "test-recount", "test-git-locale", "test-foreign", "test-rdap-keys", "test-wire-alive", "test-absent-fields", "test-wire-inputs", "test-atomic", "test-two-surfaces", "test-companion-faults", "test-fold", "test-remedy", "test-listings", "test-merge", "test-erasure", "test-rollup-sessions", "test-scan-ids", "trap-map", "test-trap-map", "test-trap-efficacy", "test-finding-rules", "test-tracer", "plugins-check", "test-plugin-report", "test-fs-scan", "validate-plugin", "test-wire",
              "probes-check", "contract"],
    "all":   ["scan-fs", "scan-gh", "scan-vault", "scan-sessions", "remotes", "scan-bb", "domains", "heroku", "scan-cloudflare", "scan-mcp", "openrouter", "env", "remote-env", "google", "leaks", "scrub-companion", "merge", "emit", "validate",
              "scan-events", "plugins", "rollup", "corroborate", "export-ledger", "lost",
              # `index` BEFORE the page, and the position is a measurement: it
              # charges the wallet through the provider boundary, and the page
              # reports the wallet. Traced 2026-09-09 — until then nothing knew
              # the indexer wrote that file, so `all` published a page whose
              # spend figure predated the spend of the same run. `tools/tick.sh`
              # already had the pair the right way round.
              "index", "findings", "dashboard", "smoke", "notify", "commit-registry", "project", "commit-projection", "fabric", "links",
              "snapshot", "deltas", "test", "test-ledger", "test-skill", "test-agent", "test-key", "test-index", "test-retention",
              "validate-plugin", "test-wire", "probes", "test-gate-purity",
              # LAST, and the position is the whole point. `contract` is red
              # whenever the published revision is behind, which only the
              # operator can clear — and it sat at #24 of 40, so a fail-fast
              # group left the sixteen steps after it unrun on every invocation
              # for the whole of this session, `probes`, `test-key`,
              # `test-index`, `test-retention`, `test-wire`, `validate-plugin`
              # and `test-gate-purity` among them. All were green when driven by
              # hand; the hole cost nothing THAT time. `check` had it
              # last already. A step whose red belongs to somebody else goes at
              # the end of a group that stops at the first failure.
              "contract"],
}


#: Groups that answer "is this tree good?" and must therefore not change it. A
#: gate that writes cannot be used to decide whether the tree is clean — and this
#: one did, twice: `probes` rewrote `fabric/probe-receipts.json` on every run, and
#: trap T32 rebuilt `registry/findings.json` against the live registry. After a
#: gate like that, `git status` answers a question nobody asked.
#: Files git ignores are not tree changes: building `docs/projects-dashboard.html`
#: is what a gate over a page has to do.
NON_MUTATING = {"check"}

#: Tracked paths that NO step of a NON_MUTATING group writes, so a change under
#: one of them during such a group is somebody else's — in practice the
#: scheduled tick, whose `emit`, `findings`, `export-ledger` and
#: `commit-registry` steps are the only writers of the registry and none of them
#: is in `check`. Verified rather than assumed: of the registry's writers only
#: `dashboard` appears in the group, and it writes a gitignored page already
#: declared in `IGNORED_WRITES_ALLOWED`.
#:
#: A prefix list, not a blanket: naming the paths is what lets the verdict say
#: whose change it was instead of accusing the gate of a rewrite it did not make.
GROUP_WRITES_NOTHING_UNDER = ("registry/",)

#: What a step needs from the MACHINE, not from the repository. The gate is
#: machine-coupled in four places and CI is what makes that visible: the projects
#: root is the estate this project watches, `claude` is a CLI only an operator
#: install has, `store/observatory.db` is gitignored so a fresh clone has none,
#: and `node` runs the page's smoke test. A step whose requirement is absent is
#: SKIPPED with its reason — the project's own rule for a source it cannot read —
#: never silently dropped and never failed as though the code were broken.
NEEDS = {
    "ledger-current":   ["store"],
    "validate-plugin":  ["claude-cli"],
    "smoke":            ["node"],
    "smoke-pages":      ["node"],
    "test-env-tab":     ["node"],
}


# The complete pipeline keeps its public command names; mutable arguments live
# in the selected private workspace rather than the program checkout.
def workspace_argument(value: str) -> str:
    for prefix, base in (("store/raw", paths.SCRATCH), ("registry", paths.REGISTRY)):
        if value == prefix or value.startswith(prefix + "/"):
            return str(base / value[len(prefix):].lstrip("/"))
    if value == "docs/projects-dashboard.html":
        return str(paths.DASHBOARD_HTML)
    return value

STEPS = {name: [workspace_argument(v) for v in cmd] for name, cmd in STEPS.items()}
STEPS["check-portable"] = [PY, "tests/run_portable.py"]

# This marker selects a published source profile, never a user's configuration.
# The historical installation retains its original gate when no marker exists.
PUBLIC_SOURCE_ONLY = {
    "setup": "install the distribution with its full extra instead",
    "deps": "install the distribution with its full extra instead",
    "docs-current": "requires the original private documentation archive",
    "paths-current": "requires the original private documentation archive",
    "trap-map": "requires the original historical trap catalogue",
    "validate-plugin": "requires the complete companion-plugin development checkout",
    "all": "historical composite includes private development gates; use local or the opt-in tick",
}
WORKSPACE_COMMANDS = {"init", "doctor", "version", "configure", "onboard", "migrate-local", "open", "agent",
                      "workspace-backup", "upgrade", "restore", "backups", "backup-passphrase", "machine", "cleanup"}
PUBLIC_HELP = """Project Observatory full engine (public profile).

  init / onboard / configure   prepare your private workspace
  local                        filesystem → registry → events → findings → dashboard
  open [--serve|--stop]        open the dashboard in a browser (builds it if needed); stop the server
  agent install|status|uninstall  Claude Code plugin; install turns auto-update on
  check [runner options]       isolated synthetic regression suites; no live providers
  check-portable               the same explicit portable regression runner
  backup                       historical SQLite-only backup
  workspace-backup             full private snapshot (requires --writers-stopped);
                               encrypted into the backups root once a passphrase is set
  backup-passphrase set|status|show  the passphrase that encrypts backups (stdin)
  backups status|migrate|decrypt     where backups go; move legacy copies; decrypt one
  upgrade                      preview; --apply requires --writers-stopped
  restore SNAPSHOT             restore a snapshot dir or encrypted .obsnap into a new empty workspace
  machine [--disk] [--explain PID]   processes by origin, memory, disk; why one process runs
  cleanup [--apply [--include manual]]  plan, or remove what loses nothing; manual tier archives first
  tick                         configured cycle; scheduler must be explicitly enabled

Local excludes metric plugins, provider collectors, secret scans and model calls.
Plugin execution remains an explicit command and can use enabled integrations.
Source-only historical gates are not part of the public acceptance check.
See docs/CLI-COMPATIBILITY.md for command and state compatibility.
"""


def public_profile() -> bool:
    marker = ROOT / "public-profile.json"
    if not marker.exists() and not marker.is_symlink():
        return False
    try:
        if marker.is_symlink():
            raise ValueError("linked profile")
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value != {"schema_version": 1, "profile": "public"}:
            raise ValueError("unsupported profile")
    except (OSError, ValueError):
        raise configuration.ConfigurationError("Invalid or unsupported source profile") from None
    return True


def unavailable_step(name: str, public: bool) -> str:
    if public and name in PUBLIC_SOURCE_ONLY:
        return PUBLIC_SOURCE_ONLY[name]
    for arg in STEPS.get(name, [])[1:]:
        # Positional source entry points, not mutable workspace input/output.
        if not arg.startswith("-") and not Path(arg).is_absolute() and Path(arg).suffix in {".py", ".sh", ".js"}:
            if not (ROOT / arg).is_file():
                return "the command's source payload is not included in this distribution"
    return ""




def capabilities() -> dict[str, str]:
    """Each requirement, and why it is absent when it is. Empty string = present."""
    import shutil
    sys.path.insert(0, str(ROOT))
    import paths
    return {
        "estate": "" if paths.DATA.is_dir() else f"{paths.DATA} does not exist here",
        "claude-cli": "" if shutil.which("claude") else "the claude CLI is not on PATH",
        "store": "" if paths.DB.exists() else f"{paths.DB.name} does not exist (it is gitignored)",
        "node": "" if shutil.which("node") else "node is not on PATH",
    }


#: A suite announcing that a BLOCK of assertions did not run. Anchored at the
#: line start, because a PASS line may name the marker while asserting something
#: about it: `PASS  a SKIP line is printed when node is absent` is a passed
#: assertion, not a skipped one, and an unanchored search counted it.
#:
#: Two words, because the suites use two: 39 sites print `SKIP` and 10 print
#: `NOTE` (measured 2026-09-07 across 27 suites). Both drop assertions; the
#: verbatim line is kept so a reader can tell which they are looking at.
SKIP_MARKER = re.compile(r"^\s+(SKIP|NOTE)\s")


def run(step: str, *, offline: bool = False) -> tuple[int, list[str]]:
    """Run one step, streaming its output, and report the blocks it skipped.

    STREAMED rather than captured. A 99-step gate whose output only appears at
    the end is a gate nobody watches, so the lines are printed as they arrive and
    counted on the way past. `stdin` is left inherited, which is what keeps
    `tools/review.py`'s `require_terminal` guard working — it reads
    `sys.stdin.isatty()`, and piping stdout does not touch stdin.

    Why the skips are returned at all: `_run_group` was rigorous about a skipped
    STEP and blind to a skipped BLOCK inside one. A suite could drop nine
    assertions, print its reason, exit 0, and be counted as passed — measured on
    identical code as three different PASS totals in one session.
    """
    print(f"\n\033[1m── {step}\033[0m", flush=True)
    skips: list[str] = []
    child_environment = dict(os.environ)
    if offline:
        child_environment["OBSERVATORY_OFFLINE"] = "1"
    proc = subprocess.Popen(STEPS[step], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            env=child_environment)
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        if SKIP_MARKER.match(line):
            skips.append(line.rstrip())
    sys.stdout.flush()
    return proc.wait(), skips


#: Artefacts a NON_MUTATING group may legitimately write, with the reason. The
#: page is the declared one: `dashboard` and `smoke` are both IN the check group
#: and read a file git ignores, so building it is what a gate over a page has to
#: do. Nothing under `store/raw/` belongs here — no step of `check` produces a
#: collector report, so every one of them is a test that forgot to redirect —
#: which is why the smoke verdict below sits beside the page rather than in
#: `store/raw/`: putting it there would have cost that ban its first
#: exception, and the ban is what catches an unredirected fixture.
IGNORED_WRITES_ALLOWED = {
    # The split pages and their smoke receipts: built by `dashboard`,
    # executed by `smoke-pages`, gitignored like the single page for the same
    # reason — generated, and regenerated by the gate that reads them.
    **{f"docs/dashboard/{_pg}.html": "a split page built by the dashboard step, "
       "read by smoke-pages and test-pages; gitignored, regenerated by the gate"
       for _pg in ("index", "findings", "projects", "domains", "heroku", "creds", "env", "mcp", "traffic", "health")},
    "docs/dashboard/app.css": "the style every split page links to, written once "
    "by the dashboard step; gitignored and regenerated with the pages",
    "docs/dashboard/app.js": "the script every split page loads, written once by the "
    "dashboard step; gitignored and regenerated with the pages",
    **{f"docs/dashboard/{_pg}.smoke.json": "the smoke receipt beside a split page, "
       "written by whoever executed that build, for the same reason as the single page's"
       for _pg in ("index", "findings", "projects", "domains", "heroku", "creds", "env", "mcp", "traffic", "health")},
    "docs/projects-dashboard.html":
        "built by the `dashboard` step, which `design` and `smoke` then read; "
        "gitignored, so a fresh clone has none and the gate must make it",
    "docs/projects-dashboard.smoke.json":
        "written by the `smoke` step beside the page it just executed, and by "
        "every other runner of that file for the same reason: whoever executed a "
        "build is who can attest to it. Had only the tick recorded a verdict, "
        "the board would have called this page unverified after every gate run "
        "— true of the record and useless to a reader",
}

#: A DIFFERENT CLAIM, and it needs its own container. Everything above says "a
#: step of the gate may write this". This says "a process that is NOT the gate
#: writes this, so finding it changed is not evidence about this tree at all".
#: They were briefly one dict, and the cost was immediate: the `store/raw` ban
#: three paragraphs up became a lie about the very first entry, and the
#: assertion that `IGNORED_WRITES_ALLOWED` holds exactly one path had to be
#: broken to fit a file no gate step writes. A blanket exemption is a bad price
#: for a true observation.
FOREIGN_WRITES_IGNORED = {
    "store/raw/integrity.json":
        "written by the scheduled tick's store check, which runs early in the "
        "cycle. Same class as the lease receipt below: the tick landing mid-gate "
        "is a fact about the schedule, not about the group under test "
        "",
    "store/raw/tick-lease.json":
        "written by the scheduled tick on every path of its lease acquire — and "
        "the tick landing mid-gate is the SUBJECT of that receipt, not an "
        "accident: the gate holds `registry` for its whole duration, so a tick "
        "firing during a `check` stands down and records the skip. Counting that "
        "write as the group's own would make the gate report itself for the very "
        "collision it caused",
    "store/raw/store-faults.jsonl":
        "appended by whatever catches a store error — the tick's `agent` and "
        "`index` steps among them. Append-only by design, so a tick landing "
        "mid-gate adds a line the group did not write",
    "store/raw/companion-faults.jsonl":
        "appended by the companion's Stop hook whenever a turn could not be "
        "recorded — in every session of every watched project, concurrently "
        "with the gate, for the same reason its receipt is exempted below. "
        "Append-only by design, so a neighbouring session losing a "
        "turn mid-gate adds a line this tree did not write",
    "store/raw/record-turn.json":
        "written by the companion's Stop hook — in EVERY session of EVERY watched "
        "project on this machine, concurrently with the gate. Forty-nine Claude "
        "processes were running when that was measured, so another "
        "session ending mid-gate changes this file and the purity verdict would "
        "report a neighbour's turn as this tree's impurity. The suites that drive "
        "the recorder redirect OBSERVATORY_SCRATCH and are checked for it "
        "separately in tests/test_gate_purity.py",
    "store/raw/serverd.json":
        "the always-on server's heartbeat receipt, rewritten every 20 seconds "
        "for as long as launchd keeps the daemon alive — which is "
        "concurrently with any gate that outlives that interval, i.e. every "
        "gate. No step of any group starts or stops the daemon, so exempting "
        "its receipt removes no coverage; the suites that drive its refresh "
        "functions redirect OBSERVATORY_SCRATCH",
}

#: Tables whose ROW COUNT must not move while a NON_MUTATING group runs. The
#: file's BYTES legitimately do — WAL checkpointing, page reuse, `secure_delete`
#: and a migration applied by any read-write connect all rewrite pages without
#: changing a single fact — so hashing the database would report a mutation on
#: every run and teach the reader to ignore it. The counts are the invariant.
WATCHED_TABLES = ("ledger", "deltas", "events", "observations", "metrics",
                  "proposals", "project_week", "outbox", "tombstones", "scans")

#: The companion plugin's recorder, which writes a ledger row when ANY Claude
#: session on this machine ends a turn — `tools/record_turn.py`'s own owner.
FOREIGN_ROW_WRITER = "agent:claude-code"

#: Row counts that must EXCLUDE that writer, and why. This is the row-count half
#: of `FOREIGN_WRITES_IGNORED`: a neighbouring session ending a turn mid-gate
#: moved `ledger` and `outbox` from 183 to 184 and the purity verdict reported a
#: mutation the group under test had not made (measured 2026-09-07; the re-run
#: was green, which is the tell — a guarantee that fails on a race teaches the
#: reader to re-run rather than to look). The recorder is invoked by a Stop hook
#: and by no step of any group, so excluding it removes no coverage: the suites
#: that DRIVE it redirect `OBSERVATORY_DB` and are checked for that separately.
FOREIGN_ROWS_EXCLUDED = {
    "ledger": "SELECT count(*) FROM ledger WHERE owner IS NOT ?",
    # `outbox` carries no owner of its own; a row is traced to the ledger row it
    # projects. A row whose ledger entry is gone is COUNTED rather than assumed
    # foreign — an orphan is exactly the kind of thing this check exists for.
    "outbox": "SELECT count(*) FROM outbox WHERE memory_id NOT IN "
              "(SELECT memory_id FROM ledger WHERE owner IS ?)",
}


def ignored_state() -> tuple[dict[str, str], dict[str, int]]:
    """(hash per file under the scratch dir, row count per watched table).

    THE HALF `git status` CANNOT SEE, and the reason this exists. `NON_MUTATING`
    promised that a gate answering "is this tree good?" does not change it — and
    it was enforced by `git status` alone, while `store/raw/` and
    `store/observatory.db` are gitignored. Measured 2026-09-07: the `check`
    group wrote ELEVEN of them, including the live `agent.json`, whose
    `halted_by` then told the operator the interpretation layer was stopped by a
    mode-644 key file in a test's temp directory that no longer exists. The
    finding an operator reads carried a fixture's outcome as the estate's state.
    """
    files: dict[str, str] = {}
    # THE JOURNALS TOO. `store/logs/*.jsonl` are the security record — who
    # revealed what, which command ran with which secret — and until 2026-09-14
    # they were outside this hash, so a suite that started a keyserver and hit
    # `/api/reveal` left rows in the live audit and the gate stayed green. A
    # gate that answers "is this tree good?" may not write the record either.
    # `tick.log`/`tick.err` stay out: launchd writes them and the lease keeps
    # the tick away during a run, so a change there is not this group's.
    watched = list(paths.SCRATCH.rglob("*")) + list((paths.STATE / "logs").glob("*.jsonl"))
    for p in sorted(set(watched)):
        if p.is_file():
            rel = str(p.relative_to(ROOT)) if ROOT in p.parents or p.is_relative_to(ROOT) \
                else str(p)
            files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    rows: dict[str, int] = {}
    if paths.DB.is_file():
        try:
            conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
            try:
                for t in WATCHED_TABLES:
                    try:
                        rows[t] = conn.execute(FOREIGN_ROWS_EXCLUDED.get(
                            t, f"SELECT count(*) FROM {t}"),
                            (FOREIGN_ROW_WRITER,) if t in FOREIGN_ROWS_EXCLUDED
                            else ()).fetchone()[0]
                    except sqlite3.Error:
                        pass
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return files, rows


def culprit_of(appeared: list[str], vanished: list[str],
               changed_files: list[str]) -> str:
    """Who moved the tree — as a decision that can be driven on its own.

    Three cases, and the third read as the first for as long as there were two:
    with the status line set unchanged, `appeared` and `vanished` are BOTH empty
    and `not vanished` named the gate — for a file another process rewrote in
    place. A scheduled tick rewriting `registry/findings.json` mid-run therefore
    came out as the gate mutating the tree, which is the wrong-cause failure the
    vanished-case comment was written to prevent.

    Extracted from `main()` so it could be tested without planting a change in
    the repository's own tracked files: a driven check on the real tree would
    have to modify and restore one, and a test that edits tracked files to prove
    a point is a worse risk than the bug.
    """
    if appeared:
        return "the gate itself"
    if vanished:
        return ("something else — dirt DISAPPEARED, which is what the "
                "scheduled tick's registry commit looks like from here")
    if changed_files and all(f.startswith(GROUP_WRITES_NOTHING_UNDER)
                             for f in changed_files):
        return ("something else — every changed path is one no step of this "
                "group writes, which is what the scheduled tick rewriting the "
                "registry looks like from here")
    return "the gate itself"


def tree_state() -> tuple[str, set[str], dict[str, str]] | None:
    """(digest, the set of status lines, a digest PER FILE), or None outside git.

    The status lines alone are NOT enough, and a planted mutation proved it: a
    gate that appends to a file which is ALREADY modified leaves ` M path` on
    both sides, so the set is identical and the change is invisible. The digest
    covers the content of every tracked change; the line set is kept because it
    is what names the paths in the report.

    **And the whole-tree digest alone was not enough either.** When the line set
    matched, the report said only "a file that was ALREADY modified was modified
    again — `git diff HEAD` says which" and then named the GATE as the cause,
    because the culprit rule read an empty `vanished` set as "not the tick". A
    scheduled tick rewriting `registry/findings.json` mid-run therefore came out
    as the gate mutating the tree — the exact wrong-cause failure the rule above
    it was written to prevent, one case over. The per-file digests are
    split out of the one `git diff HEAD` already taken, so naming the files costs
    no extra subprocess.
    """
    st = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                        capture_output=True, text=True)
    if st.returncode != 0:
        return None
    df = subprocess.run(["git", "diff", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    digest = hashlib.sha256((st.stdout + "\0" + df.stdout).encode("utf-8")).hexdigest()
    per_file: dict[str, str] = {}
    path, chunk = None, []
    for line in df.stdout.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path is not None:
                per_file[path] = hashlib.sha256("".join(chunk).encode("utf-8")).hexdigest()[:16]
            # `diff --git a/x b/x` — the b-side, which is the path as it is now.
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else line.strip()
            chunk = []
        else:
            chunk.append(line)
    if path is not None:
        per_file[path] = hashlib.sha256("".join(chunk).encode("utf-8")).hexdigest()[:16]
    return digest, set(st.stdout.splitlines()), per_file


def main(argv: list[str]) -> int:
    try:
        public = public_profile()
    except configuration.ConfigurationError as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
    if len(argv) > 1 and argv[1] in WORKSPACE_COMMANDS:
        import workspace
        return workspace.main(argv[1:])
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(PUBLIC_HELP if public else __doc__)
        print("steps:", ", ".join(name for name in STEPS if not unavailable_step(name, public)))
        print("groups:", ", ".join(name for name in GROUPS if not unavailable_step(name, public)))
        return 0
    name = argv[1]
    if name == "check-portable" or (public and name == "check"):
        reason = unavailable_step("check-portable", public)
        if reason:
            print(f"Observatory: check-portable unavailable: {reason}", file=sys.stderr)
            return 2
        # Before workspace validation/tightening: a public gate needs no live
        # state and its runner creates fresh private synthetic workspaces.
        return subprocess.call([*STEPS["check-portable"], *argv[2:]], cwd=ROOT)
    steps = GROUPS.get(name, [name] if name in STEPS else None)
    if steps is None:
        print(f"unknown step: {name}", file=sys.stderr)
        return 2
    missing = [(step, unavailable_step(step, public)) for step in [name, *steps]]
    missing = [(step, reason) for step, reason in missing if reason]
    if missing:
        for step, reason in dict(missing).items():
            print(f"Observatory: {step} unavailable: {reason}", file=sys.stderr)
        return 2
    invalid_args = [arg for arg in argv[2:] if not arg.startswith("--expect-skipped=")]
    if invalid_args:
        print("Observatory: step arguments are not accepted here; invoke the underlying tool directly", file=sys.stderr)
        return 2
    if name not in {"setup", "deps"} and not name.startswith("test"):
        try:
            configuration.validate_workspace(required=True)
            configuration.load()
        except configuration.ConfigurationError as exc:
            print(f"Observatory: {exc}", file=sys.stderr)
            return 2
        paths.tighten()
    # `--expect-skipped a,b` pins which steps this environment may skip. CI passes
    # it; a person leaves it off and gets the summary without the assertion.
    expect_skipped = None
    for a in argv[2:]:
        if a.startswith("--expect-skipped="):
            expect_skipped = a.split("=", 1)[1].split(",")
    for g, body in GROUPS.items():                                               
        unknown = [x for x in body if x not in STEPS]                              
        if unknown:                                                                   
            print(f"observatory: group {g!r} names unknown step(s): {unknown}",
                  file=sys.stderr)
            return 2
    steps = GROUPS.get(name, [name] if name in STEPS else None)
    if steps is None:
        print(f"unknown step: {name}", file=sys.stderr)
        return 2
    # Compared as a DELTA, not against cleanliness: this repository is dirty
    # while it is being worked on, and a gate must still be able to say whether
    # IT was the thing that changed something.
    # SERIALIZE against the writer, do not merely detect it afterwards. The
    # tick rewrites registry/*.json one atomic replace at a time while this
    # group spends ten minutes reading them, so a tick landing mid-gate can put
    # a NEW relations.json beside an OLD projects.json in front of a cross-file
    # check — a dangling reference that never existed, reported as a failure.
    # The detection below still stands as the backstop; this is the prevention.
    #
    # Not a refusal when the lease is unavailable: blocking the operator's gate
    # because a scheduled job holds a key is worse than the race it avoids. It
    # proceeds and says what it gave up, which is the same rule every other
    # degradation in this repository follows.
    held, why = (None, "")
    if name in NON_MUTATING:
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import tick_lease
            held, why = tick_lease.hold(tick_lease.GATE_IDENTITY)
        except Exception as exc:                                             
            held, why = None, f"the lease helper is unusable: {type(exc).__name__}: {exc}"
        # flush=True: the steps below are subprocesses writing straight to the
        # inherited descriptor, while this process's own prints sit in a buffer
        # until exit. Without it the lease line lands at the END of a redirected
        # log — after the verdict it was supposed to qualify.
        print(f"\033[2m{why}\033[0m" if held else
              f"\033[33mrunning WITHOUT the registry lease — {why}. A registry write "
              f"landing mid-run will be reported as a tree change below rather than "
              f"silently mixed into one verdict.\033[0m", flush=True)
    try:
        return _run_group(name, steps, expect_skipped,
                          attributable=held is not None or name not in NON_MUTATING)
    finally:
        if held is not None:
            import tick_lease
            msg = tick_lease.drop(held)
            if msg:
                print(f"\033[2m{msg}\033[0m", flush=True)


def _tick_stamp() -> str:
    """When the scheduled tick last finished, or "" — never an exception."""
    try:
        return json.loads((paths.SCRATCH / "tick.json").read_text(
            encoding="utf-8")).get("finished_at") or ""
    except (OSError, ValueError, AttributeError):
        return ""


def _run_group(name: str, steps: list[str], expect_skipped: list[str] | None,
               attributable: bool = True) -> int:
    before = tree_state() if name in NON_MUTATING else None
    before_ignored = ignored_state() if name in NON_MUTATING else None
    # THE TICK RUNS EVERY THIRTY MINUTES, and a long `check` overlaps it. On
    # 2026-09-12 a sweep reported three reds — a search index rebuilt mid-query,
    # a rollup read between its delete and its insert, a file scanned while it
    # was being written — and every one passed on a re-run seconds later. A
    # false red is not a small cost in a repository whose whole argument is that
    # a red means something; the tick's receipt is stamped here so the verdict
    # can say which reds to distrust.
    tick_before = _tick_stamp()
    caps, skipped = capabilities(), {}
    block_skips: dict[str, list[str]] = {}
    failed_at, failed_code = None, 0
    unreached: list[str] = []
    for pos, step in enumerate(steps):
        absent = [caps[t] for t in NEEDS.get(step, []) if caps.get(t)]
        if absent:
            skipped[step] = absent[0]
            print(f"\n\033[1m── {step}\033[0m\n\033[33mSKIP — {absent[0]}\033[0m")
            continue
        code, blocks = run(step, offline=name == "local")
        if blocks:
            block_skips[step] = blocks
        if code != 0:
            # RECORDED, NOT RETURNED. This returned here — before the purity
            # verdict below — so a group with any failing step never checked
            # whether it had changed the tree. `contract` has failed for the
            # whole of this session (rev-4 is unpublished), which means the
            # purity guarantee had not been evaluated once in all that time:
            # the seventh instance of the early-return class in this repository.
            print(f"\n\033[31mFAILED at {step} (exit {code})\033[0m", file=sys.stderr)
            if tick_before and _tick_stamp() != tick_before:
                print(f"\033[33m  the scheduled tick finished DURING this run "
                      f"({tick_before} -> {_tick_stamp()}) — it rewrites the store "
                      f"and the registry under a running suite. Re-run `{step}` "
                      f"alone before believing this red.\033[0m", file=sys.stderr)
            failed_at, failed_code = step, code
            unreached = steps[pos + 1:]
            break
    # ASSERTION BLOCKS, one level below `skipped` above. That one is about a step
    # this machine cannot run at all; this is about a suite that ran, exited 0,
    # and quietly asserted less than it contains. The wording says "assertion
    # block" and never "step" for exactly that reason.
    #
    # NOT RED, and that is a decision. The commonest cause is the agent-sync
    # lease being held by the scheduled tick, which happens every thirty minutes
    # — and a gate that goes red on a routine event is a gate whose red gets
    # ignored, which costs more than the silence it replaces. What this fixes is
    # narrower and real: a PASS total can no longer be quoted without the number
    # of assertions that did not run beside it.
    #
    # NO RECEIPT either. `check` is in NON_MUTATING and its own rule says nothing
    # under `store/raw/` belongs to it, so a gate writing its own receipt would
    # break the purity verdict it exists to enforce. Printed, and that is all.
    if block_skips:
        n = sum(len(v) for v in block_skips.values())
        print(f"\n\033[33m{n} assertion block(s) did not run, in "
              f"{len(block_skips)} of {len(steps)} step(s) — a suite that skips a "
              f"block still exits 0, so this is the number that must travel beside "
              f"any PASS total:\033[0m")
        for st, lines in block_skips.items():
            for line in lines:
                print(f"  {st}: {line.strip()}")

    if skipped:
        print(f"\n\033[33m{len(skipped)} of {len(steps)} step(s) skipped — this machine "
              f"cannot run them, and a skipped gate is not a passed gate:\033[0m")
        for s, why in skipped.items():
            print(f"  {s}: {why}")

    # THE THIRD KIND OF NOT-RUN, and it was the largest and the only silent one.
    # The two reports above cover a step this machine cannot run and an assertion
    # block a suite skipped; a step the group never REACHED was reported nowhere,
    # because the loop breaks at the first failure — which is right for a
    # pipeline, where running `emit` on a model `merge` could not build is worse
    # than stopping, and wrong to leave unsaid.
    #
    # Measured 2026-09-08: `check` broke at `test-truncation` (#74 of 128) and at
    # `test-handoff` (#98), leaving 54 and 30 steps unrun while each summary said
    # "1 failed"; `all` had `contract` at #24 of 40 and it has been red all
    # session, so sixteen steps had not run once. All were green when driven by
    # hand — which is the point: the hole cost nothing THAT time.
    if unreached:
        shown = unreached[:12]
        print(f"\n\033[33m{len(unreached)} of {len(steps)} step(s) never ran — the "
              f"group stops at the first failure, so any PASS total above covers "
              f"{len(steps) - len(unreached)} step(s) and not this group:\033[0m")
        print("  " + ", ".join(shown)
              + (f", and {len(unreached) - len(shown)} more" if len(unreached) > len(shown) else ""))

    # CI pins the set it expects to skip. Without this a step that quietly
    # becomes machine-coupled leaves CI's coverage without anything saying so —
    # the run stays green and simply stops checking that step, for ever.
    if expect_skipped is not None:
        want, got = set(expect_skipped) - {""}, set(skipped)
        if want != got:
            print(f"\n\033[31mThe skipped set is not the one this run was told to expect.\033[0m\n"
                  f"  unexpectedly skipped: {sorted(got - want) or 'none'}\n"
                  f"  expected but ran:     {sorted(want - got) or 'none'}", file=sys.stderr)
            return 1
    tree_dirty = False
    if before is not None:
        after = tree_state()
        if after and after[0] != before[0]:
            # WHO changed it matters. The scheduled tick commits the registry, so
            # a tick that lands mid-gate makes dirty paths turn CLEAN — and the
            # first version of this message accused the gate of writing them.
            # A gate that reports the wrong cause is a gate people argue with.
            appeared_now = sorted(after[1] - before[1])
            vanished_now = sorted(before[1] - after[1])
            # THE THIRD CASE, which read as the first. When the line set matches,
            # both sets are empty and `not vanished_now` named the GATE — for a
            # file another process rewrote in place. Now the changed files are
            # NAMED from the per-file digests, and a change confined to paths no
            # step of this group writes is reported as somebody else's
            #.
            changed_files = sorted(
                k for k in set(before[2]) | set(after[2])
                if before[2].get(k) != after[2].get(k))
            culprit = culprit_of(appeared_now, vanished_now, changed_files)
            print(f"\n\033[31mThe tree changed while the {name} group ran, and the cause "
                  f"is {culprit}. Either way this run saw two different trees:\033[0m",
                  file=sys.stderr)
            appeared, vanished = appeared_now, vanished_now
            for line in appeared:
                print(f"  appeared: {line}", file=sys.stderr)
            for line in vanished:
                print(f"  gone:     {line}", file=sys.stderr)
            for f in changed_files:
                print(f"  rewritten in place: {f}", file=sys.stderr)
            if not appeared and not vanished and not changed_files:
                print("  neither the file list nor any tracked file's content moved, "
                      "so the digest changed for a reason this cannot name — "
                      "`git status` and `git diff HEAD` are what it compares.",
                      file=sys.stderr)
            # NOT A RETURN. The ignored half below is the part git cannot see at
            # all, and skipping it here made this branch the eighth instance of
            # the early-return class the purity contract exists to refuse — the
            # verdict must be REACHED on every path, including this one.
            tree_dirty = True

    # THE IGNORED HALF, which the check above cannot see at all. `store/raw/` and
    # `store/observatory.db` are gitignored, so a gate that wrote them stayed
    # green — and it did: eleven files, measured 2026-09-07, including the live
    # `agent.json` whose `halted_by` then told the operator the interpretation
    # layer was stopped by a mode-644 key file in a test's temp directory.
    if before_ignored is not None:
        files_after, rows_after = ignored_state()
        files_before, rows_before = before_ignored
        touched = sorted(k for k in set(files_before) | set(files_after)
                         if files_before.get(k) != files_after.get(k)
                         and k not in IGNORED_WRITES_ALLOWED
                         and k not in FOREIGN_WRITES_IGNORED)
        moved = {t: (rows_before.get(t), rows_after.get(t))
                 for t in set(rows_before) | set(rows_after)
                 if rows_before.get(t) != rows_after.get(t)}
        if touched or moved:
            # THE VERDICT MAY NOT CLAIM MORE THAN THE RUN ESTABLISHED. Without the
            # registry lease another writer — the tick, every 1800 seconds — can
            # work inside this window, and then "the group wrote these" is false
            # about files the run itself proved it could not attribute. Measured
            # 2026-09-08: a tick took the lease first, the gate proceeded
            # unprotected as designed, and the verdict named fifteen of the
            # tick's own receipts as the group's impurity. The report
            # stays and the exit stays non-zero — erring toward reporting is the
            # existing choice — but the sentence now matches the evidence.
            print(f"\n\033[31m" + (
                  f"The {name} group wrote artefacts git cannot see. A gate "
                  f"that answers 'is this tree good?' must not change it, and these are "
                  f"the files the porcelain check is blind to:"
                  if attributable else
                  f"Artefacts git cannot see changed while the {name} group ran, and "
                  f"this run could NOT hold the registry lease — so they are not "
                  f"attributable to the group. Reported rather than dropped, because "
                  f"a gate that writes them would look exactly like this:") +
                  "\033[0m", file=sys.stderr)
            for k in touched:
                print(f"  wrote: {k}", file=sys.stderr)
            for t, (was, now_) in sorted(moved.items()):
                print(f"  rows:  {t} {was} -> {now_}", file=sys.stderr)
            print("  A test that runs a repository script must redirect "
                  "OBSERVATORY_SCRATCH and OBSERVATORY_DB. The live report is what "
                  "`tools/build_findings.py` reads, so a fixture's outcome becomes "
                  "the estate's reported state.", file=sys.stderr)
            return 1
    # THE TRACKED-TREE VERDICT, decided AFTER the ignored half has been checked
    # too. It used to `return 1` on the spot, which skipped the part git cannot
    # see and skipped the line below saying the checks ran — so a tick landing
    # mid-gate hid the purity verdict entirely.
    if tree_dirty:
        print("\n\033[31mthe tracked tree moved during the run, reported above\033[0m",
              file=sys.stderr)
        return 1
    if failed_at is not None:
        print(f"\n\033[31m{failed_at} failed, and the purity checks above ran anyway "
              f"— a gate that stops checking itself the moment a step fails is a gate "
              f"whose guarantee is strongest when nothing is wrong.\033[0m",
              file=sys.stderr)
        return failed_code
    print("\n\033[32mok\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
