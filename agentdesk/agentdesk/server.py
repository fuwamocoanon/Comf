"""Local HTTP server: JSON REST API + the browser GUI (static/index.html).

Binds to 127.0.0.1 by default. There is no authentication: the actor is taken
from the X-Actor header (or ?actor= / "actor" in the body), which is fine for a
single-user machine but means you should not expose the port to a network.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import runner
from .store import DeskError, NotFound, Store

STATIC = Path(__file__).parent / "static"
ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn.__name__))
        return fn
    return deco


class Api:
    """Route handlers. Each gets (store, actor, params, body, *url_groups)."""

    def __init__(self, store: Store):
        self.store = store
        self.running: dict[str, threading.Thread] = {}

    @route("GET", "/api/meta")
    def meta(self, s, actor, p, b):
        return s.meta()

    @route("GET", "/api/stats")
    def stats(self, s, actor, p, b):
        return s.stats()

    @route("GET", "/api/tickets")
    def tickets(self, s, actor, p, b):
        keys = ("status", "assignee", "requester", "project", "type", "parent", "q", "tag", "queue", "order", "limit")
        args = {k: p[k] for k in keys if p.get(k)}
        if args.get("assignee") == "me":
            args["assignee"] = actor
        return s.list_tickets(**args)

    @route("POST", "/api/tickets")
    def create(self, s, actor, p, b):
        b.pop("actor", None)
        return s.create_ticket(actor=actor, **b)

    @route("GET", r"/api/tickets/([\w-]+)")
    def ticket(self, s, actor, p, b, ref):
        return s.get_ticket(ref, full=True)

    @route("PATCH", r"/api/tickets/([\w-]+)")
    def update(self, s, actor, p, b, ref):
        b.pop("actor", None)
        s.update_ticket(ref, actor, **b)
        return s.get_ticket(ref, full=True)

    @route("POST", r"/api/tickets/([\w-]+)/status")
    def status(self, s, actor, p, b, ref):
        s.set_status(ref, b["status"], actor, note=b.get("note"), resolution_code=b.get("resolution_code"),
                     force=bool(b.get("force")))
        return s.get_ticket(ref, full=True)

    @route("POST", r"/api/tickets/([\w-]+)/comments")
    def comment(self, s, actor, p, b, ref):
        return s.add_comment(ref, b.get("body", ""), actor, kind=b.get("kind", "comment"))

    @route("POST", r"/api/tickets/([\w-]+)/attachments")
    def attach(self, s, actor, p, b, ref):
        return s.add_attachment(ref, p.get("filename") or "upload.bin", b, actor,
                                comment_id=p.get("comment_id") or None, mime=p.get("mime") or None)

    @route("POST", r"/api/tickets/([\w-]+)/links")
    def link(self, s, actor, p, b, ref):
        return s.link(ref, b["rel"], b["target"], actor)

    @route("DELETE", r"/api/links/(\d+)")
    def unlink(self, s, actor, p, b, lid):
        s.unlink(lid, actor)
        return {"ok": True}

    @route("POST", r"/api/tickets/([\w-]+)/feedback")
    def feedback(self, s, actor, p, b, ref):
        return s.add_feedback(ref, b["rating"], b.get("text", ""), actor, reopen=bool(b.get("reopen")))

    @route("POST", r"/api/tickets/([\w-]+)/claim")
    def claim(self, s, actor, p, b, ref):
        return s.claim(ref, actor, int(b.get("minutes", 30)))

    @route("POST", r"/api/tickets/([\w-]+)/release")
    def release(self, s, actor, p, b, ref):
        return s.release(ref, actor, force=True)

    @route("POST", r"/api/tickets/([\w-]+)/watch")
    def watch(self, s, actor, p, b, ref):
        return {"watchers": s.watch(ref, actor, on=b.get("on", True))}

    @route("GET", r"/api/tickets/([\w-]+)/context")
    def context(self, s, actor, p, b, ref):
        return {"context": runner.build_context(s, ref, p.get("as") or actor)}

    @route("GET", r"/api/actors")
    def actors(self, s, actor, p, b):
        return s.list_actors(p.get("kind"))

    @route("POST", r"/api/actors")
    def upsert_actor(self, s, actor, p, b):
        b.pop("actor", None)
        name = b.pop("name")
        if b.get("preset") in runner.PRESETS and not b.get("command"):
            b["command"] = runner.PRESETS[b["preset"]]
        b.pop("preset", None)
        return s.upsert_actor(name, b.pop("kind", None), **b)

    @route("GET", r"/api/presets")
    def presets(self, s, actor, p, b):
        return runner.PRESETS

    @route("POST", r"/api/agents/([\w.-]+)/run")
    def run_agent(self, s, actor, p, b, name):
        if name in self.running and self.running[name].is_alive():
            raise DeskError(f"{name} is already running")
        s.get_actor(name)
        ticket = b.get("ticket")

        def go():
            own = Store(s.home)
            try:
                runner.run_once(own, name, ticket=ticket)
            except Exception as e:
                if ticket:
                    own.add_comment(ticket, f"Runner error: {e}", "system", kind="work_note")
            finally:
                own.close()

        th = threading.Thread(target=go, daemon=True)
        self.running[name] = th
        th.start()
        return {"started": name, "ticket": ticket}

    @route("GET", r"/api/agents/running")
    def agents_running(self, s, actor, p, b):
        return [n for n, th in self.running.items() if th.is_alive()]

    @route("GET", r"/api/projects")
    def projects(self, s, actor, p, b):
        return s.list_projects()

    @route("POST", r"/api/projects")
    def add_project(self, s, actor, p, b):
        return s.add_project(b["key"], b.get("name"), b.get("description", ""), b.get("workspace", ""))

    @route("GET", r"/api/inbox")
    def inbox(self, s, actor, p, b):
        return s.inbox(actor, unread_only=p.get("all") != "1")

    @route("POST", r"/api/inbox/read")
    def read(self, s, actor, p, b):
        return {"marked": s.mark_read(actor, ids=b.get("ids"), ticket=b.get("ticket"))}

    @route("GET", r"/api/events")
    def events(self, s, actor, p, b):
        return {"last": s.last_event_id(), "events": s.events(int(p.get("since", 0)), ticket=p.get("ticket"))}

    @route("GET", r"/api/kb")
    def kb_list(self, s, actor, p, b):
        return s.kb_list(p.get("q"), p.get("project"))

    @route("GET", r"/api/kb/([\w-]+)")
    def kb_get(self, s, actor, p, b, slug):
        return s.kb_get(slug)

    @route("POST", r"/api/kb")
    def kb_put(self, s, actor, p, b):
        return s.kb_put(b["slug"], b["title"], b.get("body", ""), actor, b.get("project") or None, b.get("tags"))


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentDesk"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            url = urlparse(self.path)
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            path = unquote(url.path)
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            if method == "GET" and (m := re.match(r"^/api/attachments/(\d+)$", path)):
                return self._attachment(int(m.group(1)), params.get("download") == "1")
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            is_upload = method == "POST" and path.endswith("/attachments")
            try:
                body = raw if is_upload else (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON body"})
            actor = (self.headers.get("X-Actor") or params.get("actor")
                     or (body.get("actor") if isinstance(body, dict) else None) or "user").strip()
            for m_, pattern, name in ROUTES:
                if m_ != method:
                    continue
                match = pattern.match(path)
                if match:
                    try:
                        result = getattr(api, name)(api.store, actor, params, body, *match.groups())
                        return self._send(200, result)
                    except NotFound as e:
                        return self._send(404, {"error": str(e)})
                    except (DeskError, KeyError, TypeError, ValueError) as e:
                        return self._send(400, {"error": f"{type(e).__name__}: {e}" if not isinstance(e, DeskError) else str(e)})
            self._send(404, {"error": f"no route {method} {path}"})

        def _static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            f = (STATIC / name).resolve()
            if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
                f = STATIC / "index.html"
            ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                     ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
            self._send(200, f.read_bytes(), ctype)

        def _attachment(self, aid, download):
            try:
                a = api.store.get_attachment(aid)
                data = Path(a["path"]).read_bytes()
            except (NotFound, OSError) as e:
                return self._send(404, {"error": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", a["mime"] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            mime = a["mime"] or ""
            safe_inline = mime in ("text/plain", "application/pdf") or (mime.startswith("image/") and "svg" not in mime)
            disp = "inline" if safe_inline and not download else "attachment"
            self.send_header("Content-Disposition", f'{disp}; filename="{a["filename"]}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def make_server(store: Store, host="127.0.0.1", port=8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(Api(store)))
    httpd.daemon_threads = True
    return httpd


def serve(store: Store, host="127.0.0.1", port=8765, with_runner=False, auto_close_days=7):
    httpd = make_server(store, host, port)
    stop = threading.Event()
    if with_runner:
        threading.Thread(target=runner.run_loop, args=(store,), kwargs={"stop": stop}, daemon=True).start()

    def housekeeping():
        own = Store(store.home)
        while not stop.wait(600):
            try:
                own.auto_close(auto_close_days)
            except Exception:
                pass

    threading.Thread(target=housekeeping, daemon=True).start()
    print(f"AgentDesk running at http://{host}:{port}  (data: {store.home})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        time.sleep(0.1)
