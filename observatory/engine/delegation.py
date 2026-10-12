"""The callee's check of a Fabric delegation token (contract `fabric-delegation/0.1`, DEC-0039; Fabric ADR-0144).

Fabric's hub is the estate's one issuer: it signs a 10-minute JWT naming the person the work is for (`sub`), the
chain of agents acting (`act`, RFC 8693), one callee (`aud`), the capabilities (`scope`), the effect ceiling and the
grant the call is under. A callee checks it offline against the estate's published Ed25519 keys. This module is
the contract kit's `verifyDelegation` / `verifyMeterToken` (fabric-agent-contract `src/delegation.ts` at 8e49fbd)
in Python, following the specification's verifier table DL-13 in its order: nothing about a token is believed
before its signature is, so an unsigned token learns only `unauthenticated`.

A refusal carries a contract code and a short machine word (`detail`); never the token, never a key. The token is
read from a header and nowhere else (DL-11): never argv, a URL, a tool argument or a log line.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

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


def b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(segment: str) -> tuple[Optional[dict], Optional[str]]:
    if not _B64URL.match(segment):
        return None, "malformed"
    try:
        value = json.loads(b64url_decode(segment).decode("utf-8"), object_pairs_hook=_no_duplicates)
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
    if not _B64URL.match(parts[2]):
        return None, "malformed"
    return (header, payload, f"{parts[0]}.{parts[1]}".encode("ascii"), b64url_decode(parts[2])), None


def _is_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def _is_time(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return None, "no-ed25519"
    try:
        raw = b64url_decode(jwk["x"]) if _B64URL.match(jwk["x"]) else b""
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
        return None, problem, problem in ("unusable-key", "duplicate-kid", "no-ed25519")
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


def _delegation_shape(payload: dict) -> bool:
    if not all(_is_string(payload.get(k)) for k in ("iss", "sub", "aud", "scope", "jti")):
        return False
    if not (_is_time(payload.get("iat")) and _is_time(payload.get("exp"))):
        return False
    effect, grant = payload.get("effect"), payload.get("grant")
    if not isinstance(effect, list) or not effect or not all(e in EFFECT_CLASSES for e in effect):
        return False
    # Fabric ADR-0144 amendment 3: exactly one grant per token; an onward token keeps the same reference.
    if not isinstance(grant, list) or len(grant) != 1 or not _is_string(grant[0]):
        return False
    # DEC-0041: an optional claim the meter keys a window on is typed like any other.
    if "task" in payload and not (isinstance(payload["task"], str) and OPAQUE_ID.match(payload["task"])):
        return False
    return isinstance(payload.get("act"), dict)


def verify_delegation(token: Any, *, audience: str, jwks: Any, now: int, capability: str, effect: str,
                      issuer: Optional[str] = None, read_only: bool = False, max_depth: int = MAX_CHAIN_DEPTH):
    """DL-13, in order: an `Accepted` or the first `Refusal` that applies."""
    if not has_keys(jwks):
        return Refusal("issuer-unavailable", "no-published-keys")
    checked, problem, issuer_fault = _authentic(token, jwks, DELEGATION_TYP)
    if problem:
        return Refusal("issuer-unavailable" if issuer_fault else "unauthenticated", problem)
    _header, claims, kid = checked
    if not _delegation_shape(claims):
        return Refusal("unauthenticated", "malformed-claims")
    if issuer is not None and claims["iss"] != issuer:
        return Refusal("unauthenticated", "wrong-issuer")
    if claims["exp"] <= claims["iat"] or claims["exp"] - claims["iat"] > MAX_DELEGATION_LIFETIME_SECONDS:
        return Refusal("unauthenticated", "lifetime-above-profile")
    if claims["iat"] > now + LEEWAY_SECONDS:
        return Refusal("unauthenticated", "issued-in-future")
    if "nbf" in claims and (not _is_time(claims["nbf"]) or claims["nbf"] > now + LEEWAY_SECONDS):
        return Refusal("unauthenticated", "not-yet-valid")
    if now > claims["exp"] + LEEWAY_SECONDS:
        return Refusal("token-expired", "expired")
    if claims["aud"] != audience:
        return Refusal("wrong-audience", "audience-mismatch")
    depth = chain_depth(claims["act"])
    if depth == float("inf"):
        return Refusal("unauthenticated", "malformed-chain")
    if depth > max_depth:
        return Refusal("chain-too-deep", f"depth-{int(depth)}")
    if capability not in claims["scope"].split(" "):
        return Refusal("scope-not-granted", "capability-not-in-scope")
    if read_only and effect != "none":
        return Refusal("effect-above-ceiling", "read-only-door")
    if not effect_within(effect, claims["effect"]):
        return Refusal("effect-above-ceiling", f"effect-{effect}")
    return Accepted(claims=claims, principal=claims["sub"], caller=claims["act"]["sub"],
                    chain=chain_of(claims["act"]), kid=kid)


def verify_meter_token(token: Any, *, meter: str, capability: str, jwks: Any, now: int,
                       issuer: Optional[str] = None):
    """DL-21: the onward token minted for the meter (aud the meter, scope its grant-check capability, effect
    none, a read-only door). Any refusal of it is the meter's `principal-unproven`, the verifier's code kept in
    the detail."""
    # Amendment 3: the meter's hop may sit one actor deeper than the limit — the callee adds itself to a chain
    # that may already be four deep — and only a token for the meter, read-only, with effect none.
    result = verify_delegation(token, audience=meter, capability=capability, effect="none", jwks=jwks,
                               now=now, issuer=issuer, read_only=True, max_depth=MAX_CHAIN_DEPTH + 1)
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
    match = re.match(r"^Bearer\s+(\S+)$", get("Authorization") or "", re.IGNORECASE)
    if match:
        parsed, problem = parse_compact(match.group(1))
        if not problem and parsed[0].get("typ") == DELEGATION_TYP:
            return match.group(1), "authorization"
    return None
