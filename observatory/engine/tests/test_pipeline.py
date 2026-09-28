#!/usr/bin/env python3
"""Both orchestrators must be topological orderings of one dependency graph.

There are two of them and they are NOT the same pipeline — measured 2026-09-06,
and the difference is deliberate: `observatory.py`'s `all` group never calls the
agent, so it is the deterministic pass that spends nothing, while `tools/tick.sh`
is the full scheduled cycle with the delta gate and the model behind it. Demanding
one order from both would be wrong.

What must hold in both is narrower and it is the thing that actually broke: **a
step that reads an artefact runs after the step that writes it.** `tick.sh` built
the dashboard at line 93 and rebuilt `registry/findings.json` at line 111, while
`dashboard/build_dashboard.py:27` reads that very file — so every page the
scheduled job published carried the PREVIOUS tick's findings, and `all` had the
same two steps the right way round. Neither file looked wrong on its own.

So the edges are declared once, here, and both orchestrators are checked against
them. Two things keep the declaration from decaying into a comment:

* every declared path must appear literally in the step's own script, and
* every step either declares its artefacts or is named as having none, so a step
  added to the pipeline cannot join it undeclared.
"""
from __future__ import annotations
import ast, pathlib, re, shlex, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import observatory as obs  # noqa: E402

FAILURES: list[str] = []

#: step -> the files it writes and the files it reads, repo-relative.
#: Only what a reader of the step's own script can confirm: this is a contract,
#: Reads that are satisfied by the PREVIOUS tick's output, with the reason each
#: one is a fixpoint rather than an inversion. Narrow on purpose: a pair, not a
#: step, so a step gaining a genuinely inverted read is still caught.
ACROSS_TICKS: dict[tuple[str, str], str] = {
    # A CYCLE, not an ordering mistake. `notify` reads the findings this tick
    # built, and `findings` reports whether the last notification got out — so
    # one of the two must read a receipt a tick old, and it is this one.
    # Declared when the graph learned what `findings` actually opens.
    ("findings", "store/raw/notify.json"):
        "the notifier runs AFTER the board it announces, so the board can only "
        "report on the previous run's delivery. Reordering would mean announcing "
        "findings that do not exist yet.",
    # The collectors run before the emit that rewrites the registry, so anything
    # they read from it is last tick's — the same fixpoint the entry below names
    # for the project list, extended to the documents those steps really open.
    # The same cycle one step over: the projection is committed after the board
    # is built, so the board can only report on the previous commit's outcome.
    # The same cycle, one file over, and found by tracing rather than by
    # reading. `project` writes the wiki projection AFTER the board
    # that reports on it, so the receipt the board reads is the previous run's.
    # Reordering would mean reporting on a projection that does not exist yet.
    ("findings", "store/raw/projection.json"):
        "the wiki projection is written after the board that reports on it, so "
        "its receipt is one tick behind by construction — the same fixpoint as "
        "the commit receipt below.",
    ("findings", "store/raw/commit-projection.json"):
        "the projection is committed after the findings that report on it, so "
        "the row about a refused commit is one tick behind by construction.",
    ("domains", "registry/projects.json"):
        "the probe reads which sites the estate claims before the emit that "
        "rewrites the claim. A site added this tick is probed on the next.",
    ("scan-sessions", "registry/relations.json"):
        "the same fixpoint as the project list below: attribution reads a "
        "registry the emit has not yet rewritten.",
    ("scan-sessions", "registry/repositories.json"):
        "the same fixpoint as the project list below.",
    ("scan-sessions", "registry/projects.json"):
        "attribution needs the project list, and the project list needs sessions for its "
        "activity date — a fixpoint over ticks. A project's first sessions attach on the "
        "tick AFTER the project itself appears, which is a one-tick lag on a new project "
        "rather than a wrong answer on an existing one.",
}

