#!/usr/bin/env python3
"""What a registry proposal may change, and where an accepted one lands.

**A proposal cannot be applied to the registry.** `registry/*.json` is rewritten
whole from the model on every emit, so a patch written there survives until the
next tick and no longer. That is not a limitation to work around: it is why the
project has a CURATION layer — `collectors/project_overrides.json`,
`repo_overrides.json`, `repo_status.json` — which the emitter reads and applies
on top of what it measured. An accepted proposal belongs there, and nowhere else.

**So the appliable set is not a matter of taste.** It is exactly the fields those
files supply, derived from them rather than restated here: a second list would
drift, and a proposal for a field nothing can apply is a row that can only ever
be listed. Measured 2026-09-07, before this existed:

    observatory_propose(target_id="project:also-not-real", …)   -> "proposed"
    patch={"activity_tier": …, "id": …, "source_refs": …}       -> "proposed"
    patch with no evidence at all                                -> "proposed"

The first names a subject that does not exist. The second asks to change a
DERIVED field the emitter recomputes, the record's own id, and its provenance.
The third contradicts the tool's own field description — "A patch with no
evidence is a guess" — which was documentation rather than a rule.

**`domain:` accepts nothing, and that is a decision rather than a gap.**
`registry/domains.json` is the operator's transcription of two documents they
supplied; the emitter does not derive it and there is no overrides file for it.
A domain proposal would have no landing place, so it is refused with that reason
instead of queued for ever.
"""
from __future__ import annotations
import json

import paths

#: target prefix -> (curation file, the key it stores rows under)
LANDS_IN = {
    "project:": ("project_overrides.json", "projects"),
    "repository:": ("repo_overrides.json", "repositories"),
}

#: Fields that are provenance about the override rather than data in it. `why`
#: is required of an accepted proposal — it is what the next reader needs to
#: judge whether the row still belongs — and it is never something a caller
#: proposes: it is written when the decision is made.
PROVENANCE = {"why", "proposed_by", "accepted_on", "evidence"}


def _fields(name: str, key: str) -> set[str]:
    try:
        doc = json.loads((paths.config_file(name)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    out: set[str] = set()
    for row in (doc.get(key) or {}).values():
        if isinstance(row, dict):
            out |= set(row)
    return out - PROVENANCE


#: Fields a project proposal may always carry, even before any override row has
#: used them: the organization split and the resource inventory are how agents
#: report what they created, and "the operator adds the first row by hand" would
#: leave the first agent with nowhere to report it. Their shape is checked here.
STRUCTURED = {"project:": {"organization", "resources"}}


def appliable() -> dict[str, set[str]]:
    """{target prefix: the fields an accepted proposal can actually change}.

    Derived from the curation files as they stand. A file with no rows yet
    contributes nothing, which is honest: nothing can be applied through a shape
    that has never been used, and the operator adding the first row by hand is
    what teaches this function the field.
    """
    return {prefix: _fields(name, key) | STRUCTURED.get(prefix, set())
            for prefix, (name, key) in LANDS_IN.items()}


def refusal(target_id: str, patch: dict, evidence: list) -> str:
    """Why this proposal cannot be accepted, or "" if it can.

    Checked at the WIRE, not only at the decision: a caller told "proposed" for
    something no decision could ever apply has been given a receipt for nothing.
    """
    if not evidence:
        return ("a patch with no evidence is a guess. Attach at least one "
                "resolvable reference — a commit sha, a file path, a URI — "
                "because the operator deciding this cannot re-derive what you saw")
    prefix = next((p for p in LANDS_IN if target_id.startswith(p)), "")
    if not prefix:
        kinds = ", ".join(sorted(LANDS_IN))
        if target_id.startswith("domain:"):
            return ("domains are not derived: `registry/domains.json` is the "
                    "operator's transcription of documents they supplied, and "
                    "there is no overrides file an accepted change could land in. "
                    "Send it to them as a note instead")
        return f"only {kinds} can be proposed against; `{target_id}` is neither"
    if not patch:
        return "an empty patch proposes nothing"
    allowed = appliable()[prefix]
    if not allowed:
        return (f"nothing has ever been overridden for a {prefix.rstrip(':')}, so "
                f"this function cannot tell which fields are appliable. The "
                f"operator adds the first row by hand")
    if "resources" in patch:
        import organizations
        rows = patch["resources"]
        if not isinstance(rows, list) or not rows:
            return "`resources` must be a non-empty list of resource objects"
        for row in rows:
            problem = organizations.resource_problem(row)
            if problem:
                return problem
    if "organization" in patch:
        import organizations
        known = set((organizations.load().get("organizations") or {})) | {"external"}
        if patch["organization"] not in known:
            return (f"organization must be one of: {', '.join(sorted(known))} "
                    "(configured in organizations.json)")
    unknown = sorted(set(patch) - allowed)
    if unknown:
        return (f"{', '.join(unknown)} cannot be applied: an accepted proposal "
                f"lands in {LANDS_IN[prefix][0]}, which supplies "
                f"{', '.join(sorted(allowed))}. Anything else is either derived "
                f"from the scan — the emitter recomputes it on the next tick — or "
                f"provenance the decision writes rather than the caller")
    return ""


def known_target(target_id: str, registry_ids: set[str]) -> bool:
    return target_id in registry_ids
