"""Which vault folder a project name means: one answer for every door.

THE PROBLEM THIS ENDS. A vault slot lives at `projects/<PROJECT>/<ENV>/<NAME>`,
and `<PROJECT>` is the name of the project's FOLDER on disk (`alpha-web`).
The registry, the MCP answers and the dashboard name the same project by its
id (`project:local-alpha-web` for a local-only project), and the docs never
said which of the two a person types. So both were typed: `vault.py put
local-alpha-web …` created a second directory for one project, the Keys page
called that slot "a credential of an organisation", the MCP `use` command
named one folder while listing slots from two, and the keyserver refused the
slug that `vault.py` had accepted.

THE RULE. A project string resolves to the project that claims it, and every
write door files the slot under that project's ONE folder, so a project has one
vault directory. A string is claimed by a project when it is:

  folder   the basename of one of the project's local folders
  id       its id, with or without the `project:` prefix
  name     its registry `name`

and normalised to the project's folder: the folder it named, else the
project's only folder, else (a project with no folder on this machine) the
slug of its id.

WHY THE AMBIGUITY CHECK IS THE WHOLE SAFETY OF IT. A folder may literally be
called `local-alpha-web` and belong to ANOTHER project than the one whose id is
`project:local-alpha-web`. Normalising without looking would quietly file one
project's key under another's folder. So a string two projects claim, in any
of the three ways, is AMBIGUOUS: writes refuse it and name the candidates,
and readers report it rather than pick. So is a project with two folders of
different names when the string names neither.

ONE EXCEPTION: TWO CHECKOUTS WITH ONE BASENAME. Two projects may each keep a
local folder of the same basename (`~/DATA/omega` of one project and
`~/DATA/<other>/omega`, a checkout of another). The vault folder is one flat
name, so only one of them can file keys there: the one that claims the
string by its id or name as well (`project:omega`). It owns the
folder; every other project with a folder of that basename files its keys
under the slug of its own id instead, unless another project claims that slug
too. This decides nothing the trap above protects: a string claimed by one
project's id and by ANOTHER project's folder still has a claimant that does
not keep the folder, and stays ambiguous (measured 2026-11-16: a new slot
of such a project was refused while its three existing slots stayed readable).

A string nobody claims is kept as typed: an organisation's key (an ad account
shared by several products) legitimately has a folder of its own.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

import paths


@dataclass(frozen=True)
class Resolution:
    """What a project string means. `folder` is None only when `how` is
    `ambiguous`; `project_id` is None when nothing in the registry claims it."""

    text: str
    folder: str | None
    project_id: str | None
    how: str                       # folder | id | name | unknown | ambiguous | unreadable
    candidates: tuple[str, ...] = field(default=())

    @property
    def changed(self) -> bool:
        return self.folder is not None and self.folder != self.text

    def sentence(self) -> str:
        """One line for a person: what was used, or why nothing was."""
        if self.how == "ambiguous":
            return (f"{self.text!r} is ambiguous here: {'; '.join(self.candidates)}. "
                    f"Name the project's folder exactly")
        if self.how == "unreadable":
            return (f"the registry could not be read, so {self.text!r} is used as typed "
                    f"(the folder name)")
        if self.changed:
            return (f"{self.text!r} is {self.project_id} ({self.how}); its vault folder is "
                    f"{self.folder!r}, and that is the folder used")
        return ""


def load_projects() -> list[dict] | None:
    """The registry's project rows, or None when the file will not read.

    None and [] are different answers: an unreadable registry must not read as
    "no project claims this", because that would make a typed id a new folder."""
    doc = paths.REGISTRY / "projects.json"
    if not doc.is_file():
        return []
    try:
        rows = json.loads(doc.read_text(encoding="utf-8")).get("projects", [])
    except (OSError, ValueError, AttributeError):
        return None
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def _folders(project: dict) -> list[str]:
    out: list[str] = []
    for f in project.get("local_folders") or []:
        name = pathlib.PurePath(str(f)).name if f else ""
        if name and name not in out:
            out.append(name)
    return out


def _slug(project_id: str) -> str:
    return project_id.split(":", 1)[1] if project_id.startswith("project:") else project_id


def _basename_owner(folder: str, projects: list[dict]) -> str | None:
    """The one project that keeps a local folder of this basename AND is named by it (its id's
    slug or its registry name), or None when none or several are."""
    owners = [p["id"] for p in projects if folder in _folders(p)
              and (folder == _slug(p["id"]) or folder == str(p.get("name") or ""))]
    return owners[0] if len(owners) == 1 else None


def resolve(text: str, projects: list[dict] | None = None, *, loaded: bool = False) -> Resolution:
    """The vault folder `text` means. `projects` is the registry's rows; omitted,
    the registry is read (pass `loaded=True` with `None` to say it was
    unreadable)."""
    text = str(text or "").strip()
    if projects is None and not loaded:
        projects = load_projects()
    if projects is None:
        return Resolution(text, text, None, "unreadable")
    bare = _slug(text)
    claims: dict[str, list[str]] = {}
    by_id = {p["id"]: p for p in projects}
    for p in projects:
        pid = p["id"]
        if text in _folders(p):
            claims.setdefault(pid, []).append("folder")
        if bare == _slug(pid):
            claims.setdefault(pid, []).append("id")
        if p.get("name") and text == str(p["name"]):
            claims.setdefault(pid, []).append("name")
    if not claims:
        return Resolution(text, text, None, "unknown")
    if len(claims) > 1 and all("folder" in ways for ways in claims.values()):
        # Every claimant keeps a folder of this basename: the one whose id or name
        # is the string owns the vault folder (the module's ONE EXCEPTION).
        owner = _basename_owner(text, projects)
        if owner and owner in claims:
            claims = {owner: claims[owner]}
    if len(claims) > 1:
        return Resolution(text, None, None, "ambiguous", tuple(
            f"{pid} by its {' and '.join(sorted(set(ways)))}" for pid, ways in sorted(claims.items())))
    (pid, ways), = claims.items()
    project = by_id[pid]
    folders = _folders(project)
    if "folder" in ways:
        folder, how = text, "folder"
    elif len(folders) == 1:
        folder, how = folders[0], ways[0]
    elif not folders:
        folder, how = _slug(pid), ways[0]
    elif bare in folders:
        folder, how = bare, ways[0]
    else:
        # One candidate sentence, as a one-element tuple: `tuple(str)` would split it into characters.
        return Resolution(text, None, pid, "ambiguous", (
            f"{pid} keeps folders {', '.join(repr(f) for f in folders)}",))
    # The folder the string led to must not be ANOTHER project's folder too:
    # then one directory would hold two projects' keys.
    others = sorted(q["id"] for q in projects if q["id"] != pid and folder in _folders(q))
    owner = _basename_owner(folder, projects) if others else None
    if owner == pid:
        others = []
    elif owner and "folder" not in ways:
        # Another project owns this basename's vault folder: this one files under its id's slug,
        # unless some other project claims that slug as well.
        slug = _slug(pid)
        rivals = sorted(q["id"] for q in projects if q["id"] != pid and (
            slug in _folders(q) or slug == _slug(q["id"]) or slug == str(q.get("name") or "")))
        if not rivals:
            return Resolution(text, slug, pid, ways[0])
        return Resolution(text, None, pid, "ambiguous", (
            f"{pid} files its keys under {slug!r}: {owner} owns the folder {folder!r}",
            *(f"{o} claims {slug!r} too" for o in rivals)))
    if others:
        return Resolution(text, None, pid, "ambiguous", (
            f"{pid} files its keys under {folder!r}",
            *(f"{o} has a folder named {folder!r} too" for o in others)))
    return Resolution(text, folder, pid, how)


def folders_of(folder: str, root: pathlib.Path) -> list[str]:
    """Every vault folder under `root` that holds this project's slots: the one named,
    then any other the registry resolves to the same project. A project can hold two:
    slots filed under a registry name before the one-folder rule stay there, and new
    ones go to the project's own folder. A reader that looked in one missed the other
    (2026-10-06). An ambiguous or unregistered name is only itself."""
    out = [folder] if (root / folder).is_dir() else []
    projects = load_projects()
    if projects is None or not root.is_dir():
        return out
    me = resolve(folder, projects)
    if not me.project_id or me.how == "ambiguous":
        return out
    for d in sorted(root.iterdir()):
        if d.name in out or d.is_symlink() or not d.is_dir():
            continue
        other = resolve(d.name, projects)
        if other.project_id == me.project_id and other.how != "ambiguous":
            out.append(d.name)
    return out

