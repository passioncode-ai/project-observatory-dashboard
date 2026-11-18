#!/usr/bin/env python3
"""Secret access bound to an agent's project (OBS-635): secret_binding.check over a real access-bindings registry.

Under a binding with the scope `secrets.use`, a slot of the binding's project is released and a slot of another
project is refused; a binding without the scope, a revoked binding, an unreadable token file and an unknown project
are refusals, never the unbound path; with no token file the run is unbound (None)."""
from __future__ import annotations

import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
from test_memory_access import ALPHA, BETA, Workspace, tearDownModule  # noqa: E402,F401
from test_ops_digest_record import private  # noqa: E402

import secret_binding as SB  # noqa: E402

FOLDERS = {"alpha-web": ALPHA, "beta-api": BETA}


class SecretBinding(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.env: dict[str, str] = {}

    def bind(self, projects=(ALPHA,), scopes=(SB.SCOPE,), bearer="tok-secrets", **over) -> pathlib.Path:
        self.ws.bind("agent:alpha-builder", list(projects), bearer=bearer, scopes=scopes, effect="read", **over)
        path = self.ws.home / f"{bearer}.token"
        path.write_text(bearer + "\n", encoding="utf-8")
        private(path)
        self.env[SB.TOKEN_FILE_ENV] = str(path)
        return path

    def check(self, project: str):
        return SB.check(project, self.env, project_of=FOLDERS.get)

    def refused(self, project: str) -> str:
        with self.assertRaises(SB.Refused) as caught:
            self.check(project)
        return caught.exception.code

    def test_no_token_file_is_unbound(self) -> None:
        self.assertIsNone(self.check("alpha-web"))

    def test_the_bindings_own_project_is_released(self) -> None:
        self.bind()
        got = self.check("alpha-web")
        self.assertEqual(got.project_id, ALPHA)
        self.assertTrue(got.binding_id.startswith("bnd_"))

    def test_another_projects_slot_is_refused(self) -> None:
        self.bind()
        self.assertEqual(self.refused("beta-api"), "project-not-bound")

    def test_a_binding_without_the_scope_releases_nothing(self) -> None:
        self.bind(scopes=("memory.read",))
        self.assertEqual(self.refused("alpha-web"), "scope-not-bound")

    def test_an_unknown_project_is_refused(self) -> None:
        self.bind()
        self.assertEqual(self.refused("nowhere"), "project-unknown")

    def test_a_token_others_can_read_is_refused(self) -> None:
        path = self.bind()
        os.chmod(path, 0o644)
        self.assertEqual(self.refused("alpha-web"), "binding-unreadable")

    def test_a_token_the_registry_does_not_know_is_refused(self) -> None:
        path = self.bind()
        path.write_text("tok-somebody-else\n", encoding="utf-8")
        private(path)
        self.assertEqual(self.refused("alpha-web"), "binding-refused")


if __name__ == "__main__":
    unittest.main()
