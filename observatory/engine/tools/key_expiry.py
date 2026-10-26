#!/usr/bin/env python3
"""How long a credential a door mints may live, and how that is written down.

THE RULE (OBS-191, part 1). Every door that can mint a credential with an expiry
mints it with one: `cloudflare.py issue` (API tokens, `expires_on`),
`openrouter.py issue` (keys, `expires_at`) and `heroku.py issue` (OAuth
authorizations, `expires_in`). The default is `DEFAULT_DAYS`; a flag moves it
within 1 to `MAX_DAYS`, and an explicit `--no-expiry` (Cloudflare:
`--no-token-expiry`, since its `--expire-days` already means the R2 bucket's
objects) mints one that does not expire. A credential nobody asked to outlive
its use should not, and a leaked one that expires by itself bounds the damage.

THE PROVIDER'S ANSWER, NEVER THE REQUEST. What is recorded is the expiry the
provider answered (`from_answer`): an answer without the field records null
with `UNCONFIRMED`, because a request is not evidence of what was minted.

THE RECORD, NEVER THE VALUE. The expiry is written where the door already keeps
the credential's metadata: the vault slot's `meta.json` (`vault.record_expiry`),
the door's own ledger or meta file. Two fields:

  expires_at     `YYYY-MM-DDTHH:MM:SSZ`, or null when the credential does not expire
  expiry_reason  only when `expires_at` is null: why (asked for none, the provider
                 cannot set one, minted outside this program)

A record without `expires_at` at all is an older one, from before this rule, and
reads "expiry not recorded": not knowing is a different answer from "never".

A DOOR THAT CANNOT MINT says so in the same two fields. DigitalOcean's public
API has no token endpoint: its `issue` prints the control-panel recipe with an
expiry to choose, and `install --expires-on` records what the operator chose,
or null with the reason when nothing was given.
"""
from __future__ import annotations

import argparse
import datetime

#: The default lifetime of a minted credential, in days. Ninety is the common
#: rotation period for API keys, and long enough that an issue is not a weekly chore.
DEFAULT_DAYS = 90
#: The longest lifetime a door mints without being asked for none at all. A year:
#: past that a credential is forgotten rather than rotated.
MAX_DAYS = 365

#: Why a credential was minted without an expiry: the issuer asked (`flag` names how).
ASKED_FOR_NONE = "minted without an expiry on request ({flag})"
#: Why a minted credential's expiry is unknown: the provider's answer did not carry the field.
UNCONFIRMED = "the provider's answer did not confirm an expiry"
#: Why a credential minted with an expiry asked for has none: the provider answered that.
PROVIDER_SAID_NONE = "an expiry was asked for, and the provider answered that it does not expire"
#: Why an installed credential's expiry is unknown here: it was minted elsewhere.
MINTED_ELSEWHERE = "minted outside this program; its expiry is set where it was minted"


class ExpiryError(ValueError):
    """An expiry a door will not mint: out of range, or not a whole number of days."""


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(
        datetime.timezone.utc).replace(microsecond=0)


def stamp(moment: datetime.datetime) -> str:
    """The one spelling: UTC, whole seconds, `Z`. OpenRouter refuses a minute-precision
    timestamp, and Cloudflare reads this form too, so every door sends and records it."""
    return _now(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_days(days: int | None) -> int | None:
    """`days` when a door may mint with it, None for no expiry; refused otherwise."""
    if days is None:
        return None
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise ExpiryError(f"an expiry is a whole number of days from 1 to {MAX_DAYS}, "
                          f"or none at all with the door's no-expiry flag")
    return days


def expires_at(days: int | None, now: datetime.datetime | None = None) -> str | None:
    """When a credential minted now with `days` of life expires; None for no expiry."""
    if check_days(days) is None:
        return None
    return stamp(_now(now) + datetime.timedelta(days=days))


def from_seconds(seconds, now: datetime.datetime | None = None) -> str | None:
    """A provider's `expires_in` answer (seconds from now) as a timestamp; None when it gave none."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return stamp(_now(now) + datetime.timedelta(seconds=int(seconds)))


def parse(text) -> str | None:
    """A provider's or an operator's timestamp (or `YYYY-MM-DD`, read as midnight UTC) in the
    one spelling; None when absent or unreadable."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    try:
        if len(raw) == 10:
            moment = datetime.datetime.fromisoformat(raw).replace(tzinfo=datetime.timezone.utc)
        else:
            moment = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return stamp(moment)


def from_answer(answer, field: str, asked: str | None, none_reason: str) -> dict:
    """The record of what the PROVIDER answered, never of what was asked (OBS-191 review).

    `answer` is the provider's object for the credential and `field` its expiry
    field. A timestamp there is recorded. The field present and null is a
    credential that does not expire: `none_reason` when none was asked, else
    `PROVIDER_SAID_NONE`. The field absent, or unreadable, is `UNCONFIRMED`: the
    request is not evidence of what the provider did with it."""
    if not isinstance(answer, dict) or field not in answer:
        return fields(None, UNCONFIRMED)
    value = answer.get(field)
    at = parse(value)
    if at:
        return fields(at)
    if value is None or value == "":
        return fields(None, none_reason if asked is None else PROVIDER_SAID_NONE)
    return fields(None, UNCONFIRMED)


def fields(at: str | None, reason: str | None = None) -> dict:
    """The two record fields: `expires_at`, and `expiry_reason` only when there is no expiry."""
    if at:
        return {"expires_at": at}
    return {"expires_at": None, "expiry_reason": reason or "no reason recorded"}


def record(meta: dict, at: str | None, reason: str | None = None) -> dict:
    """Write the two fields into a metadata dict in place (an expiry drops a stale reason)."""
    meta.pop("expiry_reason", None)
    meta.update(fields(at, reason))
    return meta


def clear(meta: dict) -> dict:
    """Forget a recorded expiry: a value replaced by hand is not the one it described."""
    for key in ("expires_at", "expiry_reason", "expiry_recorded", "expiry_by"):
        meta.pop(key, None)
    return meta


def describe(meta: dict | None, now: datetime.datetime | None = None) -> str:
    """One line for a person: `expires 2027-01-24`, `EXPIRED 2026-01-01`, `no expiry: why`,
    or `expiry not recorded` for a record older than the rule."""
    meta = meta or {}
    if "expires_at" not in meta:
        return "expiry not recorded"
    at = parse(meta.get("expires_at"))
    if not at:
        return f"no expiry: {meta.get('expiry_reason') or 'no reason recorded'}"
    if at <= stamp(_now(now)):
        return f"EXPIRED {at[:10]}"
    return f"expires {at[:10]}"


def add_flags(parser: argparse.ArgumentParser, days_flag: str = "--expire-days",
              none_flag: str = "--no-expiry", what: str = "credential") -> None:
    """The two flags every minting door takes, as one mutually exclusive pair."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(days_flag, dest="key_expire_days", type=int, default=DEFAULT_DAYS,
                       metavar="DAYS",
                       help=f"the minted {what} expires after DAYS days "
                            f"(default {DEFAULT_DAYS}, at most {MAX_DAYS}); recorded with it")
    group.add_argument(none_flag, dest="key_no_expiry", action="store_true",
                       help=f"mint a {what} that does not expire; recorded with the reason")


def days_of(args: argparse.Namespace) -> int | None:
    """The lifetime the flags ask for: None for no expiry."""
    return None if getattr(args, "key_no_expiry", False) else getattr(args, "key_expire_days", DEFAULT_DAYS)
