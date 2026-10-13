#!/usr/bin/env python3
"""The agent's deadline reaches the model call (ENG-003).

`agent/observe.py` started a project's call while 150 s were left before the tick step's deadline,
and `providers.complete()` did not know the deadline at all: it walks a chain of models, each
attempt with `_post`'s 120 s socket timeout, so a three-model chain whose models all answered
Retryable after 120 s took 360 s, plus the catalogue fetch (45 s) and the key-usage read (30 s).
The watchdog then kills a step that has paid for calls and written nothing.

`complete(…, deadline=)` now checks before every attempt: an attempt starts only while
`MIN_ATTEMPT_S` is left after `REPORT_RESERVE_S`, its timeout is cut to what is left, and
otherwise it raises `OutOfTime` having paid for nothing new. The catalogue fetch and the key-usage
read inside the call are cut the same way. Without a deadline nothing changes.

The clock is a fake one: `_post` stands for a provider that holds the socket for the whole
timeout it was given and then fails Retryable, so no test waits and no request leaves.
"""
from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))

import configuration                                   # noqa: E402
import providers as P                                  # noqa: E402

T0 = 1_800_000_000.0
CHAIN = [{"id": f"example/model-{n}", "price_in_per_1m": 0.1, "price_out_per_1m": 0.2,
          "supported_parameters": ["structured_outputs"]} for n in (1, 2, 3)]


class Clock:
    def __init__(self, start: float = T0):
        self.t = start

    def __call__(self) -> float:
        return self.t


