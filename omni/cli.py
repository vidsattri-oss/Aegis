"""Run RittaiHarness projects and inspect sessions from a terminal."""
import argparse
import getpass
import http.client
import json
import os
import re
import sys
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from .contracts import LABELS, PERSONAS, STAGES, require
from .credentials import ProviderCredentials
from .engine import Lead, TERMINAL, check_provider, discover_models
from .extensions import Extensions
from .integrations import environment_provider, initial_provider, provider_key, provider_settings
from .sandbox import Docker
from .server import main as serve
from .server import workspace_lock
from .store import Store

IDENTIFIER = re.compile(r"[0-9a-f]{16}\Z")
MISSING = object()


class RemoteWorkspace:
    def __init__(self, port):
        require(0 < port <= 65535, "Connect port must be between 1 and 65535")
        self.port = port
        self.token = None
        self.about = self.request("/api/workspace")
        self.token = self.about["csrf"]

    def request(self, path, body=MISSING):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=90)
        try:
            if body is MISSING:
                client.request("GET", path)
            else:
                client.request("POST", path, body=json.dumps(body), headers={
                    "Content-Type": "application/json", "X-Omni-Token": self.token,
                })
            response = client.getresponse()
            data = json.load(response)
            if response.status >= 400:
                raise ValueError(data.get("error", f"Workspace returned HTTP {response.status}"))
            return data
        finally:
            client.close()

    def projects(self):
        return self.request("/api/workspace")["projects"]

    def create(self, name):
        return self.request("/api/projects", {"name": name})

    def sessions(self, project):
        return self.request(f"/api/projects/{project}/sessions")["sessions"]

    def status(self, session):
        return self.request(f"/api/sessions/{session}")

    def events(self, session, **filters):
        query = urlencode({key: value for key, value in filters.items() if value is not None})
        return self.request(f"/api/sessions/{session}/telemetry?{query}")

    def configure(self, settings):
        self.request("/api/provider", settings)

    def provider(self):
        return self.about["provider"]

    def models(self, settings, key):
        return self.request("/api/provider/models", {**settings, "api_key": key})

    def credential(self, settings, key, *, remember):
        return self.request("/api/provider/credential", {**settings, "api_key": key,
                            "remember_key": remember, "forget_key": not remember})

    def extensions(self, check):
        return self.request("/api/extensions/check", {}) if check else self.request("/api/extensions")

    def provider_test(self):
        return self.request("/api/provider/test", {})

    def run(self, project, prompt, sample):
        return self.request(f"/api/projects/{project}/sessions", {"prompt": prompt, "sample": sample})["id"]

    def cancel(self, session):
        self.request(f"/api/sessions/{session}/cancel", {})

    def retry(self, session):
        self.request(f"/api/sessions/{session}/retry", {})

    def preview(self, session, start):
        return self.request(f"/api/sessions/{session}/preview", {"start": start})["url"]


class LocalWorkspace:
    def __init__(self, directory, lead=None):
        self.store = lead.store if lead else Store(directory)
        self.credentials = lead.credentials if lead else ProviderCredentials(self.store)
        self.lead = lead

    def projects(self):
        return self.store.projects()

    def create(self, name):
        return self.store.project(name, "")

    def sessions(self, project):
        return self.store.rows("""SELECT id,project_id,prompt,state,stage,branch,created,updated,error FROM sessions
                                WHERE project_id=? ORDER BY created DESC""", (project,))

    def status(self, session):
        result = self.store.summary(session)
        require(result, "Session does not exist")
        return result

    def events(self, session, event=None, **filters):
        require(self.store.row("SELECT id FROM sessions WHERE id=?", (session,)), "Session does not exist")
        if event is not None:
            result = self.store.row("SELECT * FROM events WHERE session_id=? AND id=?", (session, event))
            require(result, "Event does not exist")
            return result
        return self.store.telemetry(session, **filters)

    def configure(self, settings):
        self.lead.configure(settings)

    def provider(self):
        return self.store.setting("provider") or initial_provider(require_model=False)

    def models(self, settings, key):
        return discover_models(provider_settings(settings, require_model=False), self.credentials, key)

    def credential(self, settings, key, *, remember):
        target = provider_settings(settings, require_model=False)
        return self.credentials.update(target, key=key, remember=remember, forget=not remember)

    def extensions(self, check):
        return Extensions(self.store.directory, self.store.redact).describe(check)

    def provider_test(self):
        settings = initial_provider() if environment_provider() else self.provider()
        key = provider_key(settings["kind"]) or self.credentials.get(settings)
        self.store.redact.add(key)
        try:
            return check_provider(settings, key)
        except (ValueError, RuntimeError) as exc:
            raise ValueError(self.store.redact(str(exc))) from None

    def run(self, project, prompt, sample):
        return self.lead.launch(project, prompt, sample=sample)

    def cancel(self, session):
        require(self.store.cancel(session), "Session is inactive or already stopping")

    def retry(self, session):
        self.lead.retry(session)

    def preview(self, session, start):
        return self.lead.change_preview(session, start)


