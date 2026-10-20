#!/usr/bin/env python3
"""Which Fabric project each of this registry's projects is: asked of Fabric's hub, kept here.

    collectors/scan_fabric_attribution.py [OUT]     # default store/raw/fabric-attribution.json

Fabric owns the canonical project id (fabric-agent-contract DEC-0089, Fabric ADR-0166). This
pulls: for every folder the registry holds it calls `fabric.project.resolve` on the hub that
`hub.json` names, and records each project's `fabric:<uuid>` alias with its provenance in
Observatory's own store. It writes nothing to Fabric but the one access request
(`fabric_hub.request_access`), and never creates a Fabric project.

Reads registry/projects.json, registry/repositories.json and registry/relations.json (the
folders each project holds, through `fabric_attribution.project_folders`) and Fabric's hub.

Bounded: at most `fabric_attribution.MAX_FOLDERS` folders and `BUDGET_SECONDS` a pass, a
success cached for six hours, a refusal asked again next pass. A hub that is absent, whose pid
is dead, that refuses access or answers with a refusal is named in `degraded`, and the last
recorded aliases stand. Exit 0 in every one of those cases: an unreachable Fabric is a state,
not a failed tick.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fabric_attribution  # noqa: E402
import fabric_hub  # noqa: E402


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else fabric_attribution.output_path()
    previous = {}
    try:
        import json
        previous = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    except (OSError, ValueError):
        previous = {}
    doc = fabric_attribution.collect(previous if isinstance(previous, dict) else {}, fabric_hub.Resolver)
    fabric_attribution.write(doc, out)
    reasons = ", ".join(sorted({d["reason"] for d in doc["degraded"]})) or "none"
    print(f"fabric-attribution: {len(doc['aliases'])} alias(es), {len(doc['conflicts'])} conflict(s), "
          f"{len(doc['unattributed'])} unattributed; degraded: {reasons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
