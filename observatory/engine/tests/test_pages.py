#!/usr/bin/env python3
"""The split dashboard: nine pages, two shared files, and what each page carries.

WHY THIS FILE. The single page was 1.3 MB with seven tabs, and the operator's
verdict was that it could not be used (plan v2, M1; scenario S12). The split
answers it, and three of its properties break silently:

  1. the NAVIGATION must come before the findings in the document — three
     earlier iterations put alerts above the way out, and every one of them
     passed every check that existed;
  2. a page must carry ONLY the data it renders, or the split saves nothing;
  3. the shared style and script must sit BESIDE the page as relative
     siblings, or `file://` — which is how these pages are opened most of the
     time — loads a page with no style and no script at all.

`dashboard/smoke_pages.py` executes each page; this reads what smoke cannot:
document order, byte size, the slice, and the routes the server answers with.
"""
from __future__ import annotations
import http.client
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup, PROJECT_ID as SYNTHETIC_PROJECT
portable_setup()

sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT / "tests"))
import paths                                                        # noqa: E402
import shell                                                        # noqa: E402
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
#: An index a person waits for is an index nobody opens (plan v2, T-02 DoD).
INDEX_BUDGET = 120 * 1024
PORT = 47989
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def built() -> dict[str, str]:
    """Every page as text. The `dashboard` step builds them before this runs;
    a missing one is a failure here rather than an exception."""
    out = {}
    for name, _t, _k in shell.PAGES:
        p = paths.DASHBOARD_DIR / f"{name}.html"
        out[name] = p.read_text(encoding="utf-8") if p.is_file() else ""
    return out


def test_the_pages_exist_and_the_index_is_small() -> None:
    pages = built()
    missing = [n for n, html in pages.items() if not html]
    check("every page in the shell's list is built", not missing,
          f"missing: {missing} — ./observatory.py dashboard")
    for asset in (shell.ASSET_CSS, shell.ASSET_JS):
        f = paths.DASHBOARD_DIR / asset
        check(f"{asset} is written beside the pages", f.is_file() and f.stat().st_size > 1000,
              "the pages link to it by name; without it they are text on white")
    size = len((pages.get("index") or "").encode("utf-8"))
    check(f"the index is under {INDEX_BUDGET // 1024} KB", 0 < size <= INDEX_BUDGET,
          f"{size // 1024} KB — the slice or the shared files have drifted back inline")
    for name, html in pages.items():
        if not html:
            continue
        check(f"{name}.html says which page it is", f'const PAGE = "{name}";' in html,
              "the whole split behaviour keys off that constant")


def test_the_navigation_comes_before_the_alerts() -> None:
    for name, html in built().items():
        if not html:
            continue
        nav, find = html.find('<nav class="pages"'), html.find('<section id="findings"')
        check(f"{name}: the nav is in the document and precedes the findings",
              0 <= nav < find, f"nav at {nav}, findings at {find} (S12)")
        cur = html.count('aria-current="page"')
        check(f"{name}: exactly one nav item is marked as the current page", cur == 1, str(cur))


def test_the_shared_files_are_relative_siblings() -> None:
    """`file://` is the common way these are opened, and it has no root: a
    leading slash resolves to the filesystem root and loads nothing."""
    for name, html in built().items():
        if not html:
            continue
        css = re.search(r'<link rel="stylesheet" href="([^"]+)"', html)
        js = re.search(r'<script src="([^"]+)"', html)
        check(f"{name}: links a stylesheet and a script by relative name",
              bool(css) and bool(js) and css.group(1) == shell.ASSET_CSS
              and js.group(1) == shell.ASSET_JS,
              f"css={css and css.group(1)} js={js and js.group(1)}")
        for m in (css, js):
            if m:
                check(f"{name}: {m.group(1)} has no leading slash and no scheme",
                      not m.group(1).startswith(("/", "http")), m.group(1))
    single = paths.DASHBOARD_HTML.read_text(encoding="utf-8") if paths.DASHBOARD_HTML.is_file() else ""
    check("the single page stays self-contained", bool(single) and "<style>" in single
          and f'src="{shell.ASSET_JS}"' not in single,
          "design and smoke read it as one file; a sibling would make it three")


