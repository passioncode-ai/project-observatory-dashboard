#!/usr/bin/env python3
"""Routing by context (DEC-0082): node/context_route.py over the contract's own vectors."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from node import context_route  # noqa: E402

VECTORS = json.loads((ROOT / 'fabric' / 'node-vectors' / 'context-route.json').read_text(encoding='utf-8'))


class ContextRoute(unittest.TestCase):
    def test_contexts(self):
        for case in VECTORS['contexts']:
            with self.subTest(case['name']):
                self.assertIs(context_route.is_context(case['value']), case['valid'])

    def test_route_target(self):
        for case in VECTORS['cases']:
            with self.subTest(case['name']):
                self.assertEqual(context_route.route_target(case.get('session'), case.get('enrollments')),
                                 case['answer'])

    def test_grant_usage(self):
        for case in VECTORS['grantUsage']:
            with self.subTest(case['name']):
                self.assertEqual(context_route.route_grant_usage(case.get('grantContext'), case.get('enrollments'),
                                                                 stated='grantContext' in case), case['answer'])


if __name__ == '__main__':
    unittest.main()
