#!/usr/bin/env python3
"""What a step ACTUALLY opens, measured by running it — not by reading it.

`tests/test_pipeline.py` orders the two orchestrators against a declared graph:
each step names the files it writes and the files it reads, and two derivations
keep the declaration honest — what the script's source names in four spellings,
and what its command line hands it. Both read TEXT, and both said so: neither
sees a path built into a variable.

Four steps fall entirely into that blind spot. `dashboard`, `validate`,
`scan-events` and `scan-fs` contain no file access either derivation can read,
so their entries rest on the author's word — named in the handoff four
iterations running and deferred every time, which is its own kind of evidence.

This settles them by a third technique: run the step with every path-opening
door wrapped, and write down what came through. Sources are ranked by how much
they can lie:

    read/written by name          measured here
    read through a subprocess     NOT measured — `git` and `node` are opened by
                                  this process but read their own files, and a
                                  wrapper in this interpreter cannot see them
    read by a C extension         NOT measured, for the same reason; sqlite is
                                  wrapped at `connect`, which is the file it opens

So a clean report means "nothing undeclared passed through Python", never
"nothing undeclared happened". That distinction is the whole reason this file
exists rather than a sentence in a docstring saying the four are fine.

    tools/trace_opens.py dashboard              # one step, redirected
    tools/trace_opens.py dashboard validate     # several
    tools/trace_opens.py --all                  # every step the graph declares

**Not a gate step.** It EXECUTES collectors, which take minutes and touch the
estate's checkouts; the invariant it measures moves when a step's code changes,
not when the data does. Run it when a step gains a file access, and when the
graph is edited.
"""
from __future__ import annotations
import argparse
import builtins
import importlib.util
import json
import os
import pathlib
import runpy
import shutil
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import paths                                                                    
import tmp as tmpdir                                                            

#: A step this tool will not run, and why. Tracing means EXECUTING, and three
#: of these leave the machine while two spend money — a measurement that sends a
#: notification or writes a commit is not a measurement. Named rather than
#: skipped silently: a reader has to be able to see which part of the graph this
#: tool cannot settle, and go and settle it another way.
REFUSED: dict[str, str] = {
    "notify": "sends a notification to a person, and no redirect makes an "
              "already-delivered message undelivered",
    "commit-registry": "makes a git commit in this repository",
    "commit-projection": "makes a git commit in the operator's wiki",
}

#: A step that WOULD act on the world, and the environment that makes it
#: harmless — with the reason, because "it is safe now" is a claim.
#:
#: Three steps sat in `REFUSED` until 2026-09-09 for want of this table, and
#: each already had its safe path built and tested: the vault is redirectable
#: like every other artefact, and both spenders degrade when no credential
#: resolves — a degradation this repository drives in four suites. Refusing to
#: measure something the code already knows how to do safely is a gap invented
#: rather than found.
SAFE_MODE: dict[str, tuple[dict[str, str], str]] = {
    "project": ({"OBSERVATORY_VAULT": "<work>/vault"},
                "the projection is written into a temporary vault, not the "
                "operator's; `paths.VAULT` exists for exactly this"),
    "agent": ({"OPENROUTER_API_KEY": "", "OBSERVATORY_KEY_FILE": "/nonexistent/key"},
              "with no credential the agent takes its documented degradation and "
              "spends nothing — the path a test drives in `test_degrades_without_credential`"),
    "index": ({"OPENROUTER_API_KEY": "", "OBSERVATORY_KEY_FILE": "/nonexistent/key"},
              "the indexer degrades to the lexical half when it cannot embed, so "
              "the vector call — the only thing that costs — never happens"),
}

#: Everything this process can be asked to open by name. Enumerated rather than
#: guessed: a door missing from this list is a read the report will not carry,
#: and the report would look identical.
TOUCHED: list[tuple[str, str]] = []


#: A redirected artefact, mapped back to the name the GRAPH speaks. Without
#: this the sandbox swallows the very writes under test: `dashboard` writes the
#: page into a temporary directory, the report sees a path outside the
#: repository and drops it, and the comparison then accuses the step of
#: declaring a write it did not make. The redirect is the tracer's own doing, so
#: undoing it is the tracer's own job.
def _canonical() -> list[tuple[pathlib.Path, str]]:
    out = []
    for var, name in (("OBSERVATORY_DASHBOARD", "docs/projects-dashboard.html"),
                      ("OBSERVATORY_SCRATCH", "store/raw"),
                      ("OBSERVATORY_DB", "store/observatory.db"),
                      ("OBSERVATORY_REGISTRY", "registry"),
                      ("OBSERVATORY_ACKS", "collectors/finding_acks.json"),
                      ("OBSERVATORY_STATE", "store")):
        v = os.environ.get(var)
        if v:
            out.append((pathlib.Path(v).resolve(), name))
    # Longest first: STATE is `store` and SCRATCH is `store/raw` in the real
    # layout, and in the sandbox they are siblings — but a caller may point them
    # at nested paths, and the more specific mapping has to win.
    return sorted(out, key=lambda kv: len(str(kv[0])), reverse=True)


