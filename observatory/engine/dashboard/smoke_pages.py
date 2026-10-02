#!/usr/bin/env python3
"""Execute every split page's script against the stub DOM — the same check
`dashboard/smoke.js` runs on the single page, once per page.

A page that carries only its own data slice is exactly where a renderer can
reach for a key the slice does not hold, and nothing static can see that."""
from __future__ import annotations
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))
import paths                                                                    
import shell                                                                    


def main() -> int:
    bad = 0
    for name, _t, _k in shell.PAGES:
        page = paths.DASHBOARD_DIR / f"{name}.html"
        if not page.is_file():
            print(f"  {name}: not built — project-observatory full dashboard")
            bad += 1
            continue
        p = subprocess.run(["node", str(ROOT / "dashboard/smoke.js"), str(page)],
                           capture_output=True, text=True, timeout=120)
        tail = (p.stdout + p.stderr).strip().splitlines()
        print(f"  {name}: {'ok' if p.returncode == 0 else 'FAILED'}"
              + (f" — {tail[-1][:120]}" if tail and p.returncode else ""))
        bad += p.returncode != 0
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
