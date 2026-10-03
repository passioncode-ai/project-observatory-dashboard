#!/usr/bin/env python3
"""Build the real dashboard over a fictional estate, for screenshots and demos.

    python tools/demo_estate.py OUT_DIR [--locale en|ru]

Every fact here is invented: the company (Northwind Labs), its projects, their
repositories, commits, measurements, findings and variable names. Nothing is read
from the machine running it — no workspace, no registry, no store, no secret — and
the pages are rendered by the unmodified engine (`collectors/emit_registry.py`,
then `dashboard/build_dashboard.py`), so a screenshot of `OUT_DIR/pages/` shows
what Observatory draws, not a mock-up of it.

The output directory must not exist or must be empty; it is the demo's own home
(`OUT_DIR/home` is its OBSERVATORY_HOME). Unlike a workspace's dashboard, the
pages embed no path of the machine that built them (`OBSERVATORY_DEMO=1`): their
copy-a-command buttons read `project-observatory full …` and `"$HOME"`, and
`tests/test_demo_estate.py` builds under a distinctive HOME and searches every page
for it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
import subprocess
import sys

ENGINE = Path(__file__).resolve().parents[1] / "observatory" / "engine"
OWNER = "northwind-labs"

#: (id, name, description, language, weekly commit shape, last active days ago)
PROJECTS = (
    ("atlas-billing", "Atlas Billing", "Subscription billing service and invoice renderer", "Python", "busy", 0),
    ("harbor-web", "Harbor Web", "Customer-facing web app and marketing pages", "TypeScript", "busy", 1),
    ("lumen-mobile", "Lumen Mobile", "iOS and Android client for Harbor", "Kotlin", "steady", 3),
    ("orbit-agents", "Orbit Agents", "Agent workflows that triage support tickets", "Python", "rising", 0),
    ("quarry-data", "Quarry Data", "Nightly warehouse loads and product analytics", "SQL", "steady", 6),
    ("signal-docs", "Signal Docs", "Developer documentation site", "MDX", "cooling", 24),
    ("tern-cli", "Tern CLI", "Command-line tool for Harbor's API", "Go", "cooling", 41),
    ("vesper-legacy", "Vesper Legacy", "The first monolith, kept read-only for exports", "Ruby", "dormant", 190),
)
SHAPES = {
    "busy": lambda w: 14 + (w * 7) % 11,
    "steady": lambda w: 5 + (w * 5) % 6,
    "rising": lambda w: max(0, w * 2 - 6),
    "cooling": lambda w: max(0, 9 - w),
    "dormant": lambda w: 0,
}
FINDINGS = (
    ("critical", "credential.leaked", "project:atlas-billing", "A payment webhook secret appeared in an agent transcript",
     "The value of PAYMENTS_SIGNING_SECRET was seen in a session log on 2026-09-24.",
     "Rotate the secret at the provider, then record the rotation with vault.py rotate."),
    ("warning", "repo.unpushed", "project:orbit-agents", "Eleven commits exist only on this machine",
     "orbit-agents is 11 commits ahead of its remote on branch triage-v2.",
     "Push the branch or record why it stays local."),
    ("warning", "env.shared_secret", "env:harbor-web/.env", "One database password is shared by two projects",
     "DATABASE_URL in harbor-web and lumen-mobile has the same salted fingerprint.",
     "Split the credential so each project holds its own."),
    ("info", "project.drift", "project:tern-cli", "Declared active, measured cooling",
     "No commit in 41 days while the project is marked active.",
     "Mark it paused, or pick the work back up."),
    ("info", "domain.expiring", "domain:signal-docs.example", "A documentation domain expires in 38 days",
     "Auto-renewal is off at the registrar.",
     "Turn auto-renewal on or let the name go deliberately."),
)
ENV_FILES = (
    ("harbor-web", "env", [("DATABASE_URL", "secret", "shared-db"), ("SESSION_SECRET", "secret", "hw-session"),
                           ("NEXT_PUBLIC_API_URL", "config", None)]),
    ("lumen-mobile", "env", [("DATABASE_URL", "secret", "shared-db"), ("PUSH_KEY", "secret", "lm-push")]),
    ("atlas-billing", "env", [("PAYMENTS_SIGNING_SECRET", "secret", "ab-webhook"), ("PAYMENTS_API_KEY", "secret", "ab-stripe"),
                              ("INVOICE_BUCKET", "config", None)]),
    ("orbit-agents", "env", [("OPENROUTER_API_KEY", "secret", "oa-router"), ("TICKET_QUEUE", "config", None)]),
    ("orbit-agents", "template", [("OPENROUTER_API_KEY", "secret", None)]),
)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--locale", default="en", choices=("en", "ru"))
    a = ap.parse_args(argv)
    root = a.out.resolve()
    if root.exists() and any(root.iterdir()):
        print(f"demo_estate: {root} is not empty; give it a new directory", file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)
    # THE DEMO'S OWN HOME, set before any engine module is imported: the engine
    # resolves settings, sources and secrets from OBSERVATORY_HOME, and without
    # this line it would read the invoking user's real workspace.
    os.environ["OBSERVATORY_HOME"] = str(root / "home")
    os.environ.pop("OBSERVATORY_LOCALE", None)
    # An initialized, empty workspace: the reviewed defaults and nothing else.
    made = subprocess.run([sys.executable, str(ENGINE / "observatory.py"), "init"], cwd=ENGINE,
                          env={**os.environ}, capture_output=True, text=True)
    if made.returncode:
        print("demo_estate: could not initialize the demo workspace:\n" + made.stderr[-600:], file=sys.stderr)
        return 1
    sys.path.insert(0, str(ENGINE))
    sys.path.insert(0, str(ENGINE / "tests"))
    from emitter_fixture import environment, seed
    env = seed(root)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for name in ("state", "estate", "wiki", "empty-vault"):
        (root / name).mkdir(exist_ok=True)
    env.update(OBSERVATORY_DATA=str(root / "estate"), OBSERVATORY_VAULT=str(root / "wiki"),
               CLAUDE_MEM_DB=str(root / "absent-companion.db"), OBSERVATORY_DB=str(root / "state/data.db"),
               OBSERVATORY_DASHBOARD=str(root / "page.html"), OBSERVATORY_DASHBOARD_DIR=str(root / "pages"),
               OBSERVATORY_LOCALE=a.locale, OBSERVATORY_OFFLINE="1",
               # The pages carry no path of this machine: commands render as
               # `project-observatory full …` and `"$HOME"` (build_dashboard.runtime_paths).
               OBSERVATORY_DEMO="1")

    model = json.loads((root / "raw/model.json").read_text())
    project = dict(anchor=OWNER, ownership="owned", owners=[OWNER], archived=False, kinds=[],
                   has_note=True, rules=[], vault=None, sites=[])
    model["projects"], model["repositories"] = {}, {}
    for pid, name, description, language, _shape, ago in PROJECTS:
        nwo = f"{OWNER}/{pid}"
        model["projects"][pid] = dict(project, name=name, description=description, folders=[pid], repos=[nwo],
                                      archived=pid == "vesper-legacy",
                                      last_activity=(now - timedelta(days=ago)).strftime("%Y-%m-%d"))
        model["repositories"][nwo] = dict(host="github", url=f"https://github.com/{nwo}", visibility="private",
                                          description=description, archived=pid == "vesper-legacy", fork=False,
                                          language=language, topics=[], pushed_at=None, created_at=None,
                                          source="github-api", local=None)
    (root / "raw/model.json").write_text(json.dumps(model))
    emitted = subprocess.run([sys.executable, str(ENGINE / "collectors/emit_registry.py"), str(root / "raw")],
                             cwd=ENGINE, env=env, capture_output=True, text=True)
    if emitted.returncode:
        print("demo_estate: the emitter refused the fictional estate:\n" + emitted.stderr[-800:], file=sys.stderr)
        return 1

    os.environ.update({k: v for k, v in env.items() if k.startswith("OBSERVATORY_") or k == "CLAUDE_MEM_DB"})
    from store import db
    rng = random.Random(7)
    conn = db.connect(root / "state/data.db")
    with conn:
        conn.execute("INSERT INTO scans(id,started_at,finished_at,counts_json,degraded_json,collector_version)"
                     " VALUES (?,?,?,?,?,?)", ("demo-scan", stamp, stamp, "{}", "[]", "demo/1"))
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
        for pid, _name, _d, _l, shape, ago in PROJECTS:
            for w in range(12):
                start = monday - timedelta(weeks=11 - w)
                commits = SHAPES[shape](w)
                if start > now - timedelta(days=ago) and shape != "busy":
                    commits = 0 if ago > 7 else commits
                iso = start.isocalendar()
                conn.execute("INSERT INTO project_week(project_id,week,week_start,commits,active_days,authors,"
                             "sessions,session_days,worked_days,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (f"project:{pid}", f"{iso[0]}-W{iso[1]:02d}", start.strftime("%Y-%m-%d"), commits,
                              min(5, commits), 1 + commits // 8, commits // 3, min(5, commits // 3),
                              min(6, commits), stamp))
                for i in range(commits if start >= now - timedelta(days=28) else 0):
                    at = (start + timedelta(hours=9 + i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if at > stamp:
                        continue
                    conn.execute("INSERT INTO events(id,project_id,kind,ref,actor,occurred_at,payload_json)"
                                 " VALUES (?,?,?,?,?,?,?)",
                                 (f"demo-{pid}-{w}-{i}", f"project:{pid}", "commit", f"{pid}-{w}-{i}", "demo",
                                  at, json.dumps({"subject": "Synthetic change"})))
            size = rng.randint(40, 900) * 1_000_000
            for at, value in ((now - timedelta(days=7), size), (now, int(size * rng.uniform(1.0, 1.3)))):
                conn.execute("INSERT INTO metrics(project_id,metric,at,value,unit,source,recorded_at)"
                             " VALUES (?,?,?,?,?,?,?)", (f"project:{pid}", "disk.bytes",
                                                        at.strftime("%Y-%m-%dT%H:%M:%SZ"), value, "bytes",
                                                        "disk-usage", stamp))
    conn.close()
    (root / "state/wallet.json").write_text(json.dumps({"days": {now.strftime("%Y-%m-%d"): 0.4},
                                                        "months": {now.strftime("%Y-%m"): 3.1},
                                                        "denomination": "credits"}))
    (root / "state/provider-health.json").write_text("{}")
    counts = {s: sum(1 for f in FINDINGS if f[0] == s) for s in ("critical", "warning", "info")}
    (root / "registry/findings.json").write_text(json.dumps({"counts": counts, "built_at": stamp, "findings": [
        {"id": f"finding:demo-{i}", "severity": sev, "type": kind, "subject": subject, "title": title,
         "detail": detail, "action": action, "evidence": ["demo"]}
        for i, (sev, kind, subject, title, detail, action) in enumerate(FINDINGS)]}))
    from collectors.env_registry import document
    files = [{"path": f"{project}/{'.env' if kind == 'env' else '.env.example'}", "project": project, "kind": kind,
              "git": "ignored", "mode": "600", "modified_on": stamp[:10], "unparsed_lines": 0,
              "variables": [{"name": n, "class": c, **({"fingerprint": fp} if fp else {})} for n, c, fp in variables]}
             for project, kind, variables in ENV_FILES]
    (root / "registry/env-inventory.json").write_text(json.dumps(document(
        # A neutral root: the page carries this document whole, and the output
        # directory is a path on the machine that built the demo.
        {"files": files, "scanned_at": stamp, "root": "~/projects"}, stamp[:10])))
    built = subprocess.run([sys.executable, str(ENGINE / "dashboard/build_dashboard.py")], cwd=ENGINE, env=env,
                           capture_output=True, text=True)
    if built.returncode:
        print("demo_estate: the dashboard failed:\n" + built.stderr[-800:], file=sys.stderr)
        return 1
    print(json.dumps({"pages": str(root / "pages"), "locale": a.locale, "projects": len(PROJECTS),
                      "findings": counts}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