REDIRECTS: list[tuple[pathlib.Path, str]] = []


def _record(mode: str, path) -> None:
    try:
        p = pathlib.Path(path).resolve()
    except (OSError, TypeError, ValueError):
        return
    kind = "w" if any(c in str(mode) for c in "wax+") else "r"
    for sandbox, name in REDIRECTS:
        if p == sandbox:
            TOUCHED.append((kind, name)); return
        if sandbox in p.parents:
            TOUCHED.append((kind, f"{name}/{p.relative_to(sandbox).as_posix()}")); return
    try:
        rel = p.relative_to(ROOT).as_posix()
    except ValueError:
        return                                                                    
    TOUCHED.append((kind, rel))


def install() -> None:
    real_open, real_p_open = builtins.open, pathlib.Path.open
    real_rt, real_wt = pathlib.Path.read_text, pathlib.Path.write_text
    real_rb, real_wb = pathlib.Path.read_bytes, pathlib.Path.write_bytes
    real_connect, real_replace = sqlite3.connect, os.replace
    real_copy, real_copy2 = shutil.copy, shutil.copy2

    def op(file, mode="r", *a, **k):
        _record(mode, file)
        return real_open(file, mode, *a, **k)

    def p_open(self, mode="r", *a, **k):
        _record(mode, self)
        return real_p_open(self, mode, *a, **k)

    builtins.open = op
    pathlib.Path.open = p_open
    pathlib.Path.read_text = lambda self, *a, **k: (_record("r", self), real_rt(self, *a, **k))[1]
    pathlib.Path.write_text = lambda self, *a, **k: (_record("w", self), real_wt(self, *a, **k))[1]
    pathlib.Path.read_bytes = lambda self, *a, **k: (_record("r", self), real_rb(self, *a, **k))[1]
    pathlib.Path.write_bytes = lambda self, *a, **k: (_record("w", self), real_wb(self, *a, **k))[1]
    # A STORE IS A FILE. `sqlite3.connect` opens it in the C layer where no
    # wrapper of `open` can see it, and three of the four steps under test are
    # store writers — a report that missed them would be confidently empty.
    def connect(target, *a, **k):
        # `file:…?mode=ro` is a URI, not a path: recording it verbatim produced
        # a "write" of a filename containing a query string. The mode is IN the
        # string, and a read-only connection is a read.
        s = str(target)
        if s.startswith("file:"):
            path, _, query = s[len("file:"):].partition("?")
            _record("r" if "mode=ro" in query else "w", path)
        else:
            _record("w", target)
        return real_connect(target, *a, **k)

    sqlite3.connect = connect
    # The atomic writer's LAST act. `atomic.write_json` writes a sibling and
    # renames it, so without this the report names the temporary file and not
    # the artefact the graph is declared in terms of.
    os.replace = lambda src, dst, *a, **k: (_record("w", dst), real_replace(src, dst, *a, **k))[1]
    shutil.copy = lambda s, d, *a, **k: (_record("r", s), _record("w", d), real_copy(s, d, *a, **k))[2]
    shutil.copy2 = lambda s, d, *a, **k: (_record("r", s), _record("w", d), real_copy2(s, d, *a, **k))[2]


def steps() -> dict[str, list[str]]:
    spec = importlib.util.spec_from_file_location("obs_trace", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obs)
    return obs.STEPS


def declared() -> dict[str, dict[str, list[str]]]:
    spec = importlib.util.spec_from_file_location("tp_trace", ROOT / "tests/test_pipeline.py")
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)
    return tp.ARTEFACTS


