#!/usr/bin/env python3
"""Run every metric plugin, and refuse what they should not have written.

Before this, adding an analytics source — traffic, errors, uptime, spend — meant
editing six core files: the step table, the merge, the emitter, the validator,
the findings builder and the dashboard. The reason was a category error, not a
missing abstraction: a traffic figure was being treated as a REGISTRY FACT.

It is not one. The registry answers what EXISTS, is validated, lives in git and
is rewritten whole on every emit. A measurement taken at an instant accumulates
and belongs beside `events`. Once metrics live in the store, nothing downstream
has to learn a plugin's name — and a new source is two files in `plugins/`.

What this runner owes each plugin, and each plugin's readers:

* **A missing requirement is a SKIP with its reason, never a failure.** The rule
  every collector here follows (AGENTS.md, rule 7).
* **Isolation.** A plugin that crashes, hangs or floods is reported; the others
  still run.
* **Refusal, counted.** A row naming a metric the manifest does not declare, or
  a project the registry does not hold, or a timestamp that is not UTC `Z`, is
  rejected and reported. A plugin that can write any name is a plugin whose
  output nobody can check.
* **Idempotence.** `(project_id, metric, at)` is the key: re-measuring a period
  replaces rather than duplicates.
"""
from __future__ import annotations
import argparse, json, math, pathlib, re, subprocess, sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                    
from store import db as store_db                                                

# Overridable for the same reason the registry and the store are: a test that
# plants a misbehaving plugin must not plant it in the live directory, and a
# hardcoded path is what made the validator silently check the real registry
# while claiming to check a copy.
import os
PLUGINS = pathlib.Path(os.environ.get("OBSERVATORY_PLUGINS") or (ROOT / "plugins"))
UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TIMEOUT_SECONDS = 300
#: Lowered by the tests that plant a plugin that does not finish; bounded so a setting
#: cannot give one plugin the whole tick.
TIMEOUT_ENV = "OBSERVATORY_PLUGIN_TIMEOUT_SECONDS"
#: Seconds of its limit a plugin is told to keep, so it stops before the runner kills it.
DEADLINE_MARGIN_SECONDS = 15
DEADLINE_ENV = "OBSERVATORY_STEP_DEADLINE"


def timeout_seconds() -> float:
    try:
        value = float(os.environ.get(TIMEOUT_ENV, ""))
    except ValueError:
        return float(TIMEOUT_SECONDS)
    return value if 1 <= value <= TIMEOUT_SECONDS else float(TIMEOUT_SECONDS)


def plugin_environment(limit: float) -> dict:
    """The plugin's environment, with the moment it must stop by: the tick step's own
    deadline or the runner's limit for this plugin, less a margin, whichever is first."""
    import time
    own = time.time() + max(limit - DEADLINE_MARGIN_SECONDS, limit / 2)
    try:
        step = float(os.environ.get(DEADLINE_ENV, ""))
    except ValueError:
        step = own
    return {**os.environ, DEADLINE_ENV: f"{min(own, step):.0f}"}
PLUGIN_API_VERSION = 1


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifests() -> list[dict]:
    out = []
    for path in sorted(PLUGINS.glob("*.json")):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            out.append({"id": path.stem, "_broken": f"manifest is not JSON: {exc}"})
            continue
        if not isinstance(m, dict):
            out.append({"id": path.stem, "_broken": "manifest must be an object"})
            continue
        m["_path"] = str(path)
        out.append(m)
    return out


REQUIREMENT_KINDS = {"env", "path", "bin", "secret", "config", "registry", "integration"}


def requirement_error(req: str) -> str:
    if req == "network":
        return ""
    kind, separator, value = req.partition(":")
    if not separator or kind not in REQUIREMENT_KINDS or not value:
        return "requirement must be network or a supported KIND:VALUE"
    if kind in {"secret", "config", "registry"}:
        relative = pathlib.Path(value)
        if relative.is_absolute() or ".." in relative.parts or value in {".", ""}:
            return "private requirement path must remain relative to its configured root"
    if kind == "integration" and not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        return "integration requirement must name a configuration key"
    return ""