def remote_workspace(directory, port):
    if port is not None:
        return RemoteWorkspace(port)
    marker = directory / ".control-port"
    if not marker.is_file():
        return None
    try:
        remote = RemoteWorkspace(int(marker.read_text(encoding="ascii")))
        identity = Store(directory).setting("workspace_id")
        require(remote.about.get("workspace_id") == identity, "The loopback server belongs to another workspace")
        return remote
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        # A crash may leave a stale endpoint. The workspace lock still guards standalone writes.
        return None


@contextmanager
def workspace(args):
    remote = remote_workspace(args.data, getattr(args, "connect", None))
    if remote is not None:
        yield remote
    elif args.command in ("run", "retry", "preview"):
        with workspace_lock(args.data):
            lead = Lead(args.data)
            try:
                yield LocalWorkspace(args.data, lead)
            finally:
                lead.close()
    else:
        yield LocalWorkspace(args.data)


def project_id(backend, name, create=False):
    projects = backend.projects()
    exact = next((project["id"] for project in projects if project["id"] == name), None)
    if exact:
        return exact
    matches = [project["id"] for project in projects if project["name"] == name]
    require(len(matches) <= 1, "Project name is ambiguous. Use its ID.")
    if matches:
        return matches[0]
    require(create and not IDENTIFIER.fullmatch(name), "Project does not exist")
    require(0 < len(name.strip()) <= 100, "Enter a project name up to 100 characters")
    return backend.create(name.strip())["id"]


def progress(backend, sid):
    previous = None
    while True:
        session = backend.status(sid)
        current = (session["state"], session["stage"],
                   tuple((gate["stage"], gate["state"], (gate["details"] or {}).get("attempt"))
                         for gate in session["gates"]))
        if current != previous:
            stage = LABELS.get(session["stage"], "Lead plan")
            gate = next((gate for gate in session["gates"] if gate["stage"] == session["stage"]), None)
            attempt = (gate.get("details") or {}).get("attempt") if gate else None
            suffix = f" · attempt {attempt}" if attempt else ""
            print(f"{stage} · {session['state']}{suffix}", file=sys.stderr, flush=True)
            previous = current
        if session["state"] in TERMINAL:
            return session
        time.sleep(0.4)


def print_status(session):
    label = LABELS.get(session["stage"], "Lead plan")
    print(f"{session['id']}  {session['state']}  {label}")
    print(f"Branch  {session['branch']}")
    if session.get("bundle"):
        print(f"Build   {session['bundle']}")
    if session.get("preview"):
        print(f"Preview {session['preview']}")
    if session.get("error"):
        print(session["error"], file=sys.stderr)


def logs(backend, args):
    if args.event is not None:
        print(json.dumps(backend.events(args.session, event=args.event), indent=2, ensure_ascii=False))
        return
    filters = {key: getattr(args, key, None) for key in ("role", "stage", "attempt", "before")}
    page = backend.events(args.session, **filters)

    def show(events):
        for event in events:
            if getattr(args, "json", False):
                print(json.dumps(event, ensure_ascii=False), flush=True)
            else:
                actor = " / ".join(filter(None, (event["stage"], event["role"])))
                excerpt = event["excerpt"].strip().replace("\n", " ")
                print(f"{event['id']:>5}  {event['time']}  {actor}  {event['tool'] or event['kind']}  {excerpt}", flush=True)

    show(reversed(page["events"]))
    if args.follow:
        cursor = page["events"][0]["id"] if page["events"] else 0
        filters["before"] = None
        while True:
            terminal = backend.status(args.session)["state"] in TERMINAL
            batch = backend.events(args.session, after=cursor, **filters)["events"]
            if batch:
                show(batch)
                cursor = batch[-1]["id"]
            elif terminal:
                break
            else:
                time.sleep(0.5)


def wait_command(backend, sid, json_output):
    print(f"Session {sid}", file=sys.stderr, flush=True)
    try:
        result = progress(backend, sid)
    except KeyboardInterrupt:
        if isinstance(backend, RemoteWorkspace):
            print("Detached from the running workspace.", file=sys.stderr)
            return 130
        if backend.status(sid)["state"] not in TERMINAL:
            backend.cancel(sid)
            print("Cancellation requested. Waiting for the active attempt to reach a safe boundary.", file=sys.stderr)
            progress(backend, sid)
        return 130
    if isinstance(backend, LocalWorkspace):
        # Closing the standalone Lead also closes its preview.
        result["preview"] = None
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print_status(result)
    return 0 if result["state"] == "completed" else 1