def run(step: str, cmd: list[str], work: pathlib.Path) -> dict:
    """Execute one step in a CHILD, with the artefacts redirected.

    In a child, and the reason is a destroyed page. Running the step in this
    interpreter would let the wrappers above apply — but `paths.py` resolves
    every artefact at IMPORT time, and this process has usually imported it
    already (through `tests/test_pipeline.py`, which imports `observatory`).
    So `OBSERVATORY_DASHBOARD` would be set after the constant it controls had
    been frozen, and tracing `dashboard` would REBUILD THE LIVE PAGE from an
    empty temporary store. A redirect that arrives after the import is not a
    redirect.

    A child imports `paths` fresh, so the redirect binds. The wrappers travel to
    it through `--child`, which installs them and then runs the script.

    `OBSERVATORY_REGISTRY` is deliberately NOT redirected: the registry is the
    input the graph is about, and it is read, never written, by these steps.
    """
    script = next((c for c in cmd if isinstance(c, str) and c.endswith(".py")), None)
    if script is None:
        return {"step": step, "skipped": "no python script in the command"}
    argv = [script] + [c for c in cmd[cmd.index(script) + 1:]]
    env_before = dict(os.environ)
    (work / "raw").mkdir(parents=True, exist_ok=True)
    (work / "state").mkdir(parents=True, exist_ok=True)
    # FROM THE WORKSPACE, not from beside the code: the engine keeps its store
    # under the selected state, so the source tree holds no data to copy and a
    # sandbox seeded from it traces every step against nothing.
    for f in paths.STATE.glob("*.json"):
        shutil.copy2(f, work / "state" / f.name)
    for f in paths.SCRATCH.glob("*"):
        if f.is_file():
            shutil.copy2(f, work / "raw" / f.name)
    # THE STORE TOO, and it is not an optimisation. A step's file access is
    # DATA-dependent: against an empty database `dashboard` skipped every panel
    # that needs one and read seven files instead of thirteen, so the report
    # came back clean about a step that reads the wallet and four plugin
    # receipts in real life. A sandbox that changes which branch runs measures
    # the sandbox (2026-09-09).
    if paths.DB.is_file():
        shutil.copy2(paths.DB, work / "t.db")
    shutil.copytree(paths.REGISTRY, work / "registry", dirs_exist_ok=True)
    if paths.FINDING_ACKS.is_file():
        shutil.copy2(paths.FINDING_ACKS, work / "acks.json")
    env = dict(os.environ)
    env.update({"OBSERVATORY_DB": str(work / "t.db"),
                "OBSERVATORY_SCRATCH": str(work / "raw"),
                "OBSERVATORY_DASHBOARD": str(work / "page.html"),
                "OBSERVATORY_STATE": str(work / "state"),
                # THE REGISTRY TOO. `emit`, `findings` and `export-ledger` all
                # write into it, and the first version of this tool did not
                # redirect it — which is how tracing `dashboard` rewrote the
                # operator's page one door over. A tracer that damages what it
                # measures is not one anybody will run twice.
                "OBSERVATORY_REGISTRY": str(work / "registry"),
                "OBSERVATORY_ACKS": str(work / "acks.json"),
                "OBSERVATORY_TRACE_OUT": str(work / "touched.json")})
    for var, value in (SAFE_MODE.get(step, ({}, ""))[0]).items():
        env[var] = value.replace("<work>", str(work))
    if step in SAFE_MODE:
        (work / "vault" / "inventory").mkdir(parents=True, exist_ok=True)
    os.environ.clear()
    os.environ.update(env_before)
    p = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()),
                        "--child", script] + argv[1:],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=3600)
    out = work / "touched.json"
    if not out.is_file():
        return {"step": step, "outcome": f"the child left no observation: "
                                         f"{(p.stdout + p.stderr)[-200:]}",
                "reads": [], "writes": []}
    seen = json.loads(out.read_text(encoding="utf-8"))
    return {"step": step, "outcome": f"exit {p.returncode}",
            "reads": sorted({x[1] for x in seen if x[0] == "r"}),
            "writes": sorted({x[1] for x in seen if x[0] == "w"})}


def written_by_anyone(decl: dict) -> list[str]:
    """Every path some step declares as a write — the only paths that can invert.

    The graph orders READERS after WRITERS, so an undeclared read of a file no
    step writes cannot produce an inversion: nothing upstream can change it
    mid-pipeline. `findings` reads all 119 test suites to count what the gate
    covers, and reporting those as defects buried the four reads that mattered
    under 133 that could not (2026-09-09).
    """
    return sorted({w for io in decl.values() for w in io["writes"]})