def test_the_split_keeps_one_template() -> None:
    import build_dashboard as bd
    page, css, js = shell.split_template(bd.TEMPLATE)
    check("the style comes out whole and the page links to it",
          len(css) > 1000 and "<style>" not in page
          and f'href="{shell.ASSET_CSS}"' in page, f"{len(css)} chars of css")
    check("the shared script comes out whole and carries no per-page data",
          len(js) > 10000 and "__DATA__" not in js and "__PAGE__" not in js,
          f"{len(js)} chars of js")
    check("what stays inline is the page, the table list and the data",
          all(x in page for x in ("__PAGE__", "__DATA__", "TABLE_PAGES")),
          "those three are what differ per page; everything else is shared")
    check("and the shared file is loaded after them", page.index("__DATA__")
          < page.index(f'src="{shell.ASSET_JS}"'),
          "app.js reads D at load; loading it first is a ReferenceError on every page")


def test_a_page_carries_only_what_it_renders() -> None:
    payload = {
        "rows": [{"id": "project:a", "name": "a", "anchor": "x", "products": [],
                  "tier": 1, "lifecycle": "live", "events": [1] * 50, "repos": [1] * 20}],
        "env": {"totals": {"secrets": 3}, "vars": [1] * 100},
        "heroku": {"apps": [1, 2]}, "creds": {"credentials": [1]},
        "mcp": {"totals": {"distinct_servers": 4}},
        "zones": [{"name": "z.dev"}], "domains": [{"name": "d.dev"}],
        "queue": [{"q": 1}],
        "health": {"server_age_s": 3, "wallet": {"month": 5, "json": [1] * 100}},
        "findings": {"counts": {"critical": 1, "warning": 2},
                     "items": [{"id": "a", "severity": "critical", "detail": "long text"},
                               {"id": "b", "severity": "warning", "detail": "long text"},
                               {"id": "c", "severity": "info", "detail": "long text"}],
                     "folded_by_type": {"t": 4}},
    }
    idx = shell.slice_for("index", payload)
    check("the index previews every severity and says how many are elsewhere",
          [f["id"] for f in idx["findings"]["items"]] == ["a", "b", "c"]
          and idx["findings"]["elsewhere"] == 0, json.dumps(idx["findings"])[:160])
    check("without their detail — it is on the findings page, in full",
          idx["findings"]["items"][0]["detail"] == "", str(idx["findings"]["items"][0]))
    check("and the counts still name every severity",
          idx["findings"]["counts"] == payload["findings"]["counts"])
    f = shell.slice_for("findings", payload)
    check("the findings page carries all three, whole",
          len(f["findings"]["items"]) == 3
          and f["findings"]["items"][2]["detail"] == "long text", "")
    for owner, other in (("env", "heroku"), ("heroku", "creds"), ("creds", "mcp"), ("mcp", "env")):
        s = shell.slice_for(owner, payload)
        check(f"the {owner} page carries {owner} and not {other}",
              s[owner] is not None and s[other] is None, "")
    p = shell.slice_for("projects", payload)
    check("only the projects page carries whole rows",
          "events" in p["rows"][0] and "events" not in idx["rows"][0]
          and idx["rows"][0]["name"] == "a",
          "other pages look a project up by name; they never draw the table")
    d = shell.slice_for("domains", payload)
    check("zones and domains live on the domains page", d["zones"] and d["domains"]
          and idx["zones"] is None and idx["domains"] == [], "")
    h = shell.slice_for("health", payload)
    check("the wallet's lists are on the health page only",
          h["health"]["wallet"]["json"] == [1] * 100
          and "json" not in idx["health"]["wallet"]
          and idx["health"]["wallet"]["month"] == 5, json.dumps(idx["health"])[:120])
    check("and the queue with it", h["queue"] and idx["queue"] == [], "")
    counts = shell.counts_of(payload)
    check("the badges are computed once and carried by every page",
          counts["findings"] == 3 and counts["projects"] == 1 and counts["env"] == 3
          and counts["domains"] == 2, str(counts))