def prompt_credential():
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Provider credential: ")
        except (getpass.GetPassWarning, EOFError):
            raise ValueError("Use an interactive terminal or supply the credential with --from-env") from None


def run_action(args, backend):
    json_output = getattr(args, "json", False)
    if args.command in ("models", "credential"):
        settings = initial_provider(require_model=False) if environment_provider() else backend.provider()
        if args.command == "models":
            result = backend.models(settings, provider_key(settings["kind"]))
            if json_output:
                print(json.dumps(result, ensure_ascii=False))
            elif result["models"]:
                for model in result["models"]:
                    print(model["id"])
            else:
                print("The Foundry project has no model deployments." if settings["kind"] == "foundry"
                      else "The endpoint returned no model IDs.")
        else:
            require(settings["kind"] != "demo", "Choose a model provider in Provider settings or set its environment variables")
            remember = args.action == "save"
            require(remember or not args.from_env, "--from-env applies to credential save")
            key = ""
            if remember:
                key = provider_key(settings["kind"]) if args.from_env else prompt_credential()
                require(key, "No provider credential was supplied")
            result = backend.credential(settings, key, remember=remember)
            print(json.dumps(result) if json_output else
                  "Credential saved in the system keyring." if remember else "Saved credential removed.")
        return 0
    if args.command == "extensions":
        result = backend.extensions(args.check)
        if json_output:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"Config  {result['config']}")
            for skill in result["skills"]:
                print(f"Skill   {skill['name']}  ({', '.join(skill['roles'])})")
            for server in result["mcp"]:
                print(f"MCP     {server['name']}  ({server['transport']})")
                for tool, grants in server["tools"].items():
                    print(f"        {tool}  ({', '.join(grants)})")
                if not server["credentials_ready"]:
                    print("        Credential environment is incomplete.")
                if args.check and not server["ready"]:
                    print("        " + (server.get("error") or
                           "Unavailable read-only tools: " + ", ".join(server["unavailable_tools"])))
            if not (result["skills"] or result["mcp"]):
                print("No Skills or MCP servers configured.")
        return int(args.check and any(not server["ready"] for server in result["mcp"]))
    if args.command == "provider-test":
        if isinstance(backend, RemoteWorkspace) and environment_provider():
            settings = initial_provider()
            backend.configure({**settings, "api_key": provider_key(settings["kind"])})
        result = backend.provider_test()
        print(json.dumps(result) if json_output else f"Model responded via {result['kind']}  {result['model']}")
        return 0
    if args.command in ("projects", "sessions"):
        if args.command == "projects":
            items = backend.projects()
        elif args.project:
            items = backend.sessions(project_id(backend, args.project))
        else:
            items = [session for project in backend.projects() for session in backend.sessions(project["id"])]
        if json_output:
            print(json.dumps(items, ensure_ascii=False))
        else:
            for item in items:
                detail = item.get("name") or f"{item['state']:<10} {item['stage']:<13} {' '.join(item['prompt'].split())[:96]}"
                print(f"{item['id']}  {detail}")
        return 0
    if args.command == "run":
        require(not args.demo or not (args.prompt or args.prompt_file), "Choose a prompt or --demo")
        require(not args.prompt or not args.prompt_file, "Choose a prompt or --prompt-file")
        require(not args.detach or isinstance(backend, RemoteWorkspace), "--detach requires the dashboard service")
        prompt = args.prompt
        if args.prompt_file:
            if args.prompt_file == "-":
                prompt = sys.stdin.read(16_001)
            else:
                with Path(args.prompt_file).open(encoding="utf-8") as source:
                    prompt = source.read(16_001)
        require(args.demo or isinstance(prompt, str) and 0 < len(prompt.strip()) <= 16_000,
                "Provide a request up to 16,000 characters or use --demo")
        require(args.demo or getattr(args, "project", None), "Specify --project for a custom request")
        if not args.demo and environment_provider():
            settings = initial_provider()
            backend.configure({**settings, "api_key": provider_key(settings["kind"])})
        project = project_id(backend, args.project or "Workshop demo", create=True)
        sid = backend.run(project, prompt, args.demo)
        if args.detach:
            print(json.dumps({"id": sid}) if json_output else sid)
            return 0
        return wait_command(backend, sid, json_output)
    sid = args.session
    require(IDENTIFIER.fullmatch(sid), "Enter a valid session ID")
    if args.command == "status":
        session = backend.status(sid)
        if json_output:
            print(json.dumps(session, ensure_ascii=False))
        else:
            print_status(session)
    elif args.command == "logs":
        require(args.event is None or 0 < args.event < 2 ** 63, "Invalid event ID")
        require(args.before is None or 0 < args.before < 2 ** 63, "Invalid log cursor")
        require(args.attempt is None or IDENTIFIER.fullmatch(args.attempt), "Invalid attempt ID")
        logs(backend, args)
    elif args.command == "cancel":
        backend.cancel(sid)
        print(f"Cancellation requested for {sid}")
    elif args.command == "retry":
        require(not args.detach or isinstance(backend, RemoteWorkspace), "--detach requires the dashboard service")
        backend.retry(sid)
        if not args.detach:
            return wait_command(backend, sid, json_output)
        print(json.dumps({"id": sid}) if json_output else sid)
    elif args.command == "preview":
        url = backend.preview(sid, not args.stop)
        print(url or "Preview stopped", flush=True)
        if url and isinstance(backend, LocalWorkspace):
            print("Keep this command running to serve the preview. Press Ctrl+C to stop it.", file=sys.stderr)
            while backend.status(sid)["preview"]:
                time.sleep(1)
    return 0