#: not a description, and an unfalsifiable one would drift like a comment.
ARTEFACTS: dict[str, dict[str, list[str]]] = {
    # MEASURED by `tools/trace_opens.py`, not read out of the source: this said
    # `reads: []` and the scanner opens two files by names it builds at runtime
    #. `README.md` is the estate's own — the scanner reads the first
    # heading of a checkout's README for a project's description — and the one
    # recorded here is this repository's, because this repository is a project
    # under the project directory like any other.
    "scan-fs":   {"writes": ["store/raw/local.json"],
                  "reads": ["collectors/folder_exclusions.json", "README.md"]},
    # KEEPS what only claude-mem remembers. Reads the verdict
    # `scan-sessions` wrote, so it runs after it in the tick; writes a receipt
    # the findings read, and one `proposed` ledger row per lost project — the
    # store is not declared here because every writer touches it and the graph
    # would say nothing.
    "lost":      {"writes": ["store/raw/lost-projects.json"],
                  "reads": ["store/raw/sessions.json"]},
    # Reads nothing of this project's: its subject is `$TMPDIR`, which no step
    # writes and no declaration can name. `findings` reads the receipt.
    "sweep":     {"writes": ["store/raw/fixtures.json"], "reads": []},
    # Reads the STORE, which no declaration names — the artefact table is about
    # scratch files. `findings` reads the receipt.
    "integrity": {"writes": ["store/raw/integrity.json"], "reads": []},
    "scan-gh":   {"writes": ["store/raw/gh"], "reads": []},                      # a directory, not one file
    "scan-vault": {"writes": ["store/raw/vault.json"], "reads": ["registry/domains.json"]},
    "scan-cloudflare": {"writes": ["store/raw/cloudflare_zones.json"], "reads": []},
    "scan-mcp": {"writes": ["store/raw/mcp.json"], "reads": []},
    # READS the registry it will later help write, and that is a fixpoint over
    # ticks rather than a cycle: attribution needs the project list, so a
    # project's first sessions attach on the NEXT tick after the project itself
    # appears. Declared here so nobody reads the dependency as broken.
    "scan-sessions": {"writes": ["store/raw/sessions.json"],
                      "reads": ["registry/projects.json", "registry/relations.json", "registry/repositories.json"]},
    "remotes":   {"writes": ["store/raw/remotes.json"], "reads": ["store/raw/local.json"]},
    "scan-bb":   {"writes": ["store/raw/bitbucket.json"], "reads": ["store/raw/local.json"]},
    "domains":   {"writes": ["store/raw/domains_live.json"], "reads": ["registry/domains.json", "registry/projects.json"]},
    # Reads NOTHING of this project's: it asks Heroku and walks the estate for
    # checkouts carrying an app's git remote. The registry is not an input —
    # deciding which project an application belongs to happens in `emit`, on
    # purpose, so the collector cannot be tempted to guess from a name.
    "heroku":    {"writes": ["store/raw/heroku.json"], "reads": []},
    # `reads: []` and the collector opens 212 files by names it discovers at
    # runtime — the same shape as `scan-fs` above. What it reads is the estate,
    # not an artefact any step here writes.
    "env":       {"writes": ["store/raw/env.json"], "reads": []},
    # WHAT PRODUCTION HOLDS. It reads the application list the
    # `heroku` step wrote — the ordering is real, there is nothing to ask about
    # without it — and the vault's archives, which no step writes.
    "remote-env": {"writes": ["store/raw/remote-env.json"],
                   "reads": ["store/raw/heroku.json"]},
    # WHAT GOOGLE SEES. It reads the machine's service accounts,
    # which no step writes, and answers over the network; nothing in the graph
    # orders it, and the cache inside it is what keeps that honest.
    "google": {"writes": ["store/raw/google.json"], "reads": []},
    # THE HUNT reads the inventory to learn what to look for, and the estate's
    # own files to read the values back. The transcripts it searches are outside
    # this repository and outside the graph — they belong to Claude Code.
    "leaks":     {"writes": ["store/raw/leak-scan.json",
                             "store/raw/leak-scan-state.json"],
                  "reads": ["store/raw/env.json"]},
    # The companion's stores live outside the repository and its journal under
    # store/logs; inside the tree it reads only the env scan, for the values
    #. Declared after `leaks` because the scrub follows the measure.
    "scrub-companion": {"writes": [], "reads": ["store/raw/env.json"]},
    # `--help` on a gate run, so the entry point is proved to still parse. It
    # writes nothing; what it READS is declared because the derivation reads the
    # source rather than the invocation, and the source does open both — the
    # inventory to list names and the scan to resolve one.
    "use":       {"writes": [],
                  "reads": ["registry/env-inventory.json", "store/raw/env.json"]},
    # Reads FOUR credential files and the provider, and declares none of them:
    # the artefact table names files a step of this pipeline writes, and a key
    # outside the repository is neither written here nor something the graph
    # could order against.
    "openrouter": {"writes": ["store/raw/openrouter.json"], "reads": []},
    # Writes a COPY of the store, which is gitignored and named by a stamp, so
    # no declaration can name the file it produces. Reads the store, which the
    # artefact table does not describe either.
    "backup":    {"writes": [], "reads": []},
    "merge":     {"writes": ["store/raw/model.json"],
                  # `store/raw/gh` is read by a GLOB — `(SP/"gh").glob("*.json")` — which no
                  # spelling the derivation reads can see. It was found by asking what
                  # the step's COMMAND names instead: `merge` is handed the whole
                  # `store/raw` directory, and the graph knew six files in it
                  #.
                  "reads": ["store/raw/gh", "store/raw/local.json", "store/raw/vault.json",
                            "store/raw/remotes.json", "store/raw/bitbucket.json",
                            "store/raw/sessions.json", "registry/domains.json",
                            "collectors/domain_claims.json",
                            "store/raw/cloudflare_zones.json"]},
    "emit":      {"writes": ["registry/projects.json", "registry/repositories.json",
                             "registry/relations.json", "registry/sources.json",
                             "registry/duplicate-repo-names.json",
                             "registry/cloudflare-zones.json", "registry/products.json",
                             "registry/mcp-servers.json",
                             "registry/domain-liveness.json",
                             # UNDECLARED until it was traced. `validate` reads
                             # this file, so without the write here the graph
                             # could not order the pair at all — the exact shape
                             # of the inversion it exists to catch.
                             "registry/stale-remotes.json",
                             # The hosting document, written here rather than by
                             # the collector: the app->project edge needs the
                             # project list, and only `emit` holds it.
                             "registry/heroku-apps.json",
                             # The credential document, for the same reason as the
                             # hosting one: the project edge needs the project
                             # list, and only `emit` holds it.
                             "registry/credentials.json",
                             # The env inventory, written here for the third time
                             # for the same reason: the scan knows a folder name
                             # and only `emit` knows whether it is a project.
                             "registry/env-inventory.json",
                             # And the production-configuration verdicts, for the
                             # fourth time for the same reason: the scan knows an
                             # application and only `emit` writes registry documents.
                             "registry/remote-env.json",
                             # The analytics inventory, written here for the
                             # same reason as the rest: the project join needs
                             # the project list and only `emit` holds it.
                             "registry/google-properties.json"],
                  "reads": ["store/raw/google.json", "store/raw/remote-env.json",
                            "store/raw/cloudflare_zones.json", "store/raw/mcp.json", "collectors/products.json",
                            "collectors/host_boundary.json", "store/raw/model.json", "registry/domains.json", "store/raw/domains_live.json",
                            "store/raw/heroku.json", "collectors/heroku_links.json",
                            "store/raw/openrouter.json", "collectors/credential_owners.json",
                            "store/raw/env.json"]},
    # Eight of its eleven inputs were undeclared until they were MEASURED
    #. A validator reads more than it validates: the domain half
    # needs the registrar export, the Cloudflare snapshot and both exclusion
    # lists to tell "not ours" from "missing".
    "validate":  {"writes": [], "reads": ["registry/projects.json", "registry/relations.json",
                                          "registry/sources.json", "registry/repositories.json",
                                          "registry/domains.json", "registry/domain-liveness.json",
                                          "registry/domain-exclusions.json",
                                          "registry/stale-remotes.json",
                                          # The engine reads the Cloudflare
                                          # snapshot from a workspace source
                                          # (`cloudflare_snapshot`), not from a
                                          # `registry/snapshots` directory.
                                          "registry/_raw",
                                          "collectors/activity_tiers.json"]},
    # NOT "store only". Measured 2026-09-09: it reads the whole registry to
    # attribute a commit to a project, and `store/retention.json` for the
    # horizon it must not insert past (T25). The store itself stays undeclared,
    # by the convention above — every writer touches it.
    "scan-events": {"writes": [],
                    "reads": ["registry/projects.json", "registry/repositories.json",
                              "registry/relations.json", "store/retention.json"]},
    "corroborate": {"writes": ["store/raw/corroboration.json"], "reads": ["registry/repositories.json"]},                    # store only
    "export-ledger": {"writes": ["registry/ledger.jsonl"], "reads": []},
    "findings":  {"writes": ["registry/findings.json"],
                  # `registry/heroku-apps.json` is read through
                  # `tools/heroku_findings.py`, and the ordering it creates is
                  # real: findings built before `emit` has rewritten the hosting
                  # document describe the previous scan.
                  # EVERY INPUT, because the ordering guarantee is only as
                  # wide as this list. It named three of twenty-two — so a
                  # reordering that put this step before `corroborate`,
                  # `plugins`, `rollup` or `agent` would have passed the
                  # topological check while building the board from the
                  # PREVIOUS tick's receipts, which is the defect the
                  # dashboard already had once.
                  # And six more, found by TRACING rather than by reading —
                  # twenty-two was what the source named, not what the process
                  # opened. Five of the six are collector outputs the
                  # board reports on; `projection.json` is the wiki write whose
                  # receipt it reads.
                  # The always-on server's heartbeat and the skill handshake:
                  # written by a daemon and a session tool OUTSIDE the step
                  # graph, so they add no ordering edge — declared so the read
                  # is known.
                  "reads": ["registry/google-properties.json", "registry/remote-env.json",
                            "store/raw/heroku.json",
                            "store/raw/serverd.json", "store/raw/skill-sessions.json",
                            "registry/heroku-apps.json", "registry/env-inventory.json",
                            "store/raw/leak-scan.json",
                            # folders agents opened that the registry never joined
                            # — read by session_findings
                            "store/raw/sessions-seen.jsonl",
                            "store/raw/bitbucket.json", "store/raw/domains_live.json",
                            "store/raw/local.json", "store/raw/model.json",
                            "store/raw/projection.json", "store/raw/vault.json",
                            "store/raw/agent.json",
                            "store/raw/commit-projection.json",
                            "store/raw/corroboration.json",
                            "store/raw/fixtures.json",
                            "store/raw/integrity.json",
                            "store/raw/lost-projects.json",
                            "store/raw/notify.json",
                            "store/raw/plugins.json",
                            "store/raw/record-turn.json",
                            "store/raw/remotes.json",
                            "store/raw/retention.json",
                            "store/raw/rollup.json",
                            "store/raw/sessions.json",
                            "store/raw/tick-lease.json",
                            "store/raw/tick.json",
                            "store/raw/vault-links.json",
                            "registry/credentials.json",
                            "registry/mcp-servers.json",
                            "registry/domain-liveness.json",
                            "registry/domains.json",
                            "registry/projects.json",
                            "registry/relations.json",
                            "registry/repositories.json",
                            "registry/stale-remotes.json",
                            # the project-identity map the engine resolves ids
                            # through; an unreadable one is its own finding
                            "registry/identity.json",
                            "collectors/finding_acks.json",
                            "collectors/host_boundary.json"]},
    # The page reads the WALLET and the plugin manifests too, which no reading
    # of its source could see: both are resolved through `paths` and a glob.
    # `store/provider-health.json` and `store/wallet.json` are written by
    # `agent`, so declaring them is what puts the page after the run it reports.
    "dashboard": {"writes": ["docs/projects-dashboard.html", "docs/dashboard"],
                  # `serverd.json` is the daemon's heartbeat: no step writes it,
                  # so it adds no ordering edge — declared because the page
                  # renders it.
                  "reads": ["store/raw/serverd.json",
                            # the Heroku config trail (names only) — the keys
                            # page's «Движения ключей» reads it for the releases
                            # nobody recorded; written by `heroku`
                            "store/raw/heroku.json",
                            "registry/google-properties.json",
                            "registry/remote-env.json",
                            "registry/credentials.json",
                            "registry/heroku-apps.json",
                            "registry/cloudflare-zones.json", "registry/products.json",
                            "registry/mcp-servers.json",
                            "registry/env-inventory.json",
                            "registry/projects.json", "registry/repositories.json",
                            "registry/relations.json", "registry/domains.json",
                            "registry/duplicate-repo-names.json",
                            "registry/findings.json", "registry/domain-liveness.json",
                            "store/wallet.json", "store/provider-health.json",
                            "plugins"]},
    "smoke":     {"writes": [], "reads": ["docs/projects-dashboard.html"]},
    "notify":    {"writes": ["store/raw/notify.json"], "reads": ["registry/findings.json"]},
    # A fingerprint is of the WHOLE registry, so it reads the whole registry —
    # measured 2026-09-09, two of the three reads undeclared. The old entry made
    # `snapshot` look like it depended on the project list alone, which is the
    # kind of half-truth an ordering check cannot use.
    "snapshot":  {"writes": [], "reads": ["registry/projects.json",
                                          "registry/repositories.json",
                                          "registry/relations.json"]},
    "deltas":    {"writes": [], "reads": []},                      # store only
    "agent":     {"writes": ["store/raw/agent.json"], "reads": ["registry/projects.json"]},                      # store only
    # NOT "store only". Traced 2026-09-09: the indexer CHARGES the wallet, so it
    # writes `store/wallet.json` — the file the page reports and `agent` also
    # writes. The graph could not order the page after the second spender while
    # only the first one declared it.
    "index":     {"writes": ["store/wallet.json"], "reads": []},
    "retention-apply": {"writes": ["store/raw/retention.json"], "reads": []},                # store only
    # A directory read: it stages the whole tree, so it depends on EVERY writer
    # of anything under registry/, not on the three files it happens to name.
    "commit-registry": {"writes": [], "reads": ["registry/"]},
    # `registry/` — the DIRECTORY, because the mirror is derived from
    # `registry/*.json` rather than from a list of names. Declaring
    # one file made this assertion measure a literal in the source, and the
    # source deliberately stopped naming files; the step reads all of them.
    "project":   {"writes": ["store/raw/projection.json"], "reads": ["registry/"]},
    "commit-projection": {"writes": ["store/raw/commit-projection.json"],
                          "reads": []},                        # the wiki, not this tree
}

