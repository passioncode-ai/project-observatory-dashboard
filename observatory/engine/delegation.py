"""The callee's check of a Fabric delegation token (contract `fabric-delegation/0.1`, DEC-0039; Fabric ADR-0144).

Fabric's hub is the estate's one issuer: it signs a 10-minute JWT naming the person the work is for (`sub`), the
chain of agents acting (`act`, RFC 8693), one callee (`aud`), the capabilities (`scope`), the effect ceiling and the
grant the call is under. A callee checks it offline against the estate's published Ed25519 keys. This module is
the contract kit's `verifyDelegation` / `verifyMeterToken` (fabric-agent-contract `src/delegation.ts` at 07042181,
DEC-0045 "Delegation sealed") in Python, following the specification's verifier table DL-13 in its order: nothing
about a token is believed before its signature is, so an unsigned token learns only `unauthenticated`. DEC-0045
made the verifier strict and ordered: the trusted issuer is required (row 1), a published key of another use or
algorithm or of small order is the issuer's fault (row 3, CT-29), every DL-2 type is checked at row 5 before rows
6-11, and the meter's token reads exactly its grant-check capability with effect exactly `["none"]`.

Not ported: DL-14's estate-record check (row 4a, DEC-0060), which the contract runs only when the callee reads the
estate record; the meter does not read one yet.

A refusal carries a contract code and a short machine word (`detail`); never the token, never a key. The token is
read from a header and nowhere else (DL-11): never argv, a URL, a tool argument or a log line.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ed25519_order import is_small_order_key

#: Every refusal code of the contract (DEC-0039 §Refusal codes); DEC-0037's grant-check codes are a subset.
REFUSAL_CODES = (
    "unauthenticated", "token-expired", "wrong-audience", "scope-not-granted", "effect-above-ceiling",
    "no-grant", "grant-expired", "grant-revoked", "limit-reached", "approval-required", "approval-invalid",
    "approval-reused", "principal-unproven", "credential-source-refused", "chain-too-deep",
    "issuer-unavailable", "unknown-state",
)
EFFECT_CLASSES = ("none", "draft", "publish", "send", "reply", "charge", "delete", "merge", "deploy",
                  "change-policy")
DELEGATION_HEADER = "Fabric-Delegation"
DELEGATION_TYP = "fabric-delegation+jwt"
APPROVAL_TYP = "fabric-approval+jwt"
MAX_DELEGATION_LIFETIME_SECONDS = 600
MAX_CHAIN_DEPTH = 4
LEEWAY_SECONDS = 60
MAX_TOKEN_LENGTH = 8192

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
#: A session or task id (`delegation-token.schema.json#/$defs/session`, `#/$defs/task`): opaque, 1-128 characters.
OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
#: An agent id (contract `src/fabric-links.ts` AGENT_ID): an actor's `sub` and the token's `aud`.
AGENT_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}(\.[a-z][a-z0-9-]{0,31})?$")
#: ECMAScript's white space and line terminators (ECMA-262 WhiteSpace, LineTerminator): what `\s` means in the
#: contract's JavaScript. Python's `\s` differs (it takes U+001C-U+001F and U+0085, and not U+FEFF), so every grammar
#: the contract writes with `\s` or `\S` is spelled with this set here (OBS-162 review round 2).
JS_SPACE = "\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
#: A URI as DL-2 types one: a scheme, a colon and no white space (`iss`, `sub`, `project`).
URI = re.compile(rf"[A-Za-z][A-Za-z0-9+.-]*:[^{JS_SPACE}]+")
#: `delegation-token.schema.json` `jti` and `scope` grammars.
JTI = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
SCOPE = re.compile(r"^[a-z][a-z0-9._-]{1,127}( [a-z][a-z0-9._-]{1,127})*$")
SPEND_WINDOWS = ("total", "task", "project", "day", "week", "month")


@dataclass(frozen=True)
class Refusal:
    code: str
    detail: str
    ok: bool = field(default=False, init=False)


@dataclass(frozen=True)
class Accepted:
    claims: dict
    principal: str          # `sub`: the person the work is for
    caller: str             # the outermost actor: the agent calling now
    chain: tuple            # every actor, the caller first
    kid: str
    ok: bool = field(default=True, init=False)


class _Duplicate(ValueError):
    pass


def _no_duplicates(pairs: list) -> dict:
    """`json` keeps the last of two equal members; a duplicate is refused, never resolved (RFC 8725 §3.2)."""
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _Duplicate(key)
        out[key] = value
    return out


def _no_constant(name: str):
    """`NaN`, `Infinity` and `-Infinity` are not JSON (RFC 8259 §6); the contract's `JSON.parse` refuses them."""
    raise ValueError(name)