def missing_requirement(req: str) -> str:
    """Resolve a declared requirement without executing the plugin.

    Private references stay below their configured root. Legacy environment,
    path, binary and network declarations remain supported for version 1.
    """
    import os
    import shutil
    malformed = requirement_error(req)
    if malformed:
        return malformed
    kind, _, value = req.partition(":")
    if kind == "integration":
        import configuration
        return "" if configuration.enabled(value) else f"integration {value} disabled"
    if kind in {"secret", "config", "registry"}:
        root = {"secret": paths.source_path("secret_store", paths.SECRETS),
                "config": paths.CONFIG, "registry": paths.REGISTRY}[kind]
        try:
            resolved_root = root.resolve()
            candidate = (root / value).resolve()
            if not candidate.is_relative_to(resolved_root):
                return "private requirement path escapes its configured root"
            return "" if candidate.exists() else f"{kind}:{value} is not configured"
        except (OSError, RuntimeError):
            return f"{kind} requirement is unreadable"
    if req == "network":
        return ""                                                                                         
    if req.startswith("env:"):
        name = req[4:]
        return "" if os.environ.get(name) else f"${name} is not set"
    if req.startswith("path:"):
        p = pathlib.Path(req[5:]).expanduser()
        return "" if p.exists() else f"{p} does not exist"
    if req.startswith("bin:"):
        return "" if shutil.which(req[4:]) else f"{req[4:]} is not on PATH"
    return f"unknown requirement {req!r} — see plugins/README.md for the shapes"


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def period_start(now: datetime, every_hours: float) -> datetime:
    """The start of the bucket `now` falls in — the unit a cadence actually means.

    **TWO CLOCKS, and they disagreed.** `due()` once measured elapsed hours since
    the last row was RECORDED, while `plugins/disk_usage.py` stamps its sample
    at midnight UTC of its day — "one sample a day". A sample recorded late in
    the evening then made every tick of the following day answer `SKIP
    disk-usage: measured within the last 24h`. Correct by the elapsed-hours
    rule, and the effect was that the plugin layer never produced a SERIES —
    while the one thing that plugin exists to answer is "is a project growing",
    which needs two points.

    The drift is one-directional and worse than the delay: each day's sample can
    only be taken at or after the previous day's clock time, so a late run pushes
    the next one later still, and a day the machine is asleep at that hour is
    lost with nothing saying so.

    A cadence in whole days is a CALENDAR bucket, because that is what a daily
    sample's own timestamp means. Anything finer buckets by hours since the
    epoch, so a 6-hour plugin lands at 00, 06, 12 and 18 rather than wherever
    the first run happened to fall.
    """
    if every_hours >= 24 and every_hours % 24 == 0:
        days = int(every_hours // 24)
        n = (now - EPOCH).days
        return EPOCH + timedelta(days=n - (n % days))
    hours = max(1.0, float(every_hours))
    n = (now - EPOCH).total_seconds() / 3600.0
    return EPOCH + timedelta(hours=hours * int(n // hours))


#: Two periods. One is the cadence itself — a sample taken at the start of the
#: period is one period old by its end, which is normal. Two means a period was
#: missed entirely.
STALE_PERIODS = 2.0


def due(conn, plugin_id: str, every_hours: float, now: datetime | None = None) -> bool:
    """Is there a sample for the CURRENT period? Not: has enough time passed.

    The question the gate has to answer is whether this period's sample exists,
    and `at` is where a sample says which period it describes. Reading
    `recorded_at` answered a different question — when the writer last ran — and
    the two only coincide when nothing is ever late.
    """
    if not every_hours:
        return True
    start = period_start(now or datetime.now(timezone.utc), float(every_hours))
    row = conn.execute("SELECT MAX(at) FROM metrics WHERE source = ?",
                       (plugin_id,)).fetchone()
    if not row or not row[0]:
        return True
    return row[0] < start.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_one(conn, m: dict, known_projects: set[str], force: bool) -> dict:
    pid = m.get("id") if isinstance(m.get("id"), str) and m["id"] else "?"
    result = {"id": pid, "written": 0, "refused": [], "skipped": ""}
    bad = check_manifest(m)
    if bad:
        result["skipped"] = "manifest refused: " + "; ".join(bad)
        return result
    declared = {x["name"] for x in m.get("metrics") or []}
    if not declared:
        result["skipped"] = "the manifest declares no metrics, so nothing it wrote could be checked"
        return result
    for req in m.get("requires") or []:
        why = missing_requirement(req)
        if why:
            result["skipped"] = why
            return result
    if not force and not due(conn, pid, (float(m["every_hours"]) if type(m.get("every_hours")) in (int, float) and math.isfinite(m["every_hours"]) else 0)):
        result["skipped"] = (f"this period already has a sample "
                             f"(cadence {m.get('every_hours')}h)")
        return result

    script = plugin_script(m)
    if not script.is_file():
        result["skipped"] = f"script {m.get('script')!r} does not exist"
        return result
    limit = timeout_seconds()
    stopped = False
    try:
        p = subprocess.run([sys.executable, str(script)], cwd=ROOT, capture_output=True,
                           text=True, timeout=limit, env=plugin_environment(limit))
        stdout, stderr = p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        # WHAT IT PRINTED BEFORE THE LIMIT IS KEPT. Python hands the captured output
        # back as bytes on a timeout, even with text=True. Losing it made `disk-usage`
        # due again on every tick (no sample for the period) and cost the tail 300 s
        # each half hour on 2026-10-09/10.
        decode = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
        stdout, stderr, stopped = decode(exc.stdout), decode(exc.stderr), True
        p = None
    if p is not None and p.returncode != 0:
        result["skipped"] = f"exited {p.returncode}: {(p.stderr or '').strip()[:120]}"
        return result

    rows = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            result["refused"].append(f"unparseable line: {line[:60]}")
            continue
        if not isinstance(r, dict):
            result["refused"].append("metric row must be an object")
            continue
        if not isinstance(r.get("metric"), str) or r["metric"] not in declared:
            result["refused"].append(f"undeclared metric {r.get('metric')!r}")
            continue
        if not isinstance(r.get("project_id"), str) or r["project_id"] not in known_projects:
            result["refused"].append(f"unknown project {r.get('project_id')!r}")
            continue
        if not UTC_Z.match(str(r.get("at", ""))):
            result["refused"].append(f"`at` is not UTC Z: {r.get('at')!r}")
            continue
        try:
            value = float(r["value"])
        except (KeyError, TypeError, ValueError):
            result["refused"].append(f"value is not a number: {r.get('value')!r}")
            continue
        if not math.isfinite(value):
            result["refused"].append("value must be finite")
            continue
        unit = next((x.get("unit", "") for x in m["metrics"] if x["name"] == r["metric"]), "")
        rows.append((r["project_id"], r["metric"], r["at"], value, unit, pid,
                     json.dumps(r.get("payload") or {}, ensure_ascii=False), now()))

    if stopped and not rows:
        result["skipped"] = f"took longer than {limit:.0f}s and was stopped"
        return result
    if stopped:
        result["partial"] = (f"stopped at its {limit:.0f}s limit; kept the {len(rows)} "
                             f"measurement(s) it had printed")
    with conn:
        conn.executemany(
            "INSERT INTO metrics (project_id, metric, at, value, unit, source, payload_json,"
            " recorded_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(project_id, metric, at) DO UPDATE SET"
            "   value=excluded.value, unit=excluded.unit, source=excluded.source,"
            "   payload_json=excluded.payload_json, recorded_at=excluded.recorded_at", rows)
    result["written"] = len(rows)
    # A PLUGIN'S OWN WORDS ABOUT WHAT IT COULD NOT ATTRIBUTE. The analytics
    # plugins print unmapped hosts to stderr — traffic on a host no project
    # claims is a fact about the REGISTRY, not noise — and dropping it here
    # would hide exactly the gap the operator most wants closed. Kept in the
    # receipt, one line, for `build_findings` to raise.
    note = (stderr or "").strip()
    if note:
        result["note"] = note[:300]
    return result


#: What a manifest must carry, and what each field is for. Checked without
#: running anything, so `--check` can answer "is this plugin well-formed" before
#: the plugin gets a chance to be wrong at runtime — the same separation
#: `tools/validate_registry.py` gives the fact base.
REQUIRED = {
    "id": "the source name every row it writes is stamped with, and the key its "
          "age gate is measured by",
    "title": "what a reader sees; a metric with no title is a number nobody can "
             "interpret",
    "script": "the file to run, relative to plugins/",
    "metrics": "the declared names. A plugin that can write any name is a plugin "
               "whose output nobody can check",
    "why": "the question this plugin exists to answer. Without it the next reader "
           "cannot tell whether removing it loses anything",
}


def plugin_script(m: dict) -> pathlib.Path:
    """Resolve a relative script within the plugin root, including symlink checks."""
    raw = m.get("script")
    if not isinstance(raw, str) or not raw:
        raise ValueError("script must be a non-empty relative path")
    relative = pathlib.Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("script must stay inside the plugin directory")
    target = (PLUGINS / relative).resolve()
    if not target.is_relative_to(PLUGINS.resolve()):
        raise ValueError("script symlink escapes the plugin directory")
    if not target.is_file():
        raise ValueError("plugin script does not exist")
    return target


def check_manifest(m: dict) -> list[str]:
    """Validate version and shape before any plugin code can execute.

    Manifests without api_version use the original version-1 contract.
    Newer or invalid versions require a compatible runner; force never bypasses it.
    """
    if not isinstance(m, dict):
        return ["manifest must be an object"]
    if m.get("_broken"):
        return [str(m["_broken"])]
    bad: list[str] = []
    version = m.get("api_version", 1)
    if type(version) is not int or version != PLUGIN_API_VERSION:
        bad.append("unsupported plugin api_version; this runner supports 1")
    for key in ("id", "title", "script", "why"):
        if not isinstance(m.get(key), str) or not m[key].strip():
            bad.append(f"missing or invalid `{key}`")
    metrics = m.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        bad.append("metrics must be a non-empty list")
    else:
        names = set()
        for i, x in enumerate(metrics):
            if not isinstance(x, dict):
                bad.append(f"metrics[{i}] must be an object")
                continue
            for key in ("name", "unit", "means"):
                if not isinstance(x.get(key), str) or not x[key].strip():
                    bad.append(f"metrics[{i}] has no `{key}`")
            name = x.get("name")
            if isinstance(name, str):
                if name in names: bad.append("duplicate metric name")
                names.add(name)
    try:
        plugin_script(m)
    except (ValueError, OSError, RuntimeError) as exc:
        bad.append(str(exc))
    hours = m.get("every_hours")
    if hours is not None and (type(hours) not in (int, float) or not math.isfinite(hours) or hours < 0):
        bad.append("every_hours must be a finite non-negative number")
    requires = m.get("requires", [])
    if not isinstance(requires, list):
        bad.append("requires must be a list")
    else:
        for req in requires:
            if not isinstance(req, str):
                bad.append("requirement must be a string")
            else:
                reason = requirement_error(req)
                if reason:
                    bad.append(reason)
    return bad


def cmd_check() -> int:
    found = manifests()
    if not found:
        print("no plugins installed — plugins/README.md says how to add one")
        return 0
    ids: dict[str, str] = {}
    problems = 0
    for m in found:
        pid = m.get("id") if isinstance(m.get("id"), str) and m["id"] else pathlib.Path(m.get("_path", "?")).stem
        bad = check_manifest(m)
        # A duplicate id is not a per-manifest fault, so it is checked here where
        # the whole set is visible: two plugins sharing an id share an age gate
        # and stamp each other's rows.
        if pid in ids:
            bad.append(f"id `{pid}` is already used by {ids[pid]}")
        ids.setdefault(pid, m.get("_path", "?"))
        if bad:
            problems += 1
            print(f"  {pid}:")
            for b in bad:
                print(f"    - {b}")
        else:
            print(f"  {pid}: ok")
    print(f"{len(found)} manifest(s), {problems} with problems")
    return 1 if problems else 0


def report(results: list[dict], **extra) -> None:
    """The run, where a reader can find it — on every path.

    `main` returned 0 unconditionally and printed everything, so a plugin that
    crashed on every tick for a month was invisible: the exit code said fine, and
    the log the tick pipes into is read by nobody on a schedule. Measured
    2026-09-07 — `grep -n 'return 1' collectors/run_plugins.py` found nothing.
    """
    doc = {"ran_at": now(), "plugins": results, **extra}
    try:
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "plugins.json").write_text(
            json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"could not write the plugin report: {exc}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="ignore each plugin's age gate")
    ap.add_argument("--only", default="", help="run one plugin by id")
    ap.add_argument("--check", action="store_true",
                    help="validate every manifest and run nothing")
    a = ap.parse_args()
    if a.check:
        return cmd_check()

    conn = store_db.connect()
    known = {p["id"] for p in json.loads(
        (paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]}
    found = manifests()
    if not found:
        print("no plugins installed — plugins/README.md says how to add one")
        report([], installed=0)
        return 0
    results = []
    for m in found:
        if a.only and m.get("id") != a.only:
            continue
        r = run_one(conn, m, known, a.force)
        # WHY IT SKIPPED, not merely that it did. A missing credential is a
        # STATE the operator may have chosen; a crash, a timeout or a non-zero
        # exit is a broken plugin. The findings builder grades them differently
        # and cannot do that from one flag.
        # THE SERIES FIRST, because the classification reads it: `not_due` is
        # only honest when the newest sample is actually current.
        r["last_at"], r["last_recorded"] = last_measurement(conn, r["id"])
        r["every_hours"] = m.get("every_hours")
        r["seriesAgePeriods"] = series_age(r["last_at"],
                                           (float(m["every_hours"]) if type(m.get("every_hours")) in (int, float) and math.isfinite(m["every_hours"]) else 0))
        r["classification"] = classify(r)
        r["manifest_problems"] = check_manifest(m)
        results.append(r)
        if r["skipped"]:
            print(f"  SKIP  {r['id']}: {r['skipped']}")
        else:
            print(f"  {r['id']}: {r['written']} measurement(s)"
                  + (f" — partial: {r['partial']}" if r.get("partial") else ""))
        for bad in r["refused"][:5]:
            print(f"    REFUSED {bad}")
        if len(r["refused"]) > 5:
            print(f"    REFUSED and {len(r['refused']) - 5} more")
    total = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    kinds = conn.execute("SELECT COUNT(DISTINCT metric) FROM metrics").fetchone()[0]
    report(results, installed=len(found), metric_rows=total, distinct_metrics=kinds)
    print(f"metrics: {total} row(s), {kinds} distinct metric(s)")
    # STILL ZERO for a plugin-level fault, deliberately. The runner's contract is
    # that one bad plugin does not stop the others, so it did its job — and a
    # non-zero exit here would make `tick.step_failed` fire beside
    # `plugin.failing` for one cause, which is exactly the duplication the
    # findings were designed to avoid. The report is what carries the fault; the
    # exit code answers whether the RUNNER worked.
    return 0


def classify(r: dict) -> str:
    """Four words, and the difference between them is what a finding needs.

    * `ok`       — it ran and wrote what it declared
    * `not_due`  — its OWN age gate declined. The healthy steady state, and the
                   reason this word exists: it was first called `stale`, which
                   reads as a fault and would have raised a finding on every
                   plugin behaving exactly as configured.
    * `waiting`  — a requirement is unmet. A missing credential is a state the
                   operator may have chosen, not a defect.
    * `broken`   — it crashed, timed out, exited non-zero, or its manifest is
                   unusable. This is the only one that means somebody must look.
    """
    why = r.get("skipped") or ""
    if not why:
        return "ok"
    if "this period already has a sample" in why:
        # NOT DUE **only if the series is actually current**. `not_due` says the
        # cadence is satisfied; a plugin whose newest sample is several periods
        # old is not satisfied, it has stopped producing — and reading that as
        # health is how a metric layer goes quiet without anyone noticing. The
        # first version of this branch matched a skip phrase nobody produced,
        # which is the same defect one level up: an unreachable branch is a
        # check that cannot fire.
        age = r.get("seriesAgePeriods")
        return "stale" if age is not None and age > STALE_PERIODS else "not_due"
    if any(s in why for s in ("is not set", "does not exist", "not on PATH")):
        return "waiting"
    return "broken"


def last_measurement(conn, plugin_id: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT MAX(at), MAX(recorded_at) FROM metrics WHERE source = ?",
        (plugin_id,)).fetchone()
    return (row[0] or "", row[1] or "") if row else ("", "")


def series_age(last_at: str, every_hours: float,
               now_dt: datetime | None = None) -> float | None:
    """How many CADENCE PERIODS old the newest sample is, or None if unknown.

    A gap in a series is a fact about the series, and `not_due` could not
    express it: that word says the cadence is satisfied, and a plugin whose last
    sample is four days old on a daily cadence is not satisfied, it has stopped
    producing. Counted in periods rather than hours so one number reads the same
    for a daily plugin and an hourly one.
    """
    if not last_at or not every_hours:
        return None
    try:
        last = datetime.strptime(last_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    elapsed = (now_dt or datetime.now(timezone.utc)) - last
    return elapsed.total_seconds() / 3600.0 / float(every_hours)


if __name__ == "__main__":
    raise SystemExit(main())
