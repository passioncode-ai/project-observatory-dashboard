"""Walk the estate root and describe every folder in it.

The BASE collector: `merge.py` starts from this file, and `scan_remotes.py` and
`scan_bitbucket.py` derive what to probe from it. Everything downstream inherits
whatever this one gets wrong.

**What it could not read, it now says.** `sh()` returned `""` for three
different facts (a git command that exited non-zero, one that timed out at 25
seconds, and one that raised), so a repository whose remote could not be read
was indistinguishable from one that has no remote. Downstream that becomes a
`local:` project: `merge.nwo("")` is None, the repository is never matched to its
listing, and the estate gains a folder-anchored project where a real repository
belongs. Some folders legitimately have no remote, and there was no way to tell
them from a failure. It is the same class of fault the domain probe and the
transfer check had already fixed.

**And the exclusions were applied one layer too late.** `merge.py` reads
`folder_exclusions.json` to decide what is not a project; this scan read nothing
and walked everything, so a container of agent worktrees, excluded by name, had
tens of thousands of files counted on every tick to produce a number the merge
then discarded, and that one folder dominated the scan's run time. The
exclusions are now read here, from the same file, and a skipped folder is
REPORTED as skipped with its reason rather than silently absent.

    scan_filesystem.py store/raw/local.json
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import atomic
import paths
import slow_command
import json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
DATA = paths.DATA

#: A folder walk is bounded. Nothing here needs an exact count above this — the
#: figure is a size signal on the dashboard — and an unbounded `rglob` over a
#: mounted volume or a node_modules forest is a tick that never finishes. The
#: cap is REPORTED per row, so a capped number is never read as an exact one.
FILE_COUNT_CAP = 20000


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: THE LOCALE GIT SPEAKS, pinned because two branches below read its English.
#: `git` translates its messages through gettext, and measured 2026-09-08 on this
#: machine:
#:
#:     LC_ALL=C              error: No such remote 'origin'
#:     LC_ALL=ru_RU.UTF-8    error: Нет такого внешнего репозитория «origin»
#:
#: Those branches turn a failure into an ANSWER — a repository with no remote and
#: one with no commits are facts about the tree, not faults of the run — and they
#: recognise it by the English. On a machine whose locale is not English they
#: would stop matching, and every such repository would be recorded as a
#: degradation instead: a permanent warning in front of the operator about a tree
#: that is exactly as intended, which is the noise the branches exist to prevent.
#: Silently, because nothing says a recogniser stopped recognising.
#:
#: The operator of this machine writes Russian, so it was one environment
#: variable away. `LANGUAGE` is emptied too: gettext lets it override `LC_ALL`,
#: and while this git honours `LC_ALL` first, relying on that is another
#: unmeasured claim.
GIT_ENV = {"LC_ALL": "C", "LANGUAGE": ""}

#: A git call's first limit, and the longer one it is given when asked again.
#: The first is the old fixed limit, so a quiet machine behaves as before.
SH_TIMEOUTS = (25, 60)
SH_BACKOFF = 3.0


def sh(args, cwd=None):
    """(output, reason_it_could_not_be_read). One of the two is always empty.

    THREE OUTCOMES, where there was one. An empty string used to mean "the
    command said nothing", "the command failed" and "the command could not run"
    at once — and the first is a fact about the repository while the other two
    are facts about this run.

    Git runs under `GIT_ENV` so the messages the caller matches are the ones it
    was written against, whatever the machine's locale.

    A TIMEOUT IS ASKED AGAIN before it becomes a reason. One `git status` that
    missed 25 s on a machine at load average 200, and answered in 0.3 s a minute
    later, held the service degraded for a whole tick; `slow_command` retries it
    once with a longer limit, and a command that misses both is reported with
    both durations ("did not finish in 25s, nor in 60s …") and the load.
    """
    try:
        r, slow = slow_command.run(args, timeouts=SH_TIMEOUTS, backoff=SH_BACKOFF, cwd=cwd,
                                   capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    except OSError as exc:
        return "", f"`{args[0]}` could not run: {type(exc).__name__}: {exc}"
    if r is None:
        return "", slow
    if r.returncode != 0:
        return "", (f"`{' '.join(args[:3])}` exited {r.returncode}: "
                    f"{(r.stderr or '').strip()[:100]}")
    return r.stdout.strip(), ""


MARKERS = [
    ("package.json","node"), ("pyproject.toml","python"), ("requirements.txt","python"),
    ("go.mod","go"), ("Cargo.toml","rust"), ("composer.json","php"), ("Gemfile","ruby"),
    ("build.gradle","android"), ("build.gradle.kts","android"), ("Package.swift","swift"),
    ("pubspec.yaml","flutter"), ("Dockerfile","docker"), ("docker-compose.yml","docker"),
    ("SKILL.md","agent-skill"), (".claude-plugin","claude-plugin"),
    ("next.config.js","nextjs"), ("next.config.ts","nextjs"), ("next.config.mjs","nextjs"),
    ("astro.config.mjs","astro"), ("vercel.json","vercel"), ("wrangler.toml","cloudflare-worker"),
    ("wrangler.jsonc","cloudflare-worker"), ("netlify.toml","netlify"),
]
DOMAIN_RX = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|org|net|io|dev|app|ai|me|pro|bot|chat|xyz|club|cards|care|social|spot|trading|business|codes|estate|fm|forex|vip|online|ru\.com))\b")


def exclusions() -> tuple[tuple[str, ...], dict[str, str]]:
    """(prefixes, {name: why}) — the SAME file `merge.py` reads.

    One source of truth, so a folder cannot be excluded from the registry while
    still costing a full walk here. A missing file is not fatal: scanning
    everything is the older, slower, correct behaviour, and refusing to scan
    because a curation file is absent would be worse than being slow.
    """
    f = paths.config_file('folder_exclusions.json')
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (), {}
    return (tuple(x["prefix"] for x in doc.get("prefixes") or []),
            {x["name"]: x.get("why", "") for x in doc.get("names") or []})


def first_para(p: Path):
    for name in ("README.md","readme.md","README.MD"):
        f = p/name
        if not f.exists(): continue
        try: txt = f.read_text(encoding="utf-8", errors="replace")
        except Exception: return ""
        out=[]
        for line in txt.splitlines():
            s=line.strip()
            if not s or s.startswith(("#","!","[!","<!--","---","```","|","> [!")):
                if out: break
                continue
            out.append(s)
            if len(" ".join(out))>240: break
        return re.sub(r"\s+"," "," ".join(out))[:300]
    return ""
def detect(p: Path):
    kinds=[]
    for marker, kind in MARKERS:
        if (p/marker).exists() and kind not in kinds: kinds.append(kind)
    if list(p.glob("*.xcodeproj")) or list(p.glob("*.xcworkspace")): kinds.append("ios")
    if (p/"skills").is_dir() and (p/".claude-plugin").exists(): kinds.append("claude-plugin")
    return kinds
def _nwo_of(url: str) -> str:
    """`owner/name` from a git URL, or "" — the merge matches by this."""
    m = re.search(r"(?:github\.com|bitbucket\.org)[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def homepage(p: Path):
    hits=[]
    pj=p/"package.json"
    if pj.exists():
        try:
            j=json.loads(pj.read_text(encoding="utf-8", errors="replace"))
            h=j.get("homepage")
            if isinstance(h,str) and h.startswith("http"): hits.append(("package.json:homepage",h))
        except Exception: pass
    for cname in list(p.glob("CNAME"))+list(p.glob("*/CNAME"))+list(p.glob("public/CNAME")):
        try: hits.append((f"{cname.relative_to(p)}", cname.read_text(encoding='utf-8',errors='replace').strip()))
        except Exception: pass
    for wr in ("wrangler.toml","wrangler.jsonc","wrangler.json"):
        f=p/wr
        if f.exists():
            try:
                t=f.read_text(encoding="utf-8", errors="replace")
                for m in set(DOMAIN_RX.findall(t.lower()))-{"example.com"}:
                    hits.append((wr,m))
            except Exception: pass
    # URL-SHAPED CONFIG, local first. A site URL variable in a checkout's
    # own env file is the project naming its public host, and it sits on THIS
    # machine: the operator's first-priority evidence. Only values that are
    # plain http(s) URLs with no `@` are read (a DATABASE_URL carries a
    # password); everything else in the file is never looked at.
    for envf in sorted(p.glob(".env*")):
        if not envf.is_file() or envf.name.endswith((".bak", ".swp")):
            continue
        try:
            for line in envf.read_text(encoding="utf-8", errors="replace").splitlines():
                # SELF-REFERENTIAL NAMES ONLY. A vendor's API URL in a checkout
                # says the project CONSUMES that host, not that it owns it, and
                # most such hits are model APIs and payment providers. What
                # names the project's own public surface is the small family of
                # site, base, domain and origin names matched below, with their
                # framework-prefixed twins.
                m = re.match(r"\s*(?:export\s+)?((?:NEXT_PUBLIC_|VITE_|REACT_APP_|NUXT_PUBLIC_)?"
                             r"(?:APP|SITE|PUBLIC|BASE|WEB|FRONTEND|CANONICAL|PRODUCTION|NEXTAUTH)?_?"
                             r"(?:URL|DOMAIN|HOST|ORIGIN))\s*=\s*[\"']?(https?://[^\s\"'#]+)", line)
                if not m or "@" in m.group(2):
                    continue
                host = re.sub(r"^https?://", "", m.group(2)).split("/")[0].split(":")[0].lower()
                if host and not host.startswith(("localhost", "127.", "0.0.0.0", "example.")) and "." in host:
                    hits.append((f"env:{envf.name}:{m.group(1)}", "https://" + host))
        except OSError:
            pass
    # nginx `server_name` in a checkout's own conf — the server naming its hosts.
    for conf in list(p.glob("*.conf")) + list(p.glob("nginx/*.conf")) + list(p.glob("deploy/*.conf")) + list(p.glob("docker/*.conf")):
        try:
            for m in re.finditer(r"server_name\s+([^;]+);", conf.read_text(encoding="utf-8", errors="replace")):
                for host in m.group(1).split():
                    host = host.strip().lower()
                    if "." in host and not host.startswith(("_", "*", "localhost")) and "$" not in host:
                        hits.append((f"nginx:{conf.relative_to(p)}", "https://" + host))
        except OSError:
            pass
    return hits


def count_files(p: Path) -> tuple[int, bool]:
    """(files, capped). Stops at FILE_COUNT_CAP and says that it did."""
    n = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                n += 1
                if n >= FILE_COUNT_CAP:
                    return n, True
    except OSError:
        # A permission error or a vanished directory mid-walk: the count so far
        # is what was measured, and `capped` is the wrong word for it.
        return n, False
    return n, False


rows=[]
degraded=[]
skipped=[]
EXCLUDED_PREFIXES, EXCLUDED_NAMES = exclusions()

# A PREFLIGHT, and the reason for it. With `git` off PATH every git folder
# answers nothing, so `merge.nwo("")` is None for all of them and each becomes a
# folder-anchored project. Running the merge against exactly that file showed
# the damage: many more projects, fewer repositories, and roughly ten times the
# local-folder anchors.
#
# The emitter would then have written all of it into the canonical registry and
# `commit_registry` would have committed it, because nothing anywhere compares an
# emit against the one before it. So a missing `git` is ONE fact about this
# machine, not a claim about every folder in the estate: the scan writes nothing
# and the last good `local.json` survives. The domain probe follows the same rule
# for a missing `dig`, which would otherwise declare every domain dead.
#
# Only when there is something to lose: an estate with no git folder at all is
# legitimately scannable without git.
# The source itself first: a new user who runs `full local` before naming a
# projects folder met a FileNotFoundError traceback from `os.listdir` below. The
# placeholder path (`<home>/unconfigured/projects`) is what an unset source reads as.
if not DATA.is_dir() and DATA.parent.name == "unconfigured" and not DATA.exists():
    # The placeholder an unset source reads as: say that, not its path.
    print("scan_filesystem: no projects folder is configured yet.\n"
          "  Point Observatory at the folder that holds your project checkouts:\n"
          "  project-observatory full configure sources projects PATH",
          file=sys.stderr)
    sys.exit(2)
if not DATA.is_dir():
    print(f"scan_filesystem: the projects source {DATA} "
          f"{'is not a directory' if DATA.exists() else 'does not exist'}.\n"
          f"  Point it at the folder that holds your project checkouts:\n"
          f"  project-observatory full configure sources projects PATH",
          file=sys.stderr)
    sys.exit(2)
_probe, _why = sh(["git", "--version"])
if _why:
    _git_folders = [e for e in os.listdir(DATA)
                    if not e.startswith(".") and (DATA/e/".git").exists()]
    if _git_folders:
        sys.exit(f"scan_filesystem: {_why}\n"
                 f"  {len(_git_folders)} folder(s) under {DATA} are git checkouts and "
                 f"none of them can be read.\n"
                 f"  Writing this scan would turn every one into a folder-anchored "
                 f"project — measured: 156 projects become 206 and 172 repositories "
                 f"become 149.\n"
                 f"  {sys.argv[1] if len(sys.argv) > 1 else 'the previous scan'} is "
                 f"left untouched.")
for entry in sorted(os.listdir(DATA)):
    p = DATA/entry
    if entry.startswith(".") or not p.is_dir(): continue
    # EXCLUDED HERE, not two modules later. Reported rather than dropped: a
    # folder absent from the output with no reason is indistinguishable from one
    # the scan failed to see.
    if entry.startswith(EXCLUDED_PREFIXES) or entry in EXCLUDED_NAMES:
        skipped.append({"folder": entry,
                        "why": EXCLUDED_NAMES.get(entry)
                        or f"excluded by prefix {[x for x in EXCLUDED_PREFIXES if entry.startswith(x)]}"})
        continue
    is_link = p.is_symlink()
    real = str(p.resolve())
    dotgit = p/".git"
    git = dotgit.exists()
    # A WORKTREE is not a project. `.git` is a FILE there, holding
    # `gitdir: <repo>/.git/worktrees/<name>`, and the checkout it points at is
    # already in the registry. Agents create worktrees under the estate root, and
    # without this the estate would grow by however many an agent happens to
    # have open, each one an invented project with its parent's whole history.
    worktree_of = ""
    if git and dotgit.is_file():
        try:
            head = dotgit.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            head = ""
        if head.startswith("gitdir:") and "/worktrees/" in head.replace("\\", "/"):
            target = head.split("gitdir:", 1)[1].strip().replace("\\", "/")
            worktree_of = target.split("/.git/")[0].rstrip("/").split("/")[-1]
    rec = {"folder": entry, "path": str(p), "real_path": real, "symlink": is_link, "is_git": git,
           "worktree_of": worktree_of,
           "kinds": detect(p), "readme": first_para(p), "homepage_hits": homepage(p)}
    # THE PROJECT'S OWN DECLARATION OF ITS PARTS. `.gitmodules` names, per
    # module, the path inside this tree and the repository it comes from: a
    # MEASURED composition fact, stronger than a wiki mention. When nothing read
    # it, a project made of several submodules reached the registry as whatever
    # a stale note named, while its modules stood beside it as phantom
    # standalone projects.
    gm = p / ".gitmodules"
    if git and gm.is_file():
        subs, cur = [], {}
        try:
            for line in gm.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s.startswith("[submodule"):
                    cur = {}
                    subs.append(cur)
                elif "=" in s:
                    k, _, v = s.partition("=")
                    cur[k.strip()] = v.strip()
        except OSError:
            subs = []
        rec["submodules"] = [
            {"path": m.get("path", ""), "url": m.get("url", ""),
             "nwo": _nwo_of(m.get("url", ""))}
            for m in subs if m.get("path")]
    # The code graph's age, so a graph that fell behind its code is a FACT the
    # findings can compare with the last commit, not a habit anyone remembers.
    gj = p / "graphify-out" / "graph.json"
    if gj.is_file():
        try:
            rec["graph_built_on"] = datetime.fromtimestamp(
                gj.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
        except OSError:
            pass
    if git:
        unread = {}
        #: (field, argv). `remote` is the load-bearing one: an unread remote
        #: sends the repository downstream as a folder-anchored project.
        for field, argv in (
                ("remote", ["git", "remote", "get-url", "origin"]),
                ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
                ("last_commit", ["git", "log", "-1", "--format=%cs"]),
                ("commits", ["git", "rev-list", "--count", "HEAD"])):
            out, why = sh(argv, cwd=p)
            rec[field] = out
            if not why:
                continue
            # SOME FAILURES ARE ANSWERS, and telling them apart is the whole
            # point of splitting the return value. `git remote get-url origin`
            # exits 2 with "No such remote" for a repository that has none, and
            # a repository with no commits has no HEAD to resolve. Recording
            # those as degradations would put a permanent warning in front of
            # the operator for a tree that is exactly as intended, which is how
            # a findings list becomes noise nobody reads.
            # THE ENGLISH IS GUARANTEED, not assumed: `sh` pins `LC_ALL=C`
            # (see `GIT_ENV`). Without that pin a localised git turns this into
            # a permanent degradation for every repository without a remote.
            if field == "remote" and "No such remote" in why:
                rec["no_origin"] = True
                continue
            if field in ("branch", "last_commit", "commits") and (
                    "unknown revision" in why or "does not have any commits" in why
                    or "ambiguous argument 'HEAD'" in why):
                rec["no_commits"] = True
                continue
            unread[field] = why
        out, why = sh(["git", "remote", "-v"], cwd=p)
        rec["remotes_all"] = out.splitlines()[:6]
        if why:
            unread["remotes_all"] = why
        out, why = sh(["git", "status", "--porcelain"], cwd=p)
        rec["dirty"] = len(out.splitlines())
        if why:
            unread["dirty"] = why
        if unread:
            # A `git remote get-url origin` that exits 1 because there IS no
            # origin is the common, legitimate case — and it is still recorded,
            # because "no origin configured" and "git could not answer" are
            # different facts and only the row itself can carry which.
            rec["unread"] = unread
            degraded.append({"source": f"folder:{entry}",
                             "reason": "; ".join(f"{k}: {v}" for k, v in unread.items())})
    else:
        # `os.stat`, not `subprocess.run(["date", "-r", ...])`. One subprocess
        # per non-git folder, with no timeout and no returncode check, for a
        # number the standard library gives exactly.
        try:
            rec["mtime"] = datetime.fromtimestamp(
                p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
        except OSError as exc:
            rec["mtime"] = ""
            degraded.append({"source": f"folder:{entry}",
                             "reason": f"mtime unreadable: {type(exc).__name__}"})
        n, capped = count_files(p)
        rec["file_count"] = n
        if capped:
            # NOT a sentinel. `-1` used to mean "not counted" for two folders and
            # nothing said so; a number a reader cannot tell from a real one is
            # worse than a flag beside it.
            rec["file_count_capped"] = True
    rows.append(rec)

atomic.write_json(sys.argv[1], {"scanned_at": now(), "folders": rows,
                                "skipped": skipped, "degraded": degraded})
print(f"scanned {len(rows)} folders -> {sys.argv[1]}")
if skipped:
    print(f"  skipped {len(skipped)}: {', '.join(s['folder'] for s in skipped)}")
for d in degraded:
    print(f"  degraded {d['source']}: {d['reason'][:110]}")
