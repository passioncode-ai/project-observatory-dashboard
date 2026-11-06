"""A synthetic secret store kept outside a workspace (OBS-350), for the suites that carry and check one.

Every value is a low-entropy fixture string (`fixture-value-NNN`), never anything credential-shaped, so a
test can search every manifest, answer and record for it and the secret checks accept the file."""
from __future__ import annotations

import json
from pathlib import Path

#: Every value the store holds, by relative path; a test asserts none of them reaches any output.
VALUES = {
    "projects/alpha/prod/API_KEY": "fixture-value-001",
    "projects/alpha/stage/DB_PASSWORD": "fixture-value-002",
    "projects/beta/local/SEED": "fixture-value-003",
    ".estate-digest-key": "fixture-value-004",
    "openrouter-admin": "fixture-value-005",
}


def make_external_store(root: Path) -> Path:
    """`root` becomes a store laid out as the vault and its doors keep one: slots with metadata, the movement
    journal, the leak register, the estate digest key, a door's stash, the vault lock and a temporary."""
    root.mkdir(parents=True, mode=0o700)
    for rel, value in VALUES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(value + "\n")
        path.chmod(0o600)
    meta = root / "projects/alpha/prod/API_KEY.meta.json"
    meta.write_text(json.dumps({"created": "2026-11-01T00:00:00Z", "rotations": 0}))
    meta.chmod(0o600)
    (root / "projects/movements.jsonl").write_text('{"event": "put", "secret": "alpha/prod/API_KEY"}\n')
    (root / "projects/leaks.jsonl").write_text("")
    (root / "projects/.vault.lock").write_text("")
    (root / "projects/.vault-write-abc123").write_text("fixture-value-006")
    (root / ".openrouter.lock").write_text("")
    (root / "stash.tmp").write_text("fixture-value-007")
    return root


def all_values() -> list[str]:
    return [*VALUES.values(), "fixture-value-006", "fixture-value-007"]
