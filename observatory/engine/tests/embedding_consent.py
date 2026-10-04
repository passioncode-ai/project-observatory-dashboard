"""Test helper: give synthetic projects an embedding-policy/1 consent.

Since PB-137 N-003 the indexer and the query path send text to a remote embedding
model only under a recorded consent. A suite that exercises the VECTOR half grants
one for its synthetic projects, in its own sandboxed workspace, for the model the
engine is configured with. Each grant raises the revision, as the real writer does.
"""
from __future__ import annotations

import json


def grant(*projects: str, classes=("public", "project-internal")) -> dict:
    import embedding_policy as EP
    path = EP.policy_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        doc = {"schema": EP.SCHEMA, "policyRevision": 0, "projects": {}}
    model = EP.configured_model()
    if not model:
        # A sandbox without the workspace's model configuration: install the
        # engine's defaults, as `full init` would, so there is a model to consent to.
        import paths
        import pathlib
        defaults = pathlib.Path(EP.__file__).resolve().parent / "defaults" / "models.json"
        target = paths.config_file("models.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(defaults.read_text(encoding="utf-8"), encoding="utf-8")
        model = EP.configured_model()
    for project in projects:
        doc["projects"][project] = {"remote": {
            "provider": model["provider"], "model": model["model"], "classes": list(classes),
            "consent": {"statement": f"Texts of {project} are sent to {model['provider']} "
                                     f"for embeddings (test fixture).",
                        "by": "operator", "at": "2026-10-04T10:00:00Z", "via": "terminal"},
            "revokedAt": None}}
    doc["policyRevision"] += 1
    EP.parse(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return doc