def parser_for_cli():
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--data", type=Path, default=argparse.SUPPRESS, help="Workspace directory")
    shared.add_argument("--connect", type=int, default=argparse.SUPPRESS, metavar="PORT",
                        help="Connect to an explicit loopback RittaiHarness port")
    shared.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Machine-readable output")
    parser = argparse.ArgumentParser(prog="rittaiharness", description=__doc__, parents=[shared])
    parser.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    parser.add_argument("--doctor", action="store_true", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    options = {
        "serve": "Start the dashboard and loopback API", "doctor": "Check Docker and the role images",
        "extensions": "List or check configured Skills and MCP tools",
        "models": "List Foundry project deployments or model IDs from other providers",
        "credential": "Save or forget the provider credential in the system keyring",
        "provider-test": "Send a connectivity check to the configured model",
        "run": "Run a request or the built-in demo", "projects": "List projects", "sessions": "List sessions",
        "status": "Show the session and build status", "logs": "Read or follow filtered session logs",
        "retry": "Resume at the first unapproved stage", "cancel": "Request cancellation",
        "preview": "Start or stop a completed web preview",
    }
    for name, explanation in options.items():
        command = commands.add_parser(name, help=explanation, parents=[shared])
        if name == "serve":
            command.add_argument("--port", type=int, default=argparse.SUPPRESS)
        if name == "extensions":
            command.add_argument("--check", action="store_true", help="Connect and discover approved MCP tools")
        if name == "credential":
            command.add_argument("action", choices=("save", "forget"))
            command.add_argument("--from-env", action="store_true", help="Save an existing provider environment credential")
        if name in ("run", "sessions"):
            command.add_argument("--project", help="Project name or ID")
        if name == "run":
            command.add_argument("prompt", nargs="?")
            command.add_argument("--prompt-file", help="Read the request from a file or '-' for stdin")
            command.add_argument("--demo", action="store_true", help="Run the bundled sample without an API key")
        if name in ("status", "logs", "retry", "cancel", "preview"):
            command.add_argument("session", metavar="SESSION_ID")
        if name in ("run", "retry"):
            command.add_argument("--detach", action="store_true", help="Queue the run on the dashboard and return")
        if name == "logs":
            command.add_argument("--role", choices=PERSONAS)
            command.add_argument("--stage", choices=("lead", *STAGES))
            command.add_argument("--attempt")
            command.add_argument("--before", type=int, help="Show events older than this ID")
            command.add_argument("--event", type=int, help="Show the complete payload of one event")
            command.add_argument("--follow", action="store_true")
        if name == "preview":
            command.add_argument("--stop", action="store_true", help="Stop an existing dashboard preview")
    return parser


def main(argv=None):
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    args.data = getattr(args, "data", Path(os.environ.get("OMNI_DATA", ".omni-data"))).resolve()
    try:
        if args.command == "doctor" or args.doctor:
            Docker().doctor()
            print("Docker and RittaiHarness images are ready.")
            return
        if args.command in (None, "serve"):
            serve(["--port", str(args.port), "--data", str(args.data)])
            return
        with workspace(args) as backend:
            result = run_action(args, backend)
        raise SystemExit(result)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (ValueError, OSError, RuntimeError, http.client.HTTPException) as exc:
        parser.exit(1, f"rittaiharness: {exc}\n")
