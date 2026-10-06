"""PB-130 / OSS-11: a project's deployments are grouped by environment
(docs/design/DEPLOYMENTS.md, PB-004d).

The placement goes the way a real workspace's does: the operator's
`config/environments.json` (read by the collector's own `load_overrides`) places one
app in production, a Heroku pipeline stage places another in staging, and a third
named like production (`fx-prod-worker`) is placed by nothing — a name is not
evidence. The collector's document and edges reach the registry, the real dashboard
is built, and tests/render_dashboard.mjs executes it. Asserted, as OSS-11 claims
(audit A46): three groups in the order production, staging, "environment not
stated"; the account on each app's hover title; the name-alike app in the unstated
group, not in production.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests")); sys.path.insert(0, str(ROOT / "collectors"))
import dashboard_fixture  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f" — {detail}"))
    if not ok:
        FAILS.append(name)


APPS = ("fx-web", "fx-staging", "fx-prod-worker")


def seed_hosting(root: Path) -> dict:
    import environments
    import heroku_registry
    reg = root / "registry"
    heroku_registry.load_verified = lambda: {}
    apps = [{"name": n, "team": "fixture-team", "monthly_cost": 7, "pipeline": None, "pipeline_error": None}
            for n in APPS]
    apps[1]["pipeline"] = {"id": "p", "name": "fx", "stage": "staging"}  # the provider's own statement
    scan = {"apps": apps, "scanned_at": "2026-09-24T00:00:00Z"}
    records, _ = heroku_registry.records(scan, [], [], [])
    for rec in records:
        rec.update(project="project:fixture-a", link_rule="verified")
    doc = heroku_registry.document(scan, records, "2026-09-24")
    (reg / "heroku-apps.json").write_text(json.dumps(doc))
    # The operator's override file, read by the collector's loader as emit_registry reads it.
    config = root / "config"
    config.mkdir(exist_ok=True)
    (config / "environments.json").write_text(json.dumps({"deployments": {"heroku:fx-web": {"environment": "production"}}}))
    overrides, problems = environments.load_overrides(config / "environments.json")
    env_doc, edges = environments.build(records, apps, [], [], [], overrides, problems, "2026-09-24")
    (reg / "environments.json").write_text(json.dumps(env_doc))
    (reg / "accounts.json").write_text(json.dumps({"schema_version": 1, "accounts": [
        {"id": "account:heroku/t1", "provider": "heroku", "label": "fixture-team", "resources": 3}]}))
    rel = json.loads((reg / "relations.json").read_text())
    rel["relations"] += [{"id": e["id"], "type": e["type"], "from": e["from"], "to": e["to"],
                          "rule": e.get("rule"), "source_refs": e["source_refs"]} for e in edges]
    rel["relations"] += [{"id": f"acct-{n}", "type": "in_account", "from": f"heroku:{n}", "to": "account:heroku/t1",
                          "rule": "heroku-team", "source_refs": []} for n in APPS]
    (reg / "relations.json").write_text(json.dumps(rel))
    return {"edges": {(e["from"], e["to"], e.get("rule")) for e in edges},
            "unassigned": [u["deployment"] for u in env_doc.get("unassigned", [])]}


def groups_in(markup: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(data-env, [(app name, hover title), …]), …] in page order."""
    out = []
    for m in re.finditer(r'<div class="envgroup" data-env="([^"]*)">(.*?)(?=<div class="envgroup"|</td>|$)', markup, re.S):
        apps = re.findall(r'<div class="st [^"]*" title="([^"]*)"><i></i><span class="mono">([^<]*)</span>', m.group(2))
        out.append((m.group(1), [(name, title) for title, name in apps]))
    return out


def main() -> int:
    node = shutil.which("node")
    root = Path(tempfile.mkdtemp(prefix="observatory-hosting-")).resolve()
    env = dashboard_fixture.seed(root)
    placed = seed_hosting(root)
    check("config/environments.json places fx-web in production, as an override",
          ("heroku:fx-web", "environment:project:fixture-a/production", "override") in placed["edges"], str(placed))
    check("the pipeline stage places fx-staging in staging",
          ("heroku:fx-staging", "environment:project:fixture-a/staging", "heroku-pipeline-stage") in placed["edges"],
          str(placed))
    check("a production-like name places nothing", placed["unassigned"] == ["heroku:fx-prod-worker"], str(placed))
    p = subprocess.run([dashboard_fixture.PYTHON, str(ROOT / "dashboard/build_dashboard.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    check("the dashboard builds with environments and accounts in the registry", p.returncode == 0, p.stderr[-400:])
    if p.returncode:
        return 1
    sys.path.insert(0, str(ROOT / "dashboard"))
    os.environ.update(env)
    page = Path(env["OBSERVATORY_DASHBOARD"])
    html = page.read_text(encoding="utf-8")
    check("the account label travels to the page", '"account": "fixture-team"' in html or '"account":"fixture-team"' in html)
    if node is None:
        print("  SKIP  node is not installed here, so the page cannot be executed")
        return 1 if FAILS else 0
    out = subprocess.run([node, str(ROOT / "tests/render_dashboard.mjs"), str(page), "--count", 'data-env="'],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    r = json.loads(out.stdout)
    check("the page runs without throwing", r.get("threw") is None, str(r.get("threw")))
    groups = sum(r.get("counts", {}).values())
    check("one project, three groups: production, staging and not specified", groups == 3, str(r.get("counts")))
    out2 = subprocess.run([node, str(ROOT / "tests/render_dashboard.mjs"), str(page), "--count", "environment not stated"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("the unplaced app is visible as not specified", sum(json.loads(out2.stdout).get("counts", {}).values()) == 1,
          out2.stdout[-300:])
    # Order and hover, read from what the script wrote into the projects table.
    where = [k for k, n in r.get("counts", {}).items() if n]
    shown = subprocess.run([node, str(ROOT / "tests/render_dashboard.mjs"), str(page), "--show", where[0] if where else "-"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300)
    groups = groups_in(json.loads(shown.stdout).get("shown") or "")
    check("production comes first, then staging, then the unstated group",
          [g for g, _ in groups] == ["production", "staging", "unassigned"], str(groups)[:400])
    check("each group holds its own app; the production-like name is not in production",
          [[n for n, _ in apps] for _, apps in groups] == [["fx-web"], ["fx-staging"], ["fx-prod-worker"]],
          str(groups)[:400])
    check("the account is on every app's hover title",
          all("account fixture-team" in title for _, apps in groups for _, title in apps) and len(groups) == 3,
          str(groups)[:400])
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
