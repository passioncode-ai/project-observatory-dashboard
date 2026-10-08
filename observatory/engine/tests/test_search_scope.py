#!/usr/bin/env python3
"""Scope and validity are filtered BEFORE the candidate window (PB-137 N-009).

`survey.search` takes the best `RETRIEVAL_WIDTH` lexical matches and only then looks at
whose they are. A caller limited to one project, or to the classes under its binding's
ceiling (N-008), could therefore lose its own record behind three hundred stronger
matches it may not see, and the rows it may not see still moved `belowFloor`, the
abstain reason and the projection-lag count. A record past its `valid_to` (or before its
`valid_from`) was served as current.

This suite builds those cases in a temporary store:

* an allowed record stays findable among more than `RETRIEVAL_WIDTH` stronger foreign
  matches, by project and by class;
* rows the caller may not see change nothing it can observe: `total`, `belowFloor`, the
  abstain reason, the lag count;
* expired and not-yet-valid records are not current; tombstoned and superseded
  revisions never come back;
* a full window is said, not hidden.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

_MADE: list[pathlib.Path] = []
ALPHA, BETA = "project:alpha-web", "project:beta-api"
OWNER = "agent:scope-test"


def tearDownModule() -> None:
    for path in _MADE:
        shutil.rmtree(path, ignore_errors=True)


def _at(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self) -> None:
        self.home = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-scope-")).resolve()
        _MADE.append(self.home)
        os.environ["OBSERVATORY_HOME"] = str(self.home)
        os.environ["OBSERVATORY_DB"] = str(self.home / "store" / "observatory.db")  # paths-check: allow — an isolated workspace's store, handed over as OBSERVATORY_DB; this IS the redirection
        os.environ["OBSERVATORY_STATE"] = str(self.home / "store")
        (self.home / "store").mkdir(parents=True, exist_ok=True)
        for name in [k for k in list(sys.modules)
                     if k in ("paths", "configuration", "survey", "store", "embedding_policy",
                              "textkeys", "providers") or k.startswith("store.")]:
            sys.modules.pop(name, None)
        import paths
        paths.CONFIG.mkdir(parents=True, exist_ok=True)
        (paths.CONFIG / "models.json").write_text(
            (ROOT / "defaults/models.json").read_text(encoding="utf-8"), encoding="utf-8")
        from store import db as sdb
        from store import ledger as L
        self.sdb, self.L = sdb, L
        self.survey = importlib.import_module("survey")
        self.conn = sdb.connect()

    def note(self, statement: str, project: str = ALPHA, classification: str = "project-internal",
             **kw) -> dict:
        out = self.L.append(self.conn, owner=OWNER, statement=statement, project_id=project,
                            classification=classification, confidence=0.5, **kw)
        self.conn.commit()
        return out

    def many(self, n: int, statement: str, **kw) -> None:
        for i in range(n):
            self.L.append(self.conn, owner=OWNER, statement=f"{statement} {i}", confidence=0.5,
                          **kw)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Store()

    def tearDown(self) -> None:
        self.s.close()

    def statements(self, out: dict) -> list[str]:
        return [r["statement"] for r in out["results"]]


class TheWindowIsTheCallersOwn(Base):
    # Distractors repeat the query words, so bm25 ranks them above the one allowed hit.
    STRONG = "exporter exporter exporter schema schema schema"

    def test_a_project_hit_survives_more_than_a_window_of_foreign_matches(self) -> None:
        width = self.s.survey.RETRIEVAL_WIDTH
        self.s.many(width + 50, self.STRONG, project_id=BETA)
        self.s.note("the exporter schema decision", project=ALPHA)
        out = self.s.survey.search("exporter schema", project_id=ALPHA, limit=5)
        self.assertEqual(self.statements(out), ["the exporter schema decision"], out)
        self.assertEqual(out["total"], 1)

    def test_a_class_hit_survives_more_than_a_window_of_unseen_classes(self) -> None:
        width = self.s.survey.RETRIEVAL_WIDTH
        self.s.many(width + 50, self.STRONG, project_id=ALPHA, classification="confidential")
        self.s.note("the exporter schema decision", classification="public")
        out = self.s.survey.search("exporter schema", project_id=ALPHA, limit=5,
                                   classes=("public",))
        self.assertEqual(self.statements(out), ["the exporter schema decision"], out)
        self.assertEqual(out["total"], 1)

    def test_a_full_window_is_said(self) -> None:
        width = self.s.survey.RETRIEVAL_WIDTH
        self.s.many(width + 5, self.STRONG, project_id=ALPHA)
        out = self.s.survey.search("exporter schema", project_id=ALPHA, limit=5)
        self.assertIn("window", [d["source"] for d in out["degraded"]], out["degraded"])


class UnseenRowsLeaveNoTrace(Base):
    def test_below_floor_and_abstain_count_only_what_the_caller_may_see(self) -> None:
        # A foreign row sharing one of three keys is below the floor; it must not be counted.
        self.s.many(5, "exporter", project_id=BETA)
        out = self.s.survey.search("exporter schema migration", project_id=ALPHA, limit=5)
        self.assertTrue(out["abstain"])
        self.assertEqual(out["floor"]["belowFloor"], 0, out["floor"])
        self.assertEqual(out["abstainReason"], "nothing matched")

    def test_lag_counts_only_the_callers_scope(self) -> None:
        self.s.many(3, "exporter schema", project_id=BETA)
        out = self.s.survey.search("exporter schema", project_id=ALPHA, limit=5)
        lag = [d for d in out["degraded"] if d["source"] == "projection"]
        self.assertEqual(lag, [], "another project's unindexed rows are not this caller's lag")
        mine = self.s.survey.search("exporter schema", project_id=BETA, limit=5)
        self.assertTrue([d for d in mine["degraded"] if d["source"] == "projection"],
                        "the owner of the rows still sees its lag")

    def test_class_filter_is_no_longer_a_degradation(self) -> None:
        self.s.note("exporter schema", classification="public")
        out = self.s.survey.search("exporter schema", project_id=ALPHA, classes=("public",))
        self.assertNotIn("class-filter", [d["source"] for d in out["degraded"]])


class OnlyCurrentRecords(Base):
    def test_expired_and_not_yet_valid_records_are_not_current(self) -> None:
        self.s.note("exporter schema expired", valid_to=_at(-1))
        self.s.note("exporter schema future", valid_from=_at(1))
        self.s.note("exporter schema current", valid_from=_at(-1), valid_to=_at(1))
        self.s.note("exporter schema open-ended")
        out = self.s.survey.search("exporter schema", project_id=ALPHA, limit=10)
        self.assertEqual(sorted(self.statements(out)),
                         ["exporter schema current", "exporter schema open-ended"])

    def test_tombstoned_and_superseded_revisions_never_return(self) -> None:
        old = self.s.note("exporter schema first wording")
        self.s.L.append(self.s.conn, owner=OWNER, statement="exporter schema second wording",
                        memory_id=old["memoryId"], expected_revision=old["revision"],
                        project_id=ALPHA, confidence=0.5)
        gone = self.s.note("exporter schema withdrawn")
        self.s.L.tombstone(self.s.conn, gone["memoryId"], reason="test", approved_by="operator")
        self.s.conn.commit()
        out = self.s.survey.search("exporter schema", project_id=ALPHA, limit=10)
        self.assertEqual(self.statements(out), ["exporter schema second wording"])
        self.assertEqual(out["total"], 1)


if __name__ == "__main__":
    unittest.main()
