"""What each finding TYPE is called, in the reader's words.

A finding's `type` is the rule's stable id (`heroku.app_down`) — a key the
board, the acks file and the notifier all address it by, and never prose. The
findings page used to print that id in its type filter and nowhere else, so a
reader picked from `heroku.app_down · 3` and a Russian reader got no word of
their own at all.

Each label here is an English message id; `locales/ru.json` translates it like
any other string the page shows. The page receives only the labels of the types
present (`build_dashboard._findings_panel`) and falls back to the raw id for a
type this table does not name, so a new rule is never blank — it is merely
untranslated until its label lands here. `tests/test_dashboard_shell.py` reads
the rule modules and fails when a type they can emit has no label.

A finding's TITLE is a message id too. Each rule builds it with `titled(id,
**args)`, which returns three keys: `title_id` (the English text with `{named}`
placeholders), `title_args` (the values, integers or plain text) and `title`,
the English rendering of the same pair through the same catalog the page reads
(`i18n.translate`). So `title` — what MCP, the notifier and the logs read — is
derived from the id and cannot drift from it, and the page renders the pair in
the reader's language. A count that drives agreement is the `n` argument, and
its id is a plural entry in `locales/en.json` and `ru.json`. A reason a
collector or provider wrote is passed as an argument verbatim and stays in its
own language. `tests/test_i18n.py` reads the rule modules and fails when a
finding title carries no id, or an id has no translation.

The detail and action stay as the rule wrote them, in English.
"""
from __future__ import annotations