def test_a_redirected_page_takes_its_pages_with_it() -> None:
    """One knob for the whole surface. A fixture that redirected only the single
    page still wrote nine LIVE pages out of its sandbox, and after a gate run the
    real dashboard said the store was unreadable and every work figure was zero —
    invisible, because the pages are gitignored and the hygiene check watches
    tracked files. Measured 2026-09-14 on `tests/render_provider_health.py`."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-redirect-"))
    code = ("import os, sys, pathlib\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import paths\n"
            "print(paths.DASHBOARD_HTML)\nprint(paths.DASHBOARD_DIR)\n")
    p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=120, env={**{k: v for k, v in os.environ.items()
                                            if k != "OBSERVATORY_DASHBOARD_DIR"},
                                         "OBSERVATORY_DASHBOARD": str(d / "page.html")})
    page, pages = (p.stdout.strip().splitlines() + ["", ""])[:2]
    check("redirecting the page redirects the split pages with it",
          pages.startswith(str(d)), f"page={page} dir={pages}")
    p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=120, env={**os.environ,
                                         "OBSERVATORY_DASHBOARD": str(d / "page.html"),
                                         "OBSERVATORY_DASHBOARD_DIR": str(d / "elsewhere")})
    check("and an explicit directory still wins",
          p.stdout.strip().endswith("elsewhere"), p.stdout.strip()[-80:])


def test_every_bridge_leads_somewhere_that_exists() -> None:
    """A copy-command is a promise about another file. The page offers three —
    restart the observer, decide a queued row, open the drifted rows — and each
    is checked against the tool it names, because a command that no longer
    parses is worse than no button at all (plan v2, T-05/T-25/T-30)."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    serverd = (ROOT / "tools/serverd.py").read_text(encoding="utf-8")
    review = (ROOT / "tools/review.py").read_text(encoding="utf-8")
    check("the observer's two bad states each carry a command",
          "SERVERD_FIX" in src and 'toolCommand("serverd.py", ["--run"])' in src
          and 'toolCommand("serverd.py", ["--status"])' in src, "S4 said SILENT and named no verb")
    check("and `--install` is a flag serverd actually has",
          '"--install"' in serverd, "the button would print an argparse error")
    from tools import serverd as service
    label = service.LABEL
    check("the observer actions use workspace-aware tools instead of another installation's launchd label",
          bool(label) and "launchctl kickstart" not in src and "OBSERVATORY_HOME=" in src,
          "the public dashboard must not restart a historical personal job")
    check("a queued row carries promote and reject with its own id",
          'toolCommand("review.py", ["promote", r.id' in src
          and 'toolCommand("review.py", ["reject", r.id' in src,
          "F9 meant retyping a memory id by hand, which nobody did 127 times")
    for verb in ("promote", "reject"):
        check(f"and `{verb}` is a verb review.py takes", f'"{verb}"' in review, "")
    check("the page never claims to decide anything itself",
          "--yes" in src and "the decision is taken in a terminal" in src,
          "the TTY rule is the point; a button that looked like a decision would undo it")
    check("the drift tile links to the table with its own filter on",
          'href="projects.html?f=drift"' in src and 'data-f="drift"' in src, "")
    check("and a chip named in the address is pressed at load",
          "URLSearchParams" in src and 'getAll("f")' in src,
          "a link that filters nothing is a link to 157 rows")


