"""Keys by source (OBS-556, CP-10): where each vault slot's value came from, by names and records only.

    issued    a door minted it: its ledger or record (`org_view.issuers`: OpenRouter, DigitalOcean, Heroku, the
              movements journal for Cloudflare), or a credential request's provenance in the slot's metadata
    received  a person, an organization or an agent handed it over: `vault.py put --from LABEL` records the label
    shared    the same value sits under another project folder of this vault, measured from the keyed digests of
              this machine's manifest (`vault_digests`); nobody declared it
    unknown   none of the above: put by hand without a label, or a slot whose metadata does not read

The first that holds wins, in that order: a value a door issued and somebody then copied to another project stays
`issued` and names the copy in `sharedWith`. A slot put without a source reads `unknown`, never `issued`.

NO VALUE IS READ HERE. The slot's metadata, the doors' records and the digest manifest hold names, places and keyed
digests; the label a person typed is checked when it is written and again when it is read, and never echoed when it
is shaped like a credential. What could not be read is in `degraded`, never read as a class.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
for _p in (ROOT, ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import credential_shape  # noqa: E402

CLASSES = ("issued", "received", "shared", "unknown")
#: The metadata key a received slot's source is kept under (CONTROL-PLANE.md, M7), and the longest label kept.
META_KEY = "source"
LABEL_MAX = 80
#: The tool that finds every copy of a slot's value (Heroku, env files, services); the rows name it.
CONSUMERS_TOOL = "observatory_key_consumers"


def label_problem(label: object) -> str | None:
    """Why `label` cannot name who handed a key over, or None. Never quotes the label."""
    if not isinstance(label, str) or not label.strip():
        return "the label is empty"
    if len(label) > LABEL_MAX:
        return f"the label is longer than {LABEL_MAX} characters"
    if any(ord(c) < 32 or ord(c) == 127 for c in label):
        return "the label carries a control character or a line break"
    if credential_shape.shaped(label):
        return "the label is shaped like a credential; name the sender, never the key"
    return None


def received(label: str, at: str) -> dict:
    """The metadata record of a received value."""
    return {"class": "received", "from": label, "at": at}


def _vault_root() -> pathlib.Path:
    import org_view
    return org_view._vault_root()


def _meta(root: pathlib.Path, slot: str) -> tuple[dict | None, str | None]:
    """(the slot's metadata, None) or (None, why it does not read). A slot with no metadata file has an empty one."""
    path = root.joinpath(*slot.split("/"))
    meta_path = path.with_name(path.name + ".meta.json")
    try:
        if meta_path.is_symlink():
            return None, "its metadata is a symbolic link"
        if not meta_path.exists():
            return {}, None
        doc = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"its metadata does not read ({type(exc).__name__})"
    if not isinstance(doc, dict):
        return None, "its metadata is not one object"
    return doc, None


def shared_index(degraded: list) -> tuple[dict[str, list[str]], str | None]:
    """({slot: other slots under another folder holding the same value}, when it was measured) from this machine's
    keyed digest manifest. No manifest, or one that does not read: an empty index, said in `degraded`."""
    import vault_digests
    try:
        path = vault_digests.local_manifest_path()
        if not path.is_file():
            degraded.append({"source": "vault-digests", "code": "shared-unmeasured",
                             "reason": "this machine has no digest manifest, so which values two projects share "
                                       f"is not measured; run `{vault_digests.CMD} write`",
                             "effect": "no slot reads `shared`"})
            return {}, None
        doc = vault_digests.read_manifest(path)
    except Exception as exc:                                        # noqa: BLE001  named, never a class
        degraded.append({"source": "vault-digests", "code": "shared-unmeasured",
                         "reason": f"this machine's digest manifest does not read ({type(exc).__name__})",
                         "effect": "no slot reads `shared`"})
        return {}, None
    by_digest: dict[str, list[str]] = {}
    for name, row in sorted(doc["slots"].items()):
        by_digest.setdefault(row["digest"], []).append(name)
    index: dict[str, list[str]] = {}
    for names in by_digest.values():
        for name in names:
            folder = name.split("/", 1)[0]
            others = [o for o in names if o.split("/", 1)[0] != folder]
            if others:
                index[name] = others
    return index, doc.get("generated_at")


def _issuers(slots: list[str], degraded: list) -> dict[str, dict]:
    import org_view
    try:
        names = org_view.cloudflare_names(org_view._quiet("cloudflare-account.json"), degraded)
        return org_view.issuers(degraded, names, slots)
    except Exception as exc:                                        # noqa: BLE001  named, never a class
        degraded.append({"source": "doors", "code": "issuers-unreadable",
                         "reason": f"the doors' records could not be read ({type(exc).__name__})",
                         "effect": "a key a door issued reads `unknown` here"})
        return {}


def classify(slots: list[str], degraded: list, *, root: pathlib.Path | None = None) -> dict[str, dict]:
    """{`FOLDER/ENV/NAME`: {source, sourceContext, sharedWith?}} for every slot named. `degraded` gains what could
    not be read: a slot's metadata, a door's record, the digest manifest."""
    root = root or _vault_root()
    doors = _issuers(list(slots), degraded)
    shared, measured = shared_index(degraded)
    out: dict[str, dict] = {}
    for slot in slots:
        meta, problem = _meta(root, slot)
        row: dict = {}
        if shared.get(slot):
            row["sharedWith"] = shared[slot]
        if meta is None:
            degraded.append({"source": "vault", "code": "meta-unreadable", "slot": slot,
                             "reason": f"{slot}: {problem}", "effect": "its source reads `unknown`"})
            out[slot] = {"source": "unknown", "sourceContext": {"why": "its metadata does not read"}, **row}
            continue
        prov = meta.get("provenance") if isinstance(meta.get("provenance"), dict) else None
        issuer = (prov or {}).get("issuer") if isinstance((prov or {}).get("issuer"), dict) else None
        mark = meta.get(META_KEY) if isinstance(meta.get(META_KEY), dict) else None
        if slot in doors:
            d = doors[slot]
            row = {"source": "issued", "sourceContext": {"door": d.get("provider"), "account": d.get("account"),
                                                         "record": d.get("record")}, **row}
        elif issuer and issuer.get("door"):
            row = {"source": "issued", "sourceContext": {"door": issuer.get("door"), "account": issuer.get("account"),
                                                         "record": "credential request",
                                                         "requestId": prov.get("requestId")}, **row}
        elif mark and mark.get("class") == "received":
            if label_problem(mark.get("from")) is None:
                row = {"source": "received", "sourceContext": {"from": mark["from"], "at": mark.get("at")}, **row}
            else:
                degraded.append({"source": "vault", "code": "label-refused", "slot": slot,
                                 "reason": f"{slot}: the recorded sender is not a plain label; not shown",
                                 "effect": "its source reads `unknown`"})
                row = {"source": "unknown", "sourceContext": {"why": "the recorded sender is not a plain label"},
                       **row}
        elif row.get("sharedWith"):
            row = {"source": "shared", "sourceContext": {"with": row["sharedWith"], "measuredAt": measured}, **row}
        else:
            row = {"source": "unknown",
                   "sourceContext": {"why": "no door record and no recorded sender: put by hand"}, **row}
        out[slot] = row
    return out


def consumers(project: str, env: str, name: str, degraded: list) -> list[dict]:
    """The services started from the slot (the key consumers reader's value-free source); every other copy of the
    value is found by `observatory_key_consumers`, which reads the value."""
    import key_consumers
    found, bad, _skipped = key_consumers.LaunchdSource((project, env, name)).find(None)
    for d in bad:
        if d not in degraded:
            degraded.append(d)
    return found
