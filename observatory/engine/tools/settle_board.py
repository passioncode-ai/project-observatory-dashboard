#!/usr/bin/env python3
"""Settle the board `local` just built: one more pass when the only thing it lacks
is the verdict `smoke` gave a moment later.

`findings` runs before `dashboard`, and `smoke` judges the page `dashboard` wrote,
so the receipt it leaves is read by the NEXT run's `findings`. On a workspace's
first `local` there is no receipt yet: the very first board a new user opens said
"the dashboard has not been verified to render at all" about the page they were
reading. When that row is on the board and the receipt now holds a clean verdict
for the page on disk, this re-runs `findings`, `dashboard`, `smoke` and
`smoke-pages` once, so the board shows the verdict. It never loops: the second
pass reads a receipt that matches the page, and every later `local` does too.

Nothing to settle (no such row, no receipt, a blank or unreadable verdict, a
receipt for another build) is said in one line and exits 0 — the board then
already carries the right row, `dashboard.blank` included."""
from __future__ import annotations
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                    


def needs_settling() -> tuple[bool, str]:
    try:
        board = json.loads((paths.REGISTRY / "findings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"no board to settle ({type(exc).__name__})"
    rows = board.get("findings", []) if isinstance(board, dict) else []
    if not any(isinstance(r, dict) and r.get("type") == "dashboard.unverified" for r in rows):
        return False, "the board already carries the page's verdict"
    receipt_file = paths.DASHBOARD_HTML.with_suffix(".smoke.json")
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        page_sha = hashlib.sha256(paths.DASHBOARD_HTML.read_bytes()).hexdigest()[:12]
    except (OSError, ValueError) as exc:
        return False, f"no smoke receipt to settle with ({type(exc).__name__})"
    if not isinstance(receipt, dict) or receipt.get("verdict") != "clean":
        return False, "the smoke verdict is not clean; the board keeps its row"
    if receipt.get("page_sha") != page_sha:
        return False, "the smoke receipt describes another build; the board keeps its row"
    return True, "the page was verified after the board was written"


def main() -> int:
    settle, why = needs_settling()
    print(f"settle: {why}" + ("; rebuilding the board once" if settle else ""), flush=True)
    if not settle:
        return 0
    steps = [[sys.executable, str(ROOT / "tools/build_findings.py")],
             [sys.executable, str(ROOT / "dashboard/build_dashboard.py")],
             ["node", str(ROOT / "dashboard/smoke.js"), str(paths.DASHBOARD_HTML)],
             [sys.executable, str(ROOT / "dashboard/smoke_pages.py")]]
    for cmd in steps:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if p.returncode != 0:
            tail = (p.stdout + p.stderr).strip().splitlines()[-3:]
            print(f"settle: {pathlib.Path(cmd[1]).name} failed (exit {p.returncode})", file=sys.stderr)
            for line in tail:
                print(f"  {line}", file=sys.stderr)
            return p.returncode
    # The counts `findings` printed earlier in the same run described the board
    # BEFORE this rebuild; the last numbers a person sees must be the board's.
    try:
        board = json.loads((paths.REGISTRY / "findings.json").read_text(encoding="utf-8")).get("findings") or []
        tally = "   ".join(f"{sev} {sum(1 for f in board if f.get('severity') == sev)}"
                           for sev in ("critical", "warning", "info"))
        print(f"settle: the board now carries the page's verdict; findings now: {tally}", flush=True)
    except (OSError, ValueError, AttributeError) as exc:
        print(f"settle: the board now carries the page's verdict (its counts could not be read: "
              f"{type(exc).__name__})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