#: Steps that touch no pipeline artefact — gates, tests and one-off tools. Listed
#: rather than defaulted, so a new step in `all` or the tick has to be classified.
NO_ARTEFACTS = {
    "fabric", "contract", "ledger-current", "design", "validate-plugin", "probes",
    "probes-check", "commit-projection", "rollup", "stats", "test-rollup", "plugins", "test-plugins", "test-dashboard", "digest", "test-queue", "test-wire-contract", "docs-current", "test-docs", "links",
    "test-activity", "activity", "migrate",
    "test", "test-ledger", "test-skill", "test-agent", "test-key", "test-index",
    "test-retention", "test-tick", "test-pipeline", "test-wire", "lease",
    # Writes nothing, like every other suite here. It joined `all` on 2026-09-07
    # because it belonged to NO group and had therefore been red for two
    # iterations unnoticed (rule 12 in tools/check_docs.py now refuses that).
    # It cannot join `check`: it drives a whole `./observatory.py check` as its
    # own fixture, so `all` pays for one nested gate run.
    "test-gate-purity",
    "setup", "deps", "chain", "wallet", "key", "revoke", "models", "review",
    "retention", "reindex", "index-status", "pending", "tick", "scan-bb-noop",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def step_signature(argv: list[str]) -> tuple[str, tuple[str, ...]] | None:
    """(script basename, every following argument). The arguments are what tells
    `compute_deltas.py snapshot` from `compute_deltas.py diff`; matching on the
    script alone made three steps look like one."""
    for i, a in enumerate(argv):
        if a.endswith((".py", ".js")):
            return pathlib.Path(a).name, tuple(argv[i + 1:])
    return None


def tick_order() -> list[str]:
    """The steps tools/tick.sh runs, in the order it runs them."""
    by_sig = {}
    for name, argv in obs.STEPS.items():
        sig = step_signature(argv)
        if sig:
            by_sig[sig] = name
    order: list[str] = []
    for raw in (ROOT / "tools/tick.sh").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Redirections FIRST, then operators. Splitting on `>` alone leaves the
        # file descriptor of `2>&1` behind as a bare argument, which made
        # `build_findings.py 2` match no step at all — so the tick's real
        # dashboard/findings inversion read as a clean pipeline. A parser that
        # silently drops a step turns this whole file green for the wrong reason.
        head = re.sub(r"\d*>>?\s*&?\S+", " ", line)
        head = re.split(r"\|\||&&|[|;]", head)[0]
        try:
            words = shlex.split(head)
        except ValueError:
            continue
        sig = step_signature(words)
        if sig and sig in by_sig and by_sig[sig] not in order:
            order.append(by_sig[sig])
    return order


def topological_problems(steps: list[str]) -> list[str]:
    """Every read that happens before its write, in this ordering. Returned rather
    than reported, so the planted-defect check below can call it without printing
    a FAIL line for a defect it put there on purpose."""
    writer_of: dict[str, str] = {}
    for s in steps:
        for w in ARTEFACTS.get(s, {}).get("writes", []):
            writer_of.setdefault(w, s)
    bad: list[str] = []
    for i, s in enumerate(steps):
        for r in ARTEFACTS.get(s, {}).get("reads", []):
            if (s, r) in ACROSS_TICKS:
                continue
            # A path ending in `/` is a directory: it depends on every writer of
            # anything beneath it, which is what a step staging a whole tree does.
            writers = ([w for p, w in writer_of.items() if p.startswith(r)] if r.endswith("/")
                       else [writer_of[r]] if r in writer_of else [])
            for w in writers:
                if steps.index(w) > i:
                    bad.append(f"{s} reads {r} but {w} writes it later")
    return sorted(set(bad))


def assert_topological(label: str, steps: list[str]) -> None:
    bad = topological_problems(steps)
    check(f"{label}: every reader runs after its writer", not bad, "; ".join(bad))


def test_both_orchestrators_are_topological() -> None:
    tick = tick_order()
    check("tick.sh parses to a step list", len(tick) > 10, f"{len(tick)} matched: {tick}")
    assert_topological("tick.sh", tick)
    assert_topological("observatory.py `all`", obs.GROUPS["all"])
    # The gate is a pipeline too: `design` and `smoke` read the page `dashboard`
    # builds, and a gate whose input is built after it inspects it is a gate over
    # yesterday's artefact — or, on a fresh clone, over nothing at all.
    assert_topological("observatory.py `check`", obs.GROUPS["check"])


def test_every_pipeline_step_is_classified() -> None:
    """A step added to a pipeline must declare its artefacts or be named as
    having none — otherwise the graph silently stops covering the pipeline."""
    seen = set(tick_order()) | set(obs.GROUPS["all"])
    unclassified = sorted(seen - set(ARTEFACTS) - NO_ARTEFACTS)
    check("no step in either pipeline is undeclared", not unclassified, str(unclassified))


#: A read the step's own script CANNOT name, with what opens it instead. The
#: rule below exists so a declaration cannot decay into fiction, and it collided
#: with the opposite guarantee on 2026-09-09: `tools/trace_opens.py` watched
#: `validate` open two files whose names it never spells, because a resolver and
#: an imported helper open them. Both rules are right; what was missing was the
#: third outcome. An entry here is a MEASUREMENT, re-derivable by running the
#: tracer, and it must say which code does the opening — a bare exemption list
#: would be the fiction the rule guards against, one level up.
TRACED: dict[tuple[str, str], str] = {
    ("validate", "registry/_raw"):
        "`paths.NAMECHEAP_EXPORT` resolves the registrar export; the validator "
        "names the resolver, never the path",
    ("index", "store/wallet.json"):
        "charged by `agent/providers.py` on the indexer's behalf — the embed "
        "call spends, and the journal it writes is named in the provider "
        "boundary rather than in the step",
    # ("findings", "store/raw/vault.json") was once exempt: it was opened only
    # by `degradations.every_collector()` and named nowhere in the builder's
    # prose. The builder now reads it deliberately — wiki staleness — so the
    # ordinary declaration covers it and the exemption retired itself. Kept as a comment because a removed exemption is evidence
    # the mechanism works.
    ("dashboard", "docs/projects-dashboard.html"):
        "the page is written to `paths.DASHBOARD_HTML`, a workspace setting, so "
        "the builder names the resolver rather than the file",
    ("validate", "collectors/activity_tiers.json"):
        "opened by `collectors/registry_read.py` on the validator's behalf, so "
        "the name is in the helper rather than in the step",
}


def test_the_declaration_is_not_a_comment() -> None:
    """Every declared path appears literally in the step's own script.

    Unless it is in `TRACED` — measured being opened, by code the step calls
    rather than by the step itself.
    """
    missing: list[str] = []
    for step, io in ARTEFACTS.items():
        argv = obs.STEPS.get(step)
        if not argv:
            missing.append(f"{step}: no such step")
            continue
        script = next((a for a in argv if a.endswith((".py", ".js"))), None)
        if not script:
            continue
        # The step's own argv counts as naming it: `scan-fs` takes its output
        # path as an argument, so the path is declared in code either way.
        src = (ROOT / script).read_text(encoding="utf-8") + "\n".join(argv)
        for path in io["writes"] + io["reads"]:
            name = pathlib.Path(path.rstrip("/")).name
            if name not in src and (step, path) not in TRACED:
                missing.append(f"{step} declares {path}, which its script never names")
    check("every declared artefact is named in the step's script", not missing,
          "; ".join(missing[:4]))


def test_every_traced_exemption_is_still_needed() -> None:
    """An exemption that has outlived its reason is a hole nobody is watching.

    Two things are checked, and neither is the trace itself — running it takes
    minutes and executes collectors. First, the step still declares the path:
    an exemption for a declaration that is gone exempts nothing. Second, the
    script still does NOT name it: the day the code spells the path out, the
    ordinary rule covers it and the exemption must go.
    """
    stale: list[str] = []
    for (step, path), why in TRACED.items():
        io = ARTEFACTS.get(step)
        if not io or path not in io["reads"] + io["writes"]:
            stale.append(f"{step} no longer declares {path}")
            continue
        argv = obs.STEPS.get(step) or []
        script = next((a for a in argv if a.endswith((".py", ".js"))), None)
        src = (ROOT / script).read_text(encoding="utf-8") if script else ""
        if pathlib.Path(path.rstrip("/")).name in src:
            stale.append(f"{step} now names {path} itself; the exemption is spent")
        if len(why) < 20:
            stale.append(f"{step}:{path} carries no reason worth reading")
    check(f"every one of the {len(TRACED)} traced exemptions is still earning it",
          not stale, "; ".join(stale[:3]))


def test_the_check_would_catch_an_inversion() -> None:
    """A gate nobody has watched fail against a planted defect is not a gate."""
    check("the ordering check passes a correct pair",
          not topological_problems(["findings", "dashboard"]))
    caught = topological_problems(["dashboard", "findings"])
    check("the ordering check catches the inverted pair — the real 2026-09-06 defect",
          len(caught) == 1 and "findings.json" in caught[0], str(caught))



def files_a_script_opens(script: pathlib.Path) -> set[str]:
    """Every registry document and scratch receipt named in one script.

    Four spellings cover almost every access in this codebase: `SCRATCH / "x"`,
    `REGISTRY / "x"`, `reg("x")` and `_load("x")`. A path built any other way is
    invisible here — which is a limit of the derivation and not a licence, so a
    new spelling belongs in this list rather than around it.
    """
    import re
    if not script.is_file():
        return set()
    src = script.read_text(encoding="utf-8")
    out = {f"store/raw/{x}" for x in re.findall(r'SCRATCH\s*/\s*"([^"]+)"', src)}
    out |= {f"registry/{x}" for x in re.findall(r'REGISTRY\s*/\s*"([^"]+)"', src)}
    out |= {f"registry/{x}" for x in re.findall(r'reg\("([^"]+)"\)', src)}
    out |= {f"registry/{x}" for x in re.findall(r'_load\("([^"]+)"\)', src)}
    return out


def test_every_step_declares_the_files_its_script_opens() -> None:
    """The ordering guarantee is only as wide as these lists, and they were hand
    written.

    `findings` declared three of the twenty-two files it opens, and the first
    dependency the declaration made visible was a live defect: `lost` wrote a
    receipt seventy lines after `findings` read it, so every board was built from
    the previous tick's answer. Sweeping the rest found sixteen more
    undeclared files across thirteen steps — no second reorderable defect, and
    four reads that are one tick old BY CONSTRUCTION and now say so in
    `ACROSS_TICKS` rather than by nobody noticing.

    Derived, not listed twice: the declaration is compared with what each script
    actually opens, so the next receipt any step reads cannot be added without
    the graph learning about it.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("obs", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obs)
    checked = 0
    for step, io in sorted(ARTEFACTS.items()):
        cmd = obs.STEPS.get(step) or []
        script = next((c for c in cmd if isinstance(c, str) and c.endswith(".py")), None)
        if not script:
            continue
        declared = set(io.get("reads") or []) | set(io.get("writes") or [])
        dirs = [d for d in declared if d.endswith("/")]
        missing = sorted(f for f in files_a_script_opens(ROOT / script) - declared
                         if not any(f.startswith(d) for d in dirs))
        if missing:
            check(f"{step} declares every file {script} opens", False,
                  f"{missing} — a dependency the graph cannot see is an ordering "
                  f"the graph cannot check")
        checked += 1
    check(f"every step with a script was compared with its source ({checked})",
          checked >= 20, f"{checked} — the sweep is only as wide as the map")



def handed_as_declared(arg: str) -> str:
    """The repository-relative spelling of a path a step is handed.

    The engine rewrites its step table so mutable arguments point into the
    selected workspace (`store/raw/x` becomes `<scratch>/x`), while the map
    above declares the historical relative names. Mapped back here so the two
    can be compared.
    """
    import paths
    for base, rel in ((paths.SCRATCH, "store/raw"), (paths.REGISTRY, "registry")):
        try:
            tail = pathlib.Path(arg).relative_to(base)
        except ValueError:
            continue
        return rel if str(tail) == "." else f"{rel}/{tail}"
    if arg == str(paths.DASHBOARD_HTML):
        return "docs/projects-dashboard.html"
    return arg


def test_the_path_a_step_is_HANDED_is_declared_too() -> None:
    """A second derivation, and this one needs no pattern matching.

    `files_a_script_opens` reads four spellings out of the source, and 22 of the
    28 pipeline scripts contain a file access it cannot see: a path built into a
    variable, an `argv`, a glob. Measured 2026-09-08, and four scripts —
    `dashboard`, `validate`, `scan-events`, `scan-fs` — have NO access the four
    spellings catch at all, so their entries in the map are checked against
    nothing.

    What needs no heuristic is the path the step is HANDED: `observatory.STEPS`
    carries it as an argument, and a step given a path touches it. That found the
    one edge the spellings could never see — `merge` globs `store/raw/gh` and the
    graph did not know, because `merge` is handed the whole directory.

    The blind spot that remains is named rather than papered over: a step reading
    a file it neither names as a literal nor receives as an argument is invisible
    to both derivations, and the only complete answer is executing it.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("obs", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obs)
    checked = 0
    for step, io in sorted(ARTEFACTS.items()):
        declared = set(io.get("reads") or []) | set(io.get("writes") or [])
        dirs = [d for d in declared if d.endswith("/")]
        for arg in (obs.STEPS.get(step) or [])[2:]:
            if not isinstance(arg, str) or arg.startswith("-") or "/" not in arg:
                continue
            if arg.endswith(".py"):
                continue
            arg = handed_as_declared(arg)
            checked += 1
            known = (arg in declared or any(arg.startswith(d) for d in dirs)
                     or any(d.startswith(arg.rstrip("/") + "/") for d in declared))
            check(f"{step} declares the path it is handed ({arg})", known,
                  f"the command names it, so the step touches it, and the graph "
                  f"cannot order what it does not know")
    check(f"every path handed on a command line was checked ({checked})",
          checked >= 8, f"{checked} — the sweep is only as wide as the commands")


def test_every_test_a_suite_defines_is_dispatched() -> None:
    """A function named `test_*` that nothing calls runs never and reports nothing.

    Every suite here dispatches by hand — `for fn in (a, b, c): fn()` — so a
    case added without a line in that tuple is silently dead, and its absence
    looks exactly like a passing run. Measured 2026-09-08: 970 test functions
    across the suites and all 970 dispatched, so this is a guard rather than a
    repair. It is the cheapest form of the property `tools/trap_map.py` checks
    for trap guards, applied to every suite instead of to eleven of them.

    A suite that dispatches dynamically through `globals()` is exempt and says
    so by using it; none does today.
    """
    dead: list[str] = []
    suites = 0
    total = 0
    for path in sorted((ROOT / "tests").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        defined = [n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name.startswith("test")]
        if not defined:
            continue
        main = [n for n in tree.body if isinstance(n, ast.If)
                and any(isinstance(c, ast.Name) and c.id == "__name__"
                        for c in ast.walk(n.test))]
        if not main:
            dead.append(f"{path.name}: {len(defined)} test(s) and no __main__ block")
            continue
        names = {s.id for m in main for s in ast.walk(m) if isinstance(s, ast.Name)}
        if names & {"globals", "vars"}:
            continue
        suites += 1
        total += len(defined)
        dead += [f"{path.name}:{d}" for d in defined if d not in names]
    check(f"every test function is dispatched ({total} across {suites} suites)",
          not dead, str(dead[:5]))
    # Every script-style suite present must have been swept; the count depends
    # on which suites a distribution ships, so it is compared with the files
    # rather than with a remembered number.
    scripted = sum(1 for path in (ROOT / "tests").glob("test_*.py")
                   if re.search(r"^def test", path.read_text(encoding="utf-8", errors="replace"), re.M))
    check("the sweep found the suites it expects", suites >= 1 and suites >= scripted - len(dead)
          and total > suites,
          f"{suites} of {scripted} suites, {total} tests — a collapsed sweep passes for free")


if __name__ == "__main__":
    print("the pipeline — one dependency graph, two orchestrators\n")
    for fn in (test_both_orchestrators_are_topological,
               test_every_pipeline_step_is_classified,
               test_the_declaration_is_not_a_comment,
               test_every_traced_exemption_is_still_needed,
               test_the_check_would_catch_an_inversion,
               test_every_step_declares_the_files_its_script_opens,
               test_the_path_a_step_is_HANDED_is_declared_too,
               test_every_test_a_suite_defines_is_dispatched):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mboth orchestrators order their readers after their writers\033[0m")