def test_the_keys_page_offers_the_doors_own_verbs() -> None:
    """S15 — a key's lifecycle goes through the door, and only the door.

    The page named the doors nowhere: both admin stashes sat among sixteen
    alphabetical machine files, every row offered one command, and two of them
    were wrong for the row (`install_key.py` for a key the door now issues, a
    vault `rotate` for a credential with no slot to rotate). Every verb the page
    offers is checked here against the tool it names — a button offering a
    subcommand the door dropped is how a reader learns to distrust the column.
    """
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the page has sections and names the doors first",
          "CRED_SECTIONS" in src and 'T("Doors")' in src
          and src.index('T("Doors")') < src.index('T("Issued keys")'), "")
    check("an empty section says so in words rather than vanishing",
          "no key minted yet" in src,
          "a section that disappears teaches that what is shown is all there is")
    check("a door is derived from who the registry says reads the file",
          "doorOf" in src and "read_by" in src,
          "listing the two ids here would be a second source for one fact")
    settle_commands = re.findall(r'toolCommand\("vault\.py", \["settle"[^\]]*\]', src)
    check("each offered settlement command requires both evidence fields",
          len(settle_commands) >= 1 and all(
              "--revocation-evidence" in command and "--consumer-evidence" in command
              for command in settle_commands), str(settle_commands))
    settle_help = subprocess.run([PY, str(ROOT / "tools/vault.py"), "settle", "--help"],
                                 capture_output=True, text=True, timeout=120)
    check("the offered settlement evidence flags exist in the CLI",
          settle_help.returncode == 0 and "--revocation-evidence" in settle_help.stdout
          and "--consumer-evidence" in settle_help.stdout)
    verbs = {"openrouter": ("limit", "disable", "enable", "rotate", "revoke",
                            "issue", "ping", "list"),
             "cloudflare": ("ping", "list", "issue"),
             "vault": ("leak", "settle", "rotate", "put")}
    for tool, subs in verbs.items():
        help_text = subprocess.run([PY, str(ROOT / f"tools/{tool}.py"), "--help"],
                                   capture_output=True, text=True, timeout=120).stdout
        for sub in subs:
            check(f"`{tool}.py {sub}` is a verb that tool still has",
                  sub in help_text, f"the page offers it; --help does not list it")
    check("a credential known only from the leak register is not offered a rotate",
          "known_only_from_the_leak" in src,
          "`rotate` refuses without a slot, and a verb that refuses teaches distrust")
    check("marking a leak asks for the place and never the value",
          'act === "leak"' in src and "where" in src and "--where" in src, "")
    check("issuing asks for a name, a ceiling and a destination — never a value",
          'act === "mint"' in src and "destination" in src
          and "value" not in src.split('act === "mint"')[1][:600].lower(), "")
    ks = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    check("and the server checks that destination against this estate",
          "check_destination" in ks and "known_projects" in ks,
          "an arbitrary destination would make a provisioning key an arbitrary writer")


def test_the_estate_pages_hand_over_commands_and_fold_what_is_long() -> None:
    """T-31 and T-06: the two remaining pages a person could not work from.

    The Heroku table named an application and left the operator to retype it
    into every command; the domains table raised three questions it could not
    answer and pointed nowhere. ENV metadata stays visible by default under
    UI-01; project filters and group counts retain orientation in a long list.
    """
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    for cmd in ("heroku ps:restart -a ", "heroku logs -t -a ", "heroku apps:info -a "):
        check(f"a Heroku row carries `{cmd.strip()}`", cmd in src,
              "the application name is the one argument every command needs")
    check("and the page still executes nothing",
          "data-copy" in src and "child_process" not in src, "read-only by design")
    check("a domain row points at RDAP, DNS and its Cloudflare zone",
          "rdap.org/domain/" in src and "dig +short" in src
          and "dash.cloudflare.com" in src, "")
    check("the ENV table retains project context and says each group's size",
          "grp-fold" in src and "data-envgroup" in src and 'T("{n} variables", {n: es.length})' in src,
          "project context must state how many variables it describes")
    # Filter expansion is driven on rendered fixture data by tests/env_tab_check.js
    # (suite env_page); the source expression is not pinned here.
    check("and a secret tracked by git opens its own group",
          'e.cls === "secret" && e.git === "tracked"' in src,
          "the one case urgent enough to be above the fold")
    # THE GROUP HEADER MUST SPAN EVERY COLUMN, checked in the renderer's own
    # source: the built page carries the script, not the table it draws, so a
    # check against the HTML would silently assert nothing. Adding a ninth
    # column and leaving `colspan="8"` leaves the last column outside every
    # group heading — which is what happened twice while writing this.
    for fn in ("renderHeroku", "renderDomains", "renderEnv", "renderCreds"):
        body = src.split(f"function {fn}(", 1)[1].split("\nfunction ", 1)[0]
        thead = re.search(r"<thead>.*?</thead>", body, re.S)
        # The span is either a literal `colspan="N"` (env keeps its own header)
        # or the first argument of `grpHead(N, …)`, the shared header since D-12.
        span = re.search(r'colspan="(\d+)"|\$\{grpHead\((\d+),', body)
        check(f"{fn}: the table and its group header are both readable here",
              bool(thead) and bool(span), "one of them moved out of this function")
        if not (thead and span):
            continue
        # A column is `<th>` or a sortable `sortTh(` (D-11) — both are one header cell.
        cols = len(re.findall(r"<th>|sortTh\(", thead.group(0)))
        said = span.group(1) or span.group(2)
        check(f"{fn}: the group header spans all {cols} column(s)",
              int(said) == cols,
              f'colspan="{said}" over {cols} columns')


