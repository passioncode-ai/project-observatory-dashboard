"""Where a finding's subject lives on the dashboard, decided at build time.

A finding names its subject by id (`domain:example.com`, `heroku:<app id>`,
`secret:<project>/<env>/<NAME>`). The page used to turn that id into a link in
its script, by prefix alone, and it could not know whether the row it pointed at
was ever rendered: `credential:` subjects of Google service-account grants
linked to `creds.html#c-…` rows the keys page never draws, `heroku:` subjects
(the provider's own id) matched no prefix at all, and `repository:` and
`secret:` subjects linked nowhere although their rows exist.

The builder holds every document the pages render, so it resolves the link
here — against the rows that will exist — and a subject with no row stays plain
text rather than becoming a link that lands on nothing. The anchors are the
ones the page script gives its rows; the slug rules are spelled the same way
(`anchorSlug` and the credential row id in `build_dashboard.TEMPLATE`).
"""
from __future__ import annotations

import re
from urllib.parse import quote

#: `anchorSlug` in the page script: env, MCP and Google credential anchors.
_ANCHOR = re.compile(r"[^A-Za-z0-9_.:/-]+")
#: The credential row id (`c-…`) in `renderCreds`.
_CRED = re.compile(r"[^A-Za-z0-9_.-]+")

#: Subjects that name a whole population rather than one row: the page that
#: lists it, narrowed by the chip that selects it where one exists. Only pages
#: and chips the template really has are named (checked by the shell test).
PAGE_SUBJECTS: dict[str, tuple[str, str]] = {
    "estate:heroku-orphans": ("heroku.html?f=noproject", "Heroku"),
    "estate:heroku-unclonned": ("heroku.html?f=nofolder", "Heroku"),
    "estate:heroku-snapshot": ("heroku.html", "Heroku"),
    "estate:remote-config": ("heroku.html", "Heroku"),
    "estate:analytics": ("traffic.html", "Traffic"),
    "estate:boundary": ("traffic.html", "Traffic"),
    "estate:mcp": ("mcp.html", "MCP"),
    "estate:lifecycle-drift": ("projects.html?f=drift", "Projects"),
    "estate:movements": ("creds.html#movements", "Keys"),
    "estate:credentials": ("creds.html", "Keys"),
    "estate:credentials-unclaimed": ("creds.html?f=c-unclaimed", "Keys"),
    "estate:credentials-shared": ("creds.html?f=c-shared", "Keys"),
    "estate:project-secrets": ("creds.html", "Keys"),
    "estate:env-shared": ("env.html?f=e-shared", "ENV"),
    "estate:env-reusable": ("env.html?f=e-reuse", "ENV"),
    "estate:env-modes": ("env.html?f=e-open", "ENV"),
    "estate:env-unignored": ("env.html", "ENV"),
    "estate:git": ("machine.html", "Machine"),
    "machine": ("machine.html", "Machine"),
    "host:volume": ("machine.html", "Machine"),
    "ledger": ("health.html", "Health"),
}


def anchor_slug(value: str) -> str:
    return _ANCHOR.sub("-", str(value))


def credential_anchor(credential_id: str) -> str:
    return "c-" + _CRED.sub("-", str(credential_id).replace("credential:", "", 1))


def google_anchor(client_email: str) -> str:
    return "gc-" + anchor_slug(client_email)


def build_index(*, projects=(), relations=(), domains=(), zones=(), apps=(),
                credentials=(), google_credentials=(), env_files=(), mcp_servers=()) -> dict:
    """The rows each page will render, keyed the way subjects name them."""
    project_ids = {p["id"] for p in projects if p.get("id")}
    folder_project = {}
    for p in projects:
        for folder in p.get("local_folders") or p.get("folders") or []:
            folder_project.setdefault(folder, p["id"])
    # A repository several projects claim links to the first by id, so the
    # link does not depend on the order the relations were written in.
    repo_project: dict[str, str] = {}
    for rel in relations:
        if rel.get("type") == "implemented_by" and rel.get("from") in project_ids:
            current = repo_project.get(rel["to"])
            if current is None or rel["from"] < current:
                repo_project[rel["to"]] = rel["from"]
    app_by_id = {}
    for a in apps:
        if a.get("name"):
            app_by_id[a["name"]] = a["name"]
            if a.get("id"):
                app_by_id[a["id"]] = a["name"]
    variables = {v.get("name") for f in env_files for v in f.get("variables") or []}
    return {
        "projects": project_ids,
        "folders": folder_project,
        "repos": repo_project,
        "domains": {d.get("name") for d in domains if d.get("name")}
                   | {z.get("name") for z in zones if z.get("name")},
        "apps": app_by_id,
        "credentials": {c["id"] for c in credentials if c.get("id")},
        "google": sorted({c.get("client_email") for c in google_credentials if c.get("client_email")},
                         key=len, reverse=True),
        "env_paths": {f.get("path") for f in env_files if f.get("path")},
        "env_names": {n for n in variables if n},
        "mcp": {f"{s.get('agent')}/{s.get('name')}" for s in mcp_servers},
    }


def resolve(subject: object, index: dict) -> dict | None:
    """`{"href", "label"}` for a subject whose row exists, `"page": True` when
    the label is a page title (a message id the page translates), else None."""
    text = str(subject or "")
    if text in PAGE_SUBJECTS:
        href, title = PAGE_SUBJECTS[text]
        return {"href": href, "label": title, "page": True}
    kind, sep, rest = text.partition(":")
    if not sep or not rest:
        return None
    if kind == "domain" and rest in index["domains"]:
        return {"href": "domains.html#d-" + rest, "label": rest}
    if kind in ("heroku", "app"):
        name = index["apps"].get(text) or index["apps"].get(rest)
        return {"href": "heroku.html#a-" + name, "label": name} if name else None
    if kind == "project" and text in index["projects"]:
        return {"href": "projects.html#" + text, "label": rest}
    if kind == "repository":
        owner = index["repos"].get(text)
        return {"href": "projects.html#" + owner, "label": rest} if owner else None
    if kind == "clone":
        owner = index["folders"].get(rest)
        return {"href": "projects.html#" + owner, "label": rest} if owner else None
    if kind == "secret":
        cid = "credential:vault/" + rest
        if cid in index["credentials"]:
            return {"href": "creds.html#" + credential_anchor(cid), "label": rest}
        return None
    if kind == "credential":
        if text in index["credentials"]:
            return {"href": "creds.html#" + credential_anchor(text), "label": rest}
        # A Google service-account grant is listed on the Traffic page by its
        # client address, not on the keys page.
        for email in index["google"]:
            if email in rest:
                return {"href": "traffic.html#" + google_anchor(email), "label": email}
        return None
    if kind == "env":
        if rest in index["env_paths"]:
            return {"href": "env.html#e-" + anchor_slug(rest), "label": rest}
        if rest in index["env_names"]:
            return {"href": "env.html?f=e-all&q=" + quote(rest, safe=""), "label": rest}
        return None
    if kind == "mcp" and rest in index["mcp"]:
        return {"href": "mcp.html#m-" + anchor_slug(rest), "label": rest}
    return None
