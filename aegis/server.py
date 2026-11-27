"""Loopback-only HTTP API and workspace interface."""
import argparse
import fcntl
import json
import os
import re
import secrets
import signal
import threading
from html import escape
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import default_workspace
from .contracts import PERSONAS, STAGES, require
from .engine import Lead
from .sandbox import detected_capacity

WEB = files("aegis").joinpath("web")
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/workspace-tools.js": ("workspace-tools.js", "text/javascript; charset=utf-8"),
          "/session.js": ("session.js", "text/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/aegis-mark.png": ("aegis-mark.png", "image/png"),
          "/agent-portraits.png": ("agent-portraits.png", "image/png")}
PROJECT = re.compile(r"/api/projects/([0-9a-f]{16})/sessions\Z")
PROJECT_BUDGET = re.compile(r"/api/projects/([0-9a-f]{16})/budget\Z")
SESSION = re.compile(r"/api/sessions/([0-9a-f]{16})(?:/(agents|telemetry|messages|build|preview|cancel|retry|publish))?"
                     r"(?:/([0-9a-f]{16}))?\Z")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class HTTPFailure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, lead):
        self.lead = lead
        self.csrf = secrets.token_urlsafe(32)
        self.bound = threading.BoundedSemaphore(24)
        super().__init__(address, Handler)
        self.host_header = f"127.0.0.1:{self.server_port}"
        self.base_url = "http://" + self.host_header


