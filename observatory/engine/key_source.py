"""Keys by source (OBS-556, OBS-604, CP-10): where each vault slot's value came from, by names and records only.

    issued    a door minted it: its ledger or record (`org_view.issuers`: OpenRouter, DigitalOcean, Heroku, the
              movements journal for Cloudflare), or a credential request's provenance in the slot's metadata
    received  a person, an organization or an agent handed it over: `vault.py put --from LABEL` records the label
    shared    it was handed to another party: a peer share this node sent (`node/shares/sent/`), a credential
              request delivered into an organization's context (the slot's provenance), or a hand-over in an
              organization's applied selection (`org_selection.py`)
    copied    the same value sits under another project folder of this vault, measured from the keyed digests of
              this machine's manifest (`vault_digests`); nobody declared it. Before OBS-604 this read `shared`
    unknown   none of the above: put by hand without a label, or a slot whose metadata does not read

The first that holds wins, in that order: a value a door issued and somebody then copied to another project stays
`issued` and names the copy in `copiedWith` (and in `sharedWith`, the field's published name); a value shared out
names where it went in `sharedTo` whatever its class. A slot put without a source reads `unknown`, never `issued`.

Each row also says whose the key is (`context`): personal, an organization's, Not ours, or undecided, read from the
project its folder belongs to the way `full org keys` reads it (`org_view`, `organizations.reading`).

NO VALUE IS READ BY `classify`. The slot's metadata, the doors' records, the share records and the digest manifest
hold names, places and keyed digests; the label a person typed is checked when it is written and again when it is
read, and never echoed when it is shaped like a credential. `consumers` reads a slot's value in memory only, to
compare it in constant time with the copies the revoke guard's sources hold (`key_consumers.GUARD_SOURCES`), and
answers names and places. What could not be read is in `degraded`, never read as a class.
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

CLASSES = ("issued", "received", "shared", "copied", "unknown")
#: Context kinds: whose the key is, from its project's organization.
CONTEXTS = ("personal", "organization", "not-ours", "undecided")
#: The revoke guard's sources (OBS-594), every one of which `consumers` reads.
GUARD_NAMES = ("openrouter-scan", "heroku", "env-file", "launchd")
#: The decisions of an organization's selection that hand a key over (`org_selection.KEY_CHOICES`).
HAND_OVER = ("hand-over-revoke", "hand-over-keep")
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


def copied_index(degraded: list) -> tuple[dict[str, list[str]], str | None]:
    """({slot: other slots under another folder holding the same value}, when it was measured) from this machine's
    keyed digest manifest. No manifest, or one that does not read: an empty index, said in `degraded` (its code keeps
    the name it was published under, `shared-unmeasured`)."""
    import vault_digests
    try:
        path = vault_digests.local_manifest_path()
        if not path.is_file():
            degraded.append({"source": "vault-digests", "code": "shared-unmeasured",
                             "reason": "this machine has no digest manifest, so which values two projects share "
                                       f"is not measured; run `{vault_digests.CMD} write`",
                             "effect": "no slot reads `copied`"})
            return {}, None
        doc = vault_digests.read_manifest(path)
    except Exception as exc:                                        # noqa: BLE001  named, never a class
        degraded.append({"source": "vault-digests", "code": "shared-unmeasured",
                         "reason": f"this machine's digest manifest does not read ({type(exc).__name__})",
                         "effect": "no slot reads `copied`"})
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


#: The name this reader was published under (OBS-556).
shared_index = copied_index


def _node_root() -> pathlib.Path:
    import paths
    return paths.setting_path("OBSERVATORY_NODE", paths.HOME / "node")


def _peer_shares(degraded: list) -> dict[str, list[dict]]:
    """{slot: [the peer shares this node sent of it]}, from `node/shares/sent/<id>/share.json` (names, states and
    keys; never a value). A record that does not read is named in `degraded`, never read as no share."""
    import private_io
    folder = _node_root() / "shares" / "sent"
    out: dict[str, list[dict]] = {}
    try:
        if any(p.is_symlink() for p in (folder, *folder.parents)) or not folder.is_dir():
            return out
        entries = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError as exc:
        degraded.append({"source": "shares", "code": "share-unreadable",
                         "reason": f"the sent shares could not be listed ({type(exc).__name__})",
                         "effect": "a key shared out through a peer share may read as not shared"})
        return out
    for entry in entries:
        path = entry / "share.json"
        if not path.is_file():
            continue
        try:
            rec = json.loads(private_io.read(path))
            if not isinstance(rec, dict):
                raise ValueError("not an object")
            project, env, slots = rec["project"], rec["env"], rec.get("slots") or []
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            degraded.append({"source": "shares", "code": "share-unreadable", "share": entry.name[:8],
                             "reason": f"share {entry.name[:8]} does not read ({type(exc).__name__})",
                             "effect": "the keys it carries may read as not shared"})
            continue
        label = rec.get("label") if isinstance(rec.get("label"), str) and label_problem(rec.get("label")) is None \
            else "a recipient whose label is not shown"
        for item in slots if isinstance(slots, list) else []:
            name = item.get("slot") if isinstance(item, dict) else None
            if isinstance(name, str) and isinstance(project, str) and isinstance(env, str):
                out.setdefault(f"{project}/{env}/{name}", []).append({
                    "kind": "peer-share", "to": label, "record": f"peer share {entry.name[:8]}",
                    "state": rec.get("state"), "takenBack": rec.get("revocation") is not None})
    return out


def _hand_overs(degraded: list) -> dict[str, list[dict]]:
    """{slot: [the hand-overs an organization's applied selection decided for it]} (OBS-417): a key item whose
    decision is `hand-over-revoke` or `hand-over-keep`. A selection that does not read is named in `degraded`."""
    import organizations
    a = organizations.Assigner(doc=organizations.load())
    out: dict[str, list[dict]] = {}
    if not a.active:
        return out
    import org_selection
    for org in sorted(a.orgs):
        try:
            if not org_selection._revisions(org):
                continue
            rec = org_selection.current(org) or {}
        except Exception as exc:                                    # noqa: BLE001  named, never a class
            degraded.append({"source": "org-selection", "code": "selection-unreadable",
                             "reason": f"an organization's selection does not read ({type(exc).__name__})",
                             "effect": "a key it hands over may read as not shared"})
            continue
        for item in rec.get("items") or []:
            if (isinstance(item, dict) and item.get("kind") == "key" and item.get("decision") in HAND_OVER
                    and isinstance(item.get("slot"), str)):
                out.setdefault(item["slot"], []).append({
                    "kind": "org-hand-over", "to": org, "decision": item["decision"],
                    "record": f"organization selection revision {rec.get('revision')}"})
    return out


def contexts(slots: list[str], degraded: list) -> dict[str, dict]:
    """{slot: whose the key is}, from the project its vault folder belongs to, as `full org keys` reads it: the
    registry's project row through `organizations.reading`, its folder through the vault resolver. Without
    `organizations.json` every key is personal. A folder no project holds is the person's own, so personal."""
    import organizations
    import org_view
    a = organizations.Assigner(doc=organizations.load())
    if not a.active:
        return {s: {"kind": "personal", "organization": None, "project": None,
                    "why": "no organizations.json: everything is personal"} for s in slots}
    pdoc = org_view._registry("projects.json", degraded, "no key's organization is known: each reads undecided")
    if pdoc is None:
        return {s: {"kind": "undecided", "organization": None, "project": None,
                    "why": "the registry's projects are not read"} for s in slots}
    projects = org_view._project_rows(pdoc)
    known = {p["id"]: p for p in projects}
    resolve = org_view._vault_resolver(projects)
    out: dict[str, dict] = {}
    for slot in slots:
        pid = resolve(slot.split("/", 1)[0])
        if pid is None:
            out[slot] = {"kind": "personal", "organization": a.default, "project": None,
                         "why": "no project in the registry holds this vault folder: the person's own"}
            continue
        r = organizations.reading(known[pid], org_view._NO_DESTINATIONS)
        org = r.get("organization")
        if r["state"] == "decided":
            kind = "personal" if org == a.default else "organization"
        else:
            kind = {"external": "not-ours", "unset": "personal"}.get(r["state"], "undecided")
        row = {"kind": kind, "organization": org if kind in ("personal", "organization") else None,
               "project": pid, "why": r.get("why") or "no organization is configured"}
        if kind == "organization":
            row["label"] = org_view._label(a.orgs.get(org)) or org
        out[slot] = row
    return out


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
    """{`FOLDER/ENV/NAME`: {source, sourceContext, context, copiedWith?, sharedWith?, sharedTo?}} for every slot
    named. `degraded` gains what could not be read: a slot's metadata, a door's record, a share record, a selection,
    the digest manifest, the registry's projects."""
    root = root or _vault_root()
    doors = _issuers(list(slots), degraded)
    copies, measured = copied_index(degraded)
    peers = _peer_shares(degraded)
    handed = _hand_overs(degraded)
    whose = contexts(list(slots), degraded)
    out: dict[str, dict] = {}
    for slot in slots:
        meta, problem = _meta(root, slot)
        row: dict = {}
        if copies.get(slot):
            row["copiedWith"] = copies[slot]
            row["sharedWith"] = copies[slot]                # its published name (OBS-556): the same copies
        outward = list(peers.get(slot) or []) + list(handed.get(slot) or [])
        if meta is None:
            degraded.append({"source": "vault", "code": "meta-unreadable", "slot": slot,
                             "reason": f"{slot}: {problem}", "effect": "its source reads `unknown`"})
            if outward:
                row["sharedTo"] = outward
            out[slot] = {"source": "unknown", "sourceContext": {"why": "its metadata does not read"}, **row,
                         "context": whose.get(slot)}
            continue
        prov = meta.get("provenance") if isinstance(meta.get("provenance"), dict) else None
        issuer = (prov or {}).get("issuer") if isinstance((prov or {}).get("issuer"), dict) else None
        recipient = (prov or {}).get("recipient") if isinstance((prov or {}).get("recipient"), dict) else {}
        if str(recipient.get("context") or "").startswith("org:"):
            # A credential request delivered into an organization's context: its agent holds it (CP-10).
            outward.append({"kind": "credential-request", "to": str(recipient["context"]),
                            "agent": recipient.get("agentRef"), "record": f"credential request {prov.get('requestId')}"})
        if outward:
            row["sharedTo"] = outward
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
        elif row.get("sharedTo"):
            row = {"source": "shared", "sourceContext": {"to": sorted({s["to"] for s in row["sharedTo"]})}, **row}
        elif row.get("copiedWith"):
            row = {"source": "copied", "sourceContext": {"with": row["copiedWith"], "measuredAt": measured}, **row}
        else:
            row = {"source": "unknown",
                   "sourceContext": {"why": "no door record and no recorded sender: put by hand"}, **row}
        row["context"] = whose.get(slot)
        out[slot] = row
    return out


class ConsumerReader:
    """Who reads each slot's value, from every source the revoke guard reads (OBS-594, OBS-604): the OpenRouter scan
    (asked by the OpenRouter ledger's hash, label and name, for a key that door issued), Heroku config vars and the
    inventoried env files (compared with the value in memory, in constant time), and the services started from the
    slot. One reader serves many slots: the Heroku and env-file sources read their copies once and compare each slot
    against them. Every degraded entry carries a `code` and is named once; what is not in use is left out."""

    def __init__(self, degraded: list, *, sources: list | None = None):
        import key_consumers
        self.degraded = degraded
        self._kc = key_consumers
        self._given = sources
        self._shared = None
        self._ledger = None

    def _sources(self, slot: tuple[str, str, str]) -> list:
        if self._given is not None:
            return list(self._given)
        kc = self._kc
        if self._shared is None:
            self._shared = [kc.OpenRouterScanSource(), kc.HerokuSource(memo=True), kc.EnvFileSource(memo=True)]
        return [*self._shared, kc.LaunchdSource(slot)]

    def _openrouter_key(self, slot: str) -> dict | None:
        """The fingerprints the OpenRouter door's ledger holds for the key it delivered to `slot`, or None."""
        if self._ledger is None:
            import org_view
            self._ledger = {}
            for name, row in sorted(org_view._openrouter_ledger(self.degraded).items()):
                dest = str((row or {}).get("destination") or "") if isinstance(row, dict) else ""
                if dest.startswith("vault:"):
                    self._ledger[dest[len("vault:"):]] = (name, row)
        hit = self._ledger.get(slot)
        if not hit:
            return None
        name, row = hit
        return {"hash": row.get("hash"), "labels": {name, row.get("label")} - {None},
                "names": {name, row.get("provider_name")} - {None}}

    def _note(self, rows: list[dict], source: str) -> None:
        for d in rows:
            d = dict(d)
            d.setdefault("source", source)
            d.setdefault("code", "source-unreadable")
            if d not in self.degraded:
                self.degraded.append(d)

    def of(self, project: str, env: str, name: str) -> list[dict]:
        slot = f"{project}/{env}/{name}"
        value, unread = None, None
        try:
            value = self._kc.vault_value(project, env, name)
        except Exception as exc:                                    # noqa: BLE001  named, typed
            unread = type(exc).__name__
        found: list[dict] = []
        for src in self._sources((project, env, name)):
            label = getattr(src, "name", type(src).__name__)
            try:
                if hasattr(src, "find_key"):
                    key = self._openrouter_key(slot)
                    if key is None:
                        continue                                    # not a key the OpenRouter door delivered
                    got, bad, _skipped = src.find_key(key)
                elif label == "launchd":
                    got, bad, _skipped = src.find(value)            # services by the slot, not by the value
                elif unread:
                    got, bad, _skipped = [], [{"code": "value-unreadable", "slot": slot,
                                               "reason": f"{slot}: the value could not be read ({unread})"}], []
                elif not value:
                    continue                                        # nothing in the slot: no copy to look for
                else:
                    got, bad, _skipped = src.find(value)
            except Exception as exc:                                # noqa: BLE001  degraded, named
                got, bad = [], [{"code": "source-failed", "reason": f"{type(exc).__name__} while reading"}]
            self._note(bad, label)
            found += got
        return found


def consumers(project: str, env: str, name: str, degraded: list, *, sources: list | None = None) -> list[dict]:
    """Who reads the slot's value, by name and place, from every source the revoke guard reads (`ConsumerReader`).
    The value is held in memory only; `observatory_key_consumers` answers the same finder for one slot."""
    return ConsumerReader(degraded, sources=sources).of(project, env, name)
