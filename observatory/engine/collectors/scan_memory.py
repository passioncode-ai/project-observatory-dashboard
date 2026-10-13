#!/usr/bin/env python3
"""Bounded memory-only receipt: scan_memory.py OUT [--budget-seconds 8]."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import memory_snapshot


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=Path)
    ap.add_argument("--budget-seconds", type=float, default=8)
    ap.add_argument("--attempt-id", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    doc = memory_snapshot.write(args.out, args.budget_seconds, args.attempt_id)
    print("memory-only: partial" if doc["partial"] else "memory-only: measured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