def _json_int(text: str):
    """An integer literal as JavaScript reads one. `JSON.parse` makes every number a double, so an integer that rounds
    past the largest double (from 2**1024 - 2**970 up: 309 digits and more, and anything past Python's 4300-digit
    limit) is an infinity there; `isTime` and `isAmount` then refuse it as a claim of the wrong type
    (`malformed-claims`). Python's int would keep it exact and accept it, so it becomes the same infinity here."""
    infinity = float("-inf") if text.startswith("-") else float("inf")
    try:
        value = int(text)
        float(value)            # OverflowError exactly where a double rounds to infinity
    except (ValueError, OverflowError):
        return infinity
    return value


def b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(segment: str) -> tuple[Optional[dict], Optional[str]]:
    if not _B64URL.fullmatch(segment):
        return None, "malformed"
    try:
        value = json.loads(b64url_decode(segment).decode("utf-8"), object_pairs_hook=_no_duplicates,
                           parse_constant=_no_constant, parse_int=_json_int)
    except _Duplicate:
        return None, "duplicate-member"
    except (ValueError, UnicodeDecodeError):
        return None, "malformed"
    return (value, None) if isinstance(value, dict) else (None, "malformed")


def parse_compact(token: Any) -> tuple[Optional[tuple[dict, dict, bytes, bytes]], Optional[str]]:
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH:
        return None, "malformed"
    parts = token.split(".")
    if len(parts) != 3:
        return None, "malformed"
    header, problem = _segment(parts[0])
    if problem:
        return None, problem
    payload, problem = _segment(parts[1])
    if problem:
        return None, problem
    if not _B64URL.fullmatch(parts[2]):
        return None, "malformed"
    try:
        signature = b64url_decode(parts[2])
    except ValueError:          # a length no base64url encoder writes (1 mod 4): no 64-byte signature, row 4 refuses
        signature = b""
    return (header, payload, f"{parts[0]}.{parts[1]}".encode("ascii"), signature), None


def _is_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def _is_time(value: Any) -> bool:
    """A NumericDate as the contract's `isTime` reads one: a non-negative integer. JSON has one number type, so
    `1791720000.0` is the integer the contract's `Number.isInteger` accepts; NaN and infinities never parse."""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return value.is_integer() and value >= 0
    return isinstance(value, int) and value >= 0


def _header_problem(header: dict, typ: str) -> Optional[str]:
    if header.get("alg") != "EdDSA":
        return "alg-not-eddsa"
    if header.get("typ") != typ:
        return "wrong-typ"
    if not _is_string(header.get("kid")):
        return "no-kid"
    for member in ("crit", "jku", "jwk", "x5u", "x5c"):
        if member in header:
            return f"header-{member}"
    return None


def _key_for(jwks: Mapping, kid: str):
    """The published key `kid` names, or the problem: a key that cannot be used is the issuer's fault."""
    matches = [k for k in jwks.get("keys", []) if isinstance(k, dict) and k.get("kid") == kid]
    if len(matches) > 1:
        return None, "duplicate-kid"
    if not matches:
        return None, "unknown-key"
    jwk = matches[0]
    if ("use" in jwk and jwk["use"] != "sig") or ("alg" in jwk and jwk["alg"] != "EdDSA"):
        return None, "key-not-for-signing"
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not _is_string(jwk.get("x")) or "d" in jwk:
        return None, "unusable-key"
    try:
        raw = b64url_decode(jwk["x"]) if _B64URL.fullmatch(jwk["x"]) else b""
    except ValueError:
        return None, "unusable-key"
    if len(raw) != 32:
        return None, "unusable-key"
    # CT-29 (DEC-0045 item 10): a small-order key verifies signatures forged without a private key under some
    # Ed25519 implementations; it is refused before anything is verified under it.
    if is_small_order_key(raw):
        return None, "small-order-key"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return None, "no-ed25519"
    try:
        return Ed25519PublicKey.from_public_bytes(raw), None
    except ValueError:
        return None, "unusable-key"


def has_keys(jwks: Any) -> bool:
    return isinstance(jwks, dict) and isinstance(jwks.get("keys"), list) and len(jwks["keys"]) > 0


def _authentic(token: Any, jwks: Mapping, typ: str):
    """Parse, check the header, find the key, verify the signature: (header, payload, kid) or (problem, issuer?)."""
    parsed, problem = parse_compact(token)
    if problem:
        return None, problem, False
    header, payload, signing_input, signature = parsed
    problem = _header_problem(header, typ)
    if problem:
        return None, problem, False
    kid = header["kid"]
    key, problem = _key_for(jwks, kid)
    if problem:
        # DL-13 row 3: an unknown kid is the token's fault; any published key that cannot be used is the issuer's.
        return None, problem, problem != "unknown-key"
    from cryptography.exceptions import InvalidSignature
    try:
        if len(signature) != 64:
            raise InvalidSignature()
        key.verify(signature, signing_input)
    except InvalidSignature:
        return None, "bad-signature", False
    return (header, payload, kid), None, False


