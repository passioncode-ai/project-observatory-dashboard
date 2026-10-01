#!/usr/bin/env python3
"""Turn measured facts into a list of things that need a person.

WHY THIS EXISTS. The registry can know that domains will expire soon with
auto-renewal off, that domains still paid for resolve nowhere, that published
project homepages are dark, and that branches of real work exist on exactly one
disk — and when every one of those sits in a JSON file that nothing reads on a
schedule, nobody is told. A measurement nobody is told about is not
observability; it is a diary.

WHY A FINDING IS NOT A LEDGER ROW. The ledger is append-only history: what was
observed, by whom, at what revision. A finding is a DERIVED projection over
current facts, and it must be rebuildable — the same rule that governs
`DERIVED_TYPES` in the emitter. Writing findings into the ledger would append
a row per finding on every scan and make history unreadable within a day.

THE ONE THING CARRIED FORWARD is `first_seen`, read from the previous file, so
"open for three months" can be said at all. Delete `findings.json` and every
finding looks new today — an acceptable and stated degradation, not a silent one.

An operator's acknowledgement lives in `collectors/finding_acks.json`, curated
and outside the rebuild, because a value kept only inside a generated file is
lost the first time it is generated again (trap T11).

    build_findings.py [--json]
"""
from __future__ import annotations
import hashlib, json, os, pathlib, re, shutil, sqlite3, sys
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import degradations              
import activity
import estate              
import identity
import atomic
import companion_faults              
import leak_register  # noqa: E402
import paths              
from store import db as store_db              

OUT = paths.REGISTRY / "findings.json"
ACKS = paths.FINDING_ACKS
TODAY = date.today().isoformat()
SOON_CRITICAL, SOON_WARNING = 30, 90
#: How long a committed revision may sit unindexed before it is a finding. Six
#: hours is twelve ticks at the launchd cadence: long enough that a provider
#: hiccup or one reached ceiling resolves itself without paging the operator,
#: short enough that a wedged queue is caught the same day it wedges.
PROJECTION_LAG_HOURS = 6

#: How long a measured change may wait for interpretation before it is a
#: finding. The same six hours as the projection's, for the same reason — twelve
#: ticks at the launchd cadence — and named separately because the two answer
#: different questions and one of them may need to move without the other.
INTERPRETATION_LAG_HOURS = 6

#: How long the wiki's copy may lag before a standing refusal becomes a
#: finding. Twenty-four hours because a refusal is usually an operator working
#: in their own wiki and finishing the same day; a mirror that has not tracked
#: the registry for a full day has stopped tracking it. Forty-eight ticks.
PROJECTION_COMMIT_HOURS = 24
#: Four ticks. `deltas.diffed_through` advances on every SUCCESSFUL diff, even
#: one that found nothing, so a cursor that has not moved while fingerprints
#: keep arriving means the `deltas` step itself has stopped running — and the
#: agent is then being fed an empty queue while the estate changes. Four ticks
#: rather than two, because a manual `snapshot` between ticks legitimately puts
#: the pair out of step for one interval.
DELTA_DIFF_HOURS = 2
#: Free space, in MiB, below which this system stops being able to do its own
#: work. Two levels because the consequences differ: at the warning level a
#: VACUUM (which rewrites the whole store beside itself) may not fit; at the
#: critical level a collector cannot write its output at all — which happened on
#: 2026-09-07 at 06:39, when four scans died in one tick on `[Errno 28]`.
DISK_WARNING_MIB, DISK_CRITICAL_MIB = 5120, 1024
#: Six missed ticks, and a day. The tick runs every thirty minutes, so three
#: hours without a scan is not a slow run — it is a scheduler that has stopped.
#: NOTHING detected this before 2026-09-07: `tick.step_failed` needs a step to
#: fail, and a tick that never starts has no steps. For a system whose whole
#: subject is watching, "the watcher stopped" is the one thing it must be able
#: to say about itself.
SCAN_WARNING_HOURS, SCAN_CRITICAL_HOURS = 3, 24

#: Tick steps whose failure is worse than a degradation, and why. Anything not
#: named here is a warning: the tick is designed to carry on, and most of its
#: steps degrade into "not measured this run". These do not.
SEVERE_STEPS = {
    "retention": "An ERASURE DID NOT COMPLETE. The ledger rows are tombstoned — the "
                 "system's own view says they are gone — while a derived index either "
                 "still holds one or could not be checked at all. Until this passes, "
                 "`search` can return text the store considers erased.",
    "registry": "The registry was rewritten and NOT committed, so the next tick's diff "
                "compares against a tree nobody recorded.",
}

REVIEW_HORIZON_DAYS = int(json.loads(
    (paths.config_file("retention.json")).read_text(encoding='utf-8')
)['ledger']['proposed_days'])
REVIEW_BACKLOG = 20

#: How many (source, reason) pairs a `collector.degraded` row prints. Six RDAP
#: 404s fit; a collector failing on ninety owners must not print ninety lines,
#: and the remainder is COUNTED rather than dropped.
DEGRADED_LISTED = 8


def hold_verdict(rec: dict) -> str:
    """`held`, `clear` or `unreadable` — three answers where there were two.

    RDAP's `status` array decides the CRITICAL `domain.hold` row. A check that
    joins the statuses and looks for "hold" gives an absent key and an empty
    array the same answer: not on hold. A rename upstream would then empty
    `statuses` for every domain and take the only signal that a domain is on
    registrar hold with it, in silence, because the fetch itself succeeded.

    A FUNCTION rather than an inline branch, so the third answer can be driven
    from a fixture instead of asserted from the source — the collector's own
    record is the only input.
    """
    if "status" in (rec.get("unread") or []):
        return "unreadable"
    return "held" if "hold" in " ".join(rec.get("statuses") or []).lower() else "clear"