def test_the_shell_names_the_page_and_offers_the_language() -> None:
    """Backlog D-03/D-04/D-05 (audit A-01, A-14, A-15): nine pages shared one
    title and one heading, the theme followed the OS with no way to choose,
    and the way out scrolled away on a long table."""
    pages = built()
    titles = {n: re.search(r"<title>([^<]*)</title>", h).group(1) for n, h in pages.items() if h}
    check("every page has its own title, and they differ",
          len(set(titles.values())) == len(titles) and all("Project Observatory" in v for v in titles.values()),
          str(titles))
    for name, html in pages.items():
        if not html:
            continue
        h1 = re.search(r"<h1\b[^>]*>([^<]*)</h1>", html)
        check(f"{name}: the heading is the page's name, not the site's",
              bool(h1) and h1.group(1) == dict((n, tt) for n, tt, _k in shell.PAGES)[name], h1 and h1.group(1))
        check(f"{name}: the page says what question it answers",
              shell.QUESTIONS[name].split(" ")[0] in html, "IS-01: a page of everything")
        check(f"{name}: navigation precedes the workspace heading",
              html.index('id="topbar"') < html.index("<header>"),
              "the navigation remains the first way out of each screen")
        # 0.4.0: one fixed PassionCode dark theme; the rail's switch is the
        # reader's LANGUAGE, one button per supported locale.
        check(f"{name}: the language switch offers every locale",
              all(f'data-locale="{m}"' in html for m in ("en", "ru")) and 'data-theme="dark"' in html, "")
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the stored language is applied in the head, before first paint",
          src.index('localStorage.getItem("observatory.locale")') < src.index("<style>"),
          "applied from the bottom script, the page would paint one language and read in another")
    check("«/» focuses the search from anywhere on a table page",
          'ev.key !== "/"' in src and 'q.focus()' in src, "the pack's keyboard-first rule")


def test_the_live_verbs_have_a_listener_and_the_pages_can_be_live() -> None:
    """The audit's one critical (A-02/A-03): `credAction()` was defined and
    nothing called it, and the keyserver served only the single page, so no
    page a person opens could ever be LIVE."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("a delegated listener reaches credAction from every [data-act] button",
          'closest("[data-act][data-cred]")' in src and "credAction(btn, c)" in src,
          "five write routes were unreachable from the UI")
    check("the findings ack copies through the one clipboard path",
          "copyText(cmd)" in src and "window.prompt(" not in src, "")
    ks = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    check("the keyserver serves the split pages from the shell's whitelist",
          "_page_for" in ks and "shell.NAMES" in ks and "ASSET_JS" in ks, "")


def test_every_table_says_what_narrowed_it() -> None:
    """IS-06/IS-11 (audit A-06, A-10): a bare "Nothing found" named nothing."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the bare empty state is gone from every renderer",
          "'<p class=\"empty\">Nothing found</p>'" not in src, "")
    check("a nothing-matches state names the narrowing and offers one way to clear",
          "function nothingFound" in src and "data-clear" in src
          and "Empty here" in src, "nothing-yet and nothing-matches are different screens")
    check("every table renders the filter line above itself",
          src.count("filterLine(") >= 7, str(src.count("filterLine(")))
    check("a chip pressed by the address says so",
          'c.dataset.fromUrl = "1"' in src and "(from a link)" in src, "")
    check("the MCP page has its own chips, an agent select and no throw",
          'id="seg-mcp"' in src and 'mcp:      [T("all agents")' in src
          and "if (sg) sg.hidden" in src, "")