def chain_depth(act: Any) -> float:
    depth, node = 0, act
    while node is not None:
        if not isinstance(node, dict) or not _is_string(node.get("sub")):
            return float("inf")
        depth += 1
        if depth > 64:
            return depth
        node = node.get("act")
    return depth


def chain_of(act: dict) -> tuple:
    chain, node = [], act
    while node is not None:
        chain.append(node["sub"])
        node = node.get("act")
    return tuple(chain)


def effect_closure(ceiling: Sequence[str]) -> set:
    """`none` is below every class and `draft` below every external one; external classes are unordered."""
    admitted = set(ceiling)
    if ceiling:
        admitted.add("none")
    if any(e != "none" for e in ceiling):
        admitted.add("draft")
    return admitted


def effect_within(effect: str, ceiling: Sequence[str]) -> bool:
    return effect in effect_closure(ceiling)


def _actor_chain_depth(act: Any) -> Optional[int]:
    """The chain's depth when every actor is DL-2's (an agent id `sub`, an optional session id), else None. A token
    always names at least one actor, so no `act` is a malformed chain (CT-31)."""
    if act is None:
        return None
    depth, node = 0, act
    while node is not None:
        if not isinstance(node, dict) or not isinstance(node.get("sub"), str) or not AGENT_ID.fullmatch(node["sub"]):
            return None
        if "session" in node and not (isinstance(node["session"], str) and OPAQUE_ID.fullmatch(node["session"])):
            return None
        depth += 1
        if depth > 64:
            return depth
        node = node.get("act")
    return depth