#: `clone.<state>` is one rule with one type per checkout state
#: (`tools/build_findings.SYNC_FINDINGS`); each state is named here in full.
LABELS: dict[str, str] = {
    "acks.unreadable": "Silence file unreadable",
    "analytics.api_disabled": "Google API switched off",
    "analytics.legacy_account_property": "Analytics property in a legacy account",
    "analytics.organization_account_unreadable": "Organisation analytics account unreadable",
    "analytics.property_unclaimed": "Analytics property without a project",
    "analytics.property_unreadable": "Analytics property unreadable",
    "analytics.property_wrong_account": "Analytics property in the wrong account",
    "analytics.stale": "Analytics scan out of date",
    "analytics.unmapped_host": "Site host mapped to no project",
    "cleanup.done": "Automatic cleanup ran",
    "clone.ahead": "Commits on no remote",
    "clone.behind": "Clone behind its remote",
    "clone.behind-or-diverged": "Clone may be behind its remote",
    "clone.diverged": "Clone diverged from its remote",
    "clone.local-only-branch": "Branch only on this disk",
    "clone.stale": "Clones behind their remotes",
    "clone.unknown": "Clone state undetermined",
    "clone.unknown-state": "Clone state not understood",
    "clone.unpushed-and-remote-moved": "Unpushed commits, remote moved on",
    "clone.unreachable": "Remote could not be compared",
    "collector.degraded": "Collector degraded",
    "collector.not_applicable": "Collector not applicable here",
    "companion.faults_unlogged": "Agent faults not logged",
    "companion.not_recording": "Agent work not recorded",
    "companion.stale_install": "Agent skill out of date",
    "credential.lifetime_cap": "Key with a lifetime ceiling",
    "credential.project_file_in_git": "Key file tracked by git",
    "credential.project_file_readable": "Key file readable by others",
    "credential.project_file_unignored": "Key file not ignored by git",
    "credential.register_unreadable": "Credential register unreadable",
    "credential.rotation_due": "Key rotation due",
    "credential.shared_rotation": "Key shared by several projects",
    "credential.unclaimed": "Key without a project",
    "credential.unsigned": "Key without a stated purpose",
    "credential.untracked": "Leaked key not in the store",
    "dashboard.blank": "Dashboard renders blank",
    "dashboard.unverified": "Dashboard build not verified",
    "deltas.not_diffed": "Changes not compared",
    "domain.dark": "Domain does not resolve",
    "domain.expiring": "Domain expiring",
    "domain.hold": "Domain on registrar hold",
    "domain.hold_unknown": "Domain hold status unknown",
    "domain.unmeasured": "Domains not probed",
    "env.key_misplaced": "Variable holds another provider's key",
    "env.reusable_slot": "Empty variable set in another project",
    "env.shared_secret": "Secret value shared across projects",
    "env.tracked_in_git": "Env file tracked by git",
    "env.unignored": "Env file not ignored by git",
    "env.world_readable": "Env file readable by others",
    "erasure.not_scrubbed": "Erased text still indexed",
    "fixtures.leaked": "Test fixtures left on disk",
    "gate.skips_uncovered": "Skipped checks without a reason",
    "git.idle_dirty_worktrees": "Idle worktrees with uncommitted work",
    "git.idle_unique_branches": "Idle branches holding the only copy",
    "heroku.app_down": "Heroku app down",
    "heroku.no_local_clone": "Heroku apps with no code here",
    "heroku.orphan_app": "Heroku apps without a project",
    "heroku.paying_for_nothing": "Heroku app billed with nothing running",
    "heroku.snapshot_stale": "Heroku scan out of date",
    "host.disk_low": "Disk space low",
    "host.disk_unknown": "Disk space not measured",
    "host.reclaimable_lever": "Disk space that can be freed",
    "identity.ambiguous": "Ambiguous project identity",
    "identity.unreadable": "Identity map unreadable",
    "interpretation.faults": "Interpretation faults",
    "interpretation.halted": "Interpretation halted",
    "interpretation.malformed": "Malformed interpretation",
    "interpretation.not_english": "Interpretation not in English",
    "interpretation.unreasoned": "Interpretation without reasons",
    "ledger.review_backlog": "Decisions waiting for a person",
    "ledger.review_expiring": "Proposals about to expire",
    "machine.detached_servers": "Detached server processes",
    "machine.disk_low": "Machine disk low",
    "machine.heavy_origin": "Heavy memory use by one origin",
    "machine.memory_pressure": "Memory pressure",
    "machine.stale": "Machine survey out of date",
    "mcp.key_in_url": "MCP key in a URL",
    "mcp.needs_auth": "MCP server needs sign-in",
    "mcp.own_unregistered": "Observatory MCP server not declared",
    "mcp.unreachable": "MCP server not answering",
    "memory.followed_rename": "Ledger follows a renamed project",
    "memory.orphan_subject": "Ledger names an unknown project",
    "model.degraded": "Model degraded",
    "notify.channel_failing": "Notification channel failing",
    "plugin.broken": "Plugin broken",
    "plugin.refused": "Plugin refused",
    "plugin.stale": "Plugin out of date",
    "plugin.waiting": "Plugin waiting",
    "portfolio.unreleased_and_quiet": "Unreleased and quiet projects",
    "project.declared_alive_measured_dead": "Declared active, measured dormant",
    "project.graph_stale": "Project graphs out of date",
    "project.seen_unobserved": "Folders worked in, not in the registry",
    "project.unobservable": "Project cannot be observed",
    "project.wiki_stale": "Project wiki out of date",
    "projection.lagging": "Search index lagging",
    "projection.uncommitted": "Wiki inventory not committed",
    "provider.chain_retired": "Retired model in the chain",
    "provider.health_unmeasured": "Provider health not measured",
    "provider.health_unreadable": "Provider health unreadable",
    "provider.model_quarantined": "Model in quarantine",
    "remote.namespace_withheld": "Production namespace withheld",
    "remote.retired_still_deployed": "Retired secret still deployed",
    "remote.same_as_local": "Production secret equals local",
    "remote.unbacked": "Production secrets with no backup",
    "remote.unreadable": "Production configuration unreadable",
    "repo.no_remote": "Repository with no remote",
    "repo.stale_remote": "Remote address out of date",
    "rollup.frozen_incomplete": "Weekly rollup frozen incomplete",
    "scan.stale": "Scan out of date",
    "secret.identifier_seen": "Account identifiers outside their files",
    "secret.journal_unreadable": "Key journal unreadable",
    "secret.leak_scan_blind": "Leak scan cannot read a source",
    "secret.leak_scan_unmeasured": "Leak scan not measured",
    "secret.leaked_unrotated": "Leaked key not rotated",
    "secret.moved_unrecorded": "Key movement not recorded",
    "secret.register_unreadable": "Leak register unreadable",
    "secret.retired_unrevoked": "Retired key not revoked",
    "secret.reveal_burst": "Burst of value reveals",
    "secret.reveal_unnamed": "Value revealed by an unnamed caller",
    "secret.seen_outside_its_home": "Secret seen outside its home",
    "secret.sighting_suppressed": "Leak sightings suppressed",
    "secret.suppression_not_applied": "Leak suppression not applied",
    "server.silent": "Local server silent",
    "site.dead": "Project site does not answer",
    "skill.stale_session": "Session runs an old skill",
    "store.faults_recurring": "Recurring store faults",
    "store.integrity": "Store integrity failure",
    "store.integrity_stale": "Store integrity not checked",
    "tick.standing_down": "Scheduled run standing down",
    "tick.step_failed": "Scheduled step failed",
    "wallet.shared_key": "Wallet key shared",
    "wiki.broken_link": "Broken wiki links",
    "wiki.links_unchecked": "Wiki links not checked",
    "work.unattributed": "Work attributed to no project",
    "work.unverifiable": "Recorded work contradicted by git",
    "work.unwitnessed": "Recorded work no clone can witness",
    "worktree.unpushed": "Worktree with unpushed work",
}


def labels_for(types) -> dict[str, str]:
    """The labels of the types given, for the page's payload. A type this table
    does not name is left out, and the page shows its raw id instead."""
    return {t: LABELS[t] for t in sorted(set(types)) if t in LABELS}


def titled(msgid: str, **args) -> dict:
    """`{"title", "title_id", "title_args"}` for a finding, spread into it.

    `title` is `msgid` rendered in English with `args` — a plural id picks its
    form by `n` — through `i18n.translate`, the function the page's `T()`
    mirrors. Arguments are kept JSON-plain so both renderings read them alike:
    an integer stays a number (grouped by the reader's language), anything
    else — a float included, whose `str` and JavaScript's `String` disagree —
    becomes its text here, once."""
    import i18n  # beside this module; its directory is on the path whenever this is
    plain = {k: v if isinstance(v, int) and not isinstance(v, bool) else str(v)
             for k, v in args.items()}
    return {"title": i18n.translate(msgid, "en", **plain), "title_id": msgid, "title_args": plain}