def test_the_findings_page_is_a_working_surface() -> None:
    """Audit A-04: no address, no filter, no link to the subject."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("every finding row carries an id and a permalink", 'id="${E(fid)}"' in src
          and 'class="fperma"' in src, "")
    check("a finding links to its subject's row where one exists",
          "function subjectHref" in src and 'heroku.html#a-' in src and 'domains.html#d-' in src, "")
    check("severity chips, a type select and a search exist on the page",
          'data-sev="${s}"' in src and 'class="ftype"' in src and 'class="fq"' in src, "")
    check("and the target rows have anchors to land on",
          'id="d-${E(d.name)}"' in src and 'id="a-${E(a.name)}"' in src and 'id="c-${E(' in src, "")
    # D-13: the two tables that had none — env variables and MCP
    # servers — and one spelling for every anchor, shared by the link and the row.
    check("env and MCP rows are addressable too",
          "id=\"e-' + E(anchorSlug(e.path + \":\" + e.name))" in src
          and 'id="m-${E(anchorSlug(s.agent + "/" + s.name))}"' in src, "")
    check("a finding about an env file or an MCP server links to its page",
          '"env.html#e-" + anchorSlug(rest)' in src and '"mcp.html#m-" + anchorSlug(rest)' in src, "")
    check("and a row named in the address is revealed inside a folded group",
          "function revealHash" in src and 'closest("tbody.grp.folded")' in src
          and "revealHash();" in src.split("function render() {", 1)[1].split("\nfunction ", 1)[0], "")
    # UI-01 supersedes the previous owner-group collapse choice. Runtime
    # visibility checks live in test_workspace_redesign; group disclosures may
    # remain for secondary content but may not hide the initial record list.
    # D-11: column sort spoken by `aria-sort`, one state per page in
    # sessionStorage so it survives search re-renders, with missing values last
    # either way. Cross-group comparison is executed in workspace_redesign.
    check("sortable headers exist on every table and speak aria-sort",
          "function sortTh" in src and 'aria-sort="${dir}"' in src and src.count("sortTh(") >= 12,
          f"sortTh used {src.count('sortTh(')} times")
    check("every grouped renderer sorts its rows before grouping",
          src.count("sortInPlace(") >= 8, f"sortInPlace used {src.count('sortInPlace(')} times")
    check("the sort state survives the search and lives per page",
          '"observatory.sort." + PAGE' in src and "render();" in src.split('closest("th[data-sort]")', 1)[1][:900], "")
    check("a missing value sorts last in both directions",
          "if (x == null) return 1;" in src and "if (y == null) return -1;" in src, "")
    # D-10: the project panel says what the project authenticates
    # with — the small inverse of the keys document's `used_by`, built in Python
    # so it rides every page, each row linking to `creds.html#c-<slug>`.
    check("the panel has a Keys section fed by the small per-project key map",
          '<h3>${T("Keys")}</h3>' in src and '"keys": KEYS,' in src and "(D.keys || {})[r.id]" in src
          and 'href="${E(k.href || ("creds.html#c-" + k.slug))}"' in src, "")
    # The keys a project holds in its own `.env` files are in the map too,
    # joined by FOLDER, each linking to its ENV row; the map rides on the
    # projects page alone so the index stays light.
    check("env-held keys join the map by folder and link to their ENV row",
          '"kind": "env-file"' in src and '"href": "env.html#e-"' in src and "_folder_pid.get(" in src, "")
    check("and the map is sliced off every page but projects",
          'out["keys"] = None' in (ROOT / "dashboard/shell.py").read_text(encoding="utf-8"), "")
    check("and a key row says leaked / unsigned in words, never colour alone",
          'chip(T("leak not closed"), "danger")' in src and 'chip(T("not signed"), "warn")' in src, "")
    # D-15: EVERY LIVE VERB THE PAGE OFFERS IS A ROUTE THE SERVER HAS.
    # `_door()` was called by three routes and defined nowhere for a week; the
    # page's third element and the server's ACTIONS table are checked against
    # each other here so a button cannot promise what the server never learned.
    ks = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    actions_src = ks.split("ACTIONS = {", 1)[1].split("}", 1)[0]
    routes = set(re.findall(r'"([a-z-]+)":', actions_src))
    # The verbs' third element: a literal `, "limit"]`, or a ternary
    # `? "enable" : "disable"]` — both spellings are read, `null` is a copy verb.
    verbs_src = src.split("function credVerbs(", 1)[1].split("\nfunction ", 1)[0]
    offered = set(re.findall(r',\s*"([a-z][a-z-]+)"\]', verbs_src))
    offered |= {t for pair in re.findall(r'\? "([a-z-]+)" : "([a-z-]+)"\]', verbs_src) for t in pair}
    check("every LIVE verb on the keys page is a keyserver route",
          offered and offered <= routes, f"offered {sorted(offered)} vs routes {sorted(routes)}")
    check("disable, enable and rotate-key are LIVE for a provider key, and `rotate` stays refused",
          {"disable", "enable", "rotate-key"} <= routes and '"rotate":' in ks.split("REFUSED = {", 1)[1].split("}", 1)[0], "")
    check("the page confirms before a rotation or a disable, as it does before a revoke",
          '(act === "revoke" || act === "rotate-key" || act === "disable") && !confirm(ask)' in src, "")
    # D-14: the movements journal has a surface, fed by the same
    # reader the board's rule uses; an unrecorded Heroku change hands over the
    # exact `vault.py moved …` that settles it.
    check("the keys page shows the movements journal and the unrecorded changes",
          "function movementsSection" in src and "D.creds.movements" in src and "D.creds.unrecorded" in src
          and 'toolCommand("vault.py", ["moved", u.project' in src, "")
    check("and the build reads the journal through tools/movements.py, as build_findings does",
          "import movements as _movements" in src
          and "import movements as _movements" in (ROOT / "tools/build_findings.py").read_text(encoding="utf-8"), "")
    # D-16: the queue's shape in one line above the rows.
    check("the health page carries the queue digest above the rows",
          'id="queue-digest"' in src and "digestLine + q.map" in src and "def _digest(conn)" in src, "")
    # P3. D-20: the single page's tab strip is stripped from split
    # pages at build and the counters are written only where they exist.
    shell_src = (ROOT / "dashboard/shell.py").read_text(encoding="utf-8")
    check("the shell strips the tab strip from split pages and the script asks before writing a counter",
          'r\'<nav class="tabs"[^>]*>.*?</nav>' in shell_src and "const setN = " in src
          and 'getElementById("n-projects").textContent' not in src, "")
    # D-21: «правила» is a VIEW switch beside the table, outside the filter group.
    seg = src.split('id="seg-projects"', 1)[1].split("</div>", 1)[0]
    check("the rules chip left the filter group and stands in a view switch",
          'data-f="rules"' not in seg and 'id="view-projects"' in src
          and 'data-f="rules"' in src.split('id="view-projects"', 1)[1][:400], "")
    # D-22: an empty estate is a sentence and the command, not eight zeros.
    check("an empty registry renders one sentence with a workspace-local observation command",
          'class="tile empty-estate"' in src and 'data-copy="${E(cliCommand("local"))}"' in src
          and "if (MORE_TILES) MORE_TILES.onclick" in src, "")
    # D-19: every grouped table declares its columns; unused tokens are gone.
    check("mcp, traffic, domains and heroku tables carry a colgroup",
          src.count("<colgroup>") >= 7, f"{src.count('<colgroup>')} colgroups")
    check("the three unused tokens left the template copy",
          "--t-card:" not in src and "--dur-state:" not in src and "--shadow-1:" not in src, "")


def test_the_panel_is_a_dialog_that_keeps_the_keyboard() -> None:
    """Audit A-08/A-09."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the panel is a labelled dialog", 'role="dialog" aria-labelledby="panel-title"' in src
          and 'id="panel-title"' in src, "")
    check("focus moves to the close button on open and back on close",
          "closeBtn.focus()" in src and "PANEL_OPENER.focus()" in src, "")
    check("a project link on a lite page goes to the projects page instead of an empty panel",
          'location.href = "projects.html" + a.getAttribute("href")' in src, "A-09")
    check("the results area announces its changes", '<main id="out" aria-live="polite">' in src, "")