class DeadlineReachesTheCall(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.timeouts: list[float] = []
        self.unhealthy: list[str] = []
        self.logged: list[str] = []

        def slow_retryable(base_url, key, body, timeout=120):
            self.timeouts.append(timeout)
            self.clock.t += min(120, timeout)
            raise P.Retryable("HTTP 503: overloaded")

        patches = [
            mock.patch.object(configuration, "enabled", lambda *a, **k: True),
            mock.patch.object(P, "_clock", self.clock),
            mock.patch.object(P, "read_key", lambda: ("stub-key", "stub")),
            mock.patch.object(P, "check_budget", lambda *a, **k: None),
            mock.patch.object(P, "resolve_chain", lambda requested=None: (list(CHAIN), "test", "fixture")),
            mock.patch.object(P, "unhealthy", lambda model_id: None),
            mock.patch.object(P, "mark_unhealthy", lambda model_id, reason: self.unhealthy.append(model_id)),
            mock.patch.object(P, "config", lambda: {"base_url": "http://127.0.0.1:9/api/v1",
                                                    "attempts": {"total": 4}}),
            mock.patch.object(P, "_post", slow_retryable),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def call(self, deadline):
        return P.complete([{"role": "user", "content": "x"}], schema_name="s", schema={"type": "object"},
                          log=self.logged.append, deadline=deadline)

    def test_a_three_model_chain_stops_inside_the_deadline(self):
        """The case in ENG-003: three models, each Retryable after 120 s, 200 s left. Today it makes
        three attempts (360 s). Now the second attempt gets the 60 s that are left after the report's
        reserve and a third is never started; the run ends inside the deadline."""
        with self.assertRaises(P.OutOfTime) as cut:
            self.call(T0 + 200)
        self.assertEqual(self.timeouts, [120, 60])
        self.assertLessEqual(self.clock.t, T0 + 200 - P.REPORT_RESERVE_S)
        self.assertEqual(cut.exception.attempts, 2)
        self.assertIn("deadline", str(cut.exception))

    def test_with_less_than_one_attempt_left_after_the_first_it_stops_before_the_second(self):
        with self.assertRaises(P.OutOfTime):
            self.call(T0 + 150)
        self.assertEqual(self.timeouts, [120], "the second attempt would have had 10 s; it is never started")

    def test_no_time_for_one_attempt_means_nothing_is_paid_for(self):
        with self.assertRaises(P.OutOfTime) as cut:
            self.call(T0 + P.REPORT_RESERVE_S + P.MIN_ATTEMPT_S - 1)
        self.assertEqual(self.timeouts, [], "no request was made")
        self.assertEqual(cut.exception.attempts, 0)

    def test_a_cut_attempt_marks_no_model_unhealthy(self):
        with self.assertRaises(P.OutOfTime):
            self.call(T0 + 150)
        self.assertEqual(self.unhealthy, ["example/model-1"], "only the model that answered 503 is marked")

    def test_a_timeout_the_deadline_clipped_marks_no_model_unhealthy(self):
        """Verification round 1: an attempt whose timeout was CUT by the deadline and then timed out
        says nothing about the model — the full 120 s might have answered. It ends the call as
        OutOfTime and leaves the model's health alone."""
        def slow_answer(base_url, key, body, timeout=120):
            self.timeouts.append(timeout)
            latency = 90
            if latency > timeout:
                self.clock.t += timeout
                raise P.Retryable("TimeoutError: timed out") from TimeoutError("timed out")
            self.clock.t += latency
            raise AssertionError("not reached in this case")
        with mock.patch.object(P, "_post", slow_answer):
            with self.assertRaises(P.OutOfTime) as cut:
                self.call(T0 + 100)
        self.assertEqual(self.timeouts, [80])
        self.assertEqual(self.unhealthy, [], "the clipped timeout is the deadline's, not the model's")
        self.assertEqual(cut.exception.attempts, 1)
        self.assertTrue(cut.exception.sent, "the clipped attempt was sent and may have been billed")
        self.assertIn("may have been billed", str(cut.exception))

    def test_an_attempt_refused_before_it_starts_was_not_sent(self):
        with self.assertRaises(P.OutOfTime) as cut:
            self.call(T0 + 150)
        self.assertFalse(cut.exception.sent)

    def test_a_timeout_at_the_full_120_s_still_marks_the_model(self):
        def stuck(base_url, key, body, timeout=120):
            self.timeouts.append(timeout)
            self.clock.t += timeout
            raise P.Retryable("URLError: timed out") from urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch.object(P, "_post", stuck):
            with self.assertRaises(P.OutOfTime):
                self.call(T0 + 300)
        self.assertEqual(self.timeouts[0], 120)
        self.assertEqual(self.unhealthy[0], "example/model-1", "a full-length timeout is the model's")

    def test_the_deadline_is_a_context_variable(self):
        import contextvars
        self.assertIsInstance(P._DEADLINE, contextvars.ContextVar)

    def test_out_of_time_is_neither_retryable_nor_fatal(self):
        """A caller that retries on Retryable or retires a model on Fatal must not do either for a
        call the clock refused."""
        self.assertFalse(issubclass(P.OutOfTime, (P.Retryable, P.Fatal)))
        self.assertTrue(issubclass(P.OutOfTime, P.ProviderError))

    def test_each_attempt_logs_the_timeout_it_was_given(self):
        with self.assertRaises(P.OutOfTime):
            self.call(T0 + 200)
        given = [line for line in self.logged if "timeout" in line]
        self.assertTrue(any("120 s" in line for line in given) and any("60 s" in line for line in given), given)

    def test_without_a_deadline_nothing_changes(self):
        with self.assertRaises(P.Retryable):
            self.call(None)
        self.assertEqual(self.timeouts, [120, 120, 120], "every model gets its full 120 s, as before")

    def test_the_reserve_is_derived_not_a_constant(self):
        import observe
        self.assertEqual(observe.CALL_RESERVE_S, P.MIN_ATTEMPT_S + P.REPORT_RESERVE_S)


class ReadsInsideTheCallAreCutToo(unittest.TestCase):
    """The catalogue fetch (45 s) and the key-usage read (30 s) run inside `complete` on the first
    call of a run; inside a deadline they get at most what is left."""

    def setUp(self):
        self.clock = Clock()
        self.seen: list[float] = []

        def urlopen(req, timeout=None):
            self.seen.append(timeout)
            raise urllib.error.URLError("offline fixture")

        for p in (mock.patch.object(P, "_clock", self.clock),
                  mock.patch.object(P.urllib.request, "urlopen", urlopen),
                  mock.patch.object(P, "read_key", lambda: ("stub-key", "stub")),
                  mock.patch.object(P, "config", lambda: {"base_url": "http://127.0.0.1:9/api/v1"})):
            p.start()
            self.addCleanup(p.stop)
        P._provider_usage_cache = None

    def test_the_catalogue_fetch_is_cut_to_the_time_left(self):
        with P.deadline_scope(T0 + 40):
            with self.assertRaises(urllib.error.URLError):
                P._fetch_catalogue("http://127.0.0.1:9/api/v1")
        self.assertEqual(self.seen, [40 - P.REPORT_RESERVE_S])

    def test_the_key_usage_read_is_cut_to_the_time_left(self):
        with P.deadline_scope(T0 + 30):
            self.assertIsNone(P.provider_usage(force=True))
        self.assertEqual(self.seen, [10])

    def test_a_catalogue_fetch_the_deadline_cut_is_a_deadline_halt(self):
        """Round 3: with nothing cached, a catalogue fetch the deadline cut read as Retryable
        "catalogue unreachable", so the agent recorded every model unavailable. It is OutOfTime,
        sent=False: no attempt was made."""
        def slow(req, timeout=None):
            self.seen.append(timeout)
            self.clock.t += timeout
            raise urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch.object(P.urllib.request, "urlopen", slow), \
                mock.patch.object(P, "CATALOGUE", Path("/nonexistent/catalogue.json")), \
                mock.patch.object(P, "config", lambda: {"base_url": "http://127.0.0.1:9/api/v1"}):
            with P.deadline_scope(T0 + 60):
                with self.assertRaises(P.OutOfTime) as cut:
                    P.catalogue()
            self.assertFalse(cut.exception.sent)
            self.assertEqual(self.seen, [40])
            with self.assertRaises(P.Retryable):       # outside a deadline it is the provider's
                P.catalogue()

    def test_a_connection_that_dies_mid_body_is_retryable(self):
        import http.client

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                raise http.client.IncompleteRead(b"")
        with mock.patch.object(P.urllib.request, "urlopen", lambda req, timeout=None: Resp()):
            with self.assertRaises(P.Retryable):
                P._post("http://127.0.0.1:9/api/v1", "stub", {"model": "x"}, timeout=10)

    def test_outside_a_deadline_they_keep_their_own_timeouts(self):
        with self.assertRaises(urllib.error.URLError):
            P._fetch_catalogue("http://127.0.0.1:9/api/v1")
        self.assertIsNone(P.provider_usage(force=True))
        self.assertEqual(self.seen, [45, 30])

    def test_a_read_with_no_time_left_is_not_made(self):
        with P.deadline_scope(T0 + P.REPORT_RESERVE_S):
            self.assertIsNone(P.provider_usage(force=True))
        self.assertEqual(self.seen, [], "nothing left: the read degrades as unreachable, with no request")


if __name__ == "__main__":
    unittest.main(verbosity=2)