def _is_amount(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and value not in (float("inf"),) and value >= 0


def _spend_shaped(spend: Any) -> bool:
    """`spend` as DL-2 types it: unlimited, or limited with one to six windows' remaining amounts and an optional
    per-call cap."""
    if not isinstance(spend, dict):
        return False
    if spend.get("mode") == "unlimited":
        return True
    remaining = spend.get("remaining")
    if spend.get("mode") != "limited" or not isinstance(remaining, list) or not 1 <= len(remaining) <= 6:
        return False
    if not all(isinstance(e, dict) and e.get("window") in SPEND_WINDOWS and _is_amount(e.get("remainingUsd"))
               for e in remaining):
        return False
    return "perCallMaxUsd" not in spend or _is_amount(spend["perCallMaxUsd"])


def _matches(pattern: re.Pattern, value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _delegation_shape(payload: dict) -> tuple[Optional[int], Optional[str]]:
    """DL-13 row 5: every claim the profile knows has DL-2's type (DEC-0045 item 7). (chain depth, None) or
    (None, the problem). A claim the profile does not know is ignored (RFC 7519 §4)."""
    depth = _actor_chain_depth(payload.get("act"))
    if depth is None:
        return None, "malformed-chain"
    effect, grant, scope = payload.get("effect"), payload.get("grant"), payload.get("scope")
    typed = (
        _matches(URI, payload.get("iss")) and _matches(URI, payload.get("sub"))
        and _matches(AGENT_ID, payload.get("aud"))
        and isinstance(scope, str) and len(scope) <= 4096 and SCOPE.fullmatch(scope) is not None
        and isinstance(effect, list) and len(effect) > 0 and all(isinstance(e, str) for e in effect)
        and len(set(effect)) == len(effect) and all(e in EFFECT_CLASSES for e in effect)
        # Fabric ADR-0144 amendment 3, DEC-0045 item 1: exactly one grant per token.
        and isinstance(grant, list) and len(grant) == 1 and isinstance(grant[0], str) and 1 <= len(grant[0]) <= 256
        and ("spend" not in payload or _spend_shaped(payload["spend"]))
        and ("project" not in payload or _matches(URI, payload["project"]))
        # DEC-0041: an optional claim the meter keys a window on is typed like any other.
        and ("task" not in payload or _matches(OPAQUE_ID, payload["task"]))
        and _is_time(payload.get("iat")) and _is_time(payload.get("exp"))
        and ("nbf" not in payload or _is_time(payload["nbf"]))
        and _matches(JTI, payload.get("jti")))
    return (depth, None) if typed else (None, "malformed-claims")


def _verify_at(token: Any, *, audience: str, jwks: Any, now: int, capability: str, effect: str, issuer: Any,
               read_only: bool, max_depth: int):
    """DL-13 for one call, in its order, the chain allowed `max_depth` actors."""
    # Row 1: whom the callee trusts, and that issuer's keys (DEC-0045 item 7: the issuer is required).
    if not _is_string(issuer):
        return Refusal("issuer-unavailable", "no-trusted-issuer")
    if not has_keys(jwks):
        return Refusal("issuer-unavailable", "no-published-keys")
    # Rows 2-4: form, header, key, signature.
    checked, problem, issuer_fault = _authentic(token, jwks, DELEGATION_TYP)
    if problem:
        return Refusal("issuer-unavailable" if issuer_fault else "unauthenticated", problem)
    _header, claims, kid = checked
    # Row 5: DL-2's types, then the issuer.
    depth, problem = _delegation_shape(claims)
    if problem:
        return Refusal("unauthenticated", problem)
    if claims["iss"] != issuer:
        return Refusal("unauthenticated", "wrong-issuer")
    # Row 6: the lifetime and the times a token can hold before it is used.
    if claims["exp"] <= claims["iat"] or claims["exp"] - claims["iat"] > MAX_DELEGATION_LIFETIME_SECONDS:
        return Refusal("unauthenticated", "lifetime-above-profile")
    if claims["iat"] > now + LEEWAY_SECONDS:
        return Refusal("unauthenticated", "issued-in-future")
    if "nbf" in claims and claims["nbf"] > now + LEEWAY_SECONDS:
        return Refusal("unauthenticated", "not-yet-valid")
    # Rows 7-11.
    if now > claims["exp"] + LEEWAY_SECONDS:
        return Refusal("token-expired", "expired")
    if claims["aud"] != audience:
        return Refusal("wrong-audience", "audience-mismatch")
    if depth > max_depth:
        return Refusal("chain-too-deep", f"depth-{depth}")
    if capability not in claims["scope"].split(" "):
        return Refusal("scope-not-granted", "capability-not-in-scope")
    if read_only and effect != "none":
        return Refusal("effect-above-ceiling", "read-only-door")
    if not effect_within(effect, claims["effect"]):
        return Refusal("effect-above-ceiling", f"effect-{effect}")
    return Accepted(claims=claims, principal=claims["sub"], caller=claims["act"]["sub"],
                    chain=chain_of(claims["act"]), kid=kid)


def verify_delegation(token: Any, *, audience: str, jwks: Any, now: int, capability: str, effect: str,
                      issuer: Optional[str], read_only: bool = False):
    """DL-13, in order: an `Accepted` or the first `Refusal` that applies. `issuer` is the `iss` this callee trusts
    and has no default (DEC-0045): None or empty is `issuer-unavailable` (`no-trusted-issuer`), never "any key"."""
    return _verify_at(token, audience=audience, jwks=jwks, now=now, capability=capability, effect=effect,
                      issuer=issuer, read_only=read_only, max_depth=MAX_CHAIN_DEPTH)


def verify_meter_token(token: Any, *, meter: str, capability: str, jwks: Any, now: int, issuer: Optional[str]):
    """DL-21: the onward token minted for the meter: `aud` the meter, `scope` exactly its grant-check capability,
    `effect` exactly `["none"]`. Only such a token may hold a fifth actor (DL-6, DEC-0045 item 2): the callee adds
    itself to a chain that may already be four deep. Any refusal of it is the meter's `principal-unproven`, the
    verifier's code and detail kept in the detail."""
    result = _verify_at(token, audience=meter, capability=capability, effect="none", jwks=jwks, now=now,
                        issuer=issuer, read_only=False, max_depth=MAX_CHAIN_DEPTH + 1)
    if result.ok:
        # DL-21 step 3 (F-B-312): the meter reads its one capability; a wider token was not minted for it.
        if result.claims["scope"] != capability:
            result = Refusal("scope-not-granted", "meter-scope-not-exact")
        elif result.claims["effect"] != ["none"]:
            result = Refusal("effect-above-ceiling", "meter-effect-not-none")
    if result.ok:
        return result
    return Refusal("principal-unproven", f"{result.code}:{result.detail}")


def read_delegation_token(headers: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """The token a request carries: `Fabric-Delegation` first (a present header is the token, even empty: DL-10
    refuses a present invalid one, never falls through), else an `Authorization: Bearer` JWT of the delegation
    `typ`. Header names are matched case-insensitively."""
    def get(name: str) -> Optional[str]:
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value[0] if isinstance(value, (list, tuple)) and value else value
        return None
    forwarded = get(DELEGATION_HEADER)
    if forwarded is not None:
        return str(forwarded).strip(), "fabric-delegation"
    match = re.fullmatch(rf"Bearer[{JS_SPACE}]+([^{JS_SPACE}]+)", get("Authorization") or "", re.IGNORECASE)
    if match:
        parsed, problem = parse_compact(match.group(1))
        if not problem and parsed[0].get("typ") == DELEGATION_TYP:
            return match.group(1), "authorization"
    return None