def get(path: str) -> tuple[int, bytes, str]:
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    ctype = r.getheader("Content-Type") or ""
    c.close()
    return r.status, body, ctype


def get_raw(path: str) -> tuple[int, bytes, str, str]:
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    out = (r.status, body, r.getheader("Content-Type") or "", r.getheader("Location") or "")
    c.close()
    return out


def test_the_server_serves_the_pages_and_refuses_the_rest() -> None:
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-pages-"))
    (work / "scratch").mkdir()
    pages = work / "pages"
    pages.mkdir()
    for name in ("index", "creds"):
        src = paths.DASHBOARD_DIR / f"{name}.html"
        if src.is_file():
            shutil.copy(src, pages / f"{name}.html")
    for asset in (shell.ASSET_CSS, shell.ASSET_JS):
        if (paths.DASHBOARD_DIR / asset).is_file():
            shutil.copy(paths.DASHBOARD_DIR / asset, pages / asset)
    (work / "single.html").write_text("<html>the single page</html>", encoding="utf-8")
    env = {**os.environ, "OBSERVATORY_SCRATCH": str(work / "scratch"),
           "OBSERVATORY_DASHBOARD": str(work / "single.html"),
           "OBSERVATORY_DASHBOARD_DIR": str(pages)}
    p = subprocess.Popen([PY, "tools/serverd.py", "--run", "--port", str(PORT)],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                if get("/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        code, body, ctype = get("/dashboard/index.html")
        check("a page in the whitelist is served as HTML", code == 200
              and b"const PAGE" in body and "text/html" in ctype, f"{code} {ctype}")
        code, body, ctype = get(f"/dashboard/{shell.ASSET_JS}")
        check("so is the script every page loads, as JavaScript",
              code == 200 and "javascript" in ctype and len(body) > 10000, f"{code} {ctype}")
        code, _b, ctype = get(f"/dashboard/{shell.ASSET_CSS}")
        check("and the style, as CSS", code == 200 and "text/css" in ctype, f"{code} {ctype}")
        for root in ("/", "/dashboard", "/dashboard/"):
            code, _b, _c, where = get_raw(root)
            check(f"{root} redirects to the index when the split is built",
                  code == 302 and where == "/dashboard/index.html", f"{code} {where}")
        index = get("/dashboard/index.html")[1].decode("utf-8")
        for rel in re.findall(r'(?:href|src)="([^"#:]+\.(?:css|js))"', index):
            code, _b, _c = get(urllib.parse.urljoin("/dashboard/index.html", rel))
            check(f"the index's {rel} resolves from where the index is served",
                  code == 200, f"{code}")
        for bad, why in (("/dashboard/../../etc/passwd", "a traversal"),
                         ("/dashboard/../projects-dashboard.html", "a traversal that ends in .html"),
                         ("/dashboard/nope.html", "a name the shell does not know"),
                         ("/dashboard/app.json", "a sibling file that is not an asset")):
            code, body, _c = get(bad)
            named = b"index" in body                    # the 404 names the real pages
            check(f"{why} is 404, and never a file read", code == 404 and named,
                  f"{code} {body[:80]!r}")
    finally:
        p.terminate()
        p.wait(timeout=10)


def test_the_server_heartbeat_age_needs_no_deprecated_clock() -> None:
    """`datetime.utcnow()` is deprecated since Python 3.12 and warns on every build;
    the age of the server's heartbeat is the same number from an aware clock."""
    import datetime as dt
    import warnings
    import build_dashboard as bd
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        at = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        check("ninety seconds after the stamp reads 90",
              bd.heartbeat_age_s("2026-01-01T00:00:00Z", now=at + dt.timedelta(seconds=90)) == 90)
        fresh = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        age = bd.heartbeat_age_s(fresh)
        check("a stamp written just now is a small whole number of seconds, from the real clock",
              isinstance(age, int) and 0 <= age < 60, repr(age))
        try:
            bd.heartbeat_age_s("yesterday")
            refused = False
        except ValueError:
            refused = True
        check("a stamp in another shape is refused as before", refused)
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the builder calls no naive UTC clock", "utcnow(" not in src)


if __name__ == "__main__":
    print("the split dashboard — navigation first, one page's data per page\n")
    for fn in (test_the_pages_exist_and_the_index_is_small,
               test_the_navigation_comes_before_the_alerts,
               test_the_shared_files_are_relative_siblings,
               test_the_split_keeps_one_template,
               test_a_page_carries_only_what_it_renders,
               test_a_redirected_page_takes_its_pages_with_it,
               test_every_bridge_leads_somewhere_that_exists,
               test_the_keys_page_offers_the_doors_own_verbs,
               test_the_estate_pages_hand_over_commands_and_fold_what_is_long,
               test_the_shell_names_the_page_and_offers_the_language,
               test_the_live_verbs_have_a_listener_and_the_pages_can_be_live,
               test_every_table_says_what_narrowed_it,
               test_the_findings_page_is_a_working_surface,
               test_the_panel_is_a_dialog_that_keeps_the_keyboard,
               test_the_server_serves_the_pages_and_refuses_the_rest,
               test_the_server_heartbeat_age_needs_no_deprecated_clock):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mnine addressable pages, the nav above the alarms, and nothing loaded twice\033[0m")
