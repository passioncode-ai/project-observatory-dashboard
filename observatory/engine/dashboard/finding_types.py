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
    "agent.secret_fallback_used": "An agent's run read a key from a .env",
    "agent.secret_outside_vault": "An agent's secrets outside the vault",
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
    # Named apart from `clone.behind` (one clone, its own row): this is the one
    # summary row of clones that are merely behind, with nothing at risk (A33).
    "clone.stale": "Clones behind, nothing at risk (summary)",
    "clone.unknown": "Clone state undetermined",
    "clone.unknown-state": "Clone state not understood",
    "clone.unpushed-and-remote-moved": "Unpushed commits, remote moved on",
    "clone.unreachable": "Remote could not be compared",
    "collector.degraded": "Collector degraded",
    "collector.degraded_persistent": "Collector degraded for over a day",
    "collector.not_applicable": "Collector not applicable here",
    "companion.faults_unlogged": "Agent faults not logged",
    "companion.not_recording": "Agent work not recorded",
    "companion.stale_install": "Agent skill out of date",
    "copy.behind_source": "Copy behind its source",
    "copy.moved_source_open": "Moved repository still open",
    "copy.record_differs": "Copy record disagrees",
    "copy.relation_unrecorded": "Copy relation not recorded",
    "copy.name_unprefixed": "Copy name lacks its prefix",
    "credential.expired": "Key expired",
    "credential.expiring": "Key expiring soon",
    "credential.lifetime_cap": "Key with a lifetime ceiling",
    "credential.request_queue_unreadable": "Key request queue unreadable",
    "credential.request_waiting": "Key request waiting for a decision",
    "credential.revocation_refused": "Key revocation refused",
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
    "dep.vulnerable": "Dependency with a known vulnerability",
    "doctor.backup_stale": "Workspace backup out of date",
    "doctor.fix_failed": "Daily doctor fix not applied",
    "doctor.registry_never_built": "Project registry never built",
    "doctor.step_failing": "Maintenance step failing",
    "doctor.vault_meta_unreadable": "Vault metadata unreadable",
    "dep.watch_stale": "Vulnerability watch out of date",
    "domain.dark": "Domain does not resolve",
    "integration.drift": "Provider answer changed shape",
    "integration.key_invalid": "Key refused by its provider",
    "integration.untracked": "Provider in use that nothing watches",
    "integration.scope_missing": "Key missing a scope it needs",
    "integration.scope_excess": "Key with more scope than it needs",
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
    "digitalocean.app_down": "DigitalOcean app with no active deployment",
    "digitalocean.droplet_off_billed": "DigitalOcean Droplet off and still billed",
    "digitalocean.snapshot_stale": "DigitalOcean scan out of date",
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
    "review.queue_waiting": "Proposals waiting for a person",
    "lifecycle.log_readable": "Logs readable by other accounts",
    "lifecycle.logs_over_cap": "Logs past their cap",
    "lifecycle.orphans": "Orphaned product processes",
    "lifecycle.overrun": "Job running past its interval",
    "lifecycle.stale_servers": "Session servers on replaced code",
    "board.duplicate_id": "One finding id produced twice",
    "machine.backup_not_carried": "Not carried by the machine backup",
    "machine.detached_servers": "Detached server processes",
    "machine.disk_low": "Machine disk low",
    "machine.vm_disk_at_risk": "Virtual machine at risk on a full disk",
    "vm.disk_errors": "Virtual machine disk file system errors",
    "disk.tmp_growth": "Temporary folder growing fast",
    "disk.vm_growth": "Virtual machine disk growing fast",
    "disk.swap_growth": "Swap growing fast",
    "machine.guard_stopping": "Disk guard Git cleanup keeps stopping",
    "machine.guard_degraded": "Disk guard measurement incomplete",
    "machine.guard_idle": "Disk guard not running",
    "machine.hung_commands": "Agent commands hanging on their input",
    "machine.heavy_origin": "Heavy memory use by one origin",
    "machine.memory_pressure": "Memory pressure",
    "machine.stale": "Machine survey out of date",
    "harness.hook_risk": "Agent hook matching a security rule",
    "harness.instruction_long": "Instruction file read whole by every session",
    "harness.scan_degraded": "Tool configuration scan incomplete",
    "harness.plaintext_secret": "Provider credential in a tool's config in plaintext",
    "harness.project_listing_overflow": "Project skills that push the listing over budget",
    "harness.listing_overflow": "Skill descriptions over the listing budget",
    "harness.mcp_deferral_off": "MCP tool schemas load upfront",
    "harness.skill_changed": "Skill changed since first seen",
    "harness.plugin_behind": "Plugin behind its marketplace's newest version",
    "harness.skill_risk": "Skill matches a security rule",
    "mcp.key_in_url": "MCP key in a URL",
    "mcp.needs_auth": "MCP server needs sign-in",
    "mcp.own_unregistered": "Observatory MCP server not declared",
    "mcp.risky_command": "MCP server command line matching a supply-chain rule",
    "mcp.tool_changed": "MCP server tool definition changed since it was pinned",
    "mcp.unpinned_server": "MCP server not pinned to a version or digest",
    "mcp.unreachable": "MCP server not answering",
    "memory.followed_rename": "Ledger follows a renamed project",
    "memory.orphan_subject": "Ledger names an unknown project",
    "model.degraded": "Model degraded",
    "notify.channel_failing": "Notification channel failing",
    "observatory.why_unanswered": "Requests for a why left unanswered",
    "org.foreign_issuer": "Key issued in another organization's account",
    "organization.config_problem": "Organizations file not fully read",
    "organization.selection_new": "Found since an organization's selection",
    "organization.unresolved": "Project with no organization (rules disagree)",
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
    "secret.in_repository": "Key in a repository",
    "secret.journal_unreadable": "Key journal unreadable",
    "secret.leak_scan_blind": "Leak scan cannot read a source",
    "secret.leak_scan_partial": "Leak scan cut short",
    "secret.leak_scan_unmeasured": "Leak scan not measured",
    "secret.leaked_unrotated": "Leaked key not rotated",
    "secret.moved_unrecorded": "Key movement not recorded",
    "secret.register_unreadable": "Leak register unreadable",
    "secret.retired_unrevoked": "Retired key not revoked",
    "secret.reveal_burst": "Burst of value reveals",
    "secret.reveal_unnamed": "Value revealed by an unnamed caller",
    "secret.seen_outside_its_home": "Secret seen outside its home",
    "secret.seen_in_agent_memory": "Secret written into agent memory",
    "secret.sighting_suppressed": "Leak sightings suppressed",
    "secret.suppression_not_applied": "Leak suppression not applied",
    "server.silent": "Local server silent",
    "session.unmanaged": "Coding sessions outside a lifecycle owner",
    # The sessions watch (OBS-630, sessions_monitor.py).
    "host.overloaded": "Host overloaded",
    "pool.no_room": "Account group with no account with room",
    "session.wrong_group": "Session served by another account group",
    "session.stalled": "Session busy with no turn",
    "site.dead": "Project site does not answer",
    "skill.stale_session": "Session runs an old skill",
    "ssh.folder_permissions": "SSH folder open to other accounts",
    "ssh.github_orphan": "GitHub key not found on this machine",
    "ssh.identity_missing": "SSH config names a missing key",
    "ssh.key_no_passphrase": "SSH key without a passphrase",
    "ssh.key_readable": "SSH key readable by others",
    "ssh.key_unregistered": "SSH key registered nowhere visible",
    "ssh.key_unused": "SSH key unused for a long time",
    "ssh.key_weak": "Weak SSH key",
    "ssh.private_in_pub": "Private SSH key saved as public",
    "ssh.public_missing": "SSH key without its public half",
    "store.faults_recurring": "Recurring store faults",
    "store.integrity": "Store integrity failure",
    "store.integrity_stale": "Store integrity not checked",
    "store.integrity_unmeasured": "Store integrity check ran out of time",
    "spend.estate_month": "Estate spend near its monthly limit",
    "fabric.attribution_conflict": "Project in two Fabric projects",
    "tick.standing_down": "Scheduled run standing down",
    "tick.collector_starved": "Collector waiting too long",
    "tick.rotation_degraded": "Collector scheduling coverage unknown",
    "tick.step_over_share": "Tick step over its share",
    "tick.step_failed": "Scheduled step failed",
    "wallet.shared_key": "Wallet key shared",
    "wiki.broken_link": "Broken wiki links",
    "wiki.links_unchecked": "Wiki links not checked",
    "work.unattributed": "Work attributed to no project",
    "work.unverifiable": "Recorded work contradicted by git",
    "work.unwitnessed": "Recorded work no clone can witness",
    "worktree.unpushed": "Worktree with unpushed work",
}


