#!/usr/bin/env python3
"""The dependency inventory and the vulnerability watch (OBS-553, CP-06, CP-07, decision D2).

Every lockfile here is written by the test into a temporary folder; every OSV and catalogue
answer comes from a fake fetcher that records what it was asked, so nothing reaches a network and
a local or private package name can be shown never to leave the machine.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402
import scan_dependencies as sd                                      # noqa: E402
import dependencies_registry as dr                                  # noqa: E402
import vuln_watch as vw                                             # noqa: E402
import dependency_findings as df                                    # noqa: E402


def folder(files: dict[str, str]) -> pathlib.Path:
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-deps-"))
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def rows(files: dict[str, str], private=(set(), ())) -> tuple[list[dict], list[dict]]:
    root = folder(files)
    got, bad = sd.scan_folder(root, "alpha-web", {}, private)
    return [dict(r, file=f["path"]) for f in got for r in f.get("packages") or []], bad


def by_name(found: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in found}


NPM_LOCK = json.dumps({
    "name": "alpha-web", "lockfileVersion": 3,
    "packages": {
        "": {"name": "alpha-web", "dependencies": {"lodash": "^4.17.0", "@example/ui": "file:../ui"},
             "devDependencies": {"jest": "^29"}},
        "node_modules/lodash": {"version": "4.17.20", "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.20.tgz"},
        "node_modules/jest": {"version": "29.0.0", "dev": True},
        "node_modules/jest/node_modules/chalk": {"version": "4.1.2", "dev": True},
        "node_modules/@example/ui": {"resolved": "../ui", "link": True},
        "node_modules/internal-kit": {"version": "1.0.0",
                                      "resolved": "https://npm.example.invalid/internal-kit/-/internal-kit-1.0.0.tgz"},
        "packages/web": {"name": "alpha-workspace", "version": "0.1.0"},
    }})


class LockfileReaders(unittest.TestCase):
    def test_npm_lock_v3(self):
        found, bad = rows({"package-lock.json": NPM_LOCK, "package.json": '{"name": "alpha-web"}'})
        self.assertEqual(bad, [])
        got = by_name(found)
        self.assertEqual(got["lodash"], {**got["lodash"], "version": "4.17.20", "ecosystem": "npm",
                                         "direct": True, "local": False})
        self.assertTrue(got["jest"]["direct"] and got["jest"]["dev"])
        self.assertFalse(got["chalk"]["direct"])
        self.assertNotIn("@example/ui", got, "a link is a symlink, never a package")
        self.assertEqual((got["internal-kit"]["local"], got["internal-kit"]["source"]), (True, "private"))
        self.assertEqual(got["alpha-workspace"]["source"], "workspace")
        self.assertNotIn("package.json", {r["file"] for r in found}, "a manifest beside a lock is not read twice")

    def test_npm_lock_v1_is_named_not_empty(self):
        root = folder({"package-lock.json": json.dumps({"lockfileVersion": 1, "dependencies": {}})})
        files, bad = sd.scan_folder(root, "alpha-web", {})
        self.assertIn("error", files[0])
        self.assertEqual(len(bad), 1)
        self.assertIn("unknown, not absent", bad[0]["reason"])

    def test_pnpm_lock_v6_and_v9_and_v5(self):
        v6 = ("lockfileVersion: '6.0'\n\nimporters:\n  .:\n    dependencies:\n      lodash:\n"
              "        specifier: ^4.17.0\n        version: 4.17.21\n    devDependencies:\n      vitest:\n"
              "        specifier: ^1\n        version: 1.0.0\n\npackages:\n\n  /lodash@4.17.21:\n"
              "    resolution: {integrity: sha512-x}\n    dev: false\n\n  /vitest@1.0.0(@types/node@20.0.0):\n"
              "    resolution: {integrity: sha512-y}\n    dev: true\n\n  /debug@4.3.4:\n    dev: false\n")
        got = by_name(rows({"pnpm-lock.yaml": v6})[0])
        self.assertEqual(got["lodash"]["version"], "4.17.21")
        self.assertTrue(got["lodash"]["direct"])
        self.assertEqual(got["vitest"]["version"], "1.0.0")
        self.assertTrue(got["vitest"].get("dev"))
        self.assertFalse(got["debug"]["direct"])
        v9 = ("lockfileVersion: '9.0'\n\nimporters:\n  .:\n    dependencies:\n      '@babel/core':\n"
              "        specifier: ^7\n        version: 7.24.0\n\npackages:\n\n  '@babel/core@7.24.0':\n"
              "    resolution: {integrity: sha512-z}\n\n  ms@2.1.3:\n    resolution: {integrity: sha512-w}\n")
        got = by_name(rows({"pnpm-lock.yaml": v9})[0])
        self.assertEqual((got["@babel/core"]["version"], got["@babel/core"]["direct"]), ("7.24.0", True))
        self.assertFalse(got["ms"]["direct"])
        v5 = ("lockfileVersion: 5.4\n\ndependencies:\n  left-pad: 1.3.0\n\npackages:\n\n  /left-pad/1.3.0:\n"
              "    dev: false\n\n  /@scope/pkg/2.0.0_react@18.0.0:\n    dev: false\n")
        got = by_name(rows({"pnpm-lock.yaml": v5})[0])
        self.assertEqual(got["left-pad"]["version"], "1.3.0")
        self.assertEqual(got["@scope/pkg"]["version"], "2.0.0")

    def test_yarn_v1_and_berry(self):
        v1 = ('# yarn lockfile v1\n\n\n"@babel/code-frame@^7.0.0", "@babel/code-frame@^7.10.4":\n'
              '  version "7.12.13"\n  resolved "https://registry.yarnpkg.com/x"\n  dependencies:\n'
              '    "@babel/highlight" "^7.12.13"\n\nlodash@^4.17.15:\n  version "4.17.15"\n\n'
              'local-thing@file:../thing:\n  version "0.0.1"\n')
        got = by_name(rows({"yarn.lock": v1, "package.json": json.dumps({"dependencies": {"lodash": "^4"}})})[0])
        self.assertEqual(got["@babel/code-frame"]["version"], "7.12.13")
        self.assertEqual((got["lodash"]["direct"], got["@babel/code-frame"]["direct"]), (True, False))
        self.assertTrue(got["local-thing"]["local"])
        self.assertNotIn("@babel/highlight", got, "a dependency line is not an entry")
        berry = ('__metadata:\n  version: 6\n  cacheKey: 8\n\n"alpha-web@workspace:.":\n  version: 0.0.0-use.local\n'
                 '  resolution: "alpha-web@workspace:."\n  languageName: unknown\n  linkType: soft\n\n'
                 '"minimist@npm:^1.2.0, minimist@npm:^1.2.5":\n  version: 1.2.5\n'
                 '  resolution: "minimist@npm:1.2.5"\n  languageName: node\n  linkType: hard\n')
        got = by_name(rows({"yarn.lock": berry})[0])
        self.assertEqual((got["minimist"]["version"], got["minimist"]["local"]), ("1.2.5", False))
        self.assertEqual(got["alpha-web"]["source"], "workspace")

    def test_package_json_alone_is_direct_and_unpinned(self):
        got = by_name(rows({"package.json": json.dumps({"dependencies": {"express": "^4.18.0", "exact": "1.2.3"},
                                                        "devDependencies": {"eslint": "^9"}})})[0])
        self.assertEqual(got["express"].get("pinned"), False)
        self.assertNotIn("pinned", got["exact"], "an exact version in a manifest is pinned")
        self.assertTrue(got["eslint"]["dev"])

    def test_python_formats(self):
        pipfile_lock = json.dumps({"_meta": {"sources": [{"name": "pypi", "url": "https://pypi.org/simple"},
                                                          {"name": "corp", "url": "https://pypi.example.invalid/simple"}]},
                                   "default": {"requests": {"version": "==2.31.0", "index": "pypi"},
                                               "corp-lib": {"version": "==1.0", "index": "corp"},
                                               "mylib": {"editable": True, "path": "."}},
                                   "develop": {"pytest": {"version": "==8.0.0"}}})
        got = by_name(rows({"Pipfile.lock": pipfile_lock, "Pipfile": '[packages]\nrequests = "*"\n'})[0])
        self.assertEqual((got["requests"]["version"], got["requests"]["direct"]), ("2.31.0", True))
        self.assertEqual(got["corp-lib"]["source"], "private")
        self.assertEqual(got["mylib"]["source"], "path")
        self.assertTrue(got["pytest"]["dev"])

        poetry = ('[[package]]\nname = "django"\nversion = "4.2.0"\n\n[[package]]\nname = "sqlparse"\nversion = "0.4.4"\n\n'
                  '[[package]]\nname = "shared"\nversion = "0.1.0"\n\n[package.source]\ntype = "directory"\nurl = "../shared"\n')
        got = by_name(rows({"poetry.lock": poetry,
                            "pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.11"\nDjango = "^4"\n'})[0])
        self.assertEqual((got["django"]["direct"], got["sqlparse"]["direct"]), (True, False))
        self.assertTrue(got["shared"]["local"])

        uv = ('version = 1\n\n[[package]]\nname = "alpha"\nversion = "0.1.0"\nsource = { editable = "." }\n'
              'dependencies = [{ name = "httpx" }]\n\n[[package]]\nname = "httpx"\nversion = "0.27.0"\n'
              'source = { registry = "https://pypi.org/simple" }\n\n[[package]]\nname = "anyio"\nversion = "4.0.0"\n'
              'source = { registry = "https://pypi.org/simple" }\n\n[[package]]\nname = "secret-sdk"\nversion = "2.0.0"\n'
              'source = { registry = "https://pypi.example.invalid/simple" }\n')
        got = by_name(rows({"uv.lock": uv})[0])
        self.assertEqual((got["httpx"]["direct"], got["anyio"]["direct"]), (True, False))
        self.assertEqual(got["alpha"]["source"], "path")
        self.assertEqual(got["secret-sdk"]["source"], "private")

        pylock = ('lock-version = "1.0"\n\n[[packages]]\nname = "attrs"\nversion = "23.2.0"\n\n'
                  '[[packages]]\nname = "here"\ndirectory = { path = "." }\n')
        got = by_name(rows({"pylock.toml": pylock})[0])
        self.assertEqual(got["attrs"]["version"], "23.2.0")
        self.assertTrue(got["here"]["local"])

        reqs = ("flask==3.0.0  # web\nrequests>=2\n-e git+https://example.invalid/x.git#egg=xlib\n"
                "-r other.txt\nmypkg @ file:../mypkg\nPyYAML[extra]==6.0.1\n")
        got = by_name(rows({"requirements-dev.txt": reqs})[0])
        self.assertEqual(got["flask"]["version"], "3.0.0")
        self.assertNotIn("requests", got, "a range is not a version")
        self.assertEqual((got["xlib"]["source"], got["mypkg"]["source"]), ("git", "path"))
        self.assertEqual(got["PyYAML"]["version"], "6.0.1")

    def test_go_cargo_gems_composer(self):
        gomod = ("module example.invalid/alpha\n\ngo 1.22\n\nrequire (\n\tgolang.org/x/net v0.17.0\n"
                 "\tgithub.com/pkg/errors v0.9.1 // indirect\n\texample.invalid/shared v0.0.0\n)\n\n"
                 "require github.com/google/uuid v1.6.0\n\nreplace example.invalid/shared => ../shared\n")
        got = by_name(rows({"go.mod": gomod})[0])
        self.assertEqual((got["golang.org/x/net"]["version"], got["golang.org/x/net"]["direct"]), ("0.17.0", True))
        self.assertFalse(got["github.com/pkg/errors"]["direct"])
        self.assertTrue(got["example.invalid/shared"]["local"])
        self.assertEqual(got["github.com/google/uuid"]["version"], "1.6.0")

        cargo = ('version = 3\n\n[[package]]\nname = "alpha"\nversion = "0.1.0"\ndependencies = ["serde"]\n\n'
                 '[[package]]\nname = "serde"\nversion = "1.0.190"\n'
                 'source = "registry+https://github.com/rust-lang/crates.io-index"\n\n'
                 '[[package]]\nname = "itoa"\nversion = "1.0.9"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n\n'
                 '[[package]]\nname = "forked"\nversion = "0.2.0"\nsource = "git+https://example.invalid/forked#abc"\n')
        got = by_name(rows({"Cargo.lock": cargo})[0])
        self.assertEqual((got["serde"]["ecosystem"], got["serde"]["direct"], got["itoa"]["direct"]),
                         ("crates.io", True, False))
        self.assertEqual((got["alpha"]["source"], got["forked"]["source"]), ("workspace", "git"))

        gems = ("PATH\n  remote: ../engine\n  specs:\n    engine (0.1.0)\n\nGEM\n  remote: https://rubygems.org/\n"
                "  specs:\n    nokogiri (1.15.4-arm64-darwin)\n      racc (~> 1.4)\n    racc (1.7.1)\n    rails (7.1.0)\n\n"
                "PLATFORMS\n  arm64-darwin\n\nDEPENDENCIES\n  engine!\n  rails (~> 7.1)\n\nBUNDLED WITH\n   2.4.10\n")
        got = by_name(rows({"Gemfile.lock": gems})[0])
        self.assertEqual(got["nokogiri"]["version"], "1.15.4")
        self.assertEqual((got["rails"]["direct"], got["racc"]["direct"]), (True, False))
        self.assertTrue(got["engine"]["local"])
        self.assertEqual(got["racc"]["ecosystem"], "RubyGems")

        composer = json.dumps({"packages": [{"name": "example/http-kernel", "version": "v5.4.20"},
                                            {"name": "acme/local", "version": "dev-main", "dist": {"type": "path"}}],
                               "packages-dev": [{"name": "phpunit/phpunit", "version": "10.0.0"}]})
        got = by_name(rows({"composer.lock": composer,
                            "composer.json": json.dumps({"require": {"example/http-kernel": "^5.4"}})})[0])
        self.assertEqual((got["example/http-kernel"]["version"], got["example/http-kernel"]["direct"]),
                         ("5.4.20", True))
        self.assertTrue(got["acme/local"]["local"])
        self.assertTrue(got["phpunit/phpunit"]["dev"])

    def test_named_private_packages_are_local(self):
        got = by_name(rows({"package-lock.json": NPM_LOCK},
                           private=({"lodash"}, ("@example/",)))[0])
        self.assertEqual((got["lodash"]["local"], got["lodash"]["source"]), (True, "private"))


class Walk(unittest.TestCase):
    def test_installed_trees_are_skipped_and_monorepo_members_read(self):
        root = folder({"node_modules/x/package-lock.json": NPM_LOCK, ".venv/lib/requirements.txt": "a==1\n",
                       "packages/web/requirements.txt": "flask==3.0.0\n", "go.mod": "module m\n"})
        self.assertEqual([p.relative_to(root).as_posix() for p, _ in sd.lockfiles(root)],
                         ["go.mod", "packages/web/requirements.txt"])

    def test_a_workspace_members_manifest_is_covered_by_the_roots_lock(self):
        root = folder({"package-lock.json": NPM_LOCK, "packages/web/package.json": '{"dependencies": {"a": "^1"}}',
                       "tools/package.json": '{"dependencies": {"b": "^1"}}', "uv.lock": "version = 1\nrevision = 3\n"})
        self.assertEqual([(p.relative_to(root).as_posix(), f) for p, f in sd.lockfiles(root)],
                         [("package-lock.json", "npm-lock"), ("uv.lock", "uv-lock")])
        files, bad = sd.scan_folder(root, "alpha-web", {})
        self.assertEqual((bad, [f for f in files if f["path"] == "uv.lock"][0]["packages"]), ([], []),
                         "a uv.lock with no dependencies has no package table and is read as empty")

    def test_an_unchanged_file_is_not_parsed_again(self):
        root = folder({"requirements.txt": "flask==3.0.0\n"})
        first, _ = sd.scan_folder(root, "alpha-web", {})
        previous = {("alpha-web", "requirements.txt"): dict(first[0], packages=[{"name": "cached"}])}
        again, _ = sd.scan_folder(root, "alpha-web", previous)
        self.assertEqual(again[0]["packages"], [{"name": "cached"}], "an unchanged file keeps its rows")
        (root / "requirements.txt").write_text("flask==3.0.1\nclick==8.1.0\n", encoding="utf-8")
        os.utime(root / "requirements.txt", ns=(1, 2))
        changed, _ = sd.scan_folder(root, "alpha-web", previous)
        self.assertEqual({r["name"] for r in changed[0]["packages"]}, {"flask", "click"})

    def test_scan_and_registry_document(self):
        data = folder({"alpha-web/package-lock.json": NPM_LOCK,
                       "alpha-web/api/requirements.txt": "flask==3.0.0\n",
                       "beta-api/Cargo.lock": "this is not toml ===\n"})
        projects = [{"id": "project:alpha-web", "local_folders": ["alpha-web"]},
                    {"id": "project:beta-api", "local_folders": ["beta-api"]},
                    {"id": "project:gamma", "local_folders": ["missing-folder"]}]
        doc = dr.document(sd.scan(projects, data), "2026-11-14")
        self.assertEqual([p["project"] for p in doc["projects"]], ["project:alpha-web", "project:beta-api"])
        alpha, beta = doc["projects"]
        self.assertEqual(alpha["ecosystems"], ["PyPI", "npm"])
        self.assertEqual({r["file"] for r in alpha["packages"]},
                         {"alpha-web/package-lock.json", "alpha-web/api/requirements.txt"})
        self.assertEqual(beta["packages"], [])
        self.assertEqual(len(beta["degraded"]), 1, "an unparseable lockfile is named, never read as none")
        self.assertEqual(doc["totals"]["unreadable_files"], 1)
        for row in alpha["packages"]:
            self.assertTrue({"name", "version", "ecosystem", "direct", "local"} <= set(row))


# ---- the vulnerability watch --------------------------------------------------------------------------

def advisory(vid: str, modified: str, *, name="lodash", eco="npm", fixed=("4.17.21",), cvss=None,
             db_severity=None, aliases=("CVE-2021-23337",)) -> dict:
    doc = {"id": vid, "modified": modified, "published": "2021-02-15T00:00:00Z", "aliases": list(aliases),
           "summary": f"{name} problem",
           "affected": [{"package": {"ecosystem": eco, "name": name},
                         "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}]
                                     + [{"fixed": f} for f in fixed]}]}]}
    if cvss:
        doc["severity"] = [{"type": "CVSS_V3", "score": cvss}]
    if db_severity:
        doc["database_specific"] = {"severity": db_severity}
    return doc


class FakeOSV:
    """Answers querybatch from `vulns` {(eco, name, version): [(id, modified)]}, details from `records`,
    and the catalogue from `kev`. Records every request so a test can read what left the machine."""

    def __init__(self, vulns, records, kev=None, page=None, down=False):
        self.vulns, self.records, self.kev, self.page, self.down = vulns, records, kev, page, down
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def __call__(self, method, url, body=None, timeout=30):
        if self.down:
            raise OSError("network unreachable")
        if method == "POST":
            doc = json.loads(body)
            self.posts.append(doc)
            results = []
            for q in doc["queries"]:
                key = (q["package"]["ecosystem"], q["package"]["name"], q["version"])
                ids = self.vulns.get(key, [])
                if self.page and key == self.page and "page_token" not in q:
                    results.append({"vulns": [{"id": i, "modified": m} for i, m in ids[:1]], "next_page_token": "p2"})
                elif self.page and key == self.page:
                    results.append({"vulns": [{"id": i, "modified": m} for i, m in ids[1:]]})
                else:
                    results.append({"vulns": [{"id": i, "modified": m} for i, m in ids]} if ids else {})
            return json.dumps({"results": results}).encode()
        self.gets.append(url)
        if url == vw.KEV_URL:
            return json.dumps(self.kev or {"vulnerabilities": []}).encode()
        vid = url.rsplit("/", 1)[-1]
        return json.dumps(self.records[vid]).encode()


def registry(packages: list[dict]) -> dict:
    return {"projects": [{"project": "project:alpha-web", "packages": packages, "read_from": [], "degraded": []}]}


PKGS = [
    {"name": "lodash", "version": "4.17.20", "ecosystem": "npm", "direct": True, "local": False,
     "file": "alpha-web/package-lock.json"},
    {"name": "internal-kit", "version": "1.0.0", "ecosystem": "npm", "direct": True, "local": True,
     "source": "private", "file": "alpha-web/package-lock.json"},
    {"name": "express", "version": "^4.18.0", "ecosystem": "npm", "direct": True, "local": False, "pinned": False,
     "file": "alpha-web/package.json"},
    {"name": "flask", "version": "3.0.0", "ecosystem": "PyPI", "direct": True, "local": False,
     "file": "alpha-web/api/requirements.txt"},
]


class Watch(unittest.TestCase):
    def setUp(self):
        self.cache = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-osv-"))

    def run_watch(self, fake, previous=None, today="2026-11-14", packages=PKGS):
        return vw.run(vw.inventory(registry(packages)), fake, self.cache, today, previous=previous)

    def test_local_and_unpinned_packages_never_leave_the_machine(self):
        fake = FakeOSV({("npm", "lodash", "4.17.20"): [("GHSA-1", "2024-01-01T00:00:00Z")]},
                       {"GHSA-1": advisory("GHSA-1", "2024-01-01T00:00:00Z", db_severity="HIGH")})
        doc = self.run_watch(fake)
        sent = json.dumps(fake.posts)
        self.assertNotIn("internal-kit", sent)
        self.assertNotIn("express", sent)
        self.assertEqual(sorted(q["package"]["name"] for p in fake.posts for q in p["queries"]), ["flask", "lodash"])
        self.assertEqual(doc["matches"], [{"ecosystem": "npm", "name": "lodash", "version": "4.17.20",
                                           "advisories": ["GHSA-1"]}])
        self.assertEqual(doc["degraded"], [])

    def test_pagination_is_followed(self):
        key = ("npm", "lodash", "4.17.20")
        fake = FakeOSV({key: [("GHSA-1", "2024-01-01T00:00:00Z"), ("GHSA-2", "2024-01-02T00:00:00Z")]},
                       {"GHSA-1": advisory("GHSA-1", "2024-01-01T00:00:00Z"),
                        "GHSA-2": advisory("GHSA-2", "2024-01-02T00:00:00Z")}, page=key)
        doc = self.run_watch(fake)
        self.assertEqual(doc["matches"][0]["advisories"], ["GHSA-1", "GHSA-2"])
        self.assertTrue(any(q.get("page_token") == "p2" for p in fake.posts for q in p["queries"]))

    def test_details_are_cached_by_modified(self):
        rec = advisory("GHSA-1", "2024-01-01T00:00:00Z")
        fake = FakeOSV({("npm", "lodash", "4.17.20"): [("GHSA-1", "2024-01-01T00:00:00Z")]}, {"GHSA-1": rec})
        self.run_watch(fake)
        self.assertEqual([u for u in fake.gets if "/vulns/" in u], [f"{vw.OSV}/v1/vulns/GHSA-1"])
        fake.gets.clear()
        self.run_watch(fake, today="2026-11-15")
        self.assertEqual([u for u in fake.gets if "/vulns/" in u], [], "an unchanged record is read from the cache")
        fake.vulns[("npm", "lodash", "4.17.20")] = [("GHSA-1", "2024-06-01T00:00:00Z")]
        fake.records["GHSA-1"] = advisory("GHSA-1", "2024-06-01T00:00:00Z", fixed=("4.17.22",))
        doc = self.run_watch(fake, today="2026-11-16")
        self.assertEqual(len([u for u in fake.gets if "/vulns/" in u]), 1, "a newer record is fetched again")
        self.assertEqual(doc["advisories"]["GHSA-1"]["fixed"]["npm/lodash"], ["4.17.22"])

    def test_the_catalogue_is_fetched_once_a_day_and_marks_exploited(self):
        fake = FakeOSV({("npm", "lodash", "4.17.20"): [("GHSA-1", "2024-01-01T00:00:00Z")]},
                       {"GHSA-1": advisory("GHSA-1", "2024-01-01T00:00:00Z", db_severity="HIGH")},
                       kev={"vulnerabilities": [{"cveID": "CVE-2021-23337", "dateAdded": "2024-03-01"}]})
        doc = self.run_watch(fake)
        self.assertTrue(doc["advisories"]["GHSA-1"]["exploited"])
        self.assertEqual(fake.gets.count(vw.KEV_URL), 1)
        self.run_watch(fake, previous=doc)
        self.assertEqual(fake.gets.count(vw.KEV_URL), 1, "the catalogue is read once a day")

    def test_offline_keeps_the_last_answer_with_its_date(self):
        fake = FakeOSV({("npm", "lodash", "4.17.20"): [("GHSA-1", "2024-01-01T00:00:00Z")]},
                       {"GHSA-1": advisory("GHSA-1", "2024-01-01T00:00:00Z")})
        first = self.run_watch(fake)
        fake.down = True
        again = self.run_watch(fake, previous=first, today="2026-11-15")
        self.assertEqual(again["matches"], first["matches"])
        self.assertEqual(again["checked_on"], "2026-11-14", "the answer keeps the date it was measured")
        self.assertFalse(again["complete"])
        self.assertTrue(any("2026-11-14" in d["reason"] for d in again["degraded"]))

    def test_switched_off_and_offline_runs(self):
        self.assertTrue(vw.switched_on({"integrations": {}}, environ={}), "on by default")
        self.assertFalse(vw.switched_on({"integrations": {"vulnerabilities": False}}, environ={}))
        self.assertFalse(vw.switched_on({"integrations": {}}, environ={"OBSERVATORY_OFFLINE": "1"}))

    def test_due_once_a_day_or_when_the_inventory_grows(self):
        queries = vw.inventory(registry(PKGS))
        state = {"last_complete_on": "2026-11-14", "inventory": vw.digest(queries)}
        self.assertFalse(vw.due(state, queries, "2026-11-14"))
        self.assertTrue(vw.due(state, queries, "2026-11-15"))
        grown = queries + [("npm", "left-pad", "1.3.0")]
        self.assertTrue(vw.due(state, grown, "2026-11-14"))

    def test_severity_and_fixed_version(self):
        self.assertEqual(vw.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"), 9.8)
        self.assertEqual(vw.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:L/I:L/A:N"), 6.4)
        self.assertEqual(vw.severity_of(advisory("A", "x", db_severity="MODERATE")), ("medium", "database"))
        self.assertEqual(vw.severity_of(advisory("A", "x", cvss="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")),
                         ("critical", "cvss 9.8"))
        self.assertEqual(vw.severity_of(advisory("A", "x"))[0], "unknown")
        self.assertEqual(vw.fixed_for(["1.2.5", "2.0.1"], "2.0.0"), "2.0.1")
        self.assertEqual(vw.fixed_for(["1.2.5", "2.0.1"], "1.2.0"), "1.2.5")
        self.assertIsNone(vw.fixed_for([], "1.0.0"))


class Findings(unittest.TestCase):
    def doc(self, sev="HIGH", kev=False):
        rec = advisory("GHSA-1", "2024-01-01T00:00:00Z", db_severity=sev)
        fake = FakeOSV({("npm", "lodash", "4.17.20"): [("GHSA-1", "2024-01-01T00:00:00Z")]}, {"GHSA-1": rec},
                       kev={"vulnerabilities": [{"cveID": "CVE-2021-23337"}]} if kev else None)
        cache = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-osv-"))
        return vw.run(vw.inventory(registry(PKGS)), fake, cache, "2026-11-14")

    def test_one_finding_per_project_package_with_advisory_fixed_and_lockfile(self):
        found = df.findings(registry(PKGS), self.doc(), "2026-11-14")
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual(f["type"], "dep.vulnerable")
        self.assertEqual(f["subject"], "dep:alpha-web/npm/lodash@4.17.20")
        self.assertEqual((f["advisory"], f["fixed"], f["exploited"], f["lockfile"]),
                         ("GHSA-1", "4.17.21", False, "package-lock.json"))
        self.assertEqual(f["aliases"], ["CVE-2021-23337"])
        self.assertEqual(f["severity"], "warning")

    def test_exploited_raises_one_level(self):
        self.assertEqual(df.findings(registry(PKGS), self.doc("HIGH", kev=True), "2026-11-14")[0]["severity"],
                         "critical")
        self.assertEqual(df.findings(registry(PKGS), self.doc("LOW", kev=True), "2026-11-14")[0]["severity"],
                         "warning")
        self.assertEqual(df.findings(registry(PKGS), self.doc("LOW"), "2026-11-14")[0]["severity"], "info")

    def test_a_stale_watch_is_said(self):
        doc = self.doc()
        found = df.findings(registry(PKGS), dict(doc, checked_on="2026-11-01"), "2026-11-14")
        self.assertIn("dep.watch_stale", {f["type"] for f in found})
        self.assertEqual(df.findings(registry(PKGS), None, "2026-11-14"), [])

    def test_every_type_is_labelled(self):
        sys.path.insert(0, str(ROOT / "dashboard"))
        import finding_types
        for kind in df.TYPES:
            self.assertIn(kind, finding_types.LABELS)


class AgentView(unittest.TestCase):
    def test_vulnerable_only_paging_and_refusals(self):
        rec = advisory("GHSA-1", "2024-01-01T00:00:00Z", db_severity="CRITICAL")
        fake = FakeOSV({("npm", "lodash", "4.17.20"): [("GHSA-1", "2024-01-01T00:00:00Z")]}, {"GHSA-1": rec})
        vulns = vw.run(vw.inventory(registry(PKGS)), fake, pathlib.Path(tmpdir.mkdtemp()), "2026-11-14")
        reg = dict(registry(PKGS), updated_on="2026-11-14", degraded=[])
        page = vw.agent_view(registry_doc=reg, vulns_doc=vulns, vulnerable_only=True)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["packages"][0]["advisories"][0]["id"], "GHSA-1")
        self.assertEqual(page["degraded"], [])
        first = vw.agent_view(registry_doc=reg, vulns_doc=vulns, limit=2)
        self.assertEqual(first["total"], 4)
        self.assertEqual(first["packages"][0]["name"], "lodash", "the vulnerable come first")
        rest = vw.agent_view(registry_doc=reg, vulns_doc=vulns, limit=2, cursor=first["nextCursor"])
        self.assertEqual(len(rest["packages"]), 2)
        self.assertNotIn("nextCursor", rest)
        self.assertEqual(vw.agent_view(registry_doc=reg, vulns_doc=vulns, cursor="nope")["error"], "unknown cursor")
        self.assertEqual(vw.agent_view(registry_doc=reg, vulns_doc=vulns, project_id="project:none",
                                       known_projects={"project:alpha-web"})["error"], "unknown project")
        missing = vw.agent_view(registry_doc=None, vulns_doc=None)
        self.assertEqual(missing["packages"], [])
        self.assertTrue(missing["degraded"])


if __name__ == "__main__":
    unittest.main()
