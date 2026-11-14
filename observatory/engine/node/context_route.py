"""Where an event of a session may go: routing by context (fabric-agent-contract DEC-0082, contexts.md "Routing").

A session's context is `personal` or `org:<orgId>`, the collector's own record of the session and never a field of the
event. `route_target` answers which of the device's enrollments may receive it: the one enrollment whose organization is
the context's id, compared as written; never anything for a personal session; nothing when two enrollments claim the
organization or the value is not a context. `route_grant_usage` does the same with a grant's own context. Malformed
input answers `none` and never raises. The contract's `routeTarget` and `routeGrantUsage` (`src/context-route.ts`), held
to its vectors in `fabric/node-vectors/context-route.json` by `tests/test_context_route.py`.
"""
from __future__ import annotations

import re
from typing import Any

PERSONAL = 'personal'
_ORG = re.compile(r'org:([A-Za-z0-9][A-Za-z0-9_-]{0,127})')


def is_context(value: Any) -> bool:
    return isinstance(value, str) and (value == PERSONAL or _ORG.fullmatch(value) is not None)


def _orgs(enrollments: Any) -> list:
    if not isinstance(enrollments, list):
        return []
    return [e.get('org') if isinstance(e, dict) and isinstance(e.get('org'), str) else None for e in enrollments]


def route_target(session: Any, enrollments: Any) -> dict:
    if not is_context(session):
        return {'route': 'none', 'reason': 'not-a-context'}
    if session == PERSONAL:
        return {'route': 'none', 'reason': 'personal'}
    org = _ORG.fullmatch(session).group(1)
    hits = [i for i, held in enumerate(_orgs(enrollments)) if held == org]
    if not hits:
        return {'route': 'none', 'reason': 'not-enrolled'}
    if len(hits) > 1:
        return {'route': 'none', 'reason': 'ambiguous'}
    return {'route': 'enrollment', 'org': org, 'index': hits[0]}


def route_grant_usage(grant_context: Any = None, enrollments: Any = None, *, stated: bool = True) -> dict:
    """`stated=False` is a grant that states no context (the vectors' absent `grantContext`)."""
    if not stated:
        return {'route': 'none', 'reason': 'no-context'}
    return route_target(grant_context, enrollments)
