#!/usr/bin/env python3
"""The key inventory, and the confident wrong answer it gave on its first run.

Every check here is a defect this file shipped and the run caught, not a
hypothetical:

1. A listing's `label` shows THREE characters after the ellipsis, not four. The
   first version compared four, matched nothing, and reported all four
   destinations as "not in this account" — the opposite of the truth.
2. Three characters COLLIDE. Across 1200 keys the birthday bound makes a
   three-hex-character tail a near-certainty to repeat, and it did: a stranger's
   `PRODUCTION_user_…` key was labelled as this project's. Matching moved to the
   provider's own label — prefix and suffix — and a collision is now REPORTED
   rather than resolved by picking one.
3. A key value must never reach the written document, and neither must the
   provisioning hash: the raw file is gitignored, but it feeds a registry
   document that is in git, and a handle that acts does not belong there.
"""
from __future__ import annotations
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def scanner():
    import scan_openrouter
    return scan_openrouter


#: A key value of the shape the provider issues. NOT a credential: 64 zeros and
#: a marker, refused by `tools/check_secrets.py` as a fixture and by the entropy
#: rule behind it.
FAKE = "sk-or-v1-" + "abc" + "0" * 55 + "def"


def test_the_label_is_derivable_without_asking():
    """Matching needs an identifier, and asking for one per key costs a request.

    `sk-or-v1-838...0ad` is what the provider calls a key, and the same string
    is computable from the value — verified against `/api/v1/key` before this
    was relied on.
    """
    S = scanner()
    got = S.label_of(FAKE)
    check("the label is prefix, ellipsis and suffix", got == "sk-or-v1-abc...def", got)
    check("a value that is not a key yields nothing",
          S.label_of("not-a-key") == "" and S.label_of("") == "")
    check("a short value cannot produce a label", S.label_of("sk-or-v1-ab") == "")


def test_a_dotenv_is_read_as_a_file_not_as_a_key():
    """claude-mem's destination is a `.env`, and its key sits on one line of it."""
    S = scanner()
    env = f'OTHER=1\nOPENROUTER_API_KEY="{FAKE}"\nMORE=2\n'
    check("the key is found inside a .env", S._label(env) == "sk-or-v1-abc...def",
          S._label(env))
    check("a bare key file works too", S._label(FAKE + "\n") == "sk-or-v1-abc...def")
    check("a file with no key yields nothing", S._label("NOTHING=1\n") == "")


def test_three_characters_are_not_an_identifier():
    """The defect that shipped: a tail collides, a label does not.

    Not a claim about hashing — a claim about THIS population. The account holds
    1200 keys and three hex characters have 4096 values, so the first run put a
    stranger's key under this project's name.
    """
    S = scanner()
    a = "sk-or-v1-" + "111" + "0" * 55 + "xyz"
    b = "sk-or-v1-" + "222" + "0" * 55 + "xyz"
    check("two different keys can share a tail",
          a[-S.TAIL:] == b[-S.TAIL:], "the fixture no longer reproduces the collision")
    check("and their labels still differ", S.label_of(a) != S.label_of(b),
          f"{S.label_of(a)} == {S.label_of(b)}")


def test_a_shared_label_is_reported_rather_than_resolved():
    """Two keys with one label leaves the destination unattributable, and the
    only honest answer is to say so."""
    src = (ROOT / "collectors/scan_openrouter.py").read_text(encoding="utf-8")
    check("the collector computes a collision set", "collisions" in src)
    check("and excludes a colliding label from attribution",
          "if lab not in collisions" in src)
    check("and says why, rather than leaving the destination blank",
          "share the label" in src)


def planted_scan(provisioning: bool = True, collide: bool = False) -> dict:
    """Run the collector in process against planted destinations and a stubbed
    listing, and return the document it wrote.

    Nothing reaches the provider: `listing` is replaced, and every destination
    file lives in a scratch directory. The rows carry a `hash` the way the
    provider's listing does, so the test can watch where the hash lands.
    """
    import configuration
    sys.path.insert(0, str(ROOT / "tests"))
    import tmp as tmpdir
    S = scanner()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-or-scan-"))
    observatory_key = d / "observatory-key"
    observatory_key.write_text(FAKE + "\n", encoding="utf-8")
    companion_env = d / "companion.env"
    companion_env.write_text("OTHER=1\nOPENROUTER_API_KEY=" + "sk-or-v1-" + "fed" + "0" * 55 + "cba\n",
                             encoding="utf-8")
    prov = d / "provisioning"
    if provisioning:
        prov.write_text("fixture-provisioning-handle\n", encoding="utf-8")
    rows = [{"label": "sk-or-v1-abc...def", "name": "tick", "hash": "h-observatory", "usage": 1.5},
            {"label": "sk-or-v1-999...zzz", "name": "unused", "hash": "h-unused"}]
    if collide:
        rows.append({"label": "sk-or-v1-abc...def", "name": "a stranger", "hash": "h-stranger"})
    saved = (S.DESTINATIONS, S.listing, configuration.enabled)
    S.DESTINATIONS = {"observatory": observatory_key, "claude-mem": companion_env,
                      "gateway": d / "absent", "provisioning": prov}
    S.listing = lambda _prov: (rows, None)
    configuration.enabled = lambda name: True
    out = d / "openrouter.json"
    try:
        S.main(["scan_openrouter.py", str(out)])
    finally:
        S.DESTINATIONS, S.listing, configuration.enabled = saved
    return json.loads(out.read_text(encoding="utf-8"))