#: THE MONITORING GROUPS (OBS-551, CP-17): which finding types the navigation's Security and Availability open
#: the Findings page narrowed to. A key ending in a dot is a prefix, any other key is one type; a type in no group
#: is listed under Findings alone. Security is keys, secrets and their files, vulnerable dependencies and the
#: agent configuration that can run or leak something; Availability is a site, an app, a server or an integration
#: that does not answer.
GROUPS: dict[str, str] = {
    "secret.": "security", "credential.": "security", "env.": "security", "ssh.": "security",
    "dep.": "security", "integration.": "security", "agent.secret_": "security", "fixtures.leaked": "security", "wallet.shared_key": "security",
    "mcp.key_in_url": "security", "mcp.risky_command": "security", "mcp.tool_changed": "security",
    "mcp.unpinned_server": "security",
    "harness.plaintext_secret": "security", "harness.hook_risk": "security",
    "harness.skill_risk": "security",
    "site.": "availability", "domain.": "availability", "heroku.app_down": "availability",
    "digitalocean.app_down": "availability", "mcp.unreachable": "availability", "mcp.needs_auth": "availability",
    "server.silent": "availability", "notify.channel_failing": "availability", "provider.health_": "availability",
    "analytics.api_disabled": "availability",
}


def group_of(kind) -> str | None:
    """`security`, `availability` or None for a finding type: an exact key first, then the longest prefix."""
    if not isinstance(kind, str):
        return None
    if kind in GROUPS:
        return GROUPS[kind]
    prefixes = [p for p in GROUPS if (p.endswith(".") or p.endswith("_")) and kind.startswith(p)]
    return GROUPS[max(prefixes, key=len)] if prefixes else None


def groups_for(types) -> dict[str, str]:
    """{type: group} of the types given that belong to a monitoring group, for the page's payload."""
    return {t: g for t in sorted(set(types)) if (g := group_of(t))}


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
