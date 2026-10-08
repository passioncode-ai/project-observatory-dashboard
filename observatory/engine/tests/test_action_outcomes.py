"""PB-038 / OSS-12: an action whose outcome is unknown says so, instead of "failed".

Runs tests/action_outcome_check.mjs against the page the real dashboard builds. It
checks two things. First, how `call()` classifies each answer. Second, what the
button does after a press of the page's own `credAction()` (audit A46: the first
alone asserted less than OSS-12 claims):

- A refusal the server explained (400, and 502 for the provider's own) reads
  "failed" ("не вышло"), the reason goes to the toast, and the button can be
  pressed again.
- A server fault, an unreadable success, a dropped connection or a timeout reads
  "outcome unknown · check" ("исход неизвестен · проверьте"), and the button
  stays locked.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import dashboard_fixture  # noqa: E402
import tmp as tmpdir  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f" — {detail}"))
    if not ok:
        FAILS.append(name)


REFUSED = {"refused": "limit must be a number", "provider refused": "the provider refused: HTTP 401"}
UNCERTAIN = ("server fault", "unreadable success", "dropped", "timeout")
LABELS = {"en": ("failed", "outcome unknown · check"), "ru": ("не вышло", "исход неизвестен · проверьте")}


def check_buttons(r: dict, locale: str) -> None:
    """OSS-12's promise about the button itself, per case, in one locale."""
    failed, unknown = LABELS[locale]
    buttons = r.get("buttons") or {}
    check(f"[{locale}] the page's click handler was driven for every case",
          set(buttons) == set(REFUSED) | set(UNCERTAIN) | {"success"}, str(sorted(buttons)))
    for case, b in buttons.items():
        check(f"[{locale}] {case}: the button is locked while the call is pending",
              b.get("pending", {}).get("disabled") is True, str(b.get("pending")))
    for case, reason in REFUSED.items():
        b = buttons.get(case, {})
        check(f"[{locale}] {case}: the button says {failed!r} and can be pressed again",
              b.get("label") == failed and b.get("disabled") is False, str(b))
        check(f"[{locale}] {case}: the server's reason reaches the reader", reason in str(b.get("toast")), str(b))
    for case in UNCERTAIN:
        b = buttons.get(case, {})
        check(f"[{locale}] {case}: the button says {unknown!r} and stays locked",
              b.get("label") == unknown and b.get("disabled") is True, str(b))
        check(f"[{locale}] {case}: the hover title and the toast say why the outcome is unknown",
              bool(b.get("title")) and bool(b.get("toast")) and failed not in str(b.get("label")), str(b))
    b = buttons.get("success", {})
    check(f"[{locale}] success: the button stays locked until a rescan",
          b.get("disabled") is True and b.get("label") not in (failed, unknown), str(b))


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("  SKIP  node is not installed here, so the page cannot be executed")
        return 0
    page = dashboard_fixture.build(Path(tmpdir.mkdtemp(prefix="observatory-actions-")).resolve())
    p = subprocess.run([node, str(ROOT / "tests/action_outcome_check.mjs"), str(page)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    r = json.loads(p.stdout or "{}")
    check("the page carries call()", "error" not in r, p.stdout + p.stderr)
    expect = {"refused": "refused:", "provider refused": "refused:", "server fault": "uncertain:",
              "unreadable success": "uncertain:", "dropped": "uncertain:", "timeout": "uncertain:",
              "success": "ok"}
    for case, prefix in expect.items():
        check(f"{case} reads as {prefix.rstrip(':')}", str(r.get(case, "")).startswith(prefix), str(r.get(case)))
    check("a timeout names its wait", "no answer within" in str(r.get("timeout")), str(r.get("timeout")))
    check_buttons(r, "en")
    # The same outcomes in Russian: the message is the catalog's, not a literal.
    p = subprocess.run([node, str(ROOT / "tests/action_outcome_check.mjs"), str(page), "ru"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    ru = json.loads(p.stdout or "{}")
    check("in Russian the timeout still reads as uncertain and names its wait",
          str(ru.get("timeout", "")).startswith("uncertain:") and "нет ответа за" in str(ru.get("timeout")),
          p.stdout + p.stderr)
    check_buttons(ru, "ru")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
