#!/usr/bin/env python3
"""The meter from a terminal (meter.py, OBS-55; contract DEC-0037/0039, Fabric ADR-0144).

    project-observatory full grant --status
        the estate's month against its limit (L0), each provider key against its ceiling, the open
        reservations, and what the meter cannot read yet (`degraded`)
    project-observatory full grant --check --amount USD --jti JTI [--grant-ref REF] [--purpose TEXT] < token
        one grant check; the onward delegation token minted for the meter is read from STDIN, never from an
        argument (contract DL-11). Prints the grant-check answer; exit 0 allowed, 3 denied, 2 a bad request
    project-observatory full grant --settle RECEIPT --usd USD
        settle a receipt with what was actually spent

Configuration: `config/meter.json` (see meter.py). Over the wire the same check is the MCP tool `grant.check`
on the memory-http door, the token in `Authorization: Bearer`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import meter as M  # noqa: E402


def amount(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("a number of USD")
    if number < 0 or number != number or number == float("inf"):
        raise argparse.ArgumentTypeError("a number of USD at least 0")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full grant",
                                description="Check, settle and summarise spend against grants (the meter).")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true", help="the month, the keys and open reservations")
    mode.add_argument("--check", action="store_true", help="one grant check; the token on stdin")
    mode.add_argument("--settle", metavar="RECEIPT", help="settle a receipt")
    p.add_argument("--amount", type=amount, metavar="USD", help="--check: the amount to reserve")
    p.add_argument("--jti", metavar="JTI", help="--check: the jti of the token the callee was called with")
    p.add_argument("--grant-ref", metavar="REF", help="--check: which of the token's grants")
    p.add_argument("--purpose", metavar="TEXT", help="--check: one line a person can read")
    p.add_argument("--usd", type=amount, metavar="USD", help="--settle: what was spent")
    return p


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    meter = M.default_meter()
    if args.status:
        print(json.dumps(meter.status(), indent=1, ensure_ascii=False))
        return 0
    if args.settle:
        if args.usd is None:
            print("grant: --settle needs --usd", file=sys.stderr)
            return 2
        try:
            print(json.dumps(meter.settle(args.settle, args.usd), indent=1))
        except M.RequestError as exc:
            print(f"grant: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.amount is None or not args.jti:
        print("grant: --check needs --amount and --jti", file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        print("grant: the meter's token is read from stdin (`< token-file`), never from an argument",
              file=sys.stderr)
        return 2
    token = sys.stdin.read().strip()
    request = {"amountUsd": args.amount, "tokenJti": args.jti}
    if args.grant_ref:
        request["grantRef"] = args.grant_ref
    if args.purpose:
        request["purpose"] = args.purpose
    try:
        answer = meter.check(token, request)
    except M.RequestError as exc:
        print(f"grant: {exc}", file=sys.stderr)
        return 2
    finally:
        del token
    print(json.dumps(answer, indent=1, ensure_ascii=False))
    return 0 if answer["decision"] == "allowed" else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
