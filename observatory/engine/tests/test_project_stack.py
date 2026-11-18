#!/usr/bin/env python3
"""A project's stack follows from its dependency inventory, and its panel lists the packages (OBS-603, CP-06).

The stack used to be guessed from marker files at a folder's top (`collectors/scan_filesystem.MARKERS`): a
`package.json` said `node`, a `next.config.js` said `nextjs`. The dependency scan already reads every lockfile a
project resolves (`collectors/scan_dependencies.py`), so the language and framework part of the stack is read from
there; a marker stands in only where no lockfile of its ecosystem was read, and says so in `degraded`. Every
inventory here is written by the test; nothing reaches a network.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "collectors"))
import tmp as tmpdir                                                # noqa: E402
import project_stack as ps                                          # noqa: E402
import dependencies_registry as dr                                  # noqa: E402


def pkg(name, version="1.0.0", eco="npm", direct=True, local=False, file="alpha-web/package-lock.json"):
    return {"name": name, "version": version, "ecosystem": eco, "direct": direct, "local": local, "file": file}


def inventory(packages, read_from=None, degraded=None):
    if read_from is None:
        read_from = [{"file": f, "ecosystem": e} for f, e in sorted({(r["file"], r["ecosystem"]) for r in packages})]
    return {"project": "project:alpha-web", "ecosystems": sorted({r["ecosystem"] for r in packages}),
            "packages": packages, "read_from": read_from, "degraded": degraded or []}


class Derive(unittest.TestCase):
    def test_languages_and_frameworks_come_from_the_lockfiles(self):
        got = ps.derive(["node"], inventory([pkg("next", "15.0.0"), pkg("react", direct=False),
                                             pkg("flask", "3.0.0", "PyPI", file="alpha-web/api/requirements.txt")]))
        self.assertEqual(got["stack"], ["nextjs", "node", "python"],
                         "a nested Python lock and a framework the root marker never showed join the stack")
        self.assertEqual(got["source"], "dependencies")
        self.assertEqual(got["degraded"], [])
        by = {e["kind"]: e for e in got["evidence"]}
        self.assertEqual(by["nextjs"], {"kind": "nextjs", "from": "dependencies", "ecosystem": "npm", "package": "next"})
        self.assertEqual(by["python"]["from"], "dependencies")

    def test_a_transitive_framework_is_not_the_projects_framework(self):
        got = ps.derive([], inventory([pkg("astro", direct=False), pkg("left-pad")]))
        self.assertEqual(got["stack"], ["node"])

    def test_platform_markers_stay_and_are_labelled(self):
        got = ps.derive(["docker", "ios", "node"], inventory([pkg("left-pad")]))
        self.assertEqual(got["stack"], ["docker", "ios", "node"])
        by = {e["kind"]: e for e in got["evidence"]}
        self.assertEqual(by["docker"]["from"], "marker")
        self.assertEqual(by["node"]["from"], "dependencies")
        self.assertEqual(got["degraded"], [], "a platform marker describes no package, so nothing is guessed")

    def test_a_language_with_no_lockfile_falls_back_to_its_marker_and_says_so(self):
        got = ps.derive(["node", "python", "nextjs"], inventory([pkg("left-pad")]))
        self.assertEqual(got["stack"], ["nextjs", "node", "python"])
        self.assertEqual(got["source"], "dependencies+markers")
        self.assertEqual([d["code"] for d in got["degraded"]], ["marker-fallback"])
        self.assertIn("nextjs", got["degraded"][0]["reason"])
        self.assertIn("python", got["degraded"][0]["reason"])
        self.assertEqual({e["kind"]: e["from"] for e in got["evidence"]}["python"], "marker")

    def test_no_lockfile_read_is_the_marker_stack_named_in_degraded(self):
        got = ps.derive(["swift", "docker"], None)
        self.assertEqual(got["stack"], ["docker", "swift"])
        self.assertEqual(got["source"], "markers")
        self.assertEqual([d["code"] for d in got["degraded"]], ["markers-only"])

    def test_a_lockfile_that_did_not_parse_is_not_evidence(self):
        broken = inventory([], read_from=[{"file": "alpha-web/Cargo.lock", "ecosystem": "crates.io",
                                            "error": "TOMLDecodeError: x"}])
        got = ps.derive(["rust"], broken)
        self.assertEqual(got["stack"], ["rust"])
        self.assertEqual(got["source"], "markers")
        self.assertEqual(got["degraded"][0]["code"], "markers-only")

    def test_a_read_lockfile_with_no_pins_still_names_its_ecosystem(self):
        got = ps.derive([], inventory([], read_from=[{"file": "alpha-web/requirements.txt", "ecosystem": "PyPI"}]))
        self.assertEqual(got["stack"], ["python"])
        self.assertEqual(got["source"], "dependencies")

    def test_nothing_measured_is_empty_and_quiet(self):
        self.assertEqual(ps.derive([], None), {"stack": [], "source": "none", "evidence": [], "degraded": []})

    def test_registry_fields_of_a_derivation(self):
        got = ps.registry_fields(ps.derive(["python"], None))
        self.assertEqual(got["stack"], ["python"])
        self.assertEqual(got["stack_source"], "markers")
        self.assertEqual(got["stack_degraded"][0]["code"], "markers-only")
        quiet = ps.registry_fields(ps.derive([], inventory([pkg("left-pad")])))
        self.assertNotIn("stack_degraded", quiet, "an empty field is absent, never an empty claim")

    def test_inventory_by_project_from_the_raw_scan(self):
        scan = {"projects": [{"project": "project:alpha-web", "files": [
            {"folder": "alpha-web", "path": "package-lock.json", "format": "npm-lock", "ecosystem": "npm",
             "packages": [{"name": "next", "version": "15.0.0", "ecosystem": "npm", "direct": True, "local": False}]}],
            "degraded": []}]}
        rows = ps.inventory_by_project(scan)
        self.assertEqual(ps.derive([], rows["project:alpha-web"])["stack"], ["nextjs", "node"])
        self.assertEqual(ps.inventory_by_project(None), {})


class Survey(unittest.TestCase):
    def test_the_survey_keeps_its_published_fields_and_the_detail_adds_the_source(self):
        import survey
        row = {"id": "project:alpha-web", "name": "alpha-web", "ownership": "owned", "lifecycle": "active",
               **ps.registry_fields(ps.derive(["python"], None))}
        view = survey._project_view(row, {}, {})
        self.assertEqual(view["stack"], ["python"])
        self.assertFalse({"stackSource", "stackEvidence"} & set(view), "the survey's item schema is closed")
        self.assertEqual(ps.detail_fields(row), {"stackSource": "markers",
                                                 "stackEvidence": [{"kind": "python", "from": "marker"}]})
        self.assertEqual(ps.detail_fields({"id": "project:old-row", "stack": ["node"]}), {})
        self.assertIn("project_stack.detail_fields", (ROOT / "survey.py").read_text(encoding="utf-8"),
                      "project.detail serves the source beside the stack")

    def test_degraded_names_each_project_alone_and_counts_them_for_the_estate(self):
        rows = [{"id": "project:alpha-web", **ps.registry_fields(ps.derive(["python"], None))},
                {"id": "project:beta-api", **ps.registry_fields(ps.derive(["go"], None))},
                {"id": "project:gamma", **ps.registry_fields(ps.derive([], None))}]
        one = ps.survey_degraded(rows[:1], one_project=True)
        self.assertEqual(one, [{"source": "stack", "reason": rows[0]["stack_degraded"][0]["reason"]}])
        estate = ps.survey_degraded(rows, one_project=False)
        self.assertEqual(len(estate), 1)
        self.assertIn("2 project", estate[0]["reason"])
        self.assertEqual(set(estate[0]), {"source", "reason"}, "the published degraded item is closed")
        self.assertEqual(ps.survey_degraded(rows[2:], one_project=False), [])


class Panel(unittest.TestCase):
    def registry(self):
        return {"updated_on": "2026-11-18", "degraded": [], "projects": [dr.project_row({
            "project": "project:alpha-web", "degraded": [],
            "files": [{"folder": "alpha-web", "path": "package-lock.json", "format": "npm-lock", "ecosystem": "npm",
                       "packages": [pkg("lodash", "4.17.20"), pkg("react", "18.0.0", direct=False),
                                    pkg("own-lib", "0.0.0", local=True) | {"source": "workspace"}]}]})]}

    def test_rows_carry_name_version_ecosystem_direct_local_and_advisory_count(self):
        vulns = {"checked_on": "2026-11-18", "degraded": [],
                 "matches": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.20",
                              "advisories": ["GHSA-aaaa", "GHSA-bbbb"]}],
                 "advisories": {"GHSA-aaaa": {"severity": "high"}, "GHSA-bbbb": {"severity": "low"}}}
        got = ps.panel(self.registry(), vulns)
        one = got["projects"]["project:alpha-web"]
        self.assertEqual(one["rows"][0], {"name": "lodash", "version": "4.17.20", "ecosystem": "npm",
                                          "direct": True, "local": False, "advisories": 2},
                         "the vulnerable package comes first, with how many advisories match it")
        self.assertEqual([r["name"] for r in one["rows"]], ["lodash", "own-lib", "react"])
        self.assertEqual({k: one[k] for k in ("total", "direct", "transitive", "local", "vulnerable", "shown")},
                         {"total": 3, "direct": 1, "transitive": 1, "local": 1, "vulnerable": 1, "shown": 3})
        self.assertEqual(got["checkedOn"], "2026-11-18")
        self.assertEqual(got["degraded"], [])

    def test_a_long_list_is_cut_and_says_how_much(self):
        many = [pkg(f"p{i:03d}", direct=False) for i in range(ps.PANEL_ROWS + 7)]
        doc = {"projects": [{"project": "project:alpha-web", "packages": many, "read_from": [], "degraded": []}]}
        one = ps.panel(doc, {"matches": [], "advisories": {}})["projects"]["project:alpha-web"]
        self.assertEqual((one["total"], one["shown"], len(one["rows"])), (ps.PANEL_ROWS + 7, ps.PANEL_ROWS, ps.PANEL_ROWS))

    def test_no_inventory_and_no_watch_are_named(self):
        got = ps.panel(None, None)
        self.assertEqual(got["projects"], {})
        self.assertEqual({d["source"] for d in got["degraded"]}, {"dependencies", "osv"})
        local = ps.panel(self.registry(), None)["projects"]["project:alpha-web"]
        self.assertIsNone(local["rows"][0]["advisories"], "an unwatched package has an unknown count, never zero")


class ThroughTheEmitter(unittest.TestCase):
    """The emit writes the derived stack into `registry/projects.json` (emitter_fixture, a synthetic workspace)."""

    def test_the_registry_stack_is_the_derived_one(self):
        import workspace
        from emitter_fixture import seed
        root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stack-emit-"))
        env = seed(root)
        workspace.initialize(root / "home", identities=False)
        env["OBSERVATORY_HOME"] = str(root / "home")
        model = json.loads((root / "raw" / "model.json").read_text(encoding="utf-8"))
        model["projects"]["fixture-a"]["kinds"] = ["docker", "node"]
        model["projects"]["fixture-b"]["kinds"] = ["python"]
        (root / "raw" / "model.json").write_text(json.dumps(model), encoding="utf-8")
        scan = {"schema": "dependencies-scan/1", "complete": True, "degraded": [], "projects": [
            {"project": "project:fixture-a", "degraded": [], "files": [
                {"folder": "fixture-a", "path": "package-lock.json", "format": "npm-lock", "ecosystem": "npm",
                 "packages": [{"name": "astro", "version": "4.0.0", "ecosystem": "npm", "direct": True,
                               "local": False}]}]}]}
        (root / "raw" / "dependencies.json").write_text(json.dumps(scan), encoding="utf-8")
        run = subprocess.run([sys.executable, "collectors/emit_registry.py", str(root / "raw")], cwd=ROOT,
                             capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(run.returncode, 0, run.stderr[-800:])
        projects = {p["name"]: p for p in json.loads((root / "registry" / "projects.json")
                                                      .read_text(encoding="utf-8"))["projects"]}
        a, b = projects["Fixture A"], projects["Fixture B"]
        self.assertEqual(a["stack"], ["astro", "docker", "node"])
        self.assertEqual(a["stack_source"], "dependencies")
        self.assertNotIn("stack_degraded", a)
        self.assertEqual(b["stack"], ["python"])
        self.assertEqual(b["stack_source"], "markers")
        self.assertEqual(b["stack_degraded"][0]["code"], "markers-only")


if __name__ == "__main__":
    unittest.main()