def _remote_asked() -> str:
    """When the remote scan last ran, as a phrase a reader can act on.

    Read from `store/raw/remotes.json` — one value for the whole run — because
    storing it per repository made every tick rewrite every registry row with
    nothing but a clock (the reason `remote_checked_on` is a date). Absent
    means the scan has never run here, and saying "never" is the honest answer:
    a missing timestamp rendered as today's date is the defect this replaces.
    """
    try:
        doc = json.loads((paths.SCRATCH / "remotes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "at an unrecorded time"
    when = str(doc.get("scanned_at") or "")
    if len(when) < 20:
        return "at an unrecorded time"
    try:
        made = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return f"at {when}"
    hours = (datetime.now(timezone.utc) - made).total_seconds() / 3600
    # THE DAY, NOT ONLY THE CLOCK. The first version printed `03:01Z, 27h ago`
    # and a reader could not tell which day that was — and it dropped the date
    # the row used to carry, which `tests/test_at_stake.py` noticed within the
    # minute because the row's age assertion had been leaning on it.
    return (f"{when[:10]} {when[11:16]}Z, {hours:.0f}h ago" if hours >= 1
            else f"{when[:10]} {when[11:16]}Z, under an hour ago")


REMOTE_ASKED = _remote_asked()

#: Owners retention never erases, read from the same file that enforces it. Two
#: of the three readers of the horizon ignored this list, so both the digest and
#: the expiry finding could put a deadline on a row that has none.
EXEMPT_OWNERS = tuple(json.loads(
    (paths.config_file("retention.json")).read_text(encoding='utf-8')
)['ledger'].get('owner_exempt') or ())


def reg(name: str) -> dict:
    f = paths.REGISTRY / name
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def days_until(iso: str) -> int | None:
    try:
        return (date.fromisoformat(iso) - date.fromisoformat(TODAY)).days
    except ValueError:
        return None


#: A daily growth big enough to be worth a sentence beside a full volume.
#: Below this, a working tree's churn is noise — an editor's swap files and a
#: build cache moving about.
GROWTH_MIB_PER_DAY = 100

#: Above this, leaked test fixtures are a warning rather than a note. Set at one
#: gibibyte because that is the order at which this project's own litter starts
#: competing with the estate it observes: leaked fixtures once grew large enough
#: to fill the volume, while `host.disk_low` named the largest repositories as
#: the holders.
FIXTURE_LEAK_GIB = 1.0

#: Below this, a shape verdict is stated plainly; at or above it the finding says
#: the reading may already be stale. `./observatory.py key` is operator-run by
#: design — the tick cannot see these variables — so the observation ages on its
#: own and the finding must not assert a fortnight-old reading as current.
KEY_SHAPE_HEDGE_DAYS = 3.0

#: Skips before the gap is worth saying. One is the design working — the next
#: tick is thirty minutes away. Two means an hour, which is when "the dashboard
#: is current" stops being true.
TICK_SKIPS_BEFORE_WARNING = 2

#: After this, an `ok` verdict is reported as a statement about the past rather
#: than about now. Twelve hours is twenty-four ticks: long enough that a single
#: missed cycle says nothing, short enough that a stalled tick shows up here as
#: well as in `tick.standing_down`.
INTEGRITY_STALE_HOURS = 12.0


#: What can be freed, as opposed to what a project costs. `host.disk_low` asks
#: "why is my disk full" and once read only the footprint role — which covered a
#: small fraction of what the project root occupied, while several times the
#: remaining free space was reclaimable. The finding named everything except
#: what would actually free space.
RECLAIMABLE_ROLE = "footprint.reclaimable.bytes"

#: The ROLE a metric plays, not the metric's name. `plugins/README.md` opens
#: with "Adding one touches no file outside this directory", and the first
#: version of `disk_culprits()` broke that promise in the same change that
#: tested it: it named `disk.bytes` in SQL, so this core file knew one plugin's
#: vocabulary and a replacement plugin could not take the question over. A
#: manifest declares `"role": "footprint.bytes"` on the metric that answers it,
#: and this resolves the name from whatever is installed.
FOOTPRINT_ROLE = "footprint.bytes"

#: Whether a project has ever shipped. Resolved by ROLE for the reason
#: `FOOTPRINT_ROLE` above exists, applied to a metric added a day later: the
#: portfolio finding asks the question and must not learn which plugin answers
#: it.
RELEASE_COUNT_ROLE = "release.count"


#: One row per clone state, and the set is CLOSED against
#: `scan_remotes.STATES` by `tests/test_clone_sync.py` — because the previous
#: version was a `.get()` whose miss was a `continue`, and `ahead` fell into it.
#: Eight clones, this repository among them, held commits that existed on no
#: remote and produced no finding at all, while `local-only-branch` — the same
#: fact where the branch has no remote counterpart — was a warning telling the
#: operator to push.
SYNC_FINDINGS = {
    "local-only-branch": ("warning", "has a branch that exists on no remote",
                          "work here survives only this disk",
                          "push the branch, or delete it deliberately"),
    "ahead": ("warning", "holds commits that are on no remote",
              "the remote's own commit is an ancestor of this checkout, so this "
              "work survives only this disk",
              "push the branch"),
    "unpushed-and-remote-moved": (
        "warning", "holds unpushed commits while its remote has moved",
        "this checkout is not contained in the branch as of its last fetch, and "
        "the remote has since moved — so there is work only on this disk, and it "
        "will not fast-forward",
        "push what is here, or reconcile the two deliberately"),
    "diverged": ("warning", "has diverged from its remote",
                 "local and remote have both moved", "reconcile them"),
    "stale": ("info", "is behind its remote",
              "the remote has moved and this checkout is contained in the branch "
              "as of its last fetch, so nothing here is at risk",
              "pull when you next work here"),
    "behind": ("info", "is behind its remote", "the remote has newer commits",
               "pull"),
    "behind-or-diverged": (
        "info", "may be behind its remote",
        "the remote commit is not in this clone and there is no tracking ref to "
        "compare against, so the direction cannot be told without a fetch",
        "fetch when it matters — nothing local can answer this one"),
    #: Set by `merge.py` when the probe could not reach the remote at all. The
    #: reason is already reported by `remote.unreachable`; this row exists so
    #: the state is DECLARED rather than silently skipped.
    "unreachable": ("info", "could not be compared with its remote",
                    "the remote did not answer when it was last probed",
                    "see the unreachable-remote finding for what git said"),
    "unknown": ("info", "has a clone whose state could not be determined",
                "the checkout has no resolvable HEAD, so there was nothing to "
                "compare",
                "check that the clone is not empty or corrupt"),
}

#: The states that deliberately produce nothing. Exactly one: `current` is the
#: common case and a finding per healthy clone is noise. Anything else absent
#: from `SYNC_FINDINGS` raises `clone.unknown-state`.
SYNC_SILENT = frozenset({"current"})

#: The states that mean "there are commits here and on no remote". Spelled out
#: rather than derived from the `warning` rows above, which it currently equals
#: exactly: severity answers "how loudly should this be said", and one day a
#: state will earn a warning for some other reason. Tying the two would move
#: this set silently when that happens. Checked against `scan_remotes.STATES` by
#: `tests/test_unpublished_work.py`, so a typo here cannot pass as a state that
#: never matches.
AT_RISK = frozenset({"local-only-branch", "ahead", "unpushed-and-remote-moved",
                     "diverged"})


def clipped(text: str, limit: int = 300) -> str:
    """`text`, and a MARKER when it did not fit.

    Several details in this file joined their measurements, sliced the result
    at a fixed length, and then concatenated an explanatory sentence after the
    cut — so the seam was invisible and the finding READ as complete:

        "…could not resolve host www.ex. These are NOT reported as dark: a
         question that could not be asked has no negative answer."

    A reader cannot tell that from a finished thought about a complete list. The
    worst instance was `model.degraded`, where the `ownership` reason ENDS with
    the remedy and `sorted()` placed it second: one of each degradation dropped
    the only actionable sentence in the finding.
    """
    if len(text) <= limit:
        return text
    return (f"{text[:limit].rstrip()}… "
            f"[{len(text) - limit} more character(s) not shown]")


def listed(items, limit: int, *, sep: str = ", ") -> str:
    """The first `limit` items, saying how many were left out.

    The pattern `wiki.broken_link` already used — join the first few targets
    and append "and N more" — made the rule, because it was the one site that
    did it. The other truncating sites were silent about what they dropped.
    """
    rows = list(items)
    if len(rows) <= limit:
        return sep.join(rows)
    return sep.join(rows[:limit]) + f"{sep}and {len(rows) - limit} more"


def metric_for_role(role: str) -> str | None:
    """Which installed metric claims this role, or None if none does.

    None is a real answer: a machine with no footprint plugin cannot be told
    which project filled its disk, and saying nothing is correct there.
    """
    for m in sorted((ROOT / "plugins").glob("*.json")):
        try:
            doc = json.loads(m.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for metric in doc.get("metrics") or []:
            if metric.get("role") == role:
                return metric.get("name")
    return None


def _footprint_metric() -> dict:
    for m in sorted((ROOT / "plugins").glob("*.json")):
        try:
            doc = json.loads(m.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for metric in doc.get("metrics") or []:
            if metric.get("role") == FOOTPRINT_ROLE:
                return {"plugin": doc.get("id"), **metric}
    return {}


def footprint_source() -> str | None:
    return _footprint_metric().get("plugin")


def footprint_means() -> str | None:
    """The manifest's own `means`, which is written to be read by a person."""
    return _footprint_metric().get("means")


def reclaimable_holders() -> list[str]:
    """The projects holding the most reinstallable bytes, coarsely."""
    metric = metric_for_role(RECLAIMABLE_ROLE)
    if not metric or not paths.DB.is_file():
        return []
    try:
        c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        return [f"{r[0].split(':', 1)[-1]} ~{r[1] / 1e9:.1f} GB" for r in c.execute(
            "SELECT project_id, value FROM metrics WHERE metric = ?"
            "   AND at = (SELECT MAX(at) FROM metrics WHERE metric = ?)"
            " ORDER BY value DESC LIMIT 5", (metric, metric)) if r[1] >= 1e9]
    except sqlite3.Error:
        return []
    finally:
        c.close()


def disk_culprits() -> tuple[list[str], list[str]]:
    """(largest consumers, fastest growers) — from the plugin layer's own rows.

    `plugins/disk-usage.json` states its reason: the data volume was nearly
    full, and nothing in the observatory could say which projects were
    responsible or whether one was growing. Both halves were once unanswered,
    for two different reasons — the measurement existed and no finding read it,
    and the series had one point so growth was not computable at all.

    Values are coarse on purpose: `registry/findings.json` is committed on every
    tick, so a byte-exact figure here is a commit on every tick for a number
    that moves continuously.
    """
    metric = metric_for_role(FOOTPRINT_ROLE)
    if not metric or not paths.DB.is_file():
        return [], []
    try:
        c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return [], []
    try:
        big = [f"{r[0].split(':', 1)[-1]} ~{r[1] / 1e9:.0f} GB" for r in c.execute(
            "SELECT project_id, value FROM metrics WHERE metric = ?"
            "   AND at = (SELECT MAX(at) FROM metrics WHERE metric = ?)"
            " ORDER BY value DESC LIMIT 5", (metric, metric)) if r[1] >= 5e8]
        # TWO SAMPLES, and the second-newest rather than "yesterday": a missed
        # period leaves a hole, and comparing against whatever the previous
        # sample actually is describes the interval that exists rather than the
        # one that was scheduled.
        rows = c.execute(
            "SELECT a.project_id, a.value - b.value AS delta FROM metrics a"
            " JOIN metrics b ON b.project_id = a.project_id AND b.metric = a.metric"
            " WHERE a.metric = ?"
            "   AND a.at = (SELECT MAX(at) FROM metrics WHERE metric = ?)"
            "   AND b.at = (SELECT MAX(at) FROM metrics WHERE metric = ?"
            "               AND at < a.at)"
            " ORDER BY delta DESC LIMIT 5", (metric, metric, metric)).fetchall()
        grow = [f"{r[0].split(':', 1)[-1]} +{r[1] / (1024 * 1024):.0f} MiB"
                for r in rows if r[1] >= GROWTH_MIB_PER_DAY * 1024 * 1024]
        return big, grow
    except sqlite3.Error:
        return [], []
    finally:
        c.close()


#: Where the harness keeps the COPY it actually runs. A module constant rather
#: than an inlined `Path.home()`, so a test can point it somewhere and drive the
#: divergence — the rule `tools/check_paths.py` enforces for every other input.
def shorten(p: pathlib.Path) -> str:
    """`~/…` when it is under home, the whole path otherwise. Never raises."""
    try:
        return "~/" + str(p.relative_to(pathlib.Path.home()))
    except ValueError:
        return str(p)


COMPANION_INSTALL = pathlib.Path(
    os.environ.get("OBSERVATORY_COMPANION_INSTALL")
    or pathlib.Path.home() / ".claude/plugins/cache/observatory-log/observatory-log")


def hours_since(stamp: str) -> float | None:
    """Hours since an ISO stamp, or None when it cannot be read.

    None is a THIRD outcome and both callers escalate on it: "we do not know how
    old this is" is not "it is fresh". A horizon check that silently passes on an
    unparseable timestamp is a check that stops checking the day the format
    changes.
    """
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600


#: How many quarantined models a finding lists before it says how many it did
#: not. A chain is three or four models long, so this is generous by design and
#: exists so the cap is a constant rather than a slice nobody can find.
QUARANTINE_LISTED = 6

#: A run older than this makes an empty quarantine list say nothing about health.
#: The tick is every thirty minutes, so six hours is twelve missed cycles — well
#: past "the agent is between runs" and into "nobody has asked a model lately".
HEALTH_STALE_HOURS = 6.0


#: Faults inside the window before the pattern is a warning rather than a note.
#: THREE, not two: `tick.step_failed` already reports the event, so a single
#: fault would be two findings for one incident, and two in a week is still
#: plausibly one bad afternoon. Three is where "the store fails sometimes"
#: becomes the claim, and that claim has a different remedy from any one crash.
STORE_FAULT_PATTERN = 3

#: The window the pattern is counted over. Matches `store_faults.recent()`'s own
#: default so the finding and the CLI answer the same question.
STORE_FAULT_DAYS = 7.0

#: The window for LOST TURNS, and it matches `companion_faults.read()`'s default
#: for the same reason. No `PATTERN` threshold beside it: one store fault in a
#: week is plausibly one bad afternoon, whereas one turn the companion could not
#: record is a hole in the ledger that nothing else will ever fill.
COMPANION_FAULT_DAYS = 7.0

#: How many sessions, projects and reasons a lost-turn finding names before it
#: says how many it did not. A systemic fault is every session at once, and a
#: finding that prints forty-nine ids is a finding nobody reads.
COMPANION_LISTED = 6

#: The owner every interpretation is written under.
AGENT_OWNER = "agent:observer"


#: How many uncovered skip sites the finding lists before it says how many it did
#: not. Forty-eight is a wall of text on a page that also carries sixty findings.
SKIP_SITES_LISTED = 5


#: What the provider's `limit_reset` words mean as a date. Only the one this
#: account actually sends is interpreted; every other word yields None, so a
#: finding says "not derivable" instead of printing a date nobody measured.
#: OpenRouter sends `monthly` for a key limit, and a monthly key limit resets at
#: the start of the next calendar month in UTC.
RESET_WORDS = ("monthly",)


#: How many holders the lever names before it says how many it did not. The
#: helper already returns at most five ≥1 GB; this bounds a longer list from any
#: other caller, and the row states the remainder rather than trimming silently.
LEVER_HOLDERS_LISTED = 5


#: How many stale checkouts the aggregate names before it says how many more.
#: Six is what fits one line beside sixty other findings; the remainder is
#: stated rather than trimmed away.
STALE_LISTED = 6


def declared_alive_measured_dead(projects: list[dict]) -> list[dict]:
    """`lifecycle` says active and `activity_tier` says nothing has moved.

    TWO FACTS, NOT ONE, and the scenarios forbid collapsing them: lifecycle is
    what the owner DECLARED, activity_tier is what was MEASURED. The screen
    shows both. What nothing did was count the gap — and the count is what makes
    it a decision rather than an ambient condition.

    An audit can find nearly every project declaring `active` while a large
    share of them measure `dormant` or `cold`. A number nothing reports grows
    in silence.

    ONE AGGREGATE ROW, not one per project. Each project's remedy is the same
    sentence — archive it or touch it — and a row per project carrying one
    sentence is the `clone.stale` defect. The count is the information; which
    ones is the tab's job.
    """
    DEAD = {"dormant", "cold"}
    stale = sorted(p["name"] for p in projects
                   if p.get("lifecycle") == "active" and p.get("activity_tier") in DEAD)
    if not stale:
        return []
    return [{
        "type": "project.declared_alive_measured_dead",
        "subject": "estate:lifecycle-drift",
        "severity": "info",
        "title": (f"{len(stale)} projects declare they are active and have not moved"
                  if len(stale) > 1 else
                  "1 project declares it is active and has not moved"),
        "detail": (f"`lifecycle` is a declaration and `activity_tier` is a "
                   f"measurement, and for these they disagree: "
                   f"{listed(stale, 6)}. an earlier review found fourteen of these; "
                   f"nothing has reported the number since, and a declaration "
                   f"nobody maintains stops being read at all."),
        "action": ("open docs/dashboard/projects.html?f=drift — the table comes up "
                   "narrowed to these rows; archive what is finished in "
                   f"{paths.config_label('project_overrides.json')}, or accept that "
                   "lifecycle means nothing here and say so"),
    }]


def mcp_registration_command() -> str:
    """The line that declares this engine's MCP server to Claude Code, runnable as written.

    This interpreter, this engine's `mcp/server.py` and this workspace, quoted
    for a shell: the server resolves its workspace from `OBSERVATORY_HOME`, and
    an installed engine has no checkout or `.venv` beside it to point at.
    """
    import shlex
    return " ".join(["claude mcp add observatory --scope user",
                     "-e", shlex.quote(f"OBSERVATORY_HOME={paths.HOME}"), "--",
                     shlex.quote(sys.executable), shlex.quote(str(ROOT / "mcp/server.py"))])


def unobservable_projects(projects: list[dict], related: set) -> list[dict]:
    """A project with no folder and no repository — nothing to watch.

    Found while checking whether the founding question is answered
    trustworthily. It is: `last_activity_on` and the store's newest commit agree
    for every project. But a project can carry `activity_tier: unknown` because
    it has no folder on this machine, no repository, and no activity date — the
    only witness that it exists is a note in the wiki.

    So the tier is HONEST and the `lifecycle: active` beside it is a claim with
    no evidence of any kind. And nothing said so: such a project reached no
    board, and `tools/record_lost_projects.py` does not catch it either — it
    tracks projects whose FOLDER disappeared and whose history it remembers,
    which these never had.

    That is this system's own blind spot: it watches projects, and a project it
    cannot watch produces no observation, so the ABSENCE of one went unobserved.

    THE TRIGGER IS THE EVIDENCE, not the tier. `unknown` is a consequence, and
    testing a consequence would fire on whatever else is unknown while missing an
    unobservable project whose tier came out otherwise.

    INFO. Nothing is broken and no work is at risk; what exists is a declaration
    the registry cannot support.
    """
    out: list[dict] = []
    for p in projects:
        if p.get("local_folders") or p["id"] in related:
            continue
        life = p.get("lifecycle") or "unset"
        note = p.get("has_vault_note")
        out.append({
            "type": "project.unobservable", "subject": p["id"],
            "severity": "info",
            "title": (f"{p.get('name') or p['id']} has no folder and no repository, "
                      f"so nothing about it can be measured"),
            "detail": ("Every answer this system gives about a project comes from a "
                       "checkout or a remote, and this project has neither: no local "
                       "folder, no related repository, no activity date. "
                       + ("Its only witness is a note in the projects wiki. "
                          if note else
                          "Not even a wiki note names it, which makes the registry "
                          "entry its sole witness. ")
                       + f"Its lifecycle says `{life}`, and nothing here can support "
                         f"or contradict that — which is why its activity tier is "
                         f"`unknown` rather than a guess."),
            "action": ("point it at a folder or a repository if the work exists "
                       "somewhere, or set `lifecycle` to what the evidence supports "
                       "— `planned` for something not started, `archived` for "
                       "something finished"),
            "evidence": [f"registry:projects.json#{p['id']}",
                         "registry:relations.json"]})
    return out


def stale_clones(names: list) -> list[dict]:
    """One row for every checkout behind its remote, instead of one row each.

    A row per stale clone, all `info`, can fill a large share of the board —
    and each one's own detail says "nothing here is at risk" while its action
    says "pull when you next work here". That is ambient state, not a decision,
    and the same state is already a `sync=stale` chip on each project's own row
    of the dashboard.

    The aggregate stays because the COUNT is information: a dozen behind is
    ordinary, a hundred would mean this machine had not fetched in weeks. What
    goes is a dozen slots of a page with limited room.

    Only `stale` collapses. `ahead`, `local-only-branch`, `diverged` and
    `unpushed-and-remote-moved` each ask a decision about ONE checkout — "push
    the branch, or delete it deliberately" — and an operator acts on them one at
    a time. The discriminator is not the family name but that stale's own text
    says nothing is at risk.

    No aggregate-of-one special case: an aggregate of one is still the
    aggregate, and a second shape would be a second thing for a reader to learn.
    """
    if not names:
        return []
    # TWO ROWS, SPLIT BY OWNERSHIP, because the two mean different things. For a
    # copy of somebody else's repository, falling behind upstream is the RESTING
    # STATE rather than news; for this estate's own checkout it is a fetch nobody
    # has run. Folding both into one row loses that distinction, and
    # `test-stale-expected` guards against it.
    # Accepts a bare name or a (name, ownership) pair, so a caller that has no
    # ownership to give — a fixture, or a future reader — still gets the row
    # rather than a TypeError.
    groups: dict[bool, list[str]] = {True: [], False: []}
    for item in names:
        name, own = item if isinstance(item, tuple) else (item, None)
        groups[own == "external"].append(name)
    out: list[dict] = []
    for is_external in (False, True):
        got = groups[is_external]
        if not got:
            continue
        shown = sorted(got)[:STALE_LISTED]
        rest = len(got) - len(shown)
        out.append({
            "type": "clone.stale",
            "subject": "estate:copies" if is_external else "estate:clones",
            "severity": "info",
            "title": ((f"{len(got)} copies of somebody else's work are behind "
                       f"their upstreams" if len(got) > 1 else
                       "1 copy of somebody else's work is behind its upstream")
                      if is_external else
                      (f"{len(got)} of this estate's checkouts are behind their "
                       f"remotes" if len(got) > 1 else
                       "1 of this estate's checkouts is behind its remote")),
            "detail": ("The remote has moved and each of these is contained in "
                       "its branch as of its last fetch, so nothing here is at "
                       "risk — which is why this is one row rather than "
                       + str(len(got)) + ". "
                       + ("Each is a copy of somebody else's repository, and "
                          "falling behind upstream is expected of a copy rather "
                          "than news. " if is_external else "")
                       + listed(shown, STALE_LISTED)
                       + (f", and {rest} more" if rest > 0 else "")
                       + ". Each carries a `stale` chip on its project's row of "
                         "the dashboard, so which ones they are stays on the "
                         "page."),
            "action": ("nothing to decide — a copy falls behind by design; pull "
                       "if you need the upstream's newer work"
                       if is_external else
                       "nothing to decide — pull when you next work in one; the "
                       "dashboard's per-project chip says which"),
            "evidence": ["registry:repositories.json#local.sync"]})
    return out


def reclaimable_pressure(free_bytes: float | None, reclaimable_bytes: float | None,
                         holders: list[str], disk_low: bool) -> list[dict]:
    """A lever the estate measures and nobody was offered.

    Package directories — `node_modules`, `.venv`, `build`, `target` and their
    kin — can add up to more than the free space left on the volume. While the
    only reader of that metric is `host.disk_low`, gated behind
    `DISK_WARNING_MIB`, the gate stays shut and the lever goes unmentioned.

    **NO THRESHOLD, and the absent constant is the decision.** Moving
    `DISK_WARNING_MIB` on one correlation was declined, and that refusal stands;
    this rule needs no level, only the COMPARISON — it fires when free is less
    than reclaimable. Two measured quantities, self-calibrating: a machine with
    ample free space and modest package directories never sees it, and where it
    is true a routine deletion more than doubles the headroom, which is only
    worth saying while the headroom is the smaller number.

    Whole GiB in the text, because `findings.json` is committed on every tick and
    free space moves continuously; `host.disk_low` carries the same rounding for
    the same reason, having been caught by `test_tick_repo`'s check for byte
    equality.

    None is not zero: an unmeasured input is silent, and a reclaimable total of
    zero means the plugin found no reinstallable directory at all — a fact, not a
    gap.
    """
    if free_bytes is None or reclaimable_bytes is None:
        return []
    free_gib, rec_gib = float(free_bytes) / 2**30, float(reclaimable_bytes) / 2**30
    if rec_gib <= 0 or rec_gib <= free_gib:
        return []
    shown = list(holders)[:LEVER_HOLDERS_LISTED]
    rest = len(holders) - len(shown)
    return [{
        "type": "host.reclaimable_lever", "subject": "host:volume",
        "severity": "warning",
        "title": (f"{rec_gib:.0f} GiB can be freed by reinstalling, against "
                  f"{free_gib:.0f} GiB free on the volume"),
        "detail": ("The plugin layer measures what sits inside the directories a "
                   "project's footprint prunes — `node_modules`, `.venv`, `build`, "
                   "`target`, `Pods` and their kin. Deleting them costs a reinstall "
                   "and nothing else, and there is "
                   + (f"{rec_gib / max(free_gib, 0.01):.1f}x" if free_gib > 0
                      else "more of it than there is free space")
                   + " more of it than the volume has left. Largest holders: "
                   + listed(shown, LEVER_HOLDERS_LISTED)
                   + (f", and {rest} more not listed" if rest > 0 else "")
                   + ("."
                      if not disk_low else
                      ". The volume is also low enough for `host.disk_low`, which "
                      "carries what that endangers — this row is only the lever.")),
        "action": "delete the package directories of a project you are not working "
                  "on; every byte comes back with a reinstall. "
                  "`./observatory.py project <name>` shows one project's footprint",
        "evidence": ["store:metrics#" + RECLAIMABLE_ROLE,
                     "shutil.disk_usage(.).free"]}]


def reset_date(word: str | None, at: datetime | None = None) -> str | None:
    """When a spend limit lifts by itself, as a date — or None.

    ONE derivation, two readers. `wallet.shared_key` is the cause and
    `interpretation.halted` is the symptom; both need the date, and two copies of
    this arithmetic would eventually disagree in front of the operator.

    None is the third outcome and it is deliberate: `limit_reset` is a WORD from
    the provider, and a word this function does not interpret must not become a
    guessed date. The findings then say the reset is not derivable, which is the
    truth and is more useful than a confident wrong day.
    """
    if not word or str(word).strip().lower() not in RESET_WORDS:
        return None
    now = at or datetime.now(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return f"{year:04d}-{month:02d}-01"


def measured_at(doc: dict) -> datetime | None:
    """When a key-usage document was measured (`checked_at`), or None.

    The reset date belongs to the MEASUREMENT, not to the clock reading it: a key
    found spent on 2026-09-07 lifts on 2026-10-01, and saying "2026-11-01" when
    the same document is read on October 1st both moves a date the provider
    already passed and tells the operator to wait a month for nothing. Measured
    2026-10-01, when `tests/test_temporary_block.py` went red on every CI row at
    midnight UTC with no change to the code. None (absent or unreadable) leaves
    `reset_date` on the wall clock, the only time left to reason from.
    """
    raw = doc.get("checked_at") if isinstance(doc, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def gate_skip_findings(sites: list[dict]) -> list[dict]:
    """Places a suite can drop assertions without saying who carries them.

    `observatory.py` counts the blocks a RUN skipped and prints them beside the
    verdict. This is the larger and more stable number: the places a run COULD
    skip, which does not depend on which machine ran it or whether the tick
    happened to hold a lease.

    INFO, and deliberately. Each message is a judgement about whether a
    property is asserted in another suite, and neither this file nor
    `tools/skip_sites.py` can make one. A warning would demand a mass rewrite of
    many files to clear it, and the pressure this row exists to apply is the
    kind that gets paid down one suite at a time.
    """
    # SKIPS ONLY. A `NOTE` printed beside assertions that run is an annotation,
    # not a place assertions vanish. Counting both inflates the figure, and an
    # inflated figure on the board is the defect this file exists to prevent,
    # committed about itself.
    skips = [s for s in sites if s.get("kind_of_site", "skip") == "skip"]
    notes = len(sites) - len(skips)
    # ONLY `none`. A skip waiting on a machine capability — no store, node,
    # `sqlite-vec` — is completely explained, and no judgement a person writes
    # moves it: fourteen of the thirty-two were of that kind.
    uncovered = [s for s in skips if s.get("cover_kind", "none") == "none"]
    # JUDGED AND STILL OPEN, reported separately rather than folded into either
    # side. A `[gap: …]` site has been examined, its property IS assertable, and
    # what would close it is written down — so counting it as unexamined would
    # discard the judgement, and counting it as covered would be a lie. A class
    # that leaves the headline must appear somewhere or it leaves silently, which
    # is the rule this file applies to every other count.
    gaps = [s for s in skips if s.get("cover_kind") == "gap"]
    if not uncovered and not gaps:
        return []
    shown = uncovered[:SKIP_SITES_LISTED]
    rest = len(uncovered) - len(shown)
    gap_text = ""
    if gaps:
        gap_text = (f"{len(gaps)} site(s) "
                    f"{'has' if len(gaps) == 1 else 'have'} been judged: the "
                    f"property is assertable and nothing asserts it yet, each "
                    f"carrying what would close it — "
                    + listed([f"{g.get('file', '?').split('/')[-1]}"
                              f":{g.get('line', '?')}" for g in gaps], 5)
                    + ". Those are a work queue, not an unread note. ")
    return [{
        "type": "gate.skips_uncovered", "subject": "gate:assertions",
        "severity": "info",
        # THREE HEADLINES, because the third state arrived: nothing unexamined
        # and a judged gap still open. "0 of 70 do not say" as a title is a
        # sentence about an empty set, and a reader who sees a zero at the front
        # stops reading — so when the unexamined count is nil the gap leads.
        "title": ((f"{len(gaps)} of {len(skips)} skip site(s) "
                   f"{'is a judged gap' if len(gaps) == 1 else 'are judged gaps'}: "
                   f"the property is assertable and nothing asserts it yet")
                  if not uncovered else
                  (f"{len(uncovered)} of {len(skips)} skip site(s) do not say which "
                   f"suite carries the property they skip"
                   + (f", and {len(gaps)} more "
                      f"{'is a judged gap' if len(gaps) == 1 else 'are judged gaps'} "
                      f"awaiting a fixture" if gaps else ""))),
        "detail": ("A suite that cannot run a block prints its reason and exits 0, so "
                   "the gate counts the step as passed — `observatory.py` now prints "
                   "the blocks each run skipped, which is what makes a PASS total "
                   "quotable. What a run cannot show is whether the skipped property "
                   "is asserted somewhere else. "
                   + (f"{notes} further marker(s) annotate a run whose assertions "
                      f"do run, and are not counted here. " if notes else "")
                   + gap_text
                   # RENDERED ONLY WHEN THERE IS A LIST. This sentence used to
                   # open with "One site does say: tests/test_footprint.py
                   # points at tests/test_delivery.py" — true when exactly one
                   # named a cover, false once 43 did — and then printed "The
                   # rest: ." with nothing between the colon and the stop, on
                   # the day the unexamined count reached zero. A hardcoded
                   # count in prose beside a computed one is the drift this
                   # whole board exists to remove.
                   + (("The unexamined: "
                       # `.get`, not `[...]`. A finding that raises on a missing
                       # field is the thing that breaks the findings build, and
                       # this one did exactly that against a fixture without
                       # `line`.
                       + listed([f"{s.get('file', '?').split('/')[-1]}"
                                 f":{s.get('line', '?')}" for s in shown], 5)
                       + (f", and {rest} more" if rest > 0 else "")
                       + ". Each is a judgement about coverage, not a defect to be "
                         "scripted away — which is why this is a note and not a "
                         "warning. ") if shown else "")
                   + "This counts sites that COULD skip, not ones that did. The "
                     "number that prioritises is the gate's own tally of blocks each "
                     "run actually skipped, which this file cannot know because "
                     "`check` writes nothing — and the one skip that fired on "
                     "2026-09-08 fired because its fixture looked for a registry file "
                     "that has never existed while the validator read another, so the "
                     "rule it guarded had never been driven."),
        "action": "`.venv/bin/python tools/skip_sites.py` prints every site with its "
                  "message; the convention is a `[covered: where]` marker in the skip "
                  "line — a bracketed token rather than prose, because "
                  "'fixture' alone matches 'no fixture here'. `[uncoverable: why]` "
                  "when nothing can carry it, and `[gap: what would close it]` when "
                  "something could and nothing does — the third one keeps the "
                  "judgement without claiming the cover. Start with the sites the "
                  "gate reports as actually skipped, not with this list",
        "evidence": ["tools/skip_sites.py"]}]


def store_fault_findings(rows: list[dict]) -> list[dict]:
    """The store's fault HISTORY, as opposed to one tick's failure.

    Two findings already touch this ground and neither says what this says.
    `tick.step_failed` names the step that stopped in the run that just ran;
    `interpretation.faults` names the projects one agent run skipped. Both are
    events inside a single cycle, and both are gone from their receipts the next
    time the tick writes them. This reads the append-only log and reports the
    PATTERN — how often, with which sqlite codes, and under what conditions.

    Conditions are the point. A store failure diagnosed without free space, WAL
    size and the holder count at the moment of failure is diagnosed by
    guessing; the log exists so the next diagnosis is a measurement. So this
    finding quotes the conditions rather than summarising them away.
    """
    out: list[dict] = []
    if not rows:
        return out
    codes = sorted({str(r.get("sqlite_errorname") or "no sqlite code") for r in rows})
    ops = sorted({str(r.get("op") or "?") for r in rows})
    frees = [r["free_bytes"] for r in rows if isinstance(r.get("free_bytes"), int)]
    holders = [r["holders"] for r in rows if isinstance(r.get("holders"), int)]
    unasked = sum(1 for r in rows if r.get("holders") is None)
    newest = max((str(r.get("at") or "") for r in rows), default="")
    pattern = len(rows) >= STORE_FAULT_PATTERN

    if frees:
        space = (f"free space at the moment of failure ranged "
                 f"{min(frees) / 2**30:.2f}–{max(frees) / 2**30:.2f} GiB")
    else:
        space = "free space was not measurable at any of them"
    if holders:
        held = (f"; {min(holders)}–{max(holders)} process(es) held the file"
                + (f", and it could not be asked for {unasked} of them" if unasked else ""))
    elif unasked:
        held = f"; the holder count could not be asked for any of the {unasked}"
    else:
        held = ""

    out.append({
        "type": "store.faults_recurring", "subject": "store:observatory.db",
        "severity": "warning" if pattern else "info",
        "title": (f"the store failed {len(rows)} time(s) in the last "
                  f"{STORE_FAULT_DAYS:.0f} days: {listed(codes, 4)}"),
        "detail": ((f"{len(rows)} recorded fault(s) across {listed(ops, 4)}, newest "
                    f"{newest}. {space}{held}. ")
                   + ("`integrity_check` has answered `ok` after every one of them, "
                      "which is why the conditions are recorded at the moment of "
                      "failure rather than looked up afterwards — by then they are "
                      "gone. " if pattern else "")
                   + ("Three or more in a week is a pattern with its own remedy, not "
                      "a run that crashed: the failed step already has its own "
                      "finding."
                      if pattern else
                      "One fault is a note, not a pattern — the step that stopped is "
                      "reported separately, and this row exists so the next one can "
                      "be compared against it.")),
        "action": "`.venv/bin/python store_faults.py --days 7` prints each fault with "
                  "the machine's state; the four incidents that preceded the "
                  "instrument are in `store/logs/tick.log` only",
        "evidence": ["store/raw/store-faults.jsonl"]})
    return out


#: WHAT TO DO, PER CLASS OF DEGRADATION, keyed by the prefix of the merge's own
#: `source`. A single hardcoded action for every reason — "check that `gh` is
#: authenticated for the PATH the tick runs with, then merge" — is the cure for
#: the `transfer:` class and the wrong one for others: when `gh` answered fine
#: and the estate gained an owner nobody declared, a reader following it would
#: check the credential, find it healthy, re-run the merge and get the same row
#: back.
#:
#: `tests/test_remedy_fits.py` reads the classes out of `collectors/merge.py`
#: and requires an entry for each, so a new degradation site cannot inherit
#: another class's cure by default.
MERGE_REMEDY = {
    "transfer": "check that `gh` is authenticated for the PATH the tick runs "
                "with, then `./observatory.py merge`",
    "ownership": "decide whether the organisation is yours — add it to "
                 "`organizations` in the workspace's `config/ownership.json` "
                 "(read as `OWNED_ORGS` by `collectors/merge.py`) and re-run "
                 "`./observatory.py merge emit`, or leave it and its projects "
                 "stay `external`",
    # The merge's own coverage sources. Each is emitted when an input the
    # merge reads was never produced, so the cure is to produce that input —
    # without an entry here they fell to the unknown-class text below, which
    # tells the reader there is nothing to run when there is.
    "github": "run the repository listing (`project-observatory full scan-gh`, "
              "which needs an authenticated `gh`), then `project-observatory full "
              "local`; until then remote repositories are not counted",
    "wiki": "point `sources.wiki` in the workspace settings at the notes folder, "
            "run `project-observatory full scan-vault`, then `project-observatory "
            "full local` — or leave it unset and project notes stay unmeasured",
    "identity": "pin a stable name for each colliding project in the workspace's "
                "`config/identity_overrides.json`, then re-run "
                "`project-observatory full local`; the suffixed keys are only a "
                "placeholder",
}

#: What a source nobody has written a remedy for gets. NOT one of those above:
#: a wrong prescription is followed, and the only evidence it was wrong is that
#: nothing changed.
MERGE_REMEDY_UNKNOWN = ("read the reason above — this degradation's class has no "
                        "recorded remedy in tools/build_findings.py, so nothing "
                        "here can tell you what to run")


def merge_findings(deg: list[dict]) -> list[dict]:
    """The merge's unmeasured sources, with the cure that fits them.

    The merge is the collector every other module reads, and without a
    degradation channel a missing `gh` made the transfer check answer "not
    moved" for every address, which put phantom repositories and a phantom
    project into the model in silence.
    """
    if not deg:
        return []
    # SOURCE AND REASON, deduplicated on the PAIR. This joined
    # `sorted({d["reason"] …})` — a set of reasons — so `d["source"]` was
    # discarded entirely and a finding titled "3 source(s) unmeasured" named
    # none of the three, while two addresses failing the same way collapsed
    # into one line and one of them vanished. The 400-char cut then landed
    # on whichever reason sorted last, and the `ownership` reason ENDS with
    # the remedy.
    why = clipped("; ".join(sorted({f"{d.get('source', '?')} — {d['reason']}"
                                    for d in deg})), 1200)
    seen: list[str] = []
    for d in deg:
        cure = MERGE_REMEDY.get(str(d.get("source") or "").split(":", 1)[0],
                                MERGE_REMEDY_UNKNOWN)
        if cure not in seen:
            seen.append(cure)
    return [{
        "type": "model.degraded", "subject": "collector:merge",
        "severity": "warning",
        "title": f"the model was built with {len(deg)} source(s) unmeasured",
        "detail": why + ". The registry was still written: 170-odd "
                  "repositories are unaffected and refusing the whole model "
                  "would age every fact in the estate to save these.",
        "action": "; ".join(seen),
        "evidence": ["store/raw/model.json#degraded"]}]


def _drain_cost(adoc: dict) -> str:
    """What clearing the queue would cost, or silence when nothing priced it.

    A reader given "N changes waiting" cannot turn that into money, and the two
    numbers that would — how many PROJECTS wait, and what one interpretation
    costs — are both recorded. The agent reports the first; the ledger's own
    provenance holds the second.
    """
    projects = adoc.get("projects_unconsumed")
    priced = interpretation_cost()
    if not projects or priced is None:
        return ""
    median, worst, n = priced
    return (f" Draining it is one call per PROJECT, not per change: {projects} "
            f"project(s) wait, and the last {n} interpretations cost a median of "
            f"{median:.5f} credits each ({worst:.5f} at worst), so the whole queue "
            f"is about {projects * median:.3f} credits — {projects * worst:.3f} if "
            f"every one costs the most any recent one did. A project's prompt does "
            f"not grow with its backlog, so waiting longer does not make the drain "
            f"dearer.")


def _erasure_horizon(row: dict) -> str:
    """How long retention leaves this row, when the receipt says enough to ask.

    The row's own age decides whether "a person must decide on it" means this
    week or next quarter, and `store/retention.days_left` is the one home for
    that rule — a second computation of `horizon - age` elsewhere would miss
    the owner exemptions. Silent when the receipt predates the fields: absent
    is not "never erased".
    """
    at, owner, state = row.get("created_at"), row.get("owner"), row.get("state")
    if not (at and owner and state):
        return ""
    try:
        sys.path.insert(0, str(ROOT / "store"))
        import retention
        left = retention.days_left(owner, state, at)
    except Exception:                                                               
        return ""
    if left is None:
        return (f" Retention never erases a row owned by {owner}, so this one waits "
                f"until somebody decides.")
    return (f" Written {at[:10]}, so retention erases it in {left} day(s) unless "
            f"somebody decides first.")


#: Cache for the day the estate began recording dark hosts — the MINIMUM first
#: sighting in the file. A host standing on that day may have been dark for
#: years; one first seen later was measured going dark. Derived rather than
#: stored, so it needs no constant to keep true.
_DARK_EPOCH: list[str | None] = []


def _dark_for(hosts: list[dict], host: str) -> str:
    """How long a host has been dark, in the terms the estate can defend.

    The field is `dark_first_seen`, not `dark_since`, because the system knows
    when it first SAW the host dark and not when the host went dark. On the day
    the field began, every dark host stood on it — so a row would have read
    "0 days" about a name dark for months. The qualifier fires exactly for the
    hosts whose date equals the earliest in the file, and stops firing for
    anything that goes dark afterwards, which is when the figure becomes a real
    measurement.
    """
    row = next((h for h in hosts if h.get("host") == host), None)
    seen = (row or {}).get("dark_first_seen")
    if not seen:
        return ""
    if not _DARK_EPOCH:
        firsts = [h["dark_first_seen"] for h in hosts if h.get("dark_first_seen")]
        _DARK_EPOCH.append(min(firsts) if firsts else None)
    epoch = _DARK_EPOCH[0]
    try:
        when = datetime.strptime(seen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    days = (datetime.now(timezone.utc) - when).days
    if seen == epoch:
        return (f" First recorded dark on {seen}, which is the day this estate "
                f"began recording it — the host may have been dark for far longer.")
    return f" First seen dark on {seen}, {days} day(s) ago."


def _hold_pressure(expires_on: str | None) -> str:
    """How long the registration itself has left, when the record says.

    Silent when it does not: a hold whose expiry nobody measured is not a hold
    that expires never.
    """
    if not expires_on:
        return ""
    try:
        when = datetime.strptime(str(expires_on)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    days = (when - datetime.now(timezone.utc)).days
    if days < 0:
        return (f" The registration itself expired on {expires_on}: the hold is no "
                f"longer the only thing standing between this name and losing it.")
    return (f" The registration runs to {expires_on}, {days} day(s) away — so the "
            f"hold is what darkens it, not the clock.")


def interpretation_cost(sample: int = 60) -> tuple[float, float, int] | None:
    """(median, worst, n) credits per interpretation, from the ledger's own rows.

    Every interpretation records what it cost in its provenance, so this is the
    system's record of its own calls rather than an estimate. Returns None when
    nothing has been interpreted — absent is not zero, and a queue nobody has
    priced is not a queue that is free.

    **Why the estimate is per PROJECT.** The agent calls once per project, and a
    project's prompt folds to one line per delta KIND — so a backlog of three
    weeks costs the same call as a backlog of one tick. That is what makes the
    reset-day arithmetic bounded.
    """
    import statistics
    try:
        conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            "SELECT provenance_json FROM ledger WHERE owner = ?"
            " ORDER BY created_at DESC LIMIT ?", (AGENT_OWNER, sample)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    costs: list[float] = []
    for (pj,) in rows:
        try:
            for prov in json.loads(pj or "[]"):
                if isinstance(prov, dict) and prov.get("cost_credits") is not None:
                    costs.append(float(prov["cost_credits"]))
        except (ValueError, TypeError):
            continue
    if not costs:
        return None
    return statistics.median(costs), max(costs), len(costs)


def _within(stamp: str, days: float) -> bool:
    """Is a stamp inside the window? An unreadable one counts as inside.

    The alternative drops a fault for the shape of its timestamp, which is the
    understating this file keeps having to remove.
    """
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return when.timestamp() >= datetime.now(timezone.utc).timestamp() - days * 86400


def companion_findings(rows: list[dict], slot: dict,
                       unread: int = 0,
                       days: float = COMPANION_FAULT_DAYS) -> list[dict]:
    """The turns the companion could not record — counted, not glimpsed.

    **What this replaces.** The rule once read `store/raw/record-turn.json`,
    which is a SLOT: one document, describing the last turn handled anywhere on
    this machine. Every session of every watched project writes it, the board is
    rebuilt on every tick, and the finding therefore fired only when a fault
    happened to be the newest turn on the whole machine at tick time. A
    permanent fault survives that; a transient one — the class that actually
    occurs — is erased by the next quiet turn of any project.

    The slot keeps its job — it says what the LAST turn did — and
    `companion_faults.py` keeps the history. Both are read here, because a fault
    the append-only log could NOT keep is the third outcome and it must not
    arrive as a smaller number.
    """
    out: list[dict] = []
    sessions = [str(r.get("session") or "?")[:8] for r in rows]
    seen: list[str] = []
    for s in sessions:
        if s not in seen:
            seen.append(s)
    projects: list[str] = []
    for r in rows:
        p = pathlib.Path(str(r.get("cwd") or "")).name or "?"
        if p not in projects:
            projects.append(p)
    reasons: list[str] = []
    for r in rows:
        # WHITESPACE NORMALISED, never a first line kept and the rest dropped:
        # `[0]` on `splitlines()` is a silent cut, and `clipped` is what marks
        # the one that has to happen.
        why = clipped(" ".join(str(r.get("reason") or "?").split()), 110)
        if why not in reasons:
            reasons.append(why)

    if rows:
        n = len(rows)
        newest = max((str(r.get("at") or "") for r in rows), default="")
        tail = (f" The log's tail is read {companion_faults.READ_TAIL} lines at a "
                f"time and that bound was reached, so the figure is a floor."
                if n >= companion_faults.READ_TAIL else "")
        lost = (f" {unread} line(s) of the log could not be read, so the count "
                f"understates by at least that much." if unread else "")
        out.append({
            "type": "companion.not_recording", "subject": "skill:observatory-log",
            "severity": "warning",
            "title": (f"the companion could not record {n} turn"
                      f"{'s' if n != 1 else ''}"),
            "detail": (f"newest at {newest}; session"
                       f"{'s' if len(seen) != 1 else ''} "
                       f"{listed(seen, COMPANION_LISTED)}; in "
                       f"{listed(projects, COMPANION_LISTED)}. "
                       + listed(reasons, COMPANION_LISTED, sep="; ")
                       + ". The Stop hook runs in every session of every watched "
                       f"project and is designed to be silent about work that is "
                       f"not its business, so a failure it does not report is "
                       f"indistinguishable from a quiet turn. Everything those "
                       f"sessions did after each of these turns is absent from "
                       f"the ledger." + tail + lost),
            "action": "run `tools/record_turn.py --cwd <project> --session-id <id>` "
                      "by hand to see the error, `companion_faults.py` for the "
                      "history, and check that the INSTALLED copy of the plugin "
                      "matches this repository",
            "evidence": ["store/raw/companion-faults.jsonl"]})

    # THE THIRD OUTCOME: the slot says the last turn faulted and the durable log
    # has no such line. The append is the one write in this path that can fail on
    # a full disk — the condition under which turns are ALSO most likely to be
    # lost — so a count that silently omits it would be at its least trustworthy
    # exactly when it matters most.
    at = str((slot or {}).get("at") or "")
    sess = str((slot or {}).get("session") or "")
    if (slot or {}).get("fault") and _within(at, days) and not any(
            str(r.get("at") or "") == at and str(r.get("session") or "") == sess
            for r in rows):
        out.append({
            "type": "companion.faults_unlogged", "subject": "skill:observatory-log",
            "severity": "warning",
            "title": "a lost turn is in the receipt and not in the fault log",
            "detail": (f"the receipt records a fault at {at} in session "
                       f"{sess[:8] or '?'} — "
                       f"{clipped(' '.join(str((slot or {}).get('reason') or '?').split()), 160)} "
                       f"— and the append-only log has no line for it, so the "
                       f"durable record could not be written. The next turn of "
                       f"any session on this machine overwrites the receipt, "
                       f"which means this fault is about to become unobservable; "
                       f"any count above it is a floor."),
            "action": "check free space and that `store/raw/` is writable, then "
                      "read the receipt now — it is the only copy",
            "evidence": ["store/raw/record-turn.json#fault"]})
    return out


def blank_page_findings(receipt: dict | None,
                        page_sha: str = "") -> list[dict]:
    """The operator's primary surface, as a finding rather than a log line.

    The tick log can hold a line like

        dashboard SMOKE FAILED — the page would render blank

    — a helper defined inside one function and called from another, so the
    page's own script threw. The machinery worked unattended and named the
    defect exactly, and its report still sat unread, because `tools/tick.sh`
    RAN smoke and did not REPORT it: no receipt, no finding, no failed step.
    This class can ship with every static check passing, which is why smoke
    exists — and a tick that runs it without telling anyone is no better.

    **Critical, by the same measure as a spent key.** `wallet.shared_key` is
    critical because the agent has stopped; a blank page is critical because
    every other row on this board became unreadable at once. Both clear
    themselves when the component works again: the next build overwrites the
    receipt.

    **Absent is not clean, and stale is not clean either.** No receipt means a
    fresh clone, a machine without `node`, or no tick since the change; a receipt
    for a DIFFERENT build means the verdict describes a page that has been
    replaced. Reading either as a pass restores exactly the silence this removes,
    so both are reported — as `info`, because nothing is known to be wrong.
    """
    if receipt is None:
        return [{
            "type": "dashboard.unverified", "subject": "surface:dashboard",
            "severity": "info",
            "title": "the dashboard has not been verified to render at all",
            "detail": ("`dashboard/smoke.js` executes the page's own script "
                       "against a stub DOM, and no receipt of a run exists. That "
                       "is a fresh checkout, a machine without `node`, or no "
                       "tick since the page was last built — not evidence that "
                       "the page renders. A ReferenceError at load leaves a "
                       "header, a footer and nothing between them, and every "
                       "static check passes; that shipped once on 2026-09-05."),
            "action": "`node dashboard/smoke.js docs/projects-dashboard.html` "
                      "answers it in a second and records the verdict beside "
                      "the page",
            "evidence": ["dashboard/smoke.js"]}]

    verdict = str(receipt.get("verdict") or "")
    ran_at = str(receipt.get("ran_at") or "an unrecorded time")
    checked = str(receipt.get("page_sha") or "")

    if verdict not in ("clean", "blank"):
        return [{
            "type": "dashboard.unverified", "subject": "surface:dashboard",
            "severity": "info",
            "title": f"the smoke receipt carries no verdict this can read: "
                     f"{verdict or 'nothing'!r}",
            "detail": ("The receipt exists and does not say whether the page "
                       "renders. Treating an unreadable verdict as a pass is the "
                       "defect this row was written against, so it is reported "
                       "as unverified."),
            "action": "`node dashboard/smoke.js docs/projects-dashboard.html`",
            "evidence": ["docs/projects-dashboard.smoke.json"]}]

    if verdict == "blank":
        reason = str(receipt.get("reason") or "no reason was recorded")
        return [{
            "type": "dashboard.blank", "subject": "surface:dashboard",
            "severity": "critical",
            "title": "the dashboard would render blank — its script dies at load",
            "detail": (f"`dashboard/smoke.js` ran the page's own script at "
                       f"{ran_at} and it did not survive: {reason}. Everything "
                       f"below the header is written by that script, so every "
                       f"other row on this board is invisible on the page while "
                       f"this holds. Every static check passes on such a page — "
                       f"that is how one shipped on 2026-09-05."),
            "action": "read the trace in `store/logs/tick.log` beside "
                      "`dashboard SMOKE FAILED`, or re-run "
                      "`node dashboard/smoke.js docs/projects-dashboard.html`",
            "evidence": ["docs/projects-dashboard.smoke.json", "store/logs/tick.log"]}]

    if page_sha and checked and checked != page_sha:
        return [{
            "type": "dashboard.unverified", "subject": "surface:dashboard",
            "severity": "info",
            "title": "the last clean smoke verdict describes an older build of "
                     "the page",
            "detail": (f"The page verified at {ran_at} hashed `{checked}`; the "
                       f"page on disk hashes `{page_sha}`. It has been rebuilt "
                       f"since, and a clean verdict about the previous build "
                       f"says nothing about this one — a stale pass is the same "
                       f"silence, differently spelled."),
            "action": "`node dashboard/smoke.js docs/projects-dashboard.html`, "
                      "or wait for the next tick, which records it",
            "evidence": ["docs/projects-dashboard.smoke.json"]}]
    return []


def provider_findings(health: dict | None, agent: dict | None) -> list[dict]:
    """The LLM boundary's health, as findings. Reads two documents, writes none.

    Takes the documents rather than reading them, for one reason: on a healthy
    machine the live pair is `{}` plus a recent run — the good state — so a rule
    that read the files could never be watched doing anything, and every branch
    here covers a state the estate may not have been in yet
    (`tests/test_provider_health.py`).

    `agent/providers.py` stays the only writer of `provider-health.json`. This
    reads it, and the gate's purity verdict is what keeps that true.

    Two subjects, and they are not merged. A QUARANTINED model failed at call
    time and `unhealthy()` re-probes it within the hour, so the remedy is to wait
    and watch. A RETIRED id is one the provider's catalogue no longer lists: the
    chain is permanently shorter until `agent/models.json` is edited. Opposite
    remedies, so opposite findings — a single finding with a mode flag would put
    "wait" and "edit the chain" behind one row.

    `health` is None when the file is absent or unparseable, and that is NOT the
    same as `{}`. `{}` with a recent run means every model answered; `{}` with no
    run means health was never established. Reporting the second as the first is
    the one outcome this repository forbids everywhere else.
    """
    out: list[dict] = []
    ran_at = (agent or {}).get("ran_at") or ""
    age = hours_since(ran_at) if ran_at else None

    if health is None:
        out.append({
            "type": "provider.health_unreadable", "subject": "provider:openrouter",
            "severity": "warning",
            "title": "the model quarantine list did not parse, so no model's health is known",
            "detail": "`store/provider-health.json` is the only record of which models "
                      "failed recently; `agent/providers.py` consults it before every "
                      "attempt and re-probes an entry after the configured window. "
                      "Unreadable, it reads as an empty list — and an empty list is "
                      "indistinguishable from every model being healthy, which is why "
                      "this is a finding rather than a silent default.",
            "action": "`./observatory.py key` prints every model's verdict and rewrites "
                      "the file; delete it to start from a clean slate — the marks are "
                      "transient by design",
            "evidence": ["store/provider-health.json"]})
        return out

    quarantined = sorted(health)
    if quarantined:
        shown = quarantined[:QUARANTINE_LISTED]
        rest = len(quarantined) - len(shown)
        for model_id in shown:
            entry = health.get(model_id) or {}
            since = entry.get("since") or ""
            reason = clipped(str(entry.get("reason") or "no reason recorded"), 160)
            hrs = hours_since(since) if since else None
            out.append({
                "type": "provider.model_quarantined", "subject": f"model:{model_id}",
                "severity": "warning",
                "title": f"{model_id} is skipped by the model chain: {reason}",
                "detail": ("`agent/providers.py` marked it on a failed attempt"
                           + (f" {hrs:.0f}h ago" if hrs is not None else
                              (f" at {since}" if since else ""))
                           + " and skips it until the re-probe window passes, so the "
                             "chain answers from a later model. That is the chain "
                             "working — but the fall is silent, and a later model may "
                             "cost more or reason worse. If the mark keeps coming back, "
                             "the model is not transiently unavailable."
                           + (f" {rest} more model(s) are also quarantined and not "
                              f"listed separately." if rest > 0 and model_id == shown[-1]
                              else "")),
                "action": "`./observatory.py key` re-probes and prints each model's "
                          "verdict; a mark that returns belongs in `agent/models.json` "
                          "as a chain change rather than a daily surprise",
                "evidence": ["store/provider-health.json#" + model_id]})

    for model_id in (agent or {}).get("chain_retired") or []:
        out.append({
            "type": "provider.chain_retired", "subject": f"model:{model_id}",
            "severity": "warning",
            "title": f"{model_id} is configured in the chain and no longer exists at the provider",
            "detail": "`resolve_chain()` skips an id the catalogue does not list. That is "
                      "deliberate — a chain exists so one model leaving is survivable — "
                      "but it means a three-model chain can become one, and in the "
                      "boundary's own words the only other visible sign would be the "
                      "bill. Unlike a quarantine this never expires: the id is gone until "
                      "the chain is edited.",
            "action": "edit the chain in `agent/models.json`; `./observatory.py key` "
                      "lists what the catalogue actually offers",
            "evidence": ["store/raw/agent.json#chain_retired"]})

    if not quarantined and (age is None or age > HEALTH_STALE_HOURS):
        out.append({
            "type": "provider.health_unmeasured", "subject": "provider:openrouter",
            "severity": "info",
            "title": ("no model has been asked anything recently, so the empty quarantine "
                      "list says nothing about health"),
            "detail": ("The quarantine list is written by failures and cleared by "
                       "successes, so it is empty both when every model answers and when "
                       "nothing has called one. "
                       + (f"The last agent run was {age:.0f}h ago"
                          if age is not None else
                          "No agent run is recorded at all")
                       + f", past the {HEALTH_STALE_HOURS:.0f}h horizon. This is 'not "
                         "measured', not 'healthy' — the distinction the file cannot make "
                         "on its own."),
            "action": "`./observatory.py key` probes every model in the chain and says so "
                      "explicitly; nothing here is broken until it reports otherwise",
            "evidence": ["store/provider-health.json", "store/raw/agent.json#ran_at"]})
    return out


def collect() -> list[dict]:
    out: list[dict] = []
    # WHERE THE ESTATE RUNS, on the same board as what it is made of. Absent is
    # not clean and the rules know it: no scan means no rows, rather than a row
    # claiming nothing is wrong.
    # THE PATH INSERT, not a bare import. `tools/` is on `sys.path` only when
    # this file is RUN as a script; imported by a test from elsewhere the bare
    # import raised `ModuleNotFoundError` and took the whole rule set down with
    # it — caught by `test-finding-rules` one gate run after it shipped.
    sys.path.insert(0, str(paths.ROOT / "tools"))
    import heroku_findings
    _hd = paths.REGISTRY / "heroku-apps.json"
    out.extend(heroku_findings.findings(
        json.loads(_hd.read_text(encoding="utf-8")) if _hd.is_file() else None))
    # WHAT THE CREDENTIAL INVENTORY OWES. `secret.leaked_unrotated` below reads
    # the leak register; these read its neighbours — a lifetime cap, an account
    # nothing claims, a credential known only from its leak.
    import credential_findings
    _cd = paths.REGISTRY / "credentials.json"
    out.extend(credential_findings.findings(
        json.loads(_cd.read_text(encoding="utf-8")) if _cd.is_file() else None))
    # WHAT SITS IN THE WORKING COPIES. The credential rules above read the
    # provider accounts; these read the disk, and the two disagree often enough
    # that neither is the other's summary.
    import env_findings
    _ed = paths.REGISTRY / "env-inventory.json"
    out.extend(env_findings.findings(
        json.loads(_ed.read_text(encoding="utf-8")) if _ed.is_file() else None))
    # WHAT GOOGLE SEES. The estate's busiest product was invisible
    # here until the properties were inventoried: the join is measured, and what
    # it cannot join is a count with the names beside it.
    import google_findings
    _gd = paths.REGISTRY / "google-properties.json"
    _gdoc = json.loads(_gd.read_text(encoding="utf-8")) if _gd.is_file() else None
    out.extend(google_findings.findings(_gdoc, TODAY))
    # WHOSE ACCOUNT a property sits in, against the organization of the project
    # it measures (organizations.json); silent when that file is not configured.
    # THE MACHINE: disk, memory, detached servers, idle worktrees and branches,
    # what the cleanup removed. Silent when the survey has never run.
    import machine_findings
    _mc = {}
    for _f in (paths.config_file("machine.json"), paths.ROOT / "defaults" / "machine.json"):
        try:
            _mc = json.loads(_f.read_text(encoding="utf-8")); break
        except (OSError, ValueError):
            continue
    out.extend(machine_findings.findings(paths.SCRATCH, paths.STATE / "logs", _mc))
    import organizations
    _pj = paths.REGISTRY / "projects.json"
    out.extend(google_findings.organization_findings(
        _gdoc, json.loads(_pj.read_text(encoding="utf-8")).get("projects", []) if _pj.is_file() else [],
        organizations.load()))
    # WHO ASKS FOR SECRETS, AND HOW OFTEN. The keyserver's journal is written on
    # every reveal, and a journal read by nothing can hold a burst of reveals of
    # one variable that no row on this board ever names.
    import reveal_findings
    out.extend(reveal_findings.findings(paths.STATE / "logs" / "keyserver.jsonl"))
    # WHERE AGENTS ACTUALLY WORKED that the registry never joined:
    # the SessionStart hook writes each such folder to `sessions-seen.jsonl`;
    # this reads it back as one row, minus the folders a tick has joined since.
    import session_findings
    _pj = paths.REGISTRY / "projects.json"
    _known = {f for p in (json.loads(_pj.read_text(encoding="utf-8")).get("projects") or [] if _pj.is_file() else [])
              for f in (p.get("local_folders") or [])}
    out.extend(session_findings.findings(paths.SCRATCH / "sessions-seen.jsonl", _known, paths.DATA))
    # WHAT PRODUCTION HOLDS. The env rules above read this disk; this one reads
    # what the applications are actually configured with, and the two differ in
    # the direction that matters: a value equal on both sides is the debt, not a
    # value that differs.
    import remote_findings
    _rd = paths.REGISTRY / "remote-env.json"
    out.extend(remote_findings.findings(
        json.loads(_rd.read_text(encoding="utf-8")) if _rd.is_file() else None))
    # A VALUE OF OURS, SEEN SOMEWHERE ELSE. `tools/check_secrets.py` asks whether
    # a tracked file holds anything key-SHAPED; this reads what the leak scan
    # measured, which is the stronger question and the one that has actually gone
    # wrong here twice.
    import leak_findings
    _ld = paths.SCRATCH / "leak-scan.json"
    out.extend(leak_findings.findings(
        json.loads(_ld.read_text(encoding="utf-8")) if _ld.is_file() else None))
    live = reg("domain-liveness.json")
    owned = {d["name"]: d for d in reg("domains.json").get("domains", [])}
    hosts = {h["host"]: h for h in live.get("hosts", [])}

    held: dict[str, dict] = {}
    #: Domains whose RDAP answer carried no `status` array at all — the hold
    #: check has nothing to read for them, which is a third answer beside
    #: held and not held.
    unread_status: list[str] = []
    for host, h in sorted(hosts.items()):
        m = h.get("measured") or {}
        exp, about = m.get("expires_on"), m.get("about", host)
        # `is False`, not `not …` — see the dark block below for what conflating
        # "does not resolve" with "could not be asked" costs. Computed HERE
        # because the expiry finding needs it too: telling the operator to keep
        # a domain renewing while the finding beside it suggests letting it go
        # would be two findings contradicting each other.
        dark = h.get("resolves") is False and host in owned
        nc = (owned.get(host) or {}).get("namecheap") or {}
        renew_on, auto_renew = nc.get("expires_on"), nc.get("auto_renew")
        # Expiry belongs to the registered name. Reporting a subdomain as
        # "expiring" would attribute its apex's deadline to something that has
        # no registration of its own.
        if exp and about == host and host in owned:
            d = days_until(exp)
            if d is not None and d <= SOON_WARNING:
                auto = (owned[host].get("namecheap") or {}).get("auto_renew")
                # Auto-renewal decides the severity, and getting this backwards
                # is how a list of deadlines becomes noise. Measured 2026-09-05:
                # 50 of 53 domains renew automatically, 2 do not, 1 is unrecorded
                # — the opposite of what a single sampled row suggested. A date
                # that renews itself is worth listing and not worth alarming
                # about; an unrecorded flag is treated as "not automatic",
                # because the safe error here is one reminder too many.
                renews = auto is True
                out.append({
                    "type": "domain.expiring", "subject": f"domain:{host}",
                    "severity": ("info" if renews else
                                 "critical" if d <= SOON_CRITICAL else "warning"),
                    "title": f"{host} expires in {d} days",
                    "detail": f"RDAP gives {exp}. Auto-renew is "
                              + (f"ON and {host} resolves nowhere, so the renewal "
                                 f"will pay for another year of serving nothing"
                                 if renews and dark else
                                 "ON, so this is a date to know rather than to act on"
                                 if renews else
                                 "OFF in the registrar export — nothing will renew it"
                                 if auto is False else
                                 "not recorded, so it is assumed not to renew"),
                    "deadline": exp,
                    # NOT "confirm the card" for a domain that serves nothing:
                    # that is an instruction to keep paying, printed beside a
                    # finding suggesting the opposite.
                    "action": ("decide before then — point it somewhere, or turn "
                               "auto-renew off at the registrar"
                               if renews and dark else
                               "confirm the card on file is current" if renews else
                               "renew it, or record a decision to let it go"),
                    "evidence": [f"registry:domain-liveness.json#{host}",
                                 f"registry:domains.json#{host}"]})

        # NO STATUSES IS NOT "NOT ON HOLD". The record now names the foreign
        # keys RDAP did not answer (`unread`), and `status` among them means the
        # hold check has nothing to read — not a clean bill. Before this, a
        # rename upstream would have emptied `statuses` for all 49 domains and
        # the CRITICAL `domain.hold` row would have vanished with no degradation
        # anywhere, because the fetch itself succeeded.
        verdict = hold_verdict(m)
        if verdict == "unreadable":
            unread_status.append(about)
        elif verdict == "held":
            # Raised against the REGISTERED name, once. Every subdomain carries
            # its apex's RDAP record, so emitting per host produced the same
            # finding twice under one id — ids are the dedupe key and must be
            # unique, which `main` now asserts rather than hopes.
            held.setdefault(about, {"statuses": m.get("statuses") or [],
                                    "expires_on": m.get("expires_on"), "hosts": []})
            held[about]["hosts"].append(host)

        # `is False`, not `not …`. `resolves: None` means the probe could not be
        # performed, and treating that as "it serves nothing" is how a missing
        # `dig` turned fourteen dark domains into fifty-three. An
        # unmeasured host gets its own finding below, which says what it is.
        if dark:
            # LAPSING IS AN ACT, NOT AN ABSENCE. An action reading "point it
            # somewhere, or let it lapse deliberately" presents the second half
            # as the effortless option — but a dark domain with
            # `auto_renew: true` renews for another year at cost if nothing is
            # done. Offering as easy the one outcome that cannot happen without
            # an act, without naming the act, misleads.
            #
            # Three outcomes, from the REGISTRAR's record rather than RDAP: a
            # domain can have no RDAP expiry and still a registrar date, so
            # citing only the liveness file would make its cost claim look
            # ungrounded. Where both exist they agree to the day.
            if auto_renew is True and renew_on:
                cost = (f" Auto-renew is ON, so on {renew_on} it will pay for "
                        f"another year of that.")
                act = ("point it somewhere, or turn auto-renew off at the "
                       "registrar — with it on, doing nothing renews it")
            elif auto_renew is False and renew_on:
                cost = (f" Auto-renew is OFF, so nothing will renew it: it "
                        f"lapses on {renew_on} unless somebody acts.")
                act = f"point it somewhere, or let it lapse on {renew_on}"
            else:
                cost = (" Whether it is still being paid for is not recorded — "
                        "registry/domains.json carries no registrar expiry for "
                        "it.")
                act = ("point it somewhere, or check the registrar for what it "
                       "is costing")
            out.append({
                "type": "domain.dark", "subject": f"domain:{host}",
                "severity": "info",
                "title": f"{host} is owned and resolves nowhere",
                "detail": ("No A record and no CNAME. It serves nothing."
                           + _dark_for(live.get("hosts") or [], host) + cost),
                "action": act,
                "evidence": [f"registry:domain-liveness.json#{host}",
                             f"registry:domains.json#{host}"]})

    # A HOST NOBODY COULD LOOK AT. One finding for the lot, because "the DNS
    # probe could not run" is one fact about this machine — a row per host
    # would be noise.
    unmeasured = {host: (h.get("unmeasured") or "the probe did not complete")
                  for host, h in sorted(hosts.items()) if h.get("resolves") is None}
    if unmeasured:
        why = sorted({r for r in unmeasured.values()})
        out.append({
            "type": "domain.unmeasured", "subject": "probe:dns",
            "severity": "warning",
            "title": f"{len(unmeasured)} host(s) could not be probed, so their "
                     f"liveness is unknown",
            "detail": clipped("; ".join(why)) + ". These are NOT reported as dark: a "
                      "question that could not be asked has no negative answer. "
                      "Until the probe runs, the estate does not know whether these "
                      "hosts serve anything.",
            "action": "check that `dig` and `curl` are on the PATH the tick runs "
                      "with, then `./observatory.py domains`",
            "evidence": ["registry:domain-liveness.json"]})

    # A CLONE WHOSE `origin` NAMES AN ADDRESS THAT MOVED. GitHub keeps the old
    # path working as a redirect, so `git fetch` succeeds and nothing anywhere
    # says the address belongs to somebody else now. The merge follows the
    # transfer on every tick — that is what keeps a phantom repository out of
    # the registry — and it recorded having done so for nobody until this rule:
    # two clones on this machine, every half hour, since the transfers happened.
    #
    # One finding per clone, not one for the lot: the remedy is a different
    # command in a different directory each time, and a merged finding would
    # make the operator reconstruct the list to act on it.
    for c in reg("stale-remotes.json").get("clones", []):
        if not c.get("folder"):
            # No local checkout means nothing to repoint — the transfer is then
            # merely history, and history is not a finding.
            continue
        out.append({
            "type": "repo.stale_remote", "subject": f"clone:{c['folder']}",
            "severity": "warning",
            "title": f"{c['folder']} still points at {c['was']}, which moved to "
                     f"{c['now']}",
            # WHY THE OPERATOR SHOULD CARE, not why the system does. This said
            # the merge "has to follow the transfer on every tick to keep the
            # phantom out of the registry" — true, and an account of the
            # system's own bookkeeping. The operator's stake is measured:
            # `tests/test_merge_membership.py` recorded 174 repositories instead
            # of 172 with `gh` off PATH, one phantom anchoring a project of its
            # own. A cached answer in `store/raw/model.json` prevents that today
            # — and that file is gitignored scratch, so a fresh clone of this
            # repository on a machine without `gh` has no cache to fall back on
            #.
            "detail": f"The address was transferred. GitHub redirects it, so "
                      f"`git fetch` keeps working and nothing reports the change. "
                      f"Until the remote is repointed, this address is correct in "
                      f"the registry only because the merge asks GitHub about it "
                      f"on every tick, or falls back on the previous run's answer "
                      f"cached in gitignored scratch: with `gh` unavailable and no "
                      f"cache, {c['was']} enters the registry as a repository that "
                      f"does not exist.",
            "action": f"git -C {c.get('path') or c['folder']} remote set-url origin "
                      f"git@github.com:{c['now']}.git",
            "evidence": [f"registry:stale-remotes.json#{c['was']}"]})

    # A PROJECTION THAT HAS STOPPED REACHING THE WIKI. Every refusal in
    # `commit_projection.py` returns 0 on purpose — a scheduled job must not
    # fail because the wiki is dirty — so a refusal that is only a line on
    # stdout lets the projection go uncommitted for a week because one stray
    # file sat in the wiki, while the tick's `step` sees exit 0.
    #
    # A refusal ALONE is not a finding: an operator editing their own wiki is
    # the normal case, and firing on it would be noise. The finding needs BOTH
    # — a current refusal and a wiki whose last commit is older than the
    # horizon. That is the difference between "somebody is working in there"
    # and "the mirror has stopped tracking the registry".
    proj = paths.SCRATCH / "commit-projection.json"
    if proj.is_file():
        try:
            pj = json.loads(proj.read_text(encoding="utf-8"))
        except ValueError:
            pj = {}
        outcome = pj.get("outcome") or ""
        stale_hours = hours_since(pj.get("wiki_last_commit") or "")
        if (outcome.startswith("refused") or outcome in
                ("lease-held-elsewhere", "commit-failed", "status-failed")) and (
                stale_hours is None or stale_hours > PROJECTION_COMMIT_HOURS):
            out.append({
                "type": "projection.uncommitted", "subject": "wiki:inventory",
                "severity": "warning",
                "title": f"the wiki's copy of the registry has not been committed "
                         f"for {int(stale_hours)}h" if stale_hours is not None
                         else "the wiki's copy of the registry is not being committed",
                "detail": clipped(f"{outcome}: {pj.get('detail', '')}")
                          + ". The mirror is regenerated every tick, so the "
                            "registry and the wiki have been drifting apart for "
                            "that long — and the wiki is what the wiki-* skills "
                            "read.",
                "action": "commit or discard the changes in the wiki by hand, then "
                          "`./observatory.py commit-projection`",
                "evidence": ["store/raw/commit-projection.json#outcome"]})

    # THE CAUSE, NOT FOUR SYMPTOMS. On 2026-09-07 the volume filled and
    # `scan-fs`, `scan-gh`, `scan-vault` and `scan-sessions` all exited 1 in the
    # same tick — so this file raised four `tick.step` findings, each pointing at
    # a healthy collector, and the one fact that explained all of them appeared
    # nowhere. Free space is a fact about the HOST, and this system's own ability
    # to measure anything depends on it.
    #
    # Whether to free the space is the operator's decision and this does not make
    # it: a finding is how a decision is handed over honestly.
    try:
        free_mib = shutil.disk_usage(ROOT).free // (1024 * 1024)
    except OSError as exc:
        free_mib = None
        out.append({
            "type": "host.disk_unknown", "subject": "host:volume",
            "severity": "info",
            "title": "the free space on this volume could not be measured",
            "detail": f"{type(exc).__name__}: {exc}",
            "action": "`df -h .`", "evidence": ["shutil.disk_usage"]})
    if free_mib is not None and free_mib < DISK_WARNING_MIB:
        critical = free_mib < DISK_CRITICAL_MIB
        out.append({
            "type": "host.disk_low", "subject": "host:volume",
            "severity": "critical" if critical else "warning",
            # WHOLE GiB, not the exact figure. `findings.json` is COMMITTED by
            # `tools/commit_registry.py` on every tick, and free space moves
            # continuously — an exact number here produces a commit on every
            # tick for a change that means nothing. `test_tick_repo` compares
            # two runs seconds apart byte for byte, which is exactly the guard
            # for this. A whole gigabyte moving IS meaningful, and `df -h .` is
            # where the precise number belongs.
            "title": (f"under {free_mib // 1024 + 1} GiB free on the volume holding "
                      f"this repository"),
            "detail": ("a collector may be unable to write its output at this level. "
                       "A full volume can interrupt database and scan writes."
                       if critical else
                       f"below {DISK_WARNING_MIB} MiB a VACUUM — which rewrites the "
                       f"whole store beside itself — may not fit, and retention "
                       f"runs one on every tick that removes anything."),
            "action": "free space on the configured project volume; the storage "
                      "metrics identify the largest working trees",
            "evidence": ["shutil.disk_usage(.).free"]})
        # WHO IS RESPONSIBLE, from the plugin layer rather than from a `du` the
        # operator has to run. This is the whole reason `disk-usage` exists, and
        # until now its 92 rows had no reader outside the dashboard.
        big, grow = disk_culprits()
        if big or grow:
            out[-1]["detail"] += (
                "\n\nLargest working trees: " + (", ".join(big) or "none over 0.5 GB")
                + ".\nGrowing since the previous sample: "
                + (", ".join(grow) or f"nothing by more than {GROWTH_MIB_PER_DAY} MiB")
                # THE INSTALLED PLUGIN'S OWN WORDS. A literal "the `disk-usage`
                # plugin, which excludes `.git`" here is a sentence that becomes
                # false the day another plugin takes the role — and it put a
                # plugin's name back into a core file, which is the promise this
                # whole indirection exists to keep.
                + f". Measured by the `{footprint_source() or 'footprint'}` plugin: "
                + (footprint_means() or "see its manifest for what the number "
                                        "includes."))
            # AND WHAT WOULD ACTUALLY FREE SPACE. The list above answers "what
            # does this project cost"; a full volume asks a different question,
            # and the two were being confused by one finding reading one metric.
            freeable = reclaimable_holders()
            if freeable:
                out[-1]["detail"] += (
                    "\n\nReinstallable and therefore reclaimable: "
                    + ", ".join(freeable)
                    + ". Deleting these frees the space and a reinstall restores "
                      "them, which is why the footprint above excludes them.")

    # THE LEVER, WHETHER OR NOT THE VOLUME IS DANGEROUS. Everything above is
    # gated behind `DISK_WARNING_MIB` — an absolute 5 GiB — so on 2026-09-07,
    # with 13.30 GiB free and 38.28 GiB reclaimable, the estate measured a lever
    # worth 288% of the remaining space and mentioned it nowhere. This rule is
    # the comparison rather than a level, and it passes `disk_low` so it can
    # point at that row instead of repeating what it endangers.
    _rec_total = None
    _rec_metric = metric_for_role(RECLAIMABLE_ROLE)
    if _rec_metric and paths.DB.is_file():
        try:
            _c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
            try:
                # PER-PROJECT latest, not a global MAX(at). Both shapes agree
                # today because the disk plugin stamps the calendar day, so all
                # 90 rows carry one timestamp — but a plugin that ever stamps a
                # real moment would make the global shape shrink this number
                # without a word.
                _rec_total = _c.execute(
                    "SELECT sum(m.value) FROM metrics m JOIN ("
                    "  SELECT project_id, MAX(at) at FROM metrics WHERE metric=?"
                    "  GROUP BY project_id) l"
                    " ON l.project_id=m.project_id AND l.at=m.at AND m.metric=?",
                    (_rec_metric, _rec_metric)).fetchone()[0]
            finally:
                _c.close()
        except sqlite3.Error as exc:
            print(f"  reclaimable total unreadable: {exc}", file=sys.stderr)
    _free_bytes = free_mib * 1024 * 1024 if free_mib is not None else None
    out += reclaimable_pressure(
        _free_bytes, _rec_total, reclaimable_holders(),
        disk_low=any(f["type"] == "host.disk_low" for f in out))

    # THIS PROJECT'S OWN LITTER, REPORTED SO THE DISK FINDING STOPS LYING.
    # `host.disk_low` above names the largest holders under the configured project directory, so a volume
    # filled by leaked test fixtures in the system temp dir reads as somebody's
    # repository being too big — and it did, out loud, on 2026-09-07 before the
    # 13,833 `observatory-*` directories holding 22.3 GiB were counted. Read from
    # the sweeper's receipt so this file makes no second measurement of its own.
    fx = paths.SCRATCH / "fixtures.json"
    if fx.is_file():
        try:
            swept = json.loads(fx.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  fixtures.json unreadable: {exc}", file=sys.stderr)
            swept = {}
        refused = swept.get("refused") or []
        # `stale_bytes`, NOT it plus `freed_bytes`. What removal reclaimed is a
        # SUBSET of what was found stale, so adding them reported 18 GiB for a
        # 9 GiB leak — caught by the suite on its first green run, which is the
        # whole reason the number is asserted rather than eyeballed.
        leaked = int(swept.get("stale_bytes") or swept.get("freed_bytes") or 0)
        if refused or int(swept.get("stale") or 0) or int(swept.get("removed") or 0):
            gib = leaked / 1024 ** 3
            heavy = leaked >= FIXTURE_LEAK_GIB * 1024 ** 3
            detail = (
                f"{swept.get('removed', 0)} stale fixture director"
                f"{'y' if swept.get('removed') == 1 else 'ies'} swept, "
                f"{gib:.2f} GiB — this project's own test fixtures, left in "
                f"{swept.get('root', 'the temp dir')} by runs that were killed "
                f"before their cleanup handlers ran. Stated here because "
                f"`host.disk_low` names the largest holders under the configured project directory and "
                f"would otherwise carry the blame for space this project spent.")
            if refused:
                detail += ("\n\nCould NOT be removed, and will recur every run: "
                           + listed([f"{r.get('path')} ({r.get('reason')})"
                                      for r in refused], 3) + ".")
            out.append({
                "type": "fixtures.leaked", "subject": "host:volume",
                "severity": "warning" if (heavy or refused) else "info",
                "title": (f"{gib:.2f} GiB of leaked test fixtures swept"
                          if not refused else
                          f"leaked test fixtures could not be swept"),
                "detail": detail,
                "action": ("fix the permissions on the paths named above"
                           if refused else
                           "nothing — the sweep runs on every tick; the figure is "
                           "here so a full volume is attributed correctly"),
                "evidence": ["store/raw/fixtures.json",
                             "tools/sweep_fixtures.py"]})

    # OUT OF `if db.is_file()`, because its input is a FILE. Inside the store
    # guard, a block reading `store/raw/sessions.json` would stop reporting
    # work the estate has lost on a machine with no store, or one whose store
    # failed to open — for a reason with nothing to do with the subject. A
    # test that points the store at a path that does not exist and still
    # expects the finding is what keeps it out.
    # WORK THIS ESTATE HAS NO RECORD OF. `collectors/scan_sessions.py`
    # attributes the memory companion's sessions to projects and reports what
    # it could not place; a report kept only in a `degraded` list nobody reads
    # on a schedule is what this file's own preamble calls a diary rather than
    # observability.
    #
    # Only names the operator has NOT classified are raised: the curated list
    # in `collectors/session_name_exclusions.json` removes most of them — a
    # plugin's own version directories among them — and reporting those beside
    # a real one is how a list of problems stops being read.
    # WHICH LOST PROJECTS ARE ALREADY KEPT, from the recorder's own receipt
    # rather than from the ledger: this file must not need a store in order
    # to report work the estate has lost.
    kept_records: dict[str, str] = {}
    lost_receipt = paths.SCRATCH / "lost-projects.json"
    if lost_receipt.is_file():
        try:
            for r in (json.loads(lost_receipt.read_text(encoding="utf-8"))
                      .get("projects") or []):
                if r.get("memoryId") and r.get("outcome") != "refused":
                    kept_records[r["name"]] = r["memoryId"]
        except (ValueError, OSError):
            kept_records = {}
    raw = paths.SCRATCH / "sessions.json"
    if raw.is_file():
        try:
            doc = json.loads(raw.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  sessions.json unreadable: {exc}", file=sys.stderr)
            doc = {}
        for row in doc.get("unattributed", []):
            n = row.get("sessions", 0)
            # One session in a folder is not evidence of a project — the
            # same reason `pack` carries in the exclusion list. Two is the
            # floor at which "someone worked here twice" starts to mean
            # something.
            if n < 2 and row.get('verdict') != 'ambiguous-project':
                continue
            name = row["name"]
            # THREE FACTS, not one ambiguous warning. The old text offered
            # two readings — "a project the registry has never seen" or "not
            # a project at all" — and the live estate held a THIRD that was
            # neither: a project this machine HAD, worked on across 69
            # sessions, whose folder existed on 2026-06-03 and is now absent
            # from the disk, the archives and the registry alike. The
            # collector now carries the verdict and the folder its work
            # touched, so each case names its own remedy.
            # IS THE FACT KEPT? `tools/record_lost_projects.py` writes one
            # `proposed` conclusion per lost project, so the perishable half of
            # this finding — evidence living only in a store this system does not
            # own — may already be resolved. What remains then is a decision with
            # no deadline, and a warning nothing the system can close is one that
            # teaches the operator to skip the list.
            kept = kept_records.get(name)
            verdict = row.get("verdict") or "no-path-recorded"
            folders = row.get("folders") or []
            where = listed([str(paths.DATA / f) for f in folders], 3)
            if verdict == 'ambiguous-project':
                candidates = ', '.join(row.get('candidates') or [])
                detail = (f"Equally strong project matches: {candidates}. "
                          f"{row.get('reason') or 'The available evidence does not identify one project.'} "
                          "This scan leaves the work unattributed.")
                action = ("Review the project/folder mapping for this session; "
                          "keep shared repository membership when it is correct.")
            elif verdict == "estate-folder-gone":
                detail = (f"claude-mem's own record of this work names "
                          f"{where} — a folder under the estate root that is "
                          f"NOT there now. So this is neither a project the "
                          f"registry has never seen nor a name that was never a "
                          f"project: it is a project this machine HAD. The "
                          f"registry is derived from what exists and cannot hold "
                          f"it; the session store is the only trace left.")
                if kept:
                    detail += (f" This is now recorded in the ledger as {kept}, "
                               f"`proposed` and awaiting your decision — so the "
                               f"evidence no longer depends on a store this system "
                               f"does not own.")
                    action = (f"`review.py show {kept}`, then promote it as a fact "
                              f"of this estate's history or reject it; if the folder "
                              f"was renamed, the new name is a project here and the "
                              f"sessions attach to it")
                else:
                    action = (f"`./observatory.py lost` records it in the ledger with "
                              f"the session store as its evidence; then decide what "
                              f"{name!r} was — if the folder was renamed the new name "
                              f"is a project here, and if it was deleted deliberately, "
                              f"{name!r} belongs in "
                              f"{paths.config_label('session_name_exclusions.json')} saying so")
            elif verdict == "folder-exists-unmatched":
                detail = (f"the work touched {where}, which still exists — so the "
                          f"name is a spelling this estate uses for work that "
                          f"lives somewhere the matcher did not connect. Nothing "
                          f"is inferred from it here: contact is not ownership, "
                          f"and a rule built to attribute by touched folder gave "
                          f"69 sessions of another project's work to the obsidian "
                          f"vault the first time it was driven.")
                action = (f"if {name!r} is that project under another spelling, add "
                          f"the mapping; if it is a different project, it belongs "
                          f"in the registry")
            else:
                detail = ("claude-mem recorded work under this name and no project "
                          "here matches it by folder, id slug, project name, "
                          "repository name or checkout folder — and its "
                          "observations name no path under the estate root, so "
                          "nothing here can say which of the three cases it is. "
                          "That is a gap in the evidence, not a verdict.")
                action = (f"add it to the registry, or add {name!r} to "
                          f"{paths.config_label('session_name_exclusions.json')} with the "
                          f"reason it is not a project here")
            out.append({
                "type": "work.unattributed", "subject": f"session-name:{name}",
                # A LOST PROJECT IS THE LOUDEST OF THE THREE, whatever its
                # session count: the other two are naming problems, and this
                # one is work whose only record is a store this system reads
                # read-only and does not own.
                # A KEPT FACT IS NO LONGER URGENT: the warning was about
                # perishability, not about the decision.
                "severity": ("info" if kept else
                             ("warning" if verdict == "estate-folder-gone" or n >= 10
                              else "info")),
                "title": (f"{n} session(s) with ambiguous project attribution for {name!r}"
                          if verdict == 'ambiguous-project' else
                          f"{n} session(s) of work on {name!r}, whose folder "
                          f"{where} is gone" if verdict == "estate-folder-gone"
                          else f"{n} session(s) of work on {name!r}, which this "
                               f"registry does not list"),
                "detail": detail, "action": action,
                "evidence": ["store:raw/sessions.json#unattributed", "SRC-0012"]})

    # A SHARED KEY, SPENT BY SOMEBODY ELSE. This is a fact about the
    # environment, not a defect of the estate — and it is the operator's
    # decision, so it is handed over rather than acted on. Until
    # 2026-09-07 the same fact was an automatic STOP: the daily and monthly caps
    # were measured against the KEY, so 104.80 credits of a neighbour's spending
    # disabled this system's agent, indexer and semantic search while this
    # project had spent 0.000001 and the key had 178 of 300 credits left.
    # THE MODEL CHAIN'S HEALTH. Both documents are read HERE and passed in, so
    # `provider_findings()` can be driven over states this machine has not been
    # in — the live pair is `{}` plus a recent run, the good state, which is
    # exactly the input that cannot exercise a rule. None means "did not parse",
    # and the rule treats that differently from `{}`.
    _ph = paths.STATE / "provider-health.json"
    _health: dict | None = None
    if _ph.is_file():
        try:
            _loaded = json.loads(_ph.read_text(encoding="utf-8"))
            _health = _loaded if isinstance(_loaded, dict) else None
        except ValueError:
            _health = None
    else:
        # ABSENT is not unreadable: the boundary writes the file on the first
        # failure, so a system that has never quarantined anything has no file.
        # Treated as empty, which then falls to the `ran_at` horizon below.
        _health = {}
    _ag = paths.SCRATCH / "agent.json"
    _agent: dict | None = None
    if _ag.is_file():
        try:
            _agent = json.loads(_ag.read_text(encoding="utf-8"))
        except ValueError:
            _agent = None
    out += provider_findings(_health, _agent)

    # WHERE ASSERTIONS CAN VANISH. Read through the measurer rather than by
    # grepping here: two readers of one convention with two regexes is how a
    # report and a CLI start disagreeing, and the first version of that regex
    # reported 0 of 49 because it could not read a two-literal message.
    try:
        sys.path.insert(0, str(paths.ROOT / "tools"))
        import skip_sites
        out += gate_skip_findings(skip_sites.survey())
    except Exception as exc:                                                        
        print(f"  skip-site survey failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    # THE STORE'S OWN FAULT HISTORY. Read through the module that writes it, so
    # the finding and `store_faults.py --days` cannot disagree about the window
    # or the tail — two readers of one log with two slicing rules is how a report
    # and a CLI start contradicting each other.
    try:
        import store_faults
        out += store_fault_findings(store_faults.recent(STORE_FAULT_DAYS))
    except Exception as exc:                                                        
        print(f"  store fault log unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)

    # THE PAGE ITSELF. Hashed here rather than inside the rule, so the rule stays
    # a pure function of two arguments and every one of its four outcomes can be
    # driven from a fixture — including the two nobody's estate has been in.
    # BESIDE THE PAGE, so `OBSERVATORY_DASHBOARD` redirects the verdict along
    # with the artefact it is about — one variable, and no second knob that can
    # point at a different build than the one being hashed.
    try:
        smoke = paths.DASHBOARD_HTML.with_suffix(".smoke.json")
        rec = json.loads(smoke.read_text(encoding="utf-8")) if smoke.is_file() else None
        sha = ""
        if paths.DASHBOARD_HTML.is_file():
            sha = hashlib.sha256(
                paths.DASHBOARD_HTML.read_bytes()).hexdigest()[:12]
        out += blank_page_findings(rec, sha)
    except Exception as exc:                                                        
        print(f"  smoke receipt unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)

    # STATE, not STORE. `agent/providers.py` WRITES all three of `wallet.json`,
    # `key-usage.json` and `provider-health.json` under `paths.STATE` — the
    # variable that exists precisely so a test can exercise the spend guardrail
    # without writing over the operator's journal. They may resolve to the same
    # directory as `paths.STORE`, so reading from the wrong one breaks nothing
    # until the day a fixture sets `OBSERVATORY_STATE`: then the writer writes
    # to the fixture while the reader reads the operator's real file, which
    # defeats that protection for exactly the files it was made for.
    ku = paths.STATE / "key-usage.json"
    if ku.is_file():
        try:
            doc = json.loads(ku.read_text(encoding="utf-8"))
        except ValueError:
            doc = {}
        own_month = 0.0
        wf = paths.STATE / "wallet.json"
        if wf.is_file():
            try:
                w = json.loads(wf.read_text(encoding="utf-8"))
                own_month = float((w.get("months") or {}).get(
                    TODAY[:7], 0.0))
            except (ValueError, TypeError):
                own_month = 0.0
        key_month = float(doc.get("monthly") or 0.0)
        remaining = doc.get("limit_remaining")
        limit = doc.get("limit")
        others = key_month - own_month
        age = hours_since(doc.get("checked_at") or "")
        # WARNING only when the key is close to spent, because that is when a
        # neighbour's spending becomes this system's problem: at zero, nothing
        # can be bought and the outer backstop stops everything.
        low = (remaining is not None and limit
               and float(remaining) < 0.2 * float(limit))
        # SPENT is not "low", and it earns its own grade. On 2026-09-07 the key
        # reached 300.0538 of 300 — of which this project's journal held 0.1185 —
        # and the finding stayed a warning while its own detail already said "at
        # zero this system stops with it". A prediction that does not change
        # grade when it comes true is one nobody has to act on, and what stopped
        # is not cosmetic: `agent` and `index` are the only two steps that spend,
        # so the interpretation layer is down until the reset or a separate key.
        spent = remaining is not None and float(remaining) <= 0
        # ONE derivation, shared with the symptom below. `limit_reset` is a word
        # ("monthly"), never a date, so a block the system knows is temporary said
        # nothing about when it lifts — and a warning that will be lit for three
        # weeks reads exactly like one nobody has looked at.
        _reset = reset_date(doc.get("limit_reset"), measured_at(doc))
        if others > max(1.0, own_month * 2):
            out.append({
                "type": "wallet.shared_key", "subject": "provider:openrouter",
                "severity": "critical" if spent else ("warning" if low else "info"),
                "title": (f"the API key has spent {key_month:.1f} of "
                          f"{limit or '?'} this month; this project accounts for "
                          f"{own_month:.2f}"),
                "detail": (f"{others:.1f} was spent by something else using the same "
                           f"key — measured {doc.get('checked_at')}"
                           + (f", {age:.0f}h ago" if age is not None else "")
                           + '. ' + (('The key is SPENT, and the two steps that spend have '
                                       'stopped with it: `agent` no longer interprets a delta '
                                       'and `index` no longer embeds what it wrote. Everything '
                                       'deterministic keeps running. ') if spent else '')
                           + f"{('Only ' + format(float(remaining), '.1f') + ' remains, and when a key is spent this system stops with it. ') if (low and not spent) else ''}"
                             "This system's own ceilings are measured against its own "
                             "journal, so a neighbour's spending no longer disables it "
                             "— but the key is shared and the budget is one."),
                "action": ("a separate key for this system, or a higher limit; "
                           "`./observatory.py wallet` shows both figures"
                           + (f". Left alone it lifts on {_reset} when the key's "
                              f"monthly counter resets" if _reset else
                              f". The provider calls the reset "
                              f"{doc.get('limit_reset') or 'nothing'}, which this "
                              f"system cannot turn into a date")),
                "evidence": ["store/key-usage.json#monthly", "store/wallet.json#months"]})

    # KNOWLEDGE THAT FELL BEHIND THE CODE. Two artefacts describe a project
    # beyond its diff — the code graph (`graphify-out/graph.json`) and the wiki
    # notes — and both rot silently: nothing fails when a project keeps
    # committing for weeks past its last note or its last graph; the registry
    # simply knows less and less while looking equally confident. One
    # AGGREGATED row per artefact — a row per project is how a board stops
    # being read — worst offenders first, measured in days of unrecorded work.
    _local = {}
    _lf = paths.SCRATCH / "local.json"
    if _lf.is_file():
        try:
            _local = {f["folder"]: f for f in
                      json.loads(_lf.read_text(encoding="utf-8"))["folders"]}
        except (OSError, ValueError, KeyError) as exc:
            print(f"  local.json unreadable for staleness: {exc}", file=sys.stderr)
    _vault_rows = {}
    _vf = paths.SCRATCH / "vault.json"
    if _vf.is_file():
        try:
            _vault_rows = {r["folder"]: r for r in
                           json.loads(_vf.read_text(encoding="utf-8"))}
        except (OSError, ValueError, TypeError) as exc:
            print(f"  vault.json unreadable for staleness: {exc}", file=sys.stderr)
    KNOWLEDGE_GRACE_DAYS = 7
    graph_stale, wiki_stale = [], []
    for folder, l in sorted(_local.items()):
        last = l.get("last_commit") or ""
        if not last:
            continue
        g = l.get("graph_built_on")
        if g and last > g:
            lag = (datetime.fromisoformat(last) - datetime.fromisoformat(g)).days
            if lag > KNOWLEDGE_GRACE_DAYS:
                graph_stale.append((lag, folder))
        v = _vault_rows.get(folder)
        n = (v or {}).get("notes_updated_on")
        if n and last > n:
            lag = (datetime.fromisoformat(last) - datetime.fromisoformat(n)).days
            if lag > KNOWLEDGE_GRACE_DAYS:
                wiki_stale.append((lag, folder))
    if graph_stale:
        graph_stale.sort(reverse=True)
        out.append({
            "type": "project.graph_stale", "subject": "estate:graphs",
            "severity": "info",
            "title": (f"{len(graph_stale)} project(s) have a code graph older than "
                      f"their code"),
            "detail": ("A graph that fell behind carries the authority of a machine "
                       "and the accuracy of a memory. Worst: "
                       + listed([f"{f} ({d}d behind)" for d, f in graph_stale], 5)
                       + f". Grace is {KNOWLEDGE_GRACE_DAYS} days; only projects "
                         f"that HAVE a graph are counted — adopting graphify is a "
                         f"choice, letting it rot is not."),
            "action": "in each project: /graphify . --update (or graphify's CLI); "
                      "the companion hook now reminds at the moment of work",
            "evidence": ["store/raw/local.json#graph_built_on"]})
    if wiki_stale:
        wiki_stale.sort(reverse=True)
        out.append({
            "type": "project.wiki_stale", "subject": "estate:wiki",
            "severity": "info",
            "title": (f"{len(wiki_stale)} project(s) have wiki notes older than "
                      f"their last commit"),
            "detail": ("The registry's own composition comes partly from these "
                       "notes ('named in vault notes'), so a stale note is not "
                       "cosmetic: outdated notes can omit repositories from a project. Worst: "
                       + listed([f"{f} ({d}d of unnarrated work)" for d, f in wiki_stale], 5)
                       + "."),
            "action": "update the project's overview in the wiki; measured "
                      "composition (submodules) now flows in without it, but the "
                      "narrative is the half only a person can write",
            "evidence": ["store/raw/vault.json#notes_updated_on",
                         "store/raw/local.json#last_commit"]})

    # THE ALWAYS-ON SERVER WENT QUIET. `tools/serverd.py --install` promises a
    # process that is up by default; the heartbeat receipt is how that promise
    # is checked rather than believed. Three states, three answers: installed
    # and beating — nothing; installed and silent — a warning naming the age;
    # not installed — nothing, because off is a legitimate state the operator
    # chose with --uninstall.
    sys.path.insert(0, str(paths.ROOT / "tools"))
    import install_launchd
    sd_plist = pathlib.Path.home() / "Library/LaunchAgents" / f"{install_launchd.instance_label('server')}.plist"
    sd = paths.SCRATCH / "serverd.json"
    if sd_plist.is_file():
        beat = None
        if sd.is_file():
            try:
                beat = json.loads(sd.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                beat = None
        age_min = hours_since((beat or {}).get("at") or "")
        age_min = None if age_min is None else age_min * 60.0
        if age_min is None or age_min > 15:
            out.append({
                "type": "server.silent", "subject": "server:local",
                "severity": "warning",
                "title": ("the local server is installed and has never reported"
                          if age_min is None else
                          f"the local server's last heartbeat is {age_min:.0f} minutes old"),
                "detail": ("launchd holds a KeepAlive agent for it, so a silence "
                           "this long means it is crash-looping or cannot write "
                           "its receipt — not that it was turned off, which "
                           "removes the agent entirely."),
                "action": "tools/serverd.py --status; the log is store/logs/serverd.err",
                "evidence": ["store/raw/serverd.json",
                             str(sd_plist)]})

    # A SESSION OBEYING OLD RULES. The handshake in `tools/skill_check.py`
    # records what version of a MANDATORY skill each session read; a recent
    # `stale` sighting means an agent is following text that has since changed.
    sk = paths.SCRATCH / "skill-sessions.json"
    if sk.is_file():
        try:
            sessions = (json.loads(sk.read_text(encoding="utf-8")) or {}).get("sessions", {})
        except (OSError, ValueError):
            sessions = {}
        for row in sessions.values():
            if not str(row.get("verdict", "")).startswith("stale"):
                continue
            age_h = hours_since(row.get("last_seen") or "")
            if age_h is None or age_h > 48:
                continue                                                
            major = row.get("verdict") == "stale-major"
            out.append({
                "type": "skill.stale_session",
                "subject": f"skill:{row.get('skill')}",
                "severity": "warning" if major else "info",
                "title": (f"a session is following {row.get('skill')} "
                          f"{row.get('reported')} — "
                          + ("a MAJOR version behind" if major else "behind the shipped text")),
                "detail": (f"Reported {row.get('last_seen')} (×{row.get('count')}). "
                           f"Skills are read at session START and never re-read, so "
                           f"`claude plugin update` alone does not reach a running "
                           f"session"
                           + (". A major bump means a rule changed MEANING — that "
                              "session should be restarted, not finished." if major
                              else "; the old text is compatible, just poorer.")),
                "action": "restart that session after `claude plugin update "
                          "observatory-log@observatory-log`",
                "evidence": ["store/raw/skill-sessions.json"]})

    # A RETIRED VALUE THAT NOBODY REVOKED. `vault.py rotate` archives the old
    # value and says out loud that it keeps working until revoked at its
    # provider. The archive sitting there a month later means that sentence was
    # read and not acted on — a live credential in a file whose name says it is
    # dead.
    vault_dir = pathlib.Path(os.environ.get(
        "OBSERVATORY_VAULT_DIR",
        paths.source_path("secret_store", paths.SECRETS) / "projects"))
    if vault_dir.is_dir():
        stale_retired = []
        for f in vault_dir.rglob("*.retired-*"):
            age_h = None
            try:
                stamp = f.name.rsplit(".retired-", 1)[1]
                iso = f"{stamp[:13]}:{stamp[13:15]}:{stamp[15:17]}Z"
                age_h = hours_since(iso)
            except (IndexError, ValueError):
                pass
            if age_h is not None and age_h > 24 * 30:
                rel = f.relative_to(vault_dir)
                stale_retired.append(f"{rel.parts[0]}/{rel.parts[1]}/{f.name}")
        if stale_retired:
            out.append({
                "type": "secret.retired_unrevoked", "subject": "vault:archives",
                "severity": "warning",
                "title": (f"{len(stale_retired)} retired secret value(s) are over a "
                          f"month old and presumably never revoked"),
                "detail": ("A rotation replaces the value HERE; the provider keeps "
                           "honouring the old one until it is revoked THERE. These "
                           "archives are past any reasonable revocation window: "
                           + listed(stale_retired, 4) + ". If the old values were "
                           "revoked, delete the archives; if not, revoke first."),
                "action": "revoke at each provider, then delete the archive file",
                "evidence": ["the vault's *.retired-* files"]})

    # TRAFFIC ON A HOST NO PROJECT CLAIMS. An analytics plugin that measured a
    # zone or property the registry cannot attribute reports it in its receipt
    # note; this surfaces it as ONE row naming the hosts, because the remedy is
    # a registry edit (claim the host on a project's `sites`, or a
    # public_domain_of relation) and the operator should make it once, not read
    # it per plugin.
    _pl = paths.SCRATCH / "plugins.json"
    if _pl.is_file():
        try:
            _plugins = json.loads(_pl.read_text(encoding="utf-8")).get("plugins", [])
        except (OSError, ValueError):
            _plugins = []
        _unmapped = []
        for pl in _plugins:
            note = pl.get("note") or ""
            if "unmapped" in note or "unmappable" in note:
                _unmapped.append(f"{pl.get('id')}: {note}")
        if _unmapped:
            # THE OPERATOR'S WORD, WHERE THERE IS ONE. `collectors/host_boundary.json`
            # names the hosts the operator has spoken about — "backend host",
            # "main site, leave it", "pending, material coming" — so this row can
            # separate what is outside BY DECISION from what is merely
            # unclassified. The second list is the only one that still wants a
            # word; the first is the estate's edge, drawn.
            _bf = paths.config_file("host_boundary.json")
            try:
                _boundary = json.loads(_bf.read_text(encoding="utf-8")).get("hosts", {})
            except (OSError, ValueError):
                _boundary = {}
            _hosts = re.findall(r"([a-z0-9][a-z0-9.-]+\.[a-z]{2,})(?: \((\d+)\))?",
                                " ".join(_unmapped))
            _seen: dict[str, int] = {}
            for h, n in _hosts:
                _seen[h] = max(_seen.get(h, 0), int(n or 0))
            _outside = {h: _boundary[h] for h in _seen if _boundary.get(h, {}).get("status") == "outside"}
            _pending = {h: _boundary[h] for h in _seen if _boundary.get(h, {}).get("status") == "pending"}
            _unclassified = sorted((h for h in _seen if h not in _boundary),
                                   key=lambda h: -_seen[h])
            # The note lists only its top twelve; the TOTAL is in its header.
            # A title that counted the listed ones alone said "4 unclassified"
            # over 79 unmapped zones (measured 2026-09-13).
            _total = sum(int(n) for n in re.findall(r"unmapp\w+ \w+ \((\d+)\)",
                                                    " ".join(_unmapped)))
            parts = []
            if _outside:
                parts.append("outside by decision: "
                             + "; ".join(f"{h} — {b['why']}" for h, b in list(_outside.items())[:4]))
            if _pending:
                parts.append("pending the operator's word: "
                             + ", ".join(f"{h} (candidates {', '.join(b.get('candidates') or [])})"
                                         for h, b in _pending.items()))
            if _unclassified:
                parts.append(f"unclassified ({len(_unclassified)}), by requests: "
                             + ", ".join(f"{h} ({_seen[h]})" if _seen[h] else h
                                         for h in _unclassified[:8])
                             + (" …" if len(_unclassified) > 8 else ""))
            out.append({
                "type": "analytics.unmapped_host", "subject": "estate:boundary",
                "severity": "info",
                "title": (f"traffic outside the estate's projects: {_total or len(_seen)} "
                          f"host(s) — {len(_outside)} by decision, {len(_pending)} pending, "
                          f"{len(_unclassified)} of the top-listed unclassified"),
                "detail": ("By the operator's rule a host with no project here is one "
                           "the operator is not working on — someone else runs it, or "
                           "it is not a priority — so this is the estate's edge, not an "
                           "omission. " + " | ".join(parts)
                           + " Sources: " + listed(_unmapped, 2)),
                "action": ("none for the decided; a word for the pending and the "
                           "unclassified — one line each in "
                           f"{paths.config_label('host_boundary.json')}, "
                           "or adopt the host onto a project when work begins here"),
                "evidence": ["store/raw/plugins.json#note", paths.config_label("host_boundary.json")]})

    # THE MCP SERVERS THE AGENTS ARE TOLD TO REACH. The gateway
    # that held them is off; each agent's own config holds them now, and these
    # rows are the only place a dead server, a key in a URL, or the
    # observatory's own server going unregistered would be said.
    _mf = paths.REGISTRY / "mcp-servers.json"
    if _mf.is_file():
        try:
            _mdoc = json.loads(_mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _mdoc = {}
        _ms = _mdoc.get("servers") or []
        _dead = sorted({f"{r['name']} ({r['agent']})" for r in _ms if r.get("liveness") == "failed"})
        if _dead:
            out.append({
                "type": "mcp.unreachable", "subject": "estate:mcp", "severity": "warning",
                "title": f"{len(_dead)} MCP server(s) Claude could not reach",
                "detail": ("Declared, and `claude mcp list` reports a failed connection: "
                           + listed(_dead, 6) + ". A session that needs one of these "
                           "fails at the tool call, not at startup."),
                "action": "run the server's command by hand for its error; fix the "
                          "declaration in the agent's own config (`claude mcp add`)",
                "evidence": ["registry/mcp-servers.json#liveness"]})
        _keyed = sorted({r["name"] for r in _ms if r.get("key_in_url")})
        if _keyed:
            out.append({
                "type": "mcp.key_in_url", "subject": "estate:mcp", "severity": "warning",
                "title": f"{len(_keyed)} MCP server(s) carry a key in their URL",
                "detail": ("A token in a query string is printed by `claude mcp list`, by "
                           "any process listing, and by every log that records a URL — "
                           "one reached a session transcript on 2026-09-14 that way: "
                           + listed(_keyed, 5) + ". A header is not echoed by the "
                           "listing; a query string is."),
                "action": "move the token from the URL into the header the provider "
                          "names — ask its 401 (searchapi says `X-MCP-Token`; probed "
                          "2026-09-14, HTTP 200) — or a wrapper that reads it from a "
                          "mode-600 file; then rotate it",
                "evidence": ["registry/mcp-servers.json#key_in_url"]})
        _auth = sorted({f"{r['name']}" for r in _ms if r.get("liveness") == "needs-auth"})
        if _auth:
            out.append({
                "type": "mcp.needs_auth", "subject": "estate:mcp", "severity": "info",
                "title": f"{len(_auth)} MCP server(s) wait for a browser sign-in",
                "detail": ("OAuth-backed servers Claude reports as `Needs authentication`: "
                           + listed(_auth, 8) + ". They work after `/mcp` in a session "
                           "signs in; nothing here can do that for a person."),
                "action": "none unless one is needed — then sign in from a session",
                "evidence": ["registry/mcp-servers.json#liveness"]})
        if _mdoc and not _mdoc.get("own_declared"):
            out.append({
                "type": "mcp.own_unregistered", "subject": "estate:mcp", "severity": "warning",
                "title": "the observatory's own MCP server is declared in no agent",
                "detail": ("`mcp/server.py` serves eighteen tools — recall, credentials by "
                           "name, proposals — and none of them is reachable until an "
                           "agent's config names the server (credentials audit G14)."),
                "action": mcp_registration_command(),
                "evidence": ["registry/mcp-servers.json#own_declared"]})

    # A LEAKED SECRET THAT NOBODY ROTATED. `tools/vault.py leak` is the rule
    # the observatory's skill makes mandatory: an agent that sees a secret value
    # where it does not belong records the sighting. The register is append-only
    # and holds names and places, never values; a `rotate` of the same slot
    # appends a settlement. What is left — a leak with no settlement — is a
    # credential KNOWN to be exposed and still in service, which is the one
    # state this rule exists to make impossible to forget.
    vault_leaks = pathlib.Path(os.environ.get(
        "OBSERVATORY_VAULT_DIR",
        paths.source_path("secret_store", paths.SECRETS) / "projects")) / "leaks.jsonl"
    # The provider's config trail (names only) — read once, used by the leak
    # rows' rotation hint below AND by `secret.moved_unrecorded` after them,
    # which is why it lives outside the leaks-file branch.
    _hk_trail: dict[str, list] = {}
    _hkf = paths.SCRATCH / "heroku.json"
    if _hkf.is_file():
        try:
            for _app in json.loads(_hkf.read_text(encoding="utf-8")).get("apps") or []:
                if _app.get("config_releases"):
                    _hk_trail[_app["name"]] = _app["config_releases"]
        except (OSError, ValueError):
            _hk_trail = {}
    if vault_leaks.is_file():
        rows, settled, legacy_rotated = [], set(), set()
        try:
            for line in vault_leaks.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                if leak_register.is_legacy_rotation(r):
                    legacy_rotated.add(r.get("of"))
                elif r.get("event") == "settled":
                    settled.add(r.get("of"))
                elif r.get("event") == "leaked":
                    rows.append(r)
        except (OSError, ValueError) as exc:
            out.append({
                "type": "secret.register_unreadable", "subject": "vault:leaks",
                "severity": "warning",
                "title": "the leak register did not parse, so no leak's status is known",
                "detail": f"{type(exc).__name__}: {exc} — an unreadable register "
                          f"reads as empty, and empty is indistinguishable from "
                          f"nothing ever having leaked.",
                "action": "read the file by eye; every line must be one JSON object",
                "evidence": [str(vault_leaks)]})
            rows = []
        # A ROTATION THE PROVIDER ALREADY SHOWS. heroku.json keeps each app's
        # config variable trail — names only. For a leak of NAME in vault project P,
        # an app named P (or tied to project:P) with a release that touched a
        # variable sharing NAME's stem, after the sighting minus an hour (a
        # rotation can precede its own record), is named in the row: the
        # critical then says "this may already be done — settle it", instead of
        # standing for a week over a finished job.

        def _rotation_hint(secret: str, seen_at: str) -> str:
            try:
                proj, _env, name = secret.split("/", 2)
            except ValueError:
                return ""
            stem = name.split("_")[0]
            floor = (datetime.fromisoformat(seen_at.replace("Z", "+00:00")) - timedelta(hours=1)
                     ).strftime("%Y-%m-%dT%H:%M:%SZ") if seen_at else ""
            for app, trail in _hk_trail.items():
                if app != proj and app.replace("-", "") != proj.replace("-", ""):
                    continue
                for rel in trail:
                    if (rel.get("at") or "") >= floor and any(
                            v.split("_")[0] == stem for v in rel.get("vars") or []):
                        return (f" Heroku shows v{rel.get('version')} on {app} at "
                                f"{(rel.get('at') or '')[:16]}Z touching "
                                f"{', '.join(rel.get('vars') or [])} ({rel.get('by')}) — "
                                f"AFTER the sighting. Verify old-version revocation and consumers before settlement.")
            return ""

        for r in rows:
            if r.get("id") in settled:
                continue
            age_d = hours_since(r.get("at") or "")
            age_d = None if age_d is None else age_d / 24.0
            _hint = _rotation_hint(r.get("secret") or "", r.get("at") or "")
            out.append({
                "type": "secret.leaked_unrotated", "subject": f"secret:{r.get('secret')}",
                "severity": "warning" if r.get("id") in legacy_rotated else "critical",
                "title": (f"{r.get('secret')} was seen leaking"
                          + (f" {age_d:.0f} day(s) ago" if age_d is not None and age_d >= 1
                             else "")
                          + (" and was closed only by a local rotate"
                             if r.get("id") in legacy_rotated else " and has no recorded settlement")),
                "detail": (("An earlier version marked this leak settled when the local slot "
                            "was replaced; that records neither revocation nor consumer checks. "
                            if r.get("id") in legacy_rotated else "")
                           + f"Recorded {r.get('at')}: the value was seen at "
                           f"{r.get('where', 'an unrecorded place')}. It remains "
                           f"potentially usable until the old version is revoked at its "
                           f"provider. A local replacement does not prove revocation "
                           f"or consumer verification. The register keeps names and "
                           f"places, never values." + _hint),
                "action": (f"verify old-version revocation at the provider and the consumers; "
                           f"if a local slot needs replacement, use `tools/vault.py rotate "
                           f"{(r.get('secret') or '//').replace('/', ' ')}` first. "
                           f"Then record manual settlement with `tools/vault.py settle "
                           f"{(r.get('secret') or '//').replace('/', ' ')} --how \"…\" "
                           f"--revocation-evidence \"…\" --consumer-evidence \"…\"`. "
                           f"These references are manual attestations, not an automatic provider check."),
                "evidence": [str(vault_leaks)]})

    # A KEY MOVED AT THE PROVIDER AND NOBODY SAID SO. Heroku's config trail
    # (names only) is the one provider trail read today; other providers join
    # by the same shape when a scan carries theirs. For every secret-shaped
    # variable a release touched in the last seven days, the movements journal
    # must hold an entry within two hours naming that variable (or its stem) —
    # the tools write one themselves; a hand-made change is recorded with
    # `vault.py moved`.
    # ONE READER, shared with the keys page: the journal, the settled rows, the
    # two-hour window and the release-number rule all live in
    # `tools/movements.py`, so the finding and the page cannot disagree about
    # what was recorded.
    import movements as _movements
    _moves = _movements.read_moves(vault_leaks)
    _unrecorded = [_movements.describe(r) for r in _movements.unrecorded(_hk_trail, _moves)]
    if _unrecorded:
        out.append({
            "type": "secret.moved_unrecorded", "subject": "estate:movements",
            "severity": "warning",
            "title": f"{len(_unrecorded)} key movement(s) at Heroku that no agent recorded",
            "detail": ("A config variable shaped like a secret changed at the provider and "
                       "the movements journal holds nothing within two hours naming it. "
                       "The operator's rule: every movement of every key is recorded by "
                       "the agent that made it, in the same turn — the tools write it "
                       "themselves, anything done by hand is `vault.py moved`. "
                       + listed(_unrecorded, 5)),
            "action": ("if an agent or a person did it: `tools/vault.py moved <project> "
                       "<env> <NAME> --at heroku --how \"…\"` (settlement also requires "
                       "--settle, --revocation-evidence and --consumer-evidence); "
                       "if nobody here did, that is a change to explain"),
            "evidence": [str(vault_leaks.parent / "movements.jsonl"), "store/raw/heroku.json#config_releases"]})

    # THE COMPANION IS NOT RECORDING, and its whole design is to be quiet.
    # `skill/plugins/observatory-log` runs a Stop hook in every session of every
    # watched project; it exits 0 in silence for anything that is not its
    # business, `ask-why.py` said nothing on any failure, and the hook discards
    # stderr. So a total failure looked exactly like a quiet afternoon — measured
    # 2026-09-07, roughly seventy-two turns of one session raised
    # `IllegalTransition: observed -> proposed` and the only sign was a ledger
    # row that had stopped moving fifteen hours earlier.
    rt = paths.SCRATCH / "record-turn.json"
    slot = {}
    if rt.is_file():
        try:
            slot = json.loads(rt.read_text(encoding="utf-8"))
        except ValueError:
            slot = {}
    try:
        lost, unread = companion_faults.read(COMPANION_FAULT_DAYS)
    except Exception as exc:                                                        
        print(f"  companion fault log unreadable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        lost, unread = [], 0
    out += companion_findings(lost, slot, unread)

    # THE INSTALLED COPY IS NOT THIS REPOSITORY, which is how a fix to the hook
    # can have no effect at all. A plugin whose marketplace source is a local
    # `directory` is COPIED into `~/.claude/plugins/cache/...` at install time
    # and the harness runs the copy — so the edit that fixed the recorder at
    # 04:22 on 2026-09-07 was still not running at 11:30, and the divergence was
    # visible nowhere. Same family as the plain-copy-shadows-a-plugin trap the
    # machine's own CLAUDE.md records.
    installed = COMPANION_INSTALL
    hook = ROOT / "skill/plugins/observatory-log/hooks/record-turn.sh"
    if installed.is_dir() and hook.is_file():
        copies = sorted(installed.glob("*/hooks/record-turn.sh"))
        stale = [c for c in copies
                 if c.read_bytes() != hook.read_bytes()]
        if stale:
            out.append({
                "type": "companion.stale_install", "subject": "skill:observatory-log",
                "severity": "warning",
                "title": "the installed companion differs from this repository",
                "detail": ("the harness runs the COPY under "
                           "`~/.claude/plugins/cache/`, not the checkout, so every "
                           "change to the hook has no effect until it is "
                           "reinstalled: "
                           # `~`-relative WHERE IT APPLIES. `relative_to`
                           # raises for a path outside home, which is every
                           # overridden install — so the tidier display broke
                           # the finding it was tidying, and a test that pointed
                           # the constant at a temp directory found it in one
                           # run. Shortening a path must not be able to fail.
                           + listed([shorten(c) for c in stale], 3)),
                "action": "`claude plugin uninstall observatory-log@observatory-log` "
                          "then install it again from the local marketplace, and "
                          "restart the session — hooks are read at session start",
                "evidence": ["~/.claude/plugins/cache/observatory-log"]})

    # THE WATCHER STOPPED WATCHING. Everything else in this file reports on the
    # ESTATE; this reports on the observatory, and it is the only finding whose
    # absence is itself the failure — a stopped tick produces no findings at
    # all, so the last built set stays on the dashboard looking current.
    #
    # Which is why the age is measured from the STORE rather than from this
    # file: `findings.json` is rebuilt by the same tick that scans, so its own
    # freshness cannot testify to anything.
    newest_scan = None
    if paths.DB.is_file():
        try:
            c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
            try:
                row = c.execute("SELECT max(started_at) FROM scans").fetchone()
                newest_scan = row[0] if row else None
            finally:
                c.close()
        except sqlite3.Error:
            newest_scan = None
    scan_age = hours_since(newest_scan or "")
    if paths.DB.is_file() and (scan_age is None or scan_age > SCAN_WARNING_HOURS):
        out.append({
            "type": "scan.stale", "subject": "observatory:tick",
            "severity": ("critical" if scan_age is None or scan_age > SCAN_CRITICAL_HOURS
                         else "warning"),
            "title": (f"no scan since {newest_scan}" if newest_scan
                      else "the store holds no scan at all"),
            "detail": ("the tick runs every thirty minutes and writes a `scans` row on "
                       "every collector run, so this is a scheduler that has stopped "
                       "rather than a slow run. Everything else on the dashboard is as "
                       "old as this: nothing here is being re-measured, and a finding "
                       "that has been fixed will still be shown."),
            "action": "`launchctl list | grep observatory` and "
                      "`tail -40 store/logs/tick.err`; `./observatory.py all` runs it "
                      "by hand",
            "evidence": ["store/observatory.db#scans.started_at"]})

    # A BROKEN WIKILINK, from the receipt the audit now leaves. The wiki is
    # written by `tools/commit_projection.py` on every tick and read by every
    # `wiki-*` skill, so a link that resolves to nothing is a fact about a
    # surface this system maintains — and until 2026-09-07 the only way to learn
    # it was to type the command.
    #
    # `vault-absent` is NOT a finding. The wiki lives on one machine; a clone
    # elsewhere legitimately has none, and raising there would be reporting the
    # checker's own reach as a defect of the estate.
    vl = paths.SCRATCH / "vault-links.json"
    if vl.is_file():
        try:
            doc = json.loads(vl.read_text(encoding="utf-8"))
        except ValueError:
            doc = {}
        broken = doc.get("broken")
        if doc.get("outcome") == "broken-links" and broken:
            targets = doc.get("targets") or []
            out.append({
                "type": "wiki.broken_link", "subject": "wiki:links",
                "severity": "warning",
                "title": f"{broken} wikilink(s) in the wiki resolve to nothing",
                "detail": (f"{doc.get('links', '?')} link(s) across "
                           f"{doc.get('notes', '?')} note(s) were checked at "
                           f"{doc.get('ran_at', '?')}. Targets: "
                           + listed(targets, 6)
                           + ". The wiki is regenerated into every tick and read by "
                             "the wiki-* skills, so a dangling link is a dead end for "
                             "a reader rather than a cosmetic issue."),
                "action": "`./observatory.py links` for the sources, then fix the note "
                          "or the target",
                "evidence": ["store/raw/vault-links.json#targets"]})
        stale = hours_since(doc.get("ran_at") or "")
        if doc.get("outcome") != "vault-absent" and (stale is None or stale > 24):
            out.append({
                "type": "wiki.links_unchecked", "subject": "wiki:links",
                "severity": "info",
                # AN ABSOLUTE STAMP, not a relative age, for the same reason
                # as the disk figure above: a stamp is stable until the audit
                # runs again, while "37h" changes every hour in a committed file.
                "title": (f"the wiki's links have not been checked since "
                          f"{doc.get('ran_at')}" if stale is not None
                          else "the wiki's link check has never completed"),
                "detail": "the audit runs in the tick; a receipt this old means the "
                          "step stopped rather than that the links are fine.",
                "action": "`./observatory.py links`",
                "evidence": ["store/raw/vault-links.json#ran_at"]})

    # THE DIFF HAS STOPPED, which nothing could say before the cursor existed.
    # `snapshot` and `diff` are two steps: the first keeps writing fingerprints
    # and the second can stop — a `diff` that raises, a step removed from the
    # tick, a store locked at the wrong moment — and the visible effect is an
    # agent with nothing to do, which looks exactly like a quiet estate. The
    # cursor advances on every successful diff, so its age answers the question
    # directly.
    cur = newest = None
    if paths.DB.is_file():
        try:
            c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            try:
                cur = c.execute("SELECT value, updated_at FROM cursors WHERE name = ?",
                                ("deltas.diffed_through",)).fetchone()
                newest = c.execute(
                    "SELECT scan_id, observed_at FROM observations WHERE kind = ?"
                    " ORDER BY observed_at DESC, rowid DESC LIMIT 1",
                    (store_db.FINGERPRINT_KIND,)).fetchone()
            finally:
                c.close()
        except sqlite3.Error:
            cur = newest = None
    if newest is not None and (cur is None or cur["value"] != newest["scan_id"]):
        lag = hours_since(cur["updated_at"]) if cur is not None else None
        if lag is None or lag > DELTA_DIFF_HOURS:
            out.append({
                "type": "deltas.not_diffed", "subject": "store:deltas",
                "severity": "warning",
                "title": (f"the registry has not been diffed for {int(lag)}h"
                          if lag is not None else
                          "the registry has never been diffed"),
                "detail": (f"fingerprints are still arriving — the newest is "
                           f"{newest['scan_id']} at {newest['observed_at']} — but "
                           f"`deltas.diffed_through` "
                           + (f"still names {cur['value']}, last advanced "
                              f"{cur['updated_at']}" if cur is not None
                              else "has never been set") +
                           ". The agent is being handed an empty queue while the "
                           "estate changes, which looks exactly like a quiet estate."),
                "action": "`./observatory.py deltas`, and check the tick's log for "
                          "why the step stopped",
                "evidence": ["store/observatory.db#cursors.deltas.diffed_through"]})

    # A MODEL ANSWERING BADLY, as a number rather than a console line. Two
    # shapes, and they need different remedies:
    #
    #   malformed  — `worth_recording: true` with an empty interpretation. The
    #                answer contradicts itself; the deltas stay unconsumed, so
    #                the work is not lost but it is not done either.
    #   unreasoned — a decline with an empty `why_not`. The judgement is usable
    #                and the delta IS consumed, so nothing will ever revisit it:
    #                the change is recorded as needing no note, with no note
    #                saying why. The first live run did this eight times.
    #
    # Read from `store/raw/agent.json`, so a run that ended in a guardrail still
    # reports what it managed to see.
    ag = paths.SCRATCH / "agent.json"
    if ag.is_file():
        try:
            adoc = json.loads(ag.read_text(encoding="utf-8"))
        except ValueError:
            adoc = {}
        bad, mute = adoc.get("malformed") or 0, adoc.get("unreasoned") or 0

        # THE COUNTER THAT HAD NO READER. `failed` is written on every run —
        # the other `rec["failed"]` in this file belongs to retention's receipt
        # — and the agent increments it in several places. Some of those have
        # their own finding (`malformed` here, a refused credential through
        # `halted_by`); the rest reach no other surface, and a guard that makes
        # one of them survivable also takes away the failed-step signal.
        faults = [x for x in (adoc.get("faults") or []) if isinstance(x, dict)]
        # Kinds that already have a finding of their own are left to it: one
        # cause, stated once.
        elsewhere = {"malformed", "credential"}
        mine = [x for x in faults if (x.get("kind") or "") not in elsewhere]
        n_failed = int(adoc.get("failed") or 0)
        # The count minus what other findings cover. A report written before
        # `faults` existed carries the count and no list, and going silent
        # because the DETAIL is missing would hide the fact.
        covered = len(faults) - len(mine)
        unexplained = max(n_failed - covered, 0) if faults else n_failed
        if unexplained:
            by_kind: dict[str, list[str]] = {}
            for x in mine:
                by_kind.setdefault(x.get("kind") or "unrecorded", []).append(
                    f"{x.get('project') or '?'} ({x.get('reason') or 'no reason recorded'})")
            detail = (f"{unexplained} project(s) the agent took were left "
                      f"uninterpreted, and their deltas stay unconsumed for the "
                      f"next run. ")
            if by_kind:
                detail += "; ".join(
                    f"{kind}: {listed(rows, 2)}" for kind, rows in sorted(by_kind.items()))
                detail += (". A `store` fault is the store's health, not a "
                           "model's; `model-unavailable` means the whole chain "
                           "refused; `ledger` means the write broke the store's "
                           "own rules.")
            else:
                detail += ("Which projects, and why, are not recorded — this "
                           "report predates the reasons being kept.")
            out.append({
                "type": "interpretation.faults", "subject": "agent:observer",
                "severity": "warning",
                "title": f"the agent could not interpret {unexplained} project(s) "
                         f"it took",
                "detail": clipped(detail, 900),
                "action": ("read the kinds above: a store fault needs "
                           "`PRAGMA integrity_check`, a chain fault needs "
                           "`./observatory.py chain`, a ledger refusal needs the "
                           "row it refused"),
                "evidence": ["store/raw/agent.json#faults",
                             "store/raw/agent.json#failed"]})
        if bad:
            out.append({
                "type": "interpretation.malformed", "subject": "agent:observer",
                "severity": "warning",
                "title": f"{bad} answer(s) contradicted themselves and were not recorded",
                "detail": "The model said a change was worth recording and gave no "
                          "interpretation. The schema cannot express that rule — "
                          "`strict` structured outputs have no `if`/`then` — so the "
                          "loop refuses the answer and leaves the deltas unconsumed "
                          "for a later run.",
                "action": "if it repeats, the chain's first model is the suspect: "
                          "`./observatory.py chain` shows the order",
                "evidence": ["store/raw/agent.json#malformed"]})
        if mute:
            out.append({
                "type": "interpretation.unreasoned", "subject": "agent:observer",
                "severity": "info",
                "title": f"{mute} decline(s) gave no reason",
                "detail": "The change was consumed as needing no note, and nothing "
                          "says why — so nothing will revisit it. The judgement is "
                          "probably right (a mechanical bump), but a decline with no "
                          "reason is not a record of one.",
                "action": "no action if occasional; if most declines are silent the "
                          "prompt's `why_not` rule is not landing",
                "evidence": ["store/raw/agent.json#unreasoned"]})

        # A RECORD ITS READER CANNOT READ. The third shape of a bad answer, and
        # the one that is invisible in a count of successes: the run reports
        # `recorded`, the row is well-formed and carries a reason, and the
        # sentence is in a language nobody who reads this ledger reads.
        # Measured 2026-09-07 across 112 waiting records: one Chinese, two
        # Russian, every other record English. It occupies the review
        # queue and cannot be judged, which is why it is a finding and not only
        # a line on stderr.
        alien = int(adoc.get("not_english") or 0)
        if alien:
            out.append({
                "type": "interpretation.not_english", "subject": "agent:observer",
                "severity": "info",
                "title": f"{alien} conclusion(s) were written in another language",
                "detail": "The ledger sits beside English documentation, is "
                          "committed into an English repository and is mirrored into "
                          "an English wiki, and the system prompt says so with its "
                          "reason. A prompt is advice to a model, so the outcome is "
                          "measured: these were recorded anyway — the content may be "
                          "right — and counted, because a conclusion its reader "
                          "cannot read cannot be promoted or rejected.",
                "action": "no action if occasional; if it recurs the model at the "
                          "head of the chain does not follow the instruction and "
                          "`agent/models.json` is where the chain is ordered",
                "evidence": ["store/raw/agent.json#not_english"]})

    # A NOTE ABOUT A PROJECT THAT NO LONGER EXISTS. `observatory_record` accepts
    # any `project_id`, deliberately: an agent may observe something about a
    # folder the registry has not caught up with, and refusing would lose a real
    # observation. But ids that never resolve — a project retired, renamed or
    # dissolved after the note was written — need counting: a note keyed to a
    # missing subject never appears in that project's view, and without this
    # nothing anywhere would say so.
    #
    # One finding for the lot, listing the ids: the remedy is one decision about
    # a set — re-key them, or accept that they are history — not N identical
    # rows, which would be noise.
    try:
        known = {p["id"] for p in reg("projects.json").get("projects", [])}
    except (KeyError, TypeError):
        known = set()
    if known and paths.DB.is_file():
        try:
            c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
            try:
                ids = {r[0] for r in c.execute(
                    "SELECT DISTINCT project_id FROM ledger WHERE project_id IS NOT NULL")}
                counts = dict(c.execute(
                    "SELECT project_id, count(*) FROM ledger"
                    " WHERE project_id IS NOT NULL GROUP BY project_id"))
            finally:
                c.close()
        except sqlite3.Error:
            ids, counts = set(), {}
        # TWO DIFFERENT FACTS, and they were one finding. An id absent from the
        # registry is either a subject that is GONE — a typo, a dissolved
        # project — or one the registry can still point at: publishing a project
        # RENAMES it (`project:local-<folder>` becomes
        # `project:<owner>-<name>`), and the rows written before that keep the
        # old name. Measured 2026-09-07: two of the five "orphans" were exactly
        # that, both promoted during one session.
        followed = identity.former_index(
            reg("projects.json").get("projects", []))
        renamed = sorted(o for o in (ids - known) if o in followed)
        orphans = sorted(o for o in (ids - known) if o not in followed)
        if renamed:
            out.append({
                "type": "memory.followed_rename", "subject": "store:ledger",
                "severity": "info",
                "title": f"{len(renamed)} project id(s) in the ledger belong to a "
                         f"project that has since been published",
                "detail": clipped("; ".join(
                    f"{o} ({counts.get(o, 0)} row(s)) is now {followed[o]}"
                    for o in renamed))
                          + ". A project's id is derived from its publication "
                            "state, so giving it a remote renames it. Nothing is "
                            "lost: the per-project view resolves the former id, "
                            "so these rows appear where they belong. Recorded "
                            "because the store holds a name the registry no "
                            "longer uses.",
                "action": "nothing — or re-key them with `observatory_record` if "
                          "you would rather the store carried only current ids",
                "evidence": ["store:ledger.project_id", "registry:projects.json",
                             "identity.py#former_ids"]})
        if orphans:
            out.append({
                "type": "memory.orphan_subject", "subject": "store:ledger",
                "severity": "info",
                "title": f"{len(orphans)} project id(s) in the ledger are not in "
                         f"the registry",
                "detail": clipped("; ".join(f"{o} ({counts.get(o, 0)} row(s))"
                                              for o in orphans))
                          + ". A note keyed to a subject the registry does not "
                            "hold never appears in that project's view, and "
                            "no unambiguous project claims these names — a rename this system "
                            "can follow is reported separately. The id may be a "
                            "typo, or the project may have been dissolved since "
                            "the note was written.",
                "action": "review project/folder mappings, then re-key with `observatory_record` (memory_id plus "
                          "expected_revision), or accept them as history",
                "evidence": ["store:ledger.project_id", "registry:projects.json"]})

    # IDENTITY AMBIGUITIES from the persisted identity map (docs/design/IDENTITY.md):
    # a project that shares strong anchors with two recorded projects, or that
    # an override points at an id another project already holds, got a NEW id.
    # Nothing was guessed; the operator decides with identity_overrides.json.
    _idmap = paths.REGISTRY / "identity.json"
    try:
        _amb = json.loads(_idmap.read_text()).get("ambiguities") or [] if _idmap.is_file() else []
    except (OSError, ValueError):
        _amb = []
        out.append({"type": "identity.unreadable", "subject": "registry:identity.json", "severity": "warning",
                    "title": "the identity map could not be read",
                    "detail": "Project ids are resolved from registry/identity.json; an unreadable map stops "
                              "the emit step so ids are not re-minted.",
                    "action": "restore registry/identity.json from the registry history",
                    "evidence": ["registry:identity.json"]})
    for a_ in _amb:
        out.append({"type": "identity.ambiguous", "subject": f"key:{a_.get('key')}", "severity": "warning",
                    "title": f"project key {a_.get('key')!r} could not be matched to one recorded project",
                    "detail": f"{a_.get('reason')}: {', '.join(a_.get('candidates') or [])}. It was given a new id "
                              "instead of borrowing another project's history.",
                    "action": "if it is a rename, pin it in config/identity_overrides.json to the id it should keep",
                    "evidence": ["registry:identity.json"]})

    # A PLUGIN NOBODY IS TOLD ABOUT. The plugin seam exists so a new analytics
    # source is two files in `plugins/` and no edit anywhere else — and it had no
    # channel out: `run_plugins.main()` returned 0 on every path (measured
    # 2026-09-07, `grep 'return 1'` found nothing) and printed into a log read on
    # no schedule. A plugin crashing on every tick for a month was invisible.
     
    # Graded by the runner's own four words, because they mean different things:
    # `broken` needs somebody, `waiting` is a state the operator may have
    # chosen, `not_due` is the healthy steady state and raises nothing.
    plug = paths.SCRATCH / "plugins.json"
    if plug.is_file():
        try:
            pdoc = json.loads(plug.read_text(encoding="utf-8"))
        except ValueError:
            pdoc = {}
        for p in pdoc.get("plugins") or []:
            cls = p.get("classification")
            refused = p.get("refused") or []
            problems = p.get("manifest_problems") or []
            if cls == "broken" or problems:
                out.append({
                    "type": "plugin.broken", "subject": f"plugin:{p['id']}",
                    "severity": "warning",
                    "title": f"the {p['id']} plugin is not producing measurements",
                    "detail": clipped((p.get("skipped") or "") + " " +
                                      ("; ".join(problems) if problems else ""))
                              + (f" Last measurement: {p['last_at']}."
                                 if p.get("last_at") else
                                 " It has never written a measurement."),
                    "action": f"./observatory.py plugins --only {p['id']} --force, "
                              f"and `--check` for the manifest",
                    "evidence": ["store/raw/plugins.json#plugins"]})
            elif refused:
                # REFUSED IS NOT SKIPPED. The plugin ran and wrote rows the
                # runner rejected — an undeclared metric, an unknown project, a
                # timestamp that is not UTC Z. The measurements are silently
                # absent rather than wrong, which is the harder failure to see.
                out.append({
                    "type": "plugin.refused", "subject": f"plugin:{p['id']}",
                    "severity": "warning",
                    "title": f"{len(refused)} row(s) from {p['id']} were refused",
                    "detail": clipped(listed(refused, 4, sep="; ")) + ". A refused row is "
                              "not written at all, so the metric is missing rather "
                              "than wrong — and the plugin exited successfully.",
                    "action": f"fix the plugin or declare what it writes in "
                              f"plugins/{p['id']}.json",
                    "evidence": ["store/raw/plugins.json#plugins"]})
            elif cls == "stale":
                # THE SERIES STOPPED, and `not_due` used to say this was fine.
                # A plugin can be installed, healthy, exiting zero and skipping
                # every tick while its newest sample gets older — which is
                # exactly how a metric layer goes quiet without anyone noticing
                #. The age is in PERIODS so one number reads the same
                # for a daily plugin and an hourly one.
                age = p.get("seriesAgePeriods")
                out.append({
                    "type": "plugin.stale", "subject": f"plugin:{p['id']}",
                    "severity": "warning",
                    "title": (f"{p['id']}'s newest measurement is from "
                              f"{p.get('last_at')}"),
                    "detail": (f"that is {age:.1f} cadence period(s) ago at "
                               f"{p.get('every_hours')}h — the plugin is installed "
                               f"and exits cleanly, so the series has simply stopped "
                               f"growing: the machine was asleep, the tick failed, or "
                               f"the sample the period wanted was never taken. "
                               f"Anything reading the metric is reading the past."
                               if age is not None else
                               "the age of the series could not be computed"),
                    "action": f"./observatory.py plugins --only {p['id']} --force",
                    "evidence": ["store/raw/plugins.json#plugins"]})
            elif cls == "waiting":
                out.append({
                    "type": "plugin.waiting", "subject": f"plugin:{p['id']}",
                    "severity": "info",
                    "title": f"the {p['id']} plugin has never run: {p.get('skipped')}",
                    "detail": "A requirement is unmet. This is a state rather than "
                              "a defect — the credential may not exist on purpose — "
                              "but until it is met the plugin measures nothing.",
                    "action": "provide the requirement, or remove the plugin so the "
                              "estate stops expecting its metric",
                    "evidence": ["store/raw/plugins.json#plugins"]})

    # A FROZEN WEEK THAT CAN NEVER BE COMPLETED. `project_week` is the only
    # permanent statistic here, and when it counted commits alone a week worked
    # on without a commit was a zero. The freeze rule forbids rewriting a week
    # once its events age out, which means each such week is not merely wrong
    # but unfixable. Read from the rollup's own receipt rather than from the
    # store, because the finding must be raisable from a registry sandbox with
    # no database at all.
    roll = paths.SCRATCH / "rollup.json"
    if roll.is_file():
        try:
            rr = json.loads(roll.read_text(encoding="utf-8"))
        except ValueError:
            rr = {}
        gap = rr.get("frozen_without_sessions") or 0
        if gap:
            out.append({
                "type": "rollup.frozen_incomplete", "subject": "store:project_week",
                "severity": "warning",
                "title": f"{gap} frozen week(s) carry no session figure and never can",
                "detail": "Those weeks were frozen while the rollup counted commits "
                          "only, and the events they were computed from have aged "
                          "out. The rows are not wrong about commits; they are "
                          "silent about work that left no commit, and the freeze "
                          "rule correctly refuses to rewrite them from data that "
                          "is gone.",
                "action": "nothing can restore them; treat weeks before this "
                          "boundary as a commit-only series",
                "evidence": ["store/raw/rollup.json#frozen_without_sessions"]})

    # AN ERASURE WHOSE BYTES ARE STILL IN THE FILE. `retention.py apply` returns
    # non-zero for this, so `tick.step_failed` grades it critical too — but that
    # finding says "a step failed" and this one says WHICH guarantee is open and
    # what reopens it. The receipt is read rather than the log, because the log
    # is not read on a schedule and the whole point of persisting it was that the
    # attestation should outlive the run.
    #
    # Only a measured NEGATIVE is raised. An absent receipt means retention has
    # not run in this checkout — true of every fresh clone — and raising it would
    # put a critical finding in front of anyone who cloned the repository.
    ret = paths.SCRATCH / "retention.json"
    if ret.is_file():
        try:
            rec = json.loads(ret.read_text(encoding="utf-8"))
        except ValueError:
            rec = {}
        scrubbed = (rec.get("scrub") or {}).get("scrubbed")
        if scrubbed is False:
            # TWO STATES, NOT ONE. `failed` means the pass never completed, so
            # nothing was erased; without it the rows went and the file kept
            # their text. A single title asserting "rows were erased" would be
            # false in the first case — a finding that misstates what happened
            # is worse than none, because it is acted on.
            broke = rec.get("failed")
            out.append({
                "type": "erasure.not_scrubbed", "subject": "store:observatory.db",
                "severity": "critical",
                "title": ("retention did not complete, so nothing was erased"
                          if broke else
                          "rows were erased but the file was not scrubbed"),
                "detail": clipped(rec["scrub"].get("detail") or "") +
                          (" Measured 2026-09-07: a purge reporting `status: "
                           "purged` and `tombstoned_rows_remaining: 0` left the "
                           "erased text in the file, through a WAL checkpoint, "
                           "until a VACUUM ran." if not broke else ""),
                "action": "re-run `./observatory.py retention-apply` when nothing "
                          "else is writing to the store",
                "evidence": ["store/raw/retention.json#scrub"]})

    # WHAT THE MERGE COULD NOT MEASURE. It is the collector every other module
    # reads, and without a degradation channel a missing `gh` made the transfer
    # check answer "not moved" for every address, which put phantom
    # repositories and a phantom project into the model in silence.
    out += merge_findings(degradations.collector("model.json"))

    # EVERY OTHER COLLECTOR'S OWN WORDS, and until 2026-09-08 four of six had
    # nowhere to say them. `model.json` reached the board through the rule above;
    # `bitbucket.json` and `domains_live.json` reached the WIRE through
    # `survey._collector_degradation`; `sessions.json` and `local.json` reached
    # nobody — and `sessions.json` held a live degradation at that moment. A
    # collector could report that claude-mem's store was unreadable, or that its
    # shape had moved, and the estate would lose the session half of its
    # activity in silence: the state `collectors/scan_sessions.py` was written
    # to prevent, reached by the back door.
    #
    # INFO, and the choice is deliberate. The collector's own reason is the whole
    # content, and six of the seven live ones are RDAP saying it has no record
    # for a TLD outside its system — structural, explained, and not the
    # operator's to fix. A warning there would teach the reader to skip the row
    # that matters. Where a degradation IS urgent it has its own rule with its
    # own remedy: `model.degraded` is a warning, and a missing Bitbucket
    # credential is an operator decision already recorded.
    #
    # ONE ROW PER RECEIPT, not per entry: six RDAP 404s are one story about one
    # collector, and six rows would be six decisions where there is none.
    for receipt, rows in sorted(degradations.every_collector().items()):
        pairs = sorted({f"{d.get('source', '?')} — {str(d.get('reason', '')).strip()}"
                        for d in rows if isinstance(d, dict)})
        shown = pairs[:DEGRADED_LISTED]
        rest = len(pairs) - len(shown)
        out.append({
            "type": "collector.degraded", "subject": f"collector:{receipt}",
            "severity": "info",
            "title": f"{receipt} reports {len(rows)} source(s) it could not measure",
            "detail": ("A collector that cannot read a source says so in its own "
                       "`degraded` list rather than returning an empty result — "
                       "AGENTS.md rule 7 — and this row is what carries that to a "
                       "person. "
                       + clipped("; ".join(shown), 900)
                       + (f" … and {rest} more source(s) not listed" if rest > 0 else "")
                       + ". Each reason is the collector's own; nothing here "
                         "judges whether it is worth acting on."),
            "action": f"read `store/raw/{receipt}` for the full list, then fix "
                      f"the source or accept the gap — the reason names which",
            "evidence": [f"store/raw/{receipt}#degraded"]})

    if unread_status:
        shown = sorted(set(unread_status))
        out.append({
            "type": "domain.hold_unknown", "subject": "collector:rdap",
            "severity": "warning",
            "title": f"whether {len(shown)} domain(s) are on registrar hold could "
                     f"not be read",
            "detail": ("RDAP answered for them and its reply carried no `status` "
                       "array, so the hold check has nothing to read — which is "
                       "not the same as 'not on hold'. "
                       + listed(shown, 8)
                       + ". Measured 2026-09-08, this happened for none of 49 "
                         "domains, so it is a key that went missing rather than a "
                         "state this estate is in; RFC 9083 does make `status` "
                         "optional, so one TLD omitting it is possible and this "
                         "row names which."),
            "action": "`.venv/bin/python collectors/scan_domains.py "
                      "store/raw/domains_live.json` and read the `degraded` list; "
                      "if RDAP renamed the field, `collectors/scan_domains.py` "
                      "reads `d.get(\"status\")`",
            "evidence": ["store/raw/domains_live.json#degraded"]})

    for name, info in sorted(held.items()):
        darkened = sorted(set(info["hosts"]))
        out.append({
            "type": "domain.hold", "subject": f"domain:{name}", "severity": "critical",
            "title": f"{name} is on registrar hold",
            # THE PRESSURE, WHICH THE SAME RECORD ALREADY HELD. A hold with eight
            # months of registration left and one expiring next week are the same
            # sentence and different decisions, and the expiry sat unread in the
            # very object this row quotes its statuses from.
            "detail": f"RDAP status: {', '.join(info['statuses'])}. The registrar has "
                      f"withdrawn it from DNS, which darkens "
                      f"{', '.join(darkened)}." + _hold_pressure(info.get("expires_on")),
            "action": "settle it with the registrar, or stop publishing the name",
            "evidence": [f"registry:domain-liveness.json#{h}" for h in darkened]})

    for p in reg("projects.json").get("projects", []):
        for s in p.get("sites", []):
            h = hosts.get(s["host"])
            if h and h.get("resolves") is False:
                # THE CAUSE, IF THE ESTATE HAS ALREADY MEASURED IT. Several
                # findings can stand as separate criticals for ONE cause: a
                # parent domain on registrar hold (a deliberate hold rather than
                # a lapse, when its expiry is years away) is withdrawn from DNS,
                # which darkens every host under it. Symptoms that tell the
                # operator to "restore the host" ask for what they CANNOT do
                # while the registrar holds the parent — a remedy its own cause
                # makes impossible is worse than no remedy, and impossible
                # criticals devalue the real items beside them.
                #
                # Matched by suffix against the domains THIS ESTATE has measured as
                # held — no public-suffix list, no guess: `held` comes from the
                # estate's own RDAP scan.
                parent = next((d for d in held if s["host"].endswith("." + d)), None)
                out.append({
                    "type": "site.dead", "subject": p["id"],
                    # A SYMPTOM OF A KNOWN CAUSE IS NOT INDEPENDENTLY CRITICAL.
                    # It stays a finding — the dashboard shows findings per
                    # project, and a project whose site is dark must not look
                    # clean — but the operator has one thing to do, not three.
                    "severity": "warning" if parent else "critical",
                    "title": f"{p['name']} publishes {s['host']}, which is dark",
                    "detail": ("The registry presents this host as the project's "
                               "site and it resolves nowhere — a published claim "
                               "that is currently false."
                               + _dark_for(live.get("hosts") or [], s["host"])
                               + (f" The cause is already measured: {parent} is on "
                                  f"registrar hold, so every host under it is dark "
                                  f"and this one cannot be restored until that is "
                                  f"settled." if parent else "")),
                    "action": (f"settle the hold on {parent} first — this host "
                               f"cannot be restored while it stands; or remove the "
                               f"site from the project" if parent else
                               "restore the host, or remove the site from the project"),
                    "evidence": [f"registry:projects.json#{p['id']}",
                                 f"registry:domain-liveness.json#{s['host']}"]})

    # WHOSE REPOSITORY IT IS, from the project that implements it. Used for ONE
    # sentence and no policy: for an `external` clone — a read-only copy of
    # somebody else's repository — "the remote has moved and nothing here is at
    # risk" is the resting state rather than news. An ownership POLICY would
    # suppress rows; this suppresses nothing and moves no severity.
    #
    # No relation means no ownership, and none is guessed: crediting a
    # third-party clone to the estate would misattribute somebody else's work.
    owner_of_repo = {rel["to"]: rel["from"]
                     for rel in reg("relations.json").get("relations", [])
                     if rel.get("type") == "implemented_by"}
    ownership_of_project = {p["id"]: p.get("ownership")
                            for p in reg("projects.json").get("projects", [])}
    # A PROJECT NOTHING CAN WATCH. `related` is every project a repository
    # relation names, whatever the relation's type: a project pointed at by any
    # of them is observable through it, and narrowing to `implemented_by` here
    # would call a project blind because its link is worded differently
    #.
    out += declared_alive_measured_dead(reg("projects.json").get("projects", []))
    out += unobservable_projects(
        reg("projects.json").get("projects", []),
        {rel["from"] for rel in reg("relations.json").get("relations", [])
         if str(rel.get("to", "")).startswith("repository:")})

    _stale_names: list[tuple[str, str | None]] = []
    for r in reg("repositories.json").get("repositories", []):
        loc = r.get("local") or {}
        state = loc.get("sync") or ""
        if not state or state in SYNC_SILENT:
            continue
        own = ownership_of_project.get(owner_of_repo.get(r["id"]))
        nwo = r["id"].split(":", 1)[1]
        # TWO CLOCKS, AND THE ROW HELD ONE. What the remote has is asked at most
        # every six hours (about ninety network round trips); how far ahead this
        # checkout is now is a local `rev-list` and is recounted every tick. The
        # sentence printed one date for both, so a figure three hours old read
        # as today's — measured on this very repository: 47 against git's 61
        #. The remote's instant is quoted from the scan's own report
        # rather than stored per repository, because a second-resolution stamp on
        # 174 rows is what made `git diff` never empty.
        where = (f"(the remote was last asked {REMOTE_ASKED}, "
                 f"branch {loc.get('checked_out_branch') or '?'})")
        rule = SYNC_FINDINGS.get(state)
        if rule is None:
            # NOT A `continue`. An unlisted state reaching this loop is how
            # `ahead` came to mean "say nothing" — nobody judged it benign,
            # nobody listed it, and both surfaces skipped it in silence. A fault
            # this code can see belongs in the findings.
            out.append({
                "type": "clone.unknown-state", "subject": r["id"],
                "severity": "warning",
                "title": f"{nwo} reports a clone state this system cannot read",
                "detail": f"the collector recorded sync={state!r}, which neither "
                          f"the findings table nor the dashboard declares, so "
                          f"whatever it means about this clone is reaching no "
                          f"surface {where}.",
                "action": "declare the state in tools/build_findings.py and the "
                          "dashboard's chip map, or stop emitting it",
                "evidence": [f"registry:repositories.json#{r['id']}",
                             "collectors/scan_remotes.py#STATES"]})
            continue
        sev, what, why, act = rule
        # ONLY `stale`, and only for a third-party clone. A branch that exists on
        # no remote is work at risk whoever owns the upstream, and `ahead` is the
        # same fact from the other end — neither is softened here.
        if state == "stale" and own == "external":
            why += (", and this is a copy of somebody else's repository: falling "
                    "behind upstream is expected of a copy rather than news")
        # `stale` IS COLLECTED, NOT EMITTED. Fourteen of them existed on
        # 2026-09-07, every one `info`, every one saying in its own detail that
        # "nothing here is at risk", and every one already a `sync=stale` chip on
        # its project's row. The page shows forty findings and names how many it
        # omits, so those fourteen were spending fourteen visible
        # slots on rows whose action — "pull when you next work here" — asks for
        # no decision, pushing fourteen rows that DO ask into the tail. One
        # aggregate row keeps the fact and returns the slots.
        if state == "stale":
            _stale_names.append((nwo, own))
            continue
        # HOW MUCH AND HOW OLD, when the probe measured it. Ten rows asked the
        # operator to push or delete a branch while withholding both, so one
        # holding a single typo from an hour ago read exactly like one holding
        # forty-four commits of a feature branch from March. Absent
        # rather than zero when it could not be counted — a state asserting work
        # on this disk and counting none of it is a contradiction, not a
        # reassurance.
        stake = ""
        n = loc.get("unpushed")
        if isinstance(n, int) and n > 0:
            stake = (f" {n} commit(s) are on no remote"
                     + (", counted just now"
                        if loc.get("unpushed_recounted")
                        else ", as counted by that remote scan and not since")
                     + (f", the newest made {loc['unpushed_newest_on']}"
                        if loc.get("unpushed_newest_on") else "")
                     + ".")
        elif state == "local-only-branch":
            # NO COMMIT IS EXCLUSIVE, and the row must say so. The branch is
            # unpublished, which is what the state means — but two of them were
            # measured with every commit already reachable from some remote ref,
            # and for those the sentence "work here survives only this disk" is
            # simply false. Measured 2026-09-08: 2 of 6.
            stake = (" No commit here is exclusive to this disk, though: every one "
                     "is reachable from some remote ref, so what is unpublished is "
                     "the branch NAME rather than any work.")
        out.append({
            "type": f"clone.{state}", "subject": r["id"], "severity": sev,
            "title": f"{nwo} {what}",
            "detail": f"{why} {where}.{stake}",
            "action": act,
            "evidence": [f"registry:repositories.json#{r['id']}"]})
    out += stale_clones(_stale_names)

    # EFFORT IN, NOTHING OUT, AND STOPPED. A portfolio question this system
    # exists to support, and nothing reported it: a tier that is computed,
    # validated and drawn on the dashboard is never turned into a question
    # unless a rule here reads it.
    #
    # Crossing "ever shipped" with the activity tier gives a small table; the
    # quiet cells of the never-shipped row are the ones reported. The ACTIVE
    # cell is deliberately not reported: a large count of active, unreleased
    # projects is the shape of how an operator works rather than a question.
    # A stricter conjunction — never released PLUS active PLUS unpushed work —
    # fails because `activity_tier` is derived from the very commits that make
    # a clone ahead.
    quiet_tiers = {t["id"] for t in activity.tiers()
                   if t["id"] != activity.tiers()[0]["id"]}
    shipped_none: dict[str, dict] = {}
    # WHICH metric answers "has this ever shipped", asked of the MANIFESTS. The
    # first version read `metric = 'release.tags'` here, and both
    # `tests/test_metric_series.py` and the plugin's own suite caught it in the
    # same run: a core file naming a plugin's metric is the six-file problem
    # this seam exists to remove. It took one iteration to reintroduce, after a
    # manifest I wrote had argued no role was needed because "nothing in this
    # repository asks how long since a release" — true when written, false the
    # moment this finding existed.
    release_metric = metric_for_role(RELEASE_COUNT_ROLE)
    # `paths.DB`, not the local `db` — that name is bound further down in this
    # function, and reading it here made the whole block an UnboundLocalError
    # rather than a finding.
    if release_metric and paths.DB.is_file():
        try:
            c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
            try:
                latest = c.execute(
                    "SELECT project_id, value FROM metrics"
                    " WHERE metric = ? AND at ="
                    "   (SELECT MAX(at) FROM metrics WHERE metric = ?)",
                    (release_metric, release_metric)).fetchall()
            finally:
                c.close()
        except sqlite3.Error:
            latest = []
        # A project with NO reading is absent, not a zero: the plugin measures
        # only projects with a checkout here, so counting the unmeasured as
        # unreleased would report a fact nobody established.
        never = {pid for pid, value in latest if (value or 0) == 0}
        for p in reg("projects.json").get("projects", []):
            if p["id"] not in never:
                continue
            if (p.get("lifecycle") or "") == "archived":
                # The operator has already decided. A finding that raises a
                # settled question again teaches that deciding does nothing.
                continue
            if (p.get("activity_tier") or "") in quiet_tiers:
                shipped_none[p["id"]] = p
    if shipped_none:
        # QUIETEST FIRST, by the date the tier is derived from. A list ordered by
        # id would put the decision that has waited longest anywhere.
        order = sorted(shipped_none.values(),
                       key=lambda p: (p.get("last_activity_on") or "", p["id"]))
        rows = [f"{p['id']} ({p.get('activity_tier')}, last "
                f"{p.get('last_activity_on') or 'unrecorded'})" for p in order]
        out.append({
            "type": "portfolio.unreleased_and_quiet", "subject": "estate",
            # INFO. A standing portfolio state that cannot be cleared quickly
            # becomes furniture at warning level — the same reasoning that
            # keeps stale clones at info.
            "severity": "info",
            "title": f"{len(order)} project(s) have released nothing and stopped",
            "detail": clipped(
                "Effort went in, no tag came out, and the work has stopped: "
                + listed(rows, 5)
                + f". `{release_metric}` is 0 for each and their activity tier "
                  "is past "
                  "`active`. Projects still being worked on are NOT counted — 41 "
                  "of 90 are active and unreleased, which is the shape of the "
                  "portfolio rather than a question.", 800),
            "action": ("decide per project: archive it, or pick it back up — "
                       "`lifecycle: archived` in the registry stops it being "
                       "asked about again"),
            "evidence": ["registry:projects.json#activity_tier",
                         f"store:metrics#{release_metric}"]})

    # THE STORE'S OWN STRUCTURE. Without a check, a corrupt store file — one
    # with no SQLite header at all — surfaces as a traceback out of
    # `project.timeline`, which is to say after it has already broken a run.
    # `tools/check_store.py` runs early in the tick and this carries its
    # verdict.
    #
    # Deliberately NOT the answer to a transient `database disk image is
    # malformed`: an image that reads `ok` minutes later could not have been
    # caught by any periodic check, and the per-project fault record is what
    # reports that class.
    ic = paths.SCRATCH / "integrity.json"
    if ic.is_file():
        try:
            iv = json.loads(ic.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  integrity.json unreadable: {exc}", file=sys.stderr)
            iv = {}
        verdict = str(iv.get("verdict") or "")
        age_h = hours_since(iv.get("ran_at") or "")
        if verdict in ("damaged", "unopenable"):
            # CRITICAL, and the only finding in this file that says so about the
            # store: `registry/ledger.jsonl` is "the only copy outside the
            # store", and everything else here is derived and rebuildable.
            said = ("SQLite reported problems in the file"
                    if verdict == "damaged" else
                    "the file would not open as a database at all")
            out.append({
                "type": "store.integrity", "subject": "store:observatory.db",
                "severity": "critical",
                "title": f"the store failed its integrity check ({verdict})",
                "detail": clipped(
                    f"{said}: {iv.get('detail') or 'no detail recorded'}. "
                    f"Checked with PRAGMA {iv.get('pragma') or '?'} over "
                    f"{int(iv.get('bytes') or 0) / 1024 ** 2:.1f} MiB.", 700),
                "action": ("stop the tick before it writes more, then rebuild "
                           "from `registry/ledger.jsonl` — the only copy of the "
                           "ledger outside this file — and the newest "
                           "store/observatory.db.backup-* beside it"),
                "evidence": ["store/raw/integrity.json",
                             "tools/check_store.py"]})
        elif verdict == "ok" and age_h is not None and age_h >= INTEGRITY_STALE_HOURS:
            # A VERDICT IS ABOUT WHEN IT WAS TAKEN. `ok` from three days ago says
            # the store was sound three days ago, and presenting it as current
            # would assert an old reading as a fresh one.
            out.append({
                "type": "store.integrity_stale", "subject": "store:observatory.db",
                "severity": "info",
                "title": f"the store's last integrity check was "
                         f"{int(age_h)}h ago",
                "detail": (f"It said `ok`, and that is a statement about "
                           f"{iv.get('ran_at')} rather than about now. The check "
                           f"runs early in the tick, so a verdict this old means "
                           f"the tick has not completed since — which "
                           f"`tick.standing_down` and `scan.stale` measure from "
                           f"their own angles."),
                "action": "`./observatory.py integrity` takes a fresh one in "
                          "about a fifth of a second",
                "evidence": ["store/raw/integrity.json"]})

    # A CYCLE THE ESTATE DID NOT RUN. The tick stands down when another run
    # holds the registry — correctly, "a skipped tick is cheaper than two
    # writers" — and the message goes to `store/logs/tick.log`, which nothing
    # reads on a schedule. Several skipped ticks in a row mean hours of stale
    # registry, findings and dashboard with every surface looking normal, and
    # the holder can be the operator's own `check` runs starving the estate's
    # schedule.
    tl = paths.SCRATCH / "tick-lease.json"
    if tl.is_file():
        try:
            lease = json.loads(tl.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  tick-lease.json unreadable: {exc}", file=sys.stderr)
            lease = {}
        skips = int(lease.get("consecutive_skips") or 0)
        # ONE skip is the design working — the next tick is thirty minutes away
        # and a single gap costs half an hour. Two means an hour, which is when
        # "the dashboard is current" stops being true.
        if skips >= TICK_SKIPS_BEFORE_WARNING:
            holder = str(lease.get("holder") or "another run")
            # NAMED, not guessed. `ga` is the gate's own tag and nothing else
            # uses it; any other holder is reported as the id it gave.
            who = ("a `./observatory.py check` run — the gate holds the registry "
                   f"for its whole duration ({holder})"
                   if holder.startswith("r-ga") else holder)
            stale = hours_since(lease.get("last_acquired_at") or "")
            age = (f"The last completed tick was {stale:.0f}h ago."
                   if stale is not None else
                   "When a tick last completed is not recorded.")
            out.append({
                "type": "tick.standing_down", "subject": "collector:tick",
                "severity": "warning",
                "title": f"the tick has skipped {skips} cycle(s) in a row",
                "detail": (f"It stood down because the registry was held by "
                           f"{who}. That is the correct choice — a skipped tick "
                           f"is cheaper than two writers — but the registry, the "
                           f"findings and this dashboard are only as fresh as the "
                           f"last completed cycle, and nothing else says so. "
                           f"{age}"),
                "action": ("run `./observatory.py tick` once the holder is done, "
                           "or space long `check` runs between ticks — the lease "
                           "is not the thing to weaken"),
                "evidence": ["store/raw/tick-lease.json",
                             "tools/tick_lease.py#record_outcome"]})

    # A KEY IN THE WRONG VARIABLE — a fault in the MACHINE, reported from a
    # receipt because the process that can see it is not the one that builds
    # findings. `OPENAI_API_KEY` is the embedding key, read only by that path,
    # and a scheduled tick inherits neither variable from the operator's shell.
    # A finding built from the tick's own environment would answer "is the
    # scheduler misconfigured" and answer `no` while the operator's shell is
    # wrong. So `./observatory.py key` writes the verdicts and this reads them.
    ks = paths.STATE / "key-shapes.json"
    if ks.is_file():
        try:
            obs = (json.loads(ks.read_text(encoding="utf-8")).get("observations")
                   or {})
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  key-shapes.json unreadable: {exc}", file=sys.stderr)
            obs = {}
        for env_name, o in sorted(obs.items()):
            if o.get("verdict") != "wrong":
                continue
            age_h = hours_since(o.get("seen_at") or "")
            days = None if age_h is None else age_h / 24.0
            # THE OBSERVATION'S OWN AGE. The step that could refute this is
            # operator-run by design, so it may not have run for days — and a
            # fortnight-old reading must not be asserted as current.
            when = ("when it was last looked at" if days is None else
                    f"{int(days)} day(s) ago" if days >= 1 else "today")
            hedge = ("" if days is not None and days < KEY_SHAPE_HEDGE_DAYS else
                     " It may already have been fixed — `./observatory.py key` "
                     "re-checks it and clears this.")
            out.append({
                "type": "env.key_misplaced", "subject": f"env:{env_name}",
                "severity": "warning",
                "title": f"${env_name} holds a key for a different provider",
                "detail": (f"{o.get('reason') or 'the value has the wrong shape'}"
                           f" — measured {when}. This system is unharmed: it sees "
                           f"the wrong shape, ignores the variable and falls "
                           f"through to the key file, which is why nothing here "
                           f"has broken. Every OTHER consumer of ${env_name} on "
                           f"this machine gets that key and a 401 that names "
                           f"nothing.{hedge}"),
                "action": f"unset ${env_name} where your shell exports it, or "
                          f"export the right provider's key there",
                "evidence": ["store/key-shapes.json",
                             "agent/providers.py#KEY_SHAPES"]})

    # THE OTHER CHECKOUTS, which the merge used to reduce to folder names.
    # `merge.py` refuses to let a worktree displace a real checkout and demotes
    # it — correctly, since a worktree is a directory an agent deletes when it is
    # done — but everything measured about it died there, and ten `codex/*`
    # branches of one project existed on no remote with nothing able to say so
    #. ONE finding per repository: ten rows for one agent's leftovers
    # is how a findings list stops being read.
    for r in reg("repositories.json").get("repositories", []):
        loc = r.get("local") or {}
        risky = [x for x in (loc.get("extra_checkouts") or [])
                 if (x.get("sync") or "") in AT_RISK]
        if not risky:
            continue
        nwo = r["id"].split(":", 1)[1]
        lines = "; ".join(
            f"{x.get('branch') or '?'} in {x.get('folder')} ({x.get('sync')})"
            for x in sorted(risky, key=lambda x: x.get("folder") or ""))
        worktrees = [x for x in risky if x.get("worktree_of")]
        out.append({
            "type": "worktree.unpushed", "subject": r["id"], "severity": "warning",
            "title": (f"{nwo} has {len(risky)} extra checkout"
                      f"{'' if len(risky) == 1 else 's'} holding work no remote has"),
            "detail": (f"{lines}. "
                       + ("These are git worktrees, so their commits live in the "
                          "parent's object database and removing the directory "
                          "does NOT lose them — but no remote has them, so the "
                          "disk is the only copy. "
                          if worktrees else
                          "No remote has this work, so the disk is the only copy. ")
                       + "The repository's own checkout is reported separately."),
            "action": ("push the branches worth keeping, then `git worktree "
                       "remove` the rest deliberately" if worktrees else
                       "push the branches worth keeping, or delete the checkouts "
                       "deliberately"),
            "evidence": [f"registry:repositories.json#{r['id']}"]})

    # A GIT REPOSITORY WITH NO REMOTE AT ALL — the strongest form of "this
    # survives only this disk", and it raised nothing. Findings iterate
    # `repositories.json`, and a checkout with no remote has no `owner/name` to
    # be keyed by, so `merge.py` records it as a project with
    # `local_only.unpublished` instead. Measured 2026-09-07: 219 commits across
    # three of them, all committed to that day.
    for p in reg("projects.json").get("projects", []):
        lo = p.get("local_only") or {}
        if not lo.get("unpublished"):
            continue
        commits = int(lo.get("commits") or 0)
        if commits < 1:
            continue                                                            
        dirty = int(lo.get("dirty") or 0)
        detail = (f"{commits} commit{'' if commits == 1 else 's'} on "
                  f"{lo.get('branch') or 'an unnamed branch'}, last on "
                  f"{lo.get('last_commit') or 'an unrecorded date'}, in "
                  f"{shorten(pathlib.Path(lo.get('path') or ''))}. There is no "
                  f"remote at all — not a branch waiting to be pushed, but a "
                  f"repository published nowhere, so this history exists on one "
                  f"disk.")
        if dirty:
            # A DIFFERENT LOSS. These are not committed even locally, so no
            # amount of pushing would have saved them; saying "push" over them
            # would be a remedy for the wrong problem.
            detail += (f" {dirty} uncommitted file{'' if dirty == 1 else 's'} "
                       f"beside it are not in any commit, local or otherwise.")
        out.append({
            "type": "repo.no_remote", "subject": p["id"], "severity": "warning",
            "title": f"{p.get('name') or p['id']} has {commits} commit"
                     f"{'' if commits == 1 else 's'} and no remote",
            "detail": detail,
            "action": "give it a remote and push, or record deliberately that "
                      "this one is meant to live on this disk only",
            "evidence": [f"registry:projects.json#{p['id']}"]})

    # `paths.DB`, not an inlined path — the fourth tool this session found with
    # the store hardcoded, so `OBSERVATORY_DB` could not point it at a fixture.
    # THE INTERPRETATION LAYER, STALLED. The agent leaves deltas unconsumed
    # whenever it cannot run — a missing credential, a reached ceiling, a model
    # that failed every attempt — and says so on stdout, which the tick swallows
    # into a log. Measured 2026-09-07: it had been halted since 01:30 by a
    # ceiling on a SHARED key another consumer had taken to 36.03 of 2.00, with
    # 77 deltas queued, and nothing outside `tick.log` said a word. The
    # collectors are unaffected, so the estate's FACTS stay current while its
    # narrative silently stops — which is why this is a warning rather than a
    # critical, and why it must be said at all.
    agentf = paths.SCRATCH / "agent.json"
    if agentf.is_file():
        try:
            adoc = json.loads(agentf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  agent.json unreadable: {exc}", file=sys.stderr)
            adoc = {}
        waiting = adoc.get("unconsumed") or 0
        since = adoc.get("oldest_unconsumed_scan")
        # THE CAUSE, READ HERE TOO. The dark-site rule above sets the precedent:
        # a symptom whose cause is measured names it, changes grade, and REPLACES
        # its action — "settle the hold first, this host cannot be restored while
        # it stands". The same applies to a spent key: offering "raise the
        # ceiling in agent/models.json" while the provider returns 402 on the
        # key's own limit is a remedy no ceiling in this repository can deliver.
        # A remedy its cause forbids costs the reader their attention and
        # returns them to the same wall.
        key_spent, key_reset = False, None
        _ku = paths.STATE / "key-usage.json"
        if _ku.is_file():
            try:
                _kd = json.loads(_ku.read_text(encoding="utf-8"))
            except ValueError:
                _kd = {}
            _rem = _kd.get("limit_remaining")
            key_spent = _rem is not None and float(_rem) <= 0
            key_reset = reset_date(_kd.get("limit_reset"), measured_at(_kd))
        stale = False
        if waiting and since:
            try:
                oldest = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)
                stale = (datetime.now(timezone.utc) - oldest) > timedelta(
                    hours=INTERPRETATION_LAG_HOURS)
            except ValueError:
                stale = True                                                             
        if stale:
            out.append({
                "type": "interpretation.halted", "subject": "agent",
                "severity": "warning",
                "title": f"{waiting} change(s) have waited over "
                         f"{INTERPRETATION_LAG_HOURS}h for the agent to interpret them",
                "detail": (adoc.get("halted_by")
                           or f"the last run recorded {adoc.get('recorded', 0)}, skipped "
                              f"{adoc.get('skipped', 0)} and failed {adoc.get('failed', 0)}")
                          + f". The collectors are unaffected — every FACT about the estate "
                            f"is current — but nothing is interpreting what moved. Oldest "
                            f"change seen {since}; last run {adoc.get('ran_at', 'unknown')}."
                          + _drain_cost(adoc)
                          + (" The cause is already measured: the shared key's own limit is "
                             "spent, reported as `wallet.shared_key`"
                             + (f", and it lifts on {key_reset}." if key_reset else
                                ", and the provider does not say when it resets.")
                             if key_spent else ""),
                "action": ((
                    "the key this system spends through is SPENT — the provider enforces "
                    "that limit and answers 402 whatever any ceiling in this repository "
                    "says, so raising one cannot help. Give this project its own key"
                    + (f", or leave it: the counter resets on {key_reset} and the queued "
                       f"deltas are interpreted then, oldest first" if key_reset else
                       ", or wait for the counter to reset — the provider does not say "
                       "when, so this system cannot either")
                    + ". Nothing is lost either way — the deltas stay queued."
                ) if key_spent else
                          "if a spend ceiling stopped it, remember the OpenRouter key is "
                          "shared with everything else on this machine: raise "
                          "the ceiling in agent/models.json deliberately, give this project "
                          "its own key, or wait for the counter to reset. Nothing is lost "
                          "either way — the deltas stay queued."),
                "evidence": ["store:raw/agent.json", "store:deltas"]})

    # A NOTIFICATION CHANNEL THAT COULD NOT DELIVER. `tools/notify_findings.py`
    # used to record a failed send as if it had happened — the dedup key is
    # `(kind, ref)`, so one failed `osascript` silenced a critical finding for
    # ever. It now records only what was delivered and writes the failure here,
    # because the alternative to a permanent silence is a retry every thirty
    # minutes that nobody hears either: the operator has to learn that the
    # channel is dead, not merely stop receiving.
    notif = paths.SCRATCH / "notify.json"
    if notif.is_file():
        try:
            ndoc = json.loads(notif.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  notify.json unreadable: {exc}", file=sys.stderr)
            ndoc = {}
        undelivered = ndoc.get("undelivered") or []
        if undelivered and not ndoc.get("delivered", True):
            worst = "critical" if any(u.get("severity") == "critical"
                                      for u in undelivered) else "warning"
            out.append({
                "type": "notify.channel_failing", "subject": "channel:notification-centre",
                "severity": worst,
                "title": f"{len(undelivered)} finding(s) could not be delivered to the "
                         f"notification centre",
                "detail": f"{ndoc.get('detail', 'the send failed')}. The usual cause is a "
                          f"launchd job with no Aqua session, which cannot reach the window "
                          f"server at all. The findings were NOT marked as notified, so a "
                          f"later run will try again — this notice is how you learn the "
                          f"channel is dead rather than merely quiet. "
                          f"Attempted {ndoc.get('attempted_at', 'unknown')}.",
                "action": "run `./observatory.py notify` from a terminal to see the error, "
                          "or read the dashboard instead — the findings are all there",
                "evidence": ["store:raw/notify.json", "registry:findings.json"]})

    # A STEP OF THE TICK THAT FAILED. Twelve of the tick's steps piped their
    # output through the logger and discarded the exit code (`set -o pipefail`
    # was on, so the value existed and was thrown away). One of them is
    # `store/retention.py apply`, whose non-zero exit means an erasure did not
    # complete — the contract's "completion blocks until every configured
    # backend attests" was true of the function and false of the system.
    tickf = paths.SCRATCH / "tick.json"
    if tickf.is_file():
        try:
            tdoc = json.loads(tickf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  tick.json unreadable: {exc}", file=sys.stderr)
            tdoc = {}
        for row in tdoc.get("failed_steps", []):
            name = row.get("step", "unknown")
            grave = SEVERE_STEPS.get(name)
            out.append({
                "type": "tick.step_failed", "subject": f"step:{name}",
                "severity": "critical" if grave else "warning",
                "title": f"the scheduled tick's `{name}` step exited {row.get('exit')}",
                "detail": (grave or
                           "The tick does not abort on a degradation, by design — "
                           "launchd would retry a state that needs reporting rather "
                           "than repeating. So a failed step is recorded here instead "
                           "of stopping the cycle.")
                          + f" Last tick finished {tdoc.get('finished_at', 'unknown')}.",
                "action": f"read the reason in store/logs/tick.log, then run "
                          f"`./observatory.py` for that step by hand to see it fail",
                "evidence": ["store:raw/tick.json", "store:logs/tick.log"]})

    # WORK GIT CAN NO LONGER PLACE. `tools/corroborate.py` refuses to promote
    # a `session` row whose recorded sha is gone or has fallen off its
    # branch, and its own docstring calls that "a finding, not a
    # corroboration" — while the refusal went to stdout and nowhere else.
    # The row then waits `proposed` until retention erases it at ninety
    # days, taking the only record of the vanished work with it.
    corr = paths.SCRATCH / "corroboration.json"
    if corr.is_file():
        try:
            cdoc = json.loads(corr.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  corroboration.json unreadable: {exc}", file=sys.stderr)
            cdoc = {}
        # A CONTRADICTION, and only that. This row carried both a witness saying
        # the work is gone and a question nobody could ask, because
        # `check_session` had two outcomes for three states — and every one of
        # the four rows on the board on 2026-09-08 was of the second kind: the
        # commit sat on `remotes/origin/<the very branch the row named>`, and
        # the local branch had merely been deleted after being pushed. The
        # unaskable bucket is the row below.
        for row in cdoc.get("refused", []):
            out.append({
                "type": "work.unverifiable",
                "subject": row.get("memory_id", "unknown"),
                "severity": "warning",
                "title": "recorded work git contradicts",
                "detail": f"{row.get('why', 'the check refused')}. A witness LOOKED and "
                          f"disagreed — this is not a branch that was tidied up, which "
                          f"is reported separately and promotes. The row stays "
                          f"`proposed` and cannot be corroborated again, so retention "
                          f"will erase it unless a person decides on it."
                          + _erasure_horizon(row),
                "action": "look at what happened to that commit, then promote or "
                          "reject the row with review.py from a terminal",
                "evidence": ["store:raw/corroboration.json", "store:ledger"]})
        for row in cdoc.get("unaskable", []):
            out.append({
                "type": "work.unwitnessed",
                "subject": row.get("memory_id", "unknown"),
                "severity": "info",
                "title": "recorded work no clone here can witness",
                "detail": f"{row.get('why', 'nothing could be asked')}. Nothing is known "
                          f"to be wrong with this record: no witness contradicted it, "
                          f"there was simply none to ask. It stays `proposed` for that "
                          f"reason alone, and retention erases a `proposed` row at "
                          f"ninety days — so the absence of a clone, not a judgement "
                          f"about the work, is what would delete it."
                          + _erasure_horizon(row),
                "action": "clone the repository and re-run `./observatory.py "
                          "corroborate`, or decide on the row with review.py from a "
                          "terminal",
                "evidence": ["store:raw/corroboration.json", "store:ledger"]})

    db = paths.DB
    if db.is_file():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            # RECORDS, not revisions. The first version counted rows and said
            # "43 are still proposed" while 28 records were: one memory had
            # nineteen revisions and was counted nineteen times. A queue length
            # that counts history is a number nobody can act on.
            n = c.execute(
                "SELECT COUNT(*) FROM ledger l JOIN (SELECT memory_id, MAX(revision) rev "
                "FROM ledger GROUP BY memory_id) m ON l.memory_id = m.memory_id "
                "AND l.revision = m.rev WHERE l.state = 'proposed'").fetchone()[0]

            # AND ITS SHAPE. A count says how long the queue is; the split says
            # whether it is worth an evening. Measured 2026-09-07: of 130
            # waiting records 107 rested on nothing but the working tree's own
            # rhythm — commits, dirty, clean — and 17 on something structural.
            # Read through `estate.conclusion_class`, the same rule
            # `tools/review.py digest` orders by, so the board and the CLI cannot
            # come to describe one queue two ways.
            shape: dict[str, int] = {}
            residue = residue_groups = 0
            try:
                waiting = [
                    {"memory_id": r[0], "project_id": r[1], "kind": r[2], "owner": r[3],
                     "created_at": r[4], "revision": r[5], "evidence_json": r[6]}
                    for r in c.execute(
                        "SELECT l.memory_id, l.project_id, l.kind, l.owner,"
                        "       l.created_at, l.revision, l.evidence_json"
                        " FROM ledger l JOIN ("
                        "  SELECT memory_id, MAX(revision) rev FROM ledger"
                        "  GROUP BY memory_id) m"
                        " ON l.memory_id = m.memory_id AND l.revision = m.rev"
                        " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
                        " WHERE t.memory_id IS NULL AND l.state = 'proposed'")]
                for w in waiting:
                    k = estate.conclusion_class(w["evidence_json"])
                    shape[k] = shape.get(k, 0) + 1
                # AND HOW MANY THE CORRECTED POLICY WOULD NOT HAVE MADE. Same
                # function the digest folds by, so the board cannot report a
                # different number from the CLI.
                _groups = estate.fold_groups(waiting)
                residue = sum(len(g["fold"]) for g in _groups)
                residue_groups = len(_groups)
            except sqlite3.Error as exc:
                print(f"  queue shape unreadable: {exc}", file=sys.stderr)

            # A COUNT is not the alarm; a DEADLINE is. "106 await review" reads as
            # housekeeping, and it read that way for as long as the number climbed.
            # "9 will be erased unreviewed in eleven days" is a decision with a date
            # on it — and erasure by timeout is what `tools/corroborate.py` calls
            # "not review" in its own docstring.
            soon = c.execute(
                "SELECT COUNT(*), MIN(l.created_at) FROM ledger l"
                " JOIN (SELECT memory_id, MAX(revision) rev FROM ledger GROUP BY memory_id) m"
                "   ON l.memory_id = m.memory_id AND l.revision = m.rev"
                " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
                " WHERE t.memory_id IS NULL AND l.state = 'proposed'"
                "   AND l.created_at < ?"
                # STRUCTURAL, as in `store/retention.py`'s own query: the
                # exempt owners are inlined rather than passed by a caller who
                # might forget. This query did forget, so a row retention will
                # never touch could be announced as "will be erased unreviewed
                # within 14 days" — a deadline that does not exist.
                f"   AND l.owner NOT IN ({','.join('?' * len(EXEMPT_OWNERS))})",
                # NOT `datetime('now', ...)`: SQLite renders `YYYY-MM-DD HH:MM:SS`
                # with a SPACE, the store writes `...T...Z`, and lexicographically
                # a space (0x20) sorts before `T` (0x54) — so the comparison was
                # almost never true and this finding silently never fired. Caught
                # by a planted row rather than in production.
                ((datetime.now(timezone.utc)
                - timedelta(days=max(REVIEW_HORIZON_DAYS - 14, 1))
                ).strftime("%Y-%m-%dT%H:%M:%SZ"), *EXEMPT_OWNERS)).fetchone()
            c.close()
        except sqlite3.Error as exc:
            # Named, not vanished. A handler here hid a closed-connection
            # bug twice in one sitting: the finding simply never appeared,
            # which is indistinguishable from a healthy queue.
            print(f"  ledger unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
            n, soon = 0, None
        if n >= REVIEW_BACKLOG:
            out.append({
                "type": "ledger.review_backlog", "subject": "ledger",
                "severity": "warning",
                "title": f"{n} ledger records await review",
                "detail": (f"{shape.get('structural', 0)} rest on a structural change "
                           f"— a project appearing or vanishing, an owner, a repository "
                           f"or a stack moving — and {shape.get('routine', 0)} on the "
                           f"working tree's own rhythm of commits and dirty files"
                           + (f"; {shape['unclassified']} cite evidence that cannot be "
                              f"classified" if shape.get("unclassified") else "")
                           + (f". {residue} of them are superseded readings of a day "
                              f"the one-per-day policy now corrects in place, in "
                              f"{residue_groups} group(s) — `review.py reject-group "
                              f"<project> <day>` folds one from a terminal, so that is "
                              f"{residue_groups} decisions rather than {residue}"
                              if residue else "")
                           + ". An automated writer cannot promote its own row, by "
                             "design. What a machine can re-check is promoted by "
                             "tools/corroborate.py; the rest carry evidence only a "
                             "person can weigh. This count is as of the last "
                             "change to the queue, and `digest` recounts at "
                             "print time — a difference of a few rows between "
                             "them is minutes, not disagreement. Retention "
                             "erases them at "
                             f"{REVIEW_HORIZON_DAYS} days whether or not anyone "
                             f"looked — except rows owned by "
                             f"{', '.join(EXEMPT_OWNERS)}, which it never erases "
                             f"because nothing could re-derive them."),
                # WHY THE DIGEST WILL SAY SOMETHING ELSE. This figure is as of
                # the last time the queue CHANGED — `built_at` is carried
                # forward when nothing moved, because a committed file must be a
                # function of the facts and not of the clock — while
                # `review.py digest` recounts from the store at print time. The
                # observer adds rows between ticks, so the two differ by
                # minutes, and a reader comparing them takes that for a
                # disagreement: measured 2026-09-08, they were 125 and 127 half
                # an hour apart and IDENTICAL when computed at one instant
                #. The stamp cannot go in this sentence — it would
                # make the document differ every tick and defeat the rule above.
                "action": "./observatory.py digest — the queue grouped by project, "
                          "structural first, with what retention will erase and when",
                "evidence": ["store:ledger"]})

        if soon and soon[0]:
            out.append({
                "type": "ledger.review_expiring", "subject": "ledger",
                "severity": "warning",
                "title": f"{soon[0]} conclusion(s) will be erased unreviewed within 14 days",
                "detail": "Retention tombstones a `proposed` row at "
                          f"{REVIEW_HORIZON_DAYS} days. These were written by an "
                          "automated reader that cannot promote its own work, and "
                          "nobody has looked. Erasure by timeout is not a decision.",
                "action": "./observatory.py digest, then review.py promote|reject "
                          "from a terminal",
                "evidence": ["store:ledger"]})


        # The projection's LAG, by age rather than by count. A count says nothing:
        # 500 rows that drain on the next tick are healthy, and one row stuck for
        # a week is not — it means every search since then answered from an index
        # missing it. The age is the ledger's `created_at`, because an append and
        # its outbox row are one transaction (`store/ledger.py:_commit_revision`),
        # so the committed time IS the enqueue time.
        stale_at = (datetime.now(timezone.utc)
                    - timedelta(hours=PROJECTION_LAG_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            lag = c.execute(
                "SELECT count(*) AS n, min(l.created_at) AS oldest FROM outbox o"
                " JOIN ledger l ON l.memory_id = o.memory_id AND l.revision = o.revision"
                " WHERE o.consumed_at IS NULL AND l.created_at < ?", (stale_at,)).fetchone()
            c.close()
        except sqlite3.Error as exc:
            print(f"  outbox unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
            lag = None
        if lag and lag["n"]:
            out.append({
                "type": "projection.lagging", "subject": "store",
                "severity": "warning",
                "title": f"{lag['n']} conclusion(s) unindexed for over "
                         f"{PROJECTION_LAG_HOURS}h",
                "detail": "Both search indexes are fed by the outbox, so every "
                          "search since then answered over an index that does not "
                          "contain these revisions. The usual cause is the "
                          "embedding provider refusing or a reached spend ceiling: "
                          "the indexer degrades to the lexical index alone and the "
                          "vector half falls behind. "
                          f"Oldest committed {lag['oldest']}.",
                "action": "./observatory.py index — and ./observatory.py wallet if "
                          "the ceiling is what stopped it",
                "evidence": ["store:outbox", "store:ledger"]})

    return out


def main(argv: list[str]) -> int:
    previous = {f["id"]: f for f in (json.loads(OUT.read_text(encoding="utf-8"))["findings"]
                                     if OUT.is_file() else [])}
    # An unreadable acknowledgement file must not stop the board, and must not
    # be mistaken for "nothing acknowledged" either: build without it and say so.
    acks, acks_unreadable = {}, False
    if ACKS.is_file():
        try:
            doc = json.loads(ACKS.read_text(encoding="utf-8"))
            acks = {a["id"]: a for a in doc["acks"]}
        except (OSError, ValueError, KeyError, TypeError):
            acks, acks_unreadable = {}, True

    findings = []
    collected = collect()
    if acks_unreadable:
        collected.append({
            "type": "acks.unreadable", "subject": "config:finding_acks",
            "severity": "warning",
            "title": "saved acknowledgements could not be read",
            "detail": ("The acknowledgement file is not valid. The board was built as if "
                       "nothing were acknowledged, so muted findings show again. The file "
                       "was left exactly as it was."),
            "action": "repair the file or restore a known good copy; `tools/ack.py` refuses to write until then",
            "evidence": [str(ACKS)]})
    for f in collected:
        f["id"] = f"{f['type']}:{f['subject']}"
        f["first_seen"] = previous.get(f["id"], {}).get("first_seen", TODAY)
        a = acks.get(f["id"])
        if a and (not a.get("until") or a["until"] >= TODAY):
            f["acked"] = {"until": a.get("until"), "why": a.get("why"), "by": a.get("by")}
        findings.append(f)

    dupes = sorted({f["id"] for f in findings
                    if sum(1 for g in findings if g["id"] == f["id"]) > 1})
    if dupes:
        sys.exit(f"build_findings: duplicate finding id(s) {dupes}. An id is the "
                 f"dedupe key and the acknowledgement key; two findings sharing one "
                 f"means an operator cannot dismiss either without the other.")

    rank = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (rank.get(f["severity"], 3), f.get("deadline") or "9999",
                                 f["id"]))
    gone = sorted(set(previous) - {f["id"] for f in findings})

    doc = {
        "schema_version": 1,
        "note": ("Derived from the typed registry on every run and REBUILT, never "
                 "appended: a finding whose cause is gone must disappear. "
                 "`first_seen` is the only value carried forward; delete this file "
                 "and every finding looks new today. Operator acknowledgements live "
                 f"in {paths.config_label('finding_acks.json')}, outside the rebuild."),
        # WHEN THE CONTENT LAST CHANGED, not when this ran. It used to be
        # `now()` unconditionally, and that one field made the whole registry
        # differ from itself on every tick: `git diff` was never empty, so the
        # tick's "the registry is clean — nothing to commit" branch was
        # unreachable and the estate collected 48 commits a day whose entire
        # content was a one-second timestamp bump under a constant subject line.
        # Measured 2026-09-07: two consecutive runs, two differing lines, both
        # this field.
        #
        # A committed file must be a function of the FACTS, not of the clock.
        # "When did the observer last run" is run metadata and already lives
        # where run metadata belongs — `scans.finished_at` in the store, which
        # is what the dashboard's health panel shows as freshness. Stamped
        # BELOW, once the rest of the document is known.
        "built_at": None,
        "counts": {s: sum(1 for f in findings if f["severity"] == s and not f.get("acked"))
                   for s in ("critical", "warning", "info")},
        "acknowledged": sum(1 for f in findings if f.get("acked")),
        "resolved_since_last_run": gone,
        "findings": findings,
    }
    now_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    carried = None
    if OUT.is_file():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            stamp = old.pop("built_at", None)
            if old == {k: v for k, v in doc.items() if k != "built_at"}:
                carried = stamp                                                    
        except (json.JSONDecodeError, OSError):
            pass
    doc["built_at"] = carried or now_z
    atomic.write_text(OUT, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")

    if "--json" in argv:
        print(OUT.read_text(encoding="utf-8"))
        return 0
    c = json.loads(OUT.read_text(encoding="utf-8"))["counts"]
    print(f"findings -> {OUT.relative_to(ROOT) if OUT.is_relative_to(ROOT) else OUT}")
    print(f"  critical {c['critical']}   warning {c['warning']}   info {c['info']}"
          f"   acknowledged {sum(1 for f in findings if f.get('acked'))}")
    for f in findings:
        if f["severity"] == "critical" and not f.get("acked"):
            print(f"  ! {f['title']}")
    if gone:
        # `listed`, on stdout too: the tick swallows this into a log, and "four
        # findings resolved" when eleven did is the same wrong reading there.
        print(f"  resolved since last run: {listed(gone, 4)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
