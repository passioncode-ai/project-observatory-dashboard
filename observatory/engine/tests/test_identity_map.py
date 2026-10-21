"""The persisted identity map: docs/design/IDENTITY.md, "Tests that define done"."""
from __future__ import annotations
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import identity_map as M  # noqa: E402

D1, D2, D3 = "2026-09-01", "2026-09-02", "2026-09-03"


def project(repos=(), local_only=None):
    return {"repos": list(repos), "local_only": local_only}


def run(projects, doc=None, *, repos=None, transfers=None, overrides=None, today=D2, complete=True):
    return M.resolve(projects, repos or {}, transfers or {}, overrides or {}, doc, today, complete)


def git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    from git_env import git_env
    env = git_env({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(path)})
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=path, check=True, env=env)


class IdentityMap(unittest.TestCase):
    def test_first_run_changes_no_id(self):
        ids, doc, _ = run({"example-app": project(["o/app"]), "local-tool": project()}, today=D1)
        self.assertEqual(ids, {"example-app": "project:example-app", "local-tool": "project:local-tool"})
        self.assertEqual(doc["projects"]["project:example-app"]["keys"], ["example-app"])

    def test_renaming_a_wiki_folder_keeps_the_id(self):
        _, doc, _ = run({"old-name": project(["o/app"])}, today=D1)
        ids, doc2, changes = run({"new-name": project(["o/app"])}, doc)
        self.assertEqual(ids["new-name"], "project:old-name")
        self.assertIn({"change": "renamed", "id": "project:old-name", "key": "new-name", "was": ["old-name"]}, changes)
        self.assertEqual(doc2["projects"]["project:old-name"]["keys"], ["new-name", "old-name"])

    def test_a_repository_transfer_keeps_the_project_and_records_the_former_name(self):
        _, doc, _ = run({"o-app": project(["o/app"])}, today=D1)
        ids, doc2, _ = run({"p-app": project(["p/app"])}, doc, transfers={"o/app": "p/app"})
        self.assertEqual(ids["p-app"], "project:o-app")
        self.assertEqual(doc2["repositories"]["repository:p/app"]["former"], ["o/app"])

    def test_a_local_git_folder_keeps_its_id_through_its_root_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            git_repo(base / "before")
            lo = {"path": str(base / "before"), "unpublished": True}
            _, doc, _ = run({"local-before": project(local_only=lo)}, today=D1)
            (base / "before").rename(base / "after")
            lo2 = {"path": str(base / "after"), "unpublished": True}
            ids, _, _ = run({"local-after": project(local_only=lo2)}, doc)
            self.assertEqual(ids["local-after"], "project:local-before")

    def test_a_local_folder_without_history_is_a_new_project(self):
        _, doc, _ = run({"local-a": project(local_only={"path": "/srv/a"})}, today=D1)
        ids, _, _ = run({"local-b": project(local_only={"path": "/srv/b"})}, doc)
        self.assertEqual(ids["local-b"], "project:local-b", "names are not anchors: no guessing")

    def test_a_new_project_sharing_a_repository_with_live_ones_is_new(self):
        _, doc, _ = run({"one": project(["o/shared", "o/one"]), "two": project(["o/shared", "o/two"])}, today=D1)
        ids, _, _ = run({"two": project(["o/shared", "o/two"]), "one": project(["o/shared", "o/one"]),
                         "third": project(["o/shared"])}, doc)
        self.assertEqual(ids["third"], "project:third", "the order of keys cannot turn it into a rename")

    def test_two_unclaimed_candidates_are_ambiguous_and_mint_a_new_id(self):
        _, doc, _ = run({"one": project(["o/shared", "o/one"]), "two": project(["o/shared", "o/two"])}, today=D1)
        ids, doc2, _ = run({"third": project(["o/shared"])}, doc, complete=False)
        self.assertEqual(ids["third"], "project:third")
        self.assertEqual(doc2["ambiguities"][0]["candidates"], ["project:one", "project:two"])

    def test_a_partial_scan_retires_nothing_and_a_complete_one_retires(self):
        _, doc, _ = run({"gone": project(["o/gone"]), "stays": project(["o/stays"])}, today=D1)
        _, partial, _ = run({"stays": project(["o/stays"])}, doc, complete=False)
        self.assertIsNone(partial["projects"]["project:gone"]["retired_on"])
        _, full, changes = run({"stays": project(["o/stays"])}, doc, complete=True)
        self.assertEqual(full["projects"]["project:gone"]["retired_on"], D2)
        self.assertIn({"change": "retired", "id": "project:gone"}, changes)

    def test_a_retired_id_is_never_minted_for_someone_else(self):
        _, doc, _ = run({"gone": project(["o/gone"])}, today=D1)
        _, doc, _ = run({}, doc, complete=True)
        ids, _, _ = run({"gone!": project(["o/other"])}, doc, today=D3)
        self.assertNotEqual(ids["gone!"], "project:gone", "a new project never inherits a retired id")

    def test_the_same_key_returning_is_the_same_project(self):
        _, doc, _ = run({"back": project(["o/back"])}, today=D1)
        _, doc, _ = run({}, doc, complete=True)
        ids, doc2, changes = run({"back": project(["o/back"])}, doc, today=D3)
        self.assertEqual(ids["back"], "project:back")
        self.assertIsNone(doc2["projects"]["project:back"]["retired_on"])
        self.assertIn({"change": "returned", "id": "project:back", "key": "back"}, changes)

    def test_an_override_always_wins(self):
        _, doc, _ = run({"old-name": project(["o/app"])}, today=D1)
        ids, _, _ = run({"new-name": project(["o/app"])}, doc, overrides={"new-name": "pinned"})
        self.assertEqual(ids["new-name"], "project:pinned")

    def test_two_current_projects_never_share_one_id(self):
        _, doc, _ = run({"a": project(["o/app"])}, today=D1)
        ids, doc2, _ = run({"a": project(["o/app"]), "b": project(["o/b"])}, doc, overrides={"b": "a"})
        self.assertEqual(len({ids["a"], ids["b"]}), 2)
        self.assertTrue(doc2["ambiguities"], "the conflict is reported, not resolved silently")

    def test_the_map_is_deterministic(self):
        projects = {k: project([f"o/{k}"]) for k in ("c", "a", "b")}
        self.assertEqual(run(projects)[1], run(dict(reversed(list(projects.items()))))[1])


if __name__ == "__main__":
    unittest.main()