def compare(step: str, seen: dict, decl: dict) -> list[str]:
    """Every disagreement, in the graph's own vocabulary.

    A declaration covers a path when it names it or names a DIRECTORY above it —
    `store/raw/gh` stands for the files inside it, and the graph is deliberately
    coarse there.
    """
    say = decl.get(step) or {"reads": [], "writes": []}
    out: list[str] = []
    # THE STORE IS NOT IN THE GRAPH, on purpose: "every writer touches it and
    # the graph would say nothing" (`tests/test_pipeline.py`). Reporting it on
    # every run would train a reader to skim this output, which is the failure
    # a report exists to avoid.
    exempt = ("store/observatory.db",)

    def covered(path: str, names: list[str]) -> bool:
        return any(path == n or path.startswith(n.rstrip("/") + "/") for n in names)

    for p in seen.get("writes", []):
        if p not in exempt and not covered(p, say["writes"]):
            out.append(f"{step} WRITES {p}, undeclared")
    produced = written_by_anyone(decl)
    other = 0
    for p in seen.get("reads", []):
        if p in exempt:
            continue
        # A file the step writes is one it may also read back; the graph is about
        # dependencies between STEPS, so a self-read is not an edge.
        if covered(p, say["reads"]) or covered(p, say["writes"]):
            continue
        if covered(p, produced):
            out.append(f"{step} READS {p}, undeclared — and some step WRITES it")
        else:
            other += 1
    if other:
        # COUNTED, never dropped. These cannot invert an ordering, but a reader
        # who sees only the first list would think the step opens four files.
        out.append(f"{step} also read {other} file(s) no step writes "
                   f"(source, fixtures, checked-in inputs) — not an ordering risk")
    for p in say["writes"]:
        if not any(q == p or q.startswith(p.rstrip("/") + "/") for q in seen.get("writes", [])):
            out.append(f"{step} declares a write of {p} and did not write it")
    return out


def child(argv: list[str]) -> int:
    """Install the wrappers, run one script, write down what came through.

    This is the half that must be a separate process: the wrappers only see what
    happens after `install()`, and the redirects only bind for a `paths` that has
    not been imported yet.
    """
    REDIRECTS[:] = _canonical()
    install()
    script, rest = argv[0], argv[1:]
    sys.argv = [script] + rest
    code = 0
    try:
        runpy.run_path(str(ROOT / script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:                                                  
        print(f"trace_opens: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    dest = os.environ.get("OBSERVATORY_TRACE_OUT")
    if dest:
        pathlib.Path(dest).write_text(json.dumps(TOUCHED), encoding="utf-8")
    return code


def main(argv: list[str]) -> int:
    if len(argv) > 2 and argv[1] == "--child":
        return child(argv[2:])
    ap = argparse.ArgumentParser(description=(__doc__ or 'Trace file access in an isolated diagnostic run.').splitlines()[0])
    ap.add_argument("steps", nargs="*")
    ap.add_argument("--all", action="store_true", help="every step the graph declares")
    ap.add_argument("--json", help="write the raw observation here")
    args = ap.parse_args(argv[1:])
    all_steps, decl = steps(), declared()
    want = list(decl) if args.all else args.steps
    if not want:
        ap.error("name a step, or pass --all")

    reports, drift = [], []
    for step in want:
        if step not in all_steps:
            print(f"  {step}: no such step"); continue
        if step in SAFE_MODE:
            print(f"  {step:20} traced in SAFE MODE — {SAFE_MODE[step][1]}")
        if step in REFUSED:
            print(f"  {step:20} REFUSED — {REFUSED[step]}")
            reports.append({"step": step, "refused": REFUSED[step]})
            continue
        work = pathlib.Path(tmpdir.mkdtemp(prefix=f"observatory-trace-{step}-"))
        r = run(step, all_steps[step], work)
        reports.append(r)
        if "skipped" in r:
            print(f"  {step:14} SKIP {r['skipped']}"); continue
        d = compare(step, r, decl)
        drift += d
        print(f"  {step:14} {r['outcome']:12} {len(r['reads'])} read(s), "
              f"{len(r['writes'])} write(s), {len(d)} disagreement(s)")
        for line in d:
            print(f"      - {line}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(reports, indent=2), encoding="utf-8")
    skipped = [r["step"] for r in reports if "refused" in r]
    if skipped:
        print(f"\n{len(skipped)} step(s) NOT traced, because running them would act "
              f"on the world: {', '.join(skipped)}. Their declarations rest on the "
              f"source derivations alone.")
    print(f"\n{len(drift)} disagreement(s) between the graph and what ran. "
          "A clean report means nothing undeclared passed through PYTHON — a "
          "subprocess or a C extension opening its own files is invisible here.")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
