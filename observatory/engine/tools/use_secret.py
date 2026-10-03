#!/usr/bin/env python3
"""Run a command with a project's secret in its environment, by NAME.

    use_secret.py names <project>                    what this project has
    use_secret.py run <project> <NAME>[,<NAME>…] -- <command…>
    use_secret.py where <project> <NAME>             which slot it would resolve

WHY THIS EXISTS. The secrets rule already says an agent works with NAMES after
an inject — and without this file there was no way to HONOUR that for a one-off
command. An agent that needs an API key for one `curl` reads the `.env`, and
from that moment the value is in a transcript that outlives the key. That is
how credentials actually leak: echoed by a database client's error, or quoted
by an HTTP library's traceback.

So: the value is resolved here, placed in the child's environment, and **removed
from everything the child prints**. The agent says the name, sees the name, and
the transcript carries the name.

    tools/use_secret.py run <project> DATABASE_URL -- psql -c 'select 1'

WHAT THIS DEFENDS AGAINST, stated honestly because the boundary matters: an
ACCIDENT. A traceback quoting the connection string, a debug line echoing the
environment, a verbose HTTP client printing its own Authorization header — the
scrubber catches all three, and all three are how credentials actually escape
here. It does NOT defend against a hostile command: anything with the value in
its environment can encode it, post it, or write it to a file this never sees.
An agent that would do that could also read the `.env` directly. The point is to
make the CAREFUL path as short as the careless one.

RESOLUTION ORDER, and it is reported rather than guessed:

  1. the vault slot `projects/<project>/<env>/<NAME>` — the managed copy
  2. the project's own env files, as `store/raw/env.json` lists them, live
     files before templates and `.env` before `.env.<something>`

A name that resolves nowhere is an error naming both places it looked. A name
that resolves in BOTH is reported on stderr, because two copies of one
credential drift and the run should say which one it took.
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402
sys.path.insert(0, str(ROOT / "tools"))
import vault as private_files

VAULT = pathlib.Path(os.environ.get(
    "OBSERVATORY_VAULT_DIR",
    paths.source_path("secret_store", paths.SECRETS) / 'projects'))
AUDIT = paths.STATE / "logs" / "secret-use.jsonl"

#: Environments the vault knows, in the order a one-off command should prefer
#: them. `local` first on purpose: a command an agent runs by hand should reach
#: for the development credential, and reaching production takes `--env prod`.
ENVS = ("local", "stage", "prod")

#: What the child prints is filtered in chunks, and a value can straddle two of
#: them. The carry-over is the longest value we hold, so a split occurrence is
#: still matched on the next pass.
CHUNK = 65536


def audit(action: str, subject: str, detail: dict) -> None:
    row = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "action": action, "subject": subject, **detail}
    private_files._append_private(AUDIT, json.dumps(row, ensure_ascii=False) + "\n")


def scan() -> dict:
    src = paths.SCRATCH / "env.json"
    if not src.is_file():
        return {"files": []}
    return json.loads(src.read_text(encoding="utf-8"))


#: The vault's environment words and the env-file environments each one
#: means (`collectors/environments.py` spells a file's environment).
FILE_ENVS = {"local": {"local", "development", "test"},
             "stage": {"staging", "review"},
             "prod": {"production"}}
USE = 'python "$(project-observatory full-path)/tools/use_secret.py"'
PUT = 'python "$(project-observatory full-path)/tools/vault.py" put'


def _file_environment(path: str) -> str | None:
    sys.path.insert(0, str(ROOT / "collectors"))
    import environments
    return environments.file_environment(pathlib.PurePath(path).name)


def env_candidates(project: str, name: str, env: str | None = None) -> list[pathlib.Path]:
    """Files in this project that the inventory says hold this variable.

    LIVE BEFORE TEMPLATE, and `.env` before `.env.staging`: a template holds no
    value by construction, and picking `.env.production` for a command an agent
    typed by hand would be the wrong default in the most expensive direction.

    WITH AN EXPLICIT `--env`, ONLY FILES OF THAT ENVIRONMENT. `--env prod` used
    to fall through to the project's unlabeled `.env` — the development file —
    and run a production command on a development key without a word. A file
    whose name says no environment (`.env`) answers only when no environment
    was asked for.
    """
    rows = []
    for f in scan().get("files", []):
        if f.get("project") != project:
            continue
        if name not in {v.get("name") for v in f.get("variables", [])}:
            continue
        if env and _file_environment(f.get("path", "")) not in FILE_ENVS[env]:
            continue
        rows.append(f)
    rows.sort(key=lambda f: (f.get("kind") != "env",
                             f.get("path", "").count("."),
                             f.get("path", "")))
    selected = []
    root = paths.DATA.absolute()
    private_files._no_symlinks(root)
    for row in rows:
        relative = pathlib.Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Inventory path must remain inside the configured projects root")
        target = root / relative
        private_files._no_symlinks(target)
        selected.append(target)
    return selected


def read_env_value(path: pathlib.Path, name: str) -> str | None:
    sys.path.insert(0, str(ROOT / "collectors"))
    import scan_env
    try:
        pairs, _ = scan_env.parse(private_files._read_private(path, private=False))
    except OSError:
        return None
    for n, v in pairs:
        if n == name:
            return v
    return None


def vault_slot(project: str, name: str, env: str | None) -> tuple[pathlib.Path | None, str | None]:
    private_files.validate_names(project, env, name)
    for e in ([env] if env else ENVS):
        slot = VAULT / project / e / name
        private_files._no_symlinks(slot)
        if slot.is_file():
            return slot, e
    return None, None


def _held_in(project: str, name: str) -> tuple[list[str], list[str]]:
    """(vault environments holding NAME, env files holding it) — names only."""
    envs = [e for e in ENVS if (VAULT / project / e / name).is_file()]
    files = []
    for f in scan().get("files", []):
        if f.get("project") == project and name in {v.get("name") for v in f.get("variables", [])}:
            label = _file_environment(f.get("path", ""))
            files.append(f"{f.get('path')} ({label or 'no environment in its name'})")
    return envs, files


def resolve(project: str, name: str, env: str | None = None) -> tuple[str, str]:
    """(value, where it came from). Raises LookupError naming where it looked
    and where the name DOES live, so the next command is the right one."""
    slot, got_env = vault_slot(project, name, env)
    files = env_candidates(project, name, env)
    if slot is not None:
        if files:
            print(f"note: {name} is in the vault ({project}/{got_env}) and in "
                  f"{files[0].relative_to(paths.DATA)}; taking the vault. Two copies "
                  f"of one credential drift.", file=sys.stderr)
        return private_files._read_private(slot).strip(), f"vault:{project}/{got_env}/{name}"
    for f in files:
        value = read_env_value(f, name)
        if value:
            return value, f"env:{f.relative_to(paths.DATA)}"
    searched = env or ",".join(ENVS)
    envs, held = _held_in(project, name)
    found = []
    if envs:
        found.append(f"the vault holds it under {', '.join(f'{project}/{e}' for e in envs)}"
                     + (f" — run with `--env {envs[0]}`" if env else ""))
    if held:
        found.append(f"env files that hold it: {'; '.join(held)}"
                     + (" — an explicit --env reads only files named for that environment"
                        if env else ""))
    raise LookupError(
        f"{name} is not in the vault under projects/{project}/{{{searched}}}"
        + (" and not in any env file the inventory lists for that environment" if env else
           " and not in any env file the inventory lists")
        + f" for {project}. "
        + (" ".join(f.capitalize() for f in found) + ". " if found else "")
        + f"`{USE} names {project}` lists what is there; to store it, `{PUT} {project} "
        f"{env or 'local'} {name}` with a protected file on stdin.")


def _redaction(values: dict[str, str]):
    replacements = {value.encode("utf-8", "surrogateescape"): f"«{name}»".encode("utf-8")
                    for name, value in values.items() if value}
    pattern = re.compile(b"|".join(re.escape(value) for value in sorted(replacements, key=len, reverse=True))) if replacements else None
    return pattern, replacements


def scrub(data: bytes, values: dict[str, str]) -> bytes:
    pattern, replacements = _redaction(values)
    return pattern.sub(lambda match: replacements[match.group()], data) if pattern else data


def pump(src, dst, values: dict[str, str], longest: int) -> None:
    """Keep raw suffix bytes until the longest possible secret is decidable.

    Replacements are never scanned again. A shorter secret that prefixes a
    longer one cannot consume the prefix while the longer value is split
    across two reads.
    """
    pattern, replacements = _redaction(values)
    longest = max((len(value) for value in replacements), default=1)
    carry = b""
    while True:
        chunk = src.read(CHUNK)
        final = not chunk
        buffer = carry + chunk
        safe_end = len(buffer) if final else max(0, len(buffer) - longest + 1)
        cursor = 0
        output = []
        while cursor < safe_end:
            match = pattern.search(buffer, cursor) if pattern else None
            if match is None or match.start() >= safe_end:
                output.append(buffer[cursor:safe_end])
                cursor = safe_end
            else:
                output.extend((buffer[cursor:match.start()], replacements[match.group()]))
                cursor = match.end()
        carry = buffer[cursor:]
        if output:
            dst.write(b"".join(output))
            dst.flush()
        if final:
            break


def project_for_reading(text: str) -> str:
    """The vault folder a typed PROJECT names, for a READ: the folder the vault
    already holds under that exact name, else the registry project's folder
    (a registry id such as `project:local-alpha-web` is accepted). A name two
    projects claim is refused with both named. The same rule as `vault.py`."""
    folder, said = private_files.project_folder(text, verb="use")
    if said:
        print(f"project: {said}", file=sys.stderr)
    if folder.startswith("project:"):
        raise private_files.VaultBoundaryError(
            "that registry id is not a project in the registry; PROJECT is the project's "
            "folder name, or the id of a project the registry holds")
    private_files.validate_names(folder)
    return folder


def cmd_names(args) -> int:
    private_files.validate_names(args.project)
    private_files._no_symlinks(VAULT / args.project)
    doc_path = paths.REGISTRY / "env-inventory.json"
    rows: list[tuple[str, str, str]] = []
    if doc_path.is_file():
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        for f in doc.get("files", []):
            if f.get("project") != args.project:
                continue
            for v in f.get("variables", []):
                rows.append((v["name"], v["class"], f["path"]))
    vdir = VAULT / args.project
    if vdir.is_dir():
        for directory in sorted(vdir.iterdir()):
            private_files._no_symlinks(directory)
            if not directory.is_dir() or directory.name not in private_files.ENVS:
                continue
            for slot in sorted(directory.iterdir()):
                private_files._no_symlinks(slot)
                if slot.is_file() and re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", slot.name):
                    rows.append((slot.name, "vault", f"vault:{args.project}/{directory.name}"))
    if not rows:
        print(f"{args.project}: nothing in the vault and nothing in the env "
              f"inventory. `project-observatory full env` refreshes the second.")
        return 0
    width = max(len(r[0]) for r in rows)
    seen = set()
    for name, cls, where in sorted(rows, key=lambda r: (r[1] != "vault", r[0])):
        key = (name, where)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {name.ljust(width)}  {cls:11}  {where}")
    print(f"\n{len(seen)} name(s). Values are never printed — to USE one:\n"
          f"  {USE} run [--env ENV] {args.project} <NAME> -- <command>")
    return 0


def cmd_where(args) -> int:
    try:
        _, where = resolve(args.project, args.name, args.env)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(where)
    return 0


def cmd_run(args) -> int:
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    if not names:
        print("no variable named", file=sys.stderr)
        return 2
    if not args.command:
        print("nothing to run — put the command after `--`", file=sys.stderr)
        return 2
    if args.command[0].startswith("-"):
        # `run P NAME --env local -- cmd`: argparse hands everything after the
        # names to the command, so `--env` would be RUN as a program.
        print(f"use_secret: {args.command[0]} came after the names; flags go first: "
              f"run --env ENV PROJECT NAME -- COMMAND", file=sys.stderr)
        return 2
    values: dict[str, str] = {}
    wheres: dict[str, str] = {}
    for n in names:
        try:
            values[n], wheres[n] = resolve(args.project, n, args.env)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    # WRITTEN BEFORE THE RUN, like every other audited action here: a log
    # written afterwards loses the run that never came back.
    audit("use", f"{args.project}:{','.join(names)}",
          {"from": wheres, "command": args.command[0],
           "argv_len": len(args.command)})
    child_env = dict(os.environ)
    child_env.update(values)
    if args.as_name and len(names) == 1:
        child_env[args.as_name] = values[names[0]]
    longest = max(len(v.encode("utf-8", "surrogateescape")) for v in values.values())
    try:
        proc = subprocess.Popen(args.command, env=child_env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (FileNotFoundError, PermissionError) as exc:
        # The row above recorded an intended use; this one says it never ran.
        audit("use-failed", f"{args.project}:{','.join(names)}",
              {"command": args.command[0], "reason": type(exc).__name__})
        print(f"use_secret: command not found: {args.command[0]}"
              if isinstance(exc, FileNotFoundError) else
              f"use_secret: command not executable: {args.command[0]}", file=sys.stderr)
        return 127 if isinstance(exc, FileNotFoundError) else 126
    import threading
    threads = [
        threading.Thread(target=pump, args=(proc.stdout, sys.stdout.buffer, values, longest)),
        threading.Thread(target=pump, args=(proc.stderr, sys.stderr.buffer, values, longest)),
    ]
    for t in threads:
        t.start()
    rc = proc.wait()
    for t in threads:
        t.join()
    proc.stdout.close()
    proc.stderr.close()
    return rc


#: Programs that read their SOURCE from stdin. A secret piped into one of these
#: is parsed as code, and the parser prints what it could not parse — the
#: connection string, password included. Measured 2026-09-14 on the operator's
#: own agent: `heroku config:get DATABASE_URL -a … | python3 - <<'PY'`; the pipe
#: and the heredoc both fed stdin, the pipe won, SyntaxError quoted the line.
STDIN_PROGRAMS = {("python", "-"), ("python3", "-"), ("node", "-"), ("sh", "-s"),
                  ("bash", "-s"), ("zsh", "-s"), ("ruby", "-"), ("perl", "-")}


def cmd_pipe(args) -> int:
    """The value arrives on STDIN (from a provider's CLI, from pbpaste), goes
    into the child's environment under NAME, and every byte the child prints is
    scrubbed of it. One stdin, one passenger: the program must come from a file.
    """
    if not args.command:
        print("nothing to run — put the command after `--`", file=sys.stderr)
        return 2
    head = tuple(pathlib.Path(args.command[0]).name.split("-")[0:1] + args.command[1:2])
    if head in STDIN_PROGRAMS or (len(args.command) > 1 and args.command[1] in ("-", "-s")
                                  and pathlib.Path(args.command[0]).name in {a for a, _ in STDIN_PROGRAMS}):
        print(f"refused: `{' '.join(args.command[:2])}` reads its PROGRAM from stdin, and "
              f"stdin is carrying the secret. The parser would print the line it cannot "
              f"parse — that is the leak of 2026-09-14. Put the script in a file: "
              f"`… | use_secret.py pipe {args.name} -- python3 probe.py`", file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        print("the value must arrive on stdin, e.g. `heroku config:get NAME -a app | "
              "use_secret.py pipe NAME -- <command>`", file=sys.stderr)
        return 2
    value = sys.stdin.read().strip()
    if not value:
        print("stdin was empty — nothing to hand the command", file=sys.stderr)
        return 2
    values = {args.name: value}
    audit("pipe", args.name, {"command": args.command[0], "argv_len": len(args.command)})
    child_env = dict(os.environ)
    child_env[args.name] = value
    longest = len(value.encode("utf-8", "surrogateescape"))
    try:
        proc = subprocess.Popen(args.command, env=child_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (FileNotFoundError, PermissionError) as exc:
        audit("pipe-failed", args.name, {"command": args.command[0], "reason": type(exc).__name__})
        print(f"use_secret: command not found: {args.command[0]}"
              if isinstance(exc, FileNotFoundError) else
              f"use_secret: command not executable: {args.command[0]}", file=sys.stderr)
        return 127 if isinstance(exc, FileNotFoundError) else 126
    import threading
    threads = [
        threading.Thread(target=pump, args=(proc.stdout, sys.stdout.buffer, values, longest)),
        threading.Thread(target=pump, args=(proc.stderr, sys.stderr.buffer, values, longest)),
    ]
    for th in threads:
        th.start()
    rc = proc.wait()
    for th in threads:
        th.join()
    proc.stdout.close()
    proc.stderr.close()
    return rc


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("names", help="what this project has, values never printed")
    p.add_argument("project")
    p.set_defaults(fn=cmd_names)

    p = sub.add_parser("where", help="which slot a name would resolve to")
    p.add_argument("project")
    p.add_argument("name")
    p.add_argument("--env", choices=ENVS)
    p.set_defaults(fn=cmd_where)

    p = sub.add_parser("run", help="run a command with the value in its environment")
    p.add_argument("project")
    p.add_argument("names", help="one NAME, or several separated by commas")
    p.add_argument("--env", choices=ENVS)
    p.add_argument("--as", dest="as_name",
                   help="also export it under this name (one variable only)")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="after `--`, the command to run")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("pipe", help="value on stdin -> env NAME -> run, output scrubbed")
    p.add_argument("name", help="the environment variable the command will read")
    p.add_argument("command", nargs=argparse.REMAINDER, help="after `--`, the command")
    p.set_defaults(fn=cmd_pipe)

    a = ap.parse_args(argv[1:])
    if getattr(a, "command", None) and a.command and a.command[0] == "--":
        a.command = a.command[1:]
    try:
        if getattr(a, "as_name", None):
            private_files.validate_names("placeholder", name=a.as_name)
        if a.cmd == "pipe":
            private_files.validate_names("placeholder", name=a.name)
        if getattr(a, "project", None):
            a.project = project_for_reading(a.project)
        return a.fn(a)
    except private_files.VaultBoundaryError as exc:
        # Its messages are written to be shown: they name the field and the
        # rule, never the input.
        print(f"use_secret: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("use_secret: private filesystem or input validation refused the operation", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
