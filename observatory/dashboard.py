"""Read-only local dashboard; this module never serves credential values."""
from __future__ import annotations

import html
import http.server
import json
from pathlib import Path
import socketserver

from .core import latest, read_json, write_private


def render(report: dict, leaks: dict | None = None) -> str:
    esc = lambda x: html.escape(str(x), quote=True)
    projects = report.get("projects", [])
    rows = []
    for p in projects:
        g = p["git"]
        state = "Partial observation" if p["degraded"] else ("Git observed" if g.get("present") else "Files observed")
        rows.append(f'<tr><th scope="row">{esc(p["name"])}</th><td>{state}</td><td>{p["files"]:,}</td><td>{p["bytes"]:,}</td><td>{esc(g.get("dirty_files", 0))}</td><td>{esc(g.get("ahead") if g.get("ahead") is not None else "—")}</td></tr>')
    findings = ''.join(f'<li><strong>{esc(f["project"])} · {esc(f["kind"])}</strong><p>{esc(f["detail"])}</p><p class="muted">{esc(f["remedy"])}</p></li>' for f in report.get("findings", []))
    leak_count = len((leaks or {}).get("findings", []))
    leak_text = (f'{leak_count} known-value exposure finding(s) in explicitly selected local targets. Review with the CLI.' if leaks else 'No leak scan has run. Register known values, then select explicit targets with the CLI.')
    empty = '<p class="empty">No projects observed yet. Use <code>project add NAME PATH</code>, then <code>scan</code>.</p>' if not projects else ''
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Project Observatory · local overview</title>
<style>:root{{color-scheme:dark;--bg:#10191e;--panel:#17262e;--ink:#eef4ed;--muted:#becdc9;--line:#365059;--accent:#b9e475}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,sans-serif}}main{{max-width:1120px;margin:auto;padding:48px 24px}}a{{color:var(--accent)}}header{{border-bottom:1px solid var(--line);padding-bottom:24px}}h1{{font-size:clamp(2rem,5vw,3.4rem);line-height:1.1;letter-spacing:-.04em}}h2{{font-size:1.4rem}}.eyebrow{{color:var(--accent);letter-spacing:.12em;text-transform:uppercase;font-size:.75rem}}.muted{{color:var(--muted)}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:28px 0}}.stats div,section{{background:var(--panel);padding:20px;border:1px solid var(--line);border-radius:10px}}.stats strong{{font-size:2rem;display:block}}section{{margin:20px 0}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;text-align:left}}th,td{{padding:12px;border-bottom:1px solid var(--line);white-space:nowrap}}li{{margin-bottom:22px}}li p{{margin:5px 0}}code{{overflow-wrap:anywhere}}footer{{color:var(--muted);font-size:.85rem}}@media(max-width:600px){{main{{padding:24px 14px}}.stats{{grid-template-columns:1fr}}}}@media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto}}}}</style>
<body><main><header><p class="eyebrow">Local observation layer · portable edition</p><h1>Project Observatory</h1><p class="muted">Registered projects, measured state, explicit next actions.</p><p>Last scan: <time>{esc(report.get("scanned_at") or "not yet run")}</time></p></header>
<div class="stats"><div><strong>{len(projects)}</strong>registered projects observed</div><div><strong>{len(report.get("findings", []))}</strong>project findings</div><div><strong>{sum(bool(p['degraded']) for p in projects)}</strong>partial observations</div></div>
<section aria-labelledby="projects"><h2 id="projects">Projects</h2>{empty}<div class="scroll" tabindex="0" role="region" aria-label="Project measurements"><table><thead><tr><th scope="col">Project</th><th scope="col">Observation</th><th scope="col">Files</th><th scope="col">Bytes</th><th scope="col">Dirty files</th><th scope="col">Ahead</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p class="muted">Git remote state comes from local tracking references. No network fetch occurs.</p></section>
<section aria-labelledby="findings"><h2 id="findings">Review next</h2>{'<ul>'+findings+'</ul>' if findings else '<p>No project findings in this snapshot. This is not a security certification.</p>'}</section>
<section aria-labelledby="credentials"><h2 id="credentials">Local credential exposure</h2><p>{esc(leak_text)}</p><p class="muted">Only known values and explicit targets are compared. Values, snippets and absolute paths never appear here.</p></section>
<footer>Private local dashboard. Do not deploy this generated file. Public marketing uses a separate static site.</footer></main></body></html>'''


def build(state: Path) -> Path:
    leaks = read_json(state / "leaks.json") if (state / "leaks.json").exists() else None
    output = state / "dashboard.html"
    write_private(output, render(latest(state), leaks))
    return output


def handler(state: Path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in allowed or (origin is not None and origin not in {f"http://{h}" for h in allowed}):
                self.send_error(403, "Local host and origin required")
                return
            if self.path != "/":
                self.send_error(404)
                return
            leaks = read_json(state / "leaks.json") if (state / "leaks.json").exists() else None
            body = render(latest(state), leaks).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_error(405, "Read-only dashboard")
    return Handler


class LoopbackServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse lookup HTTPServer.server_bind() makes between
    bind() and listen(): a slow resolver would leave the port bound but silent."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


def serve(state: Path, port: int) -> None:
    latest(state)
    with LoopbackServer(("127.0.0.1", port), handler(state)) as server:
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}", "mode": "read-only"}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