def test_no_value_and_no_hash_reach_the_registry_half():
    """The raw file may hold a hash; the per-key records that feed the registry
    may not, and neither may hold a key."""
    src = (ROOT / "collectors/scan_openrouter.py").read_text(encoding="utf-8")
    body = src.split("keys.append(", 1)[1].split("})", 1)[0]
    check("no per-key record carries a hash", "hash" not in body, body[:160])
    doc = planted_scan()
    text = json.dumps(doc["keys"])
    check("no key record holds anything key-shaped",
          "..." in text and not any(len(str(v)) > 40 and str(v).startswith("sk-or-v1-")
                                    for k in doc["keys"] for v in k.values()), text[:200])
    check("the hashes live beside the keys, not inside them",
          "hashes" in doc and all("hash" not in k for k in doc["keys"]), str(doc.get("hashes")))
    check("and no key value reached the document anywhere", FAKE not in json.dumps(doc))
    served = {k["label"]: k["serves"] for k in doc["keys"]}
    check("the key a destination holds is attributed to it by label",
          served.get("sk-or-v1-abc...def") == "observatory", str(served))
    check("and a key no destination holds serves nothing",
          served.get("sk-or-v1-999...zzz") is None, str(served))


def test_a_collision_is_reported_on_a_real_run():
    doc = planted_scan(collide=True)
    served = [k["serves"] for k in doc["keys"] if k["label"] == "sk-or-v1-abc...def"]
    check("two keys sharing a label are both left unattributed",
          len(served) == 2 and served == [None, None], str(served))
    check("and the destination says why",
          any(d["source"] == "openrouter:observatory" and "share the label" in d["reason"]
              for d in doc["degraded"]), str(doc["degraded"]))


def test_absent_provisioning_is_a_degradation_not_an_empty_estate():
    """No provisioning key means the account cannot be listed — which is a
    different claim from "this account has no keys"."""
    src = (ROOT / "collectors/scan_openrouter.py").read_text(encoding="utf-8")
    check("the collector writes a file rather than failing",
          "no provisioning key, so the account's" in src)
    check("and a truncated listing says it is partial",
          "this list is partial" in src)
    doc = planted_scan(provisioning=False)
    check("with no provisioning key the listing is empty and says so",
          doc["keys"] == [] and any(d["source"] == "openrouter:listing" for d in doc["degraded"]),
          str(doc["degraded"]))
    check("every destination not in the listing is named",
          isinstance(doc.get("unlisted_destinations"), list)
          and set(doc["unlisted_destinations"]) == {"observatory", "claude-mem"},
          str(doc.get("unlisted_destinations")))
    check("and a destination with no key file carries a reason on the degraded list",
          any(d["source"] == "openrouter:gateway" for d in doc["degraded"]), str(doc["degraded"]))
    doc = planted_scan()
    check("a destination whose key is not in the listing is named with a reason",
          doc["unlisted_destinations"] == ["claude-mem"]
          and all(any(d["source"].endswith(n) for d in doc["degraded"])
                  for n in doc["unlisted_destinations"]),
          str(doc["unlisted_destinations"]))


def test_the_step_is_reachable():
    import observatory
    check("the collector has a step", "openrouter" in observatory.STEPS)
    check("and a group reaches it",
          any("openrouter" in s for s in observatory.GROUPS.values()),
          "a step no group runs is a step that never runs")


if __name__ == "__main__":
    print("openrouter keys — three characters were not an identifier\n")
    for fn in (test_the_label_is_derivable_without_asking,
               test_a_dotenv_is_read_as_a_file_not_as_a_key,
               test_three_characters_are_not_an_identifier,
               test_a_shared_label_is_reported_rather_than_resolved,
               test_no_value_and_no_hash_reach_the_registry_half,
               test_a_collision_is_reported_on_a_real_run,
               test_absent_provisioning_is_a_degradation_not_an_empty_estate,
               test_the_step_is_reachable):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma destination is attributed by what the provider calls the key\033[0m")