class Handler(BaseHTTPRequestHandler):
    timeout = 18

    def log_message(self, *_):
        pass

    def send(self, status, payload, mime, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def json(self, data, status=200):
        self.send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def body(self, maximum=40_000):
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise HTTPFailure(415, "Expected an application/json request")
        require(not self.headers.get("Transfer-Encoding"), "Chunked requests are unsupported")
        length = int(self.headers.get("Content-Length", "0"))
        require(0 < length <= maximum, "Invalid request size")
        data = json.loads(self.rfile.read(length))
        require(isinstance(data, dict), "Expected a JSON object")
        return data

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        if not self.server.bound.acquire(blocking=False):
            self.json({"error": "Aegis server is busy"}, 503)
            return
        try:
            if self.headers.get("Host") not in (self.server.host_header, f"localhost:{self.server.server_port}"):
                raise HTTPFailure(403, "Workspace requests must originate on loopback")
            location = urlsplit(self.path)
            origin = self.headers.get("Origin")
            if self.command == "POST" or location.path.startswith("/api/"):
                if (origin and origin != "http://" + self.headers["Host"]
                        or self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site")):
                    raise HTTPFailure(403, "Cross-origin requests are disabled")
            if self.command == "POST":
                token = self.headers.get("X-Aegis-Token", self.headers.get("X-Pitcrew-Token", self.headers.get("X-Omni-Token", "")))
                if not secrets.compare_digest(token, self.server.csrf):
                    raise HTTPFailure(403, "Missing workspace request token")
            if self.command == "POST":
                maximum = 1_200_000 if location.path in ("/api/extensions/config", "/api/extensions/skill") else 40_000
                self.change(location.path, self.body(maximum))
            else:
                self.read(location.path, parse_qs(location.query))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except HTTPFailure as exc:
            self.json({"error": str(exc)}, exc.status)
        except (ValueError, TypeError) as exc:
            self.json({"error": self.server.lead.store.redact(str(exc))}, 400)
        except Exception as exc:
            self.json({"error": self.server.lead.store.redact(str(exc)) or "Workspace operation failed"}, 500)
        finally:
            self.server.bound.release()

    def read(self, path, query):
        lead, store = self.server.lead, self.server.lead.store
        if path in ASSETS:
            name, mime = ASSETS[path]
            self.send(200, (WEB / name).read_bytes(), mime)
        elif path == "/api/workspace":
            self.json({"csrf": self.server.csrf, "workspace_id": lead.workspace, "projects": store.projects(),
                       "provider": lead.provider(), "memory": store.memory_health(),
                       "capabilities": {"github_projects": True, "project_budgets": True,
                                        "github_racedays": True, "raceday_budgets": True}})
        elif path == "/api/extensions":
            self.json(lead.extensions())
        elif path == "/api/extensions/config":
            self.json(lead.extension_editor() | {"callback": self.server.base_url + "/oauth/mcp/callback"})
        elif path == "/api/extensions/skill":
            self.json(lead.skill_files(query.get("name", [""])[0]))
        elif path == "/oauth/mcp/callback" and self.command == "GET":
            try:
                lead.mcp_oauth.finish(query)
            except (ValueError, OSError, RuntimeError, http.client.HTTPException) as exc:
                message = escape(store.redact(str(exc)))
                page = f'<!doctype html><meta charset="utf-8"><title>MCP sign-in</title><h1>MCP sign-in</h1><p>{message}</p><a href="/#extensions">Return to Extensions</a>'
                self.send(400, page.encode(), "text/html; charset=utf-8")
            else:
                self.send(303, b"", "text/plain", {"Location": "/#extensions"})
        elif path in ("/api/memory/history", "/api/memory/records"):
            page = int(query.get("page", [0])[0])
            search = query.get("q", [""])[0]
            require(0 <= page <= 100_000 and len(search) <= 200, "Invalid Dreaming query")
            self.json(store.dream_history(page) if path.endswith("/history") else store.memory_browser(search, page))
        elif path == "/api/memory/playbooks":
            project = query.get("project", [None])[0]
            require(project is None or re.fullmatch(r"[0-9a-f]{16}", project), "Invalid Project ID")
            self.json({"playbooks": store.playbooks(project)})
        elif path == "/api/execution":
            self.json(lead.execution_settings())
        elif path == "/api/execution/pressure":
            lead.capacity.set_engine_capacity(detected_capacity(lead.docker))
            self.json(lead.capacity.report())
        elif path == "/api/doctor":
            try:
                self.json({"ready": True, "capacity": lead.docker.doctor()})
            except Exception as exc:
                self.json({"ready": False, "detail": store.redact(str(exc))})
        elif match := PROJECT_BUDGET.fullmatch(path):
            self.json(lead.budget(match[1]))
        elif match := PROJECT.fullmatch(path):
            project = store.row("SELECT * FROM projects WHERE id=?", (match[1],))
            if not project:
                raise HTTPFailure(404, "Project does not exist")
            self.json({"project": project, "budget": store.budget(match[1]), "sessions": store.rows(
                """SELECT id,prompt,state,stage,branch,created,updated,error FROM sessions
                   WHERE project_id=? ORDER BY created DESC""", (match[1],))})
        elif match := SESSION.fullmatch(path):
            session_id, action, agent_id = match.groups()
            if action == "agents" and agent_id:
                self.agent(session_id, agent_id, query)
            elif action == "telemetry" and not agent_id:
                self.telemetry(session_id, query)
            elif action == "messages" and not agent_id:
                lead.get(session_id)
                question = int(query.get("reply_to", [0])[0])
                require(0 < question < 2 ** 63, "Invalid message identifier")
                self.json({"reply": store.chat_reply(session_id, question)})
            elif action == "build" and not agent_id:
                session = lead.get(session_id)
                if not session["bundle"] or session["state"] != "completed":
                    raise HTTPFailure(404, "Build is not available")
                bundle = lead.repo.bundles / f"{session_id}.zip"
                self.send(200, bundle.read_bytes(), "application/zip",
                          {"Content-Disposition": f'attachment; filename="aegis-{session_id}.zip"'})
            elif not action:
                session = store.summary(session_id)
                if not session:
                    raise HTTPFailure(404, "Session does not exist")
                self.json(session)
            else:
                raise HTTPFailure(404, "Unknown workspace endpoint")
        else:
            raise HTTPFailure(404, "Unknown workspace endpoint")

    def agent(self, session_id, agent_id, query):
        store = self.server.lead.store
        agent = store.row("SELECT * FROM attempts WHERE id=? AND session_id=?", (agent_id, session_id))
        if not agent:
            raise HTTPFailure(404, "Agent does not exist")
        if query.get("metadata") == ["1"]:
            self.json({"agent": agent})
            return
        cursor = max(0, int(query.get("after", [0])[0]))
        events = store.rows("""SELECT * FROM events WHERE session_id=? AND attempt_id=? AND id>?
                             ORDER BY id LIMIT 25""", (session_id, agent_id, cursor))
        self.json({"agent": agent, "events": events, "cursor": events[-1]["id"] if events else cursor,
                   "more": len(events) == 25})

    def telemetry(self, session_id, query):
        lead, store = self.server.lead, self.server.lead.store
        lead.get(session_id)
        if query.get("event"):
            event_id = int(query["event"][0])
            require(0 < event_id < 2 ** 63, "Invalid event identifier")
            event = store.row("SELECT * FROM events WHERE session_id=? AND id=?", (session_id, event_id))
            if not event:
                raise HTTPFailure(404, "Event does not exist")
            self.json(event)
            return
        role = query.get("role", [None])[0]
        stage = query.get("stage", [None])[0]
        attempt = query.get("attempt", [None])[0]
        require(role is None or role in PERSONAS, "Unknown agent role")
        require(stage is None or stage in ("lead", *STAGES), "Unknown factory stage")
        require(attempt is None or re.fullmatch(r"[0-9a-f]{16}", attempt), "Invalid attempt identifier")
        before = int(query["before"][0]) if query.get("before") else None
        after = int(query["after"][0]) if query.get("after") else None
        require(before is None or 0 < before < 2 ** 63, "Invalid telemetry cursor")
        require(after is None or before is None and 0 <= after < 2 ** 63, "Invalid telemetry cursor")
        self.json(store.telemetry(session_id, role=role, stage=stage, attempt=attempt, before=before, after=after))

    def change(self, path, data):
        lead, store = self.server.lead, self.server.lead.store
        if path == "/api/projects":
            self.json(lead.create_project(data.get("name"), data.get("description", ""),
                                          data.get("github_url"), data.get("github_branch")), 201)
        elif path == "/api/provider":
            self.json(lead.configure(data))
        elif path == "/api/provider/models":
            self.json(lead.models(data))
        elif path == "/api/provider/azure-prices":
            self.json(lead.import_azure_prices(data))
        elif path == "/api/provider/credential":
            self.json(lead.credential(data))
        elif path == "/api/provider/test":
            self.json(lead.test_provider())
        elif path == "/api/doctor/full":
            self.json(lead.preflight(models=data.get("models") is True))
        elif path == "/api/execution":
            self.json(lead.configure_execution(data))
        elif path == "/api/extensions/check":
            self.json(lead.extensions(check=True))
        elif path == "/api/extensions/config":
            self.json(lead.save_extensions(data))
        elif path == "/api/extensions/skill":
            self.json(lead.save_skill(data))
        elif path in ("/api/extensions/oauth/start", "/api/extensions/oauth/disconnect"):
            server = lead.oauth_server(data.get("name"))
            if path.endswith("/start"):
                self.json({"url": lead.mcp_oauth.begin(server, self.server.base_url + "/oauth/mcp/callback")})
            else:
                lead.mcp_oauth.disconnect(server)
                self.json({"connected": False})
        elif path == "/api/memory/dream":
            result = lead.dream()
            if result is None:
                raise HTTPFailure(409, "Dreaming runs between active sessions")
            self.json(result)
        elif match := PROJECT_BUDGET.fullmatch(path):
            require(set(data) == {"budget_usd"}, "Provide budget_usd to set or clear the limit")
            self.json(lead.budget(match[1], data["budget_usd"]))
        elif match := PROJECT.fullmatch(path):
            self.json({"id": lead.launch(match[1], data.get("prompt"), sample=data.get("sample") is True)}, 202)
        elif match := SESSION.fullmatch(path):
            session_id, action, agent_id = match.groups()
            if agent_id:
                raise HTTPFailure(404, "Unknown workspace endpoint")
            if action == "messages":
                question = lead.chat(session_id, data.get("text"), handoff=data.get("handoff") is True)
                self.json({"queued": True, "message_id": question}, 202)
            elif action == "cancel":
                lead.cancel(session_id)
                self.json({"cancelled": True}, 202)
            elif action == "retry":
                lead.retry(session_id)
                self.json({"queued": True}, 202)
            elif action == "preview":
                self.json({"url": lead.change_preview(session_id, data.get("start") is True)})
            elif action == "publish":
                self.json(lead.publish(session_id))
            else:
                raise HTTPFailure(404, "Unknown workspace endpoint")
        else:
            raise HTTPFailure(404, "Unknown workspace endpoint")


@contextmanager
def workspace_lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / ".server.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("This Aegis workspace is already open") from exc
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=default_workspace())
    parser.add_argument("--doctor", action="store_true", help="Check Docker daemon and built images")
    args = parser.parse_args(argv)
    if args.doctor:
        from .sandbox import Docker
        try:
            Docker().doctor()
        except (OSError, RuntimeError, TimeoutError) as exc:
            parser.exit(1, f"{exc}\n")
        print("Docker and Aegis images are ready.")
        return
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    with workspace_lock(args.data):
        lead = Lead(args.data)
        try:
            with WorkspaceServer(("127.0.0.1", args.port), lead) as server:
                def shutdown(*_):
                    threading.Thread(target=server.shutdown, daemon=True).start()

                signal.signal(signal.SIGTERM, shutdown)
                marker = lead.store.directory / ".control-port"
                pending = marker.with_suffix(".tmp")
                pending.write_text(str(server.server_port), encoding="ascii")
                pending.replace(marker)
                print("Aegis workspace is available at " + server.base_url, flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    marker.unlink(missing_ok=True)
        finally:
            lead.close()


if __name__ == "__main__":
    main()
