"""Manage Aegis Projects, sessions, and telemetry from a terminal."""
import argparse
import getpass
import http.client
import json
import os
import re
import sqlite3
import sys
import time
import warnings
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlencode
from zipfile import BadZipFile

from .agent_cli import AGENT_CLIS
from .config import default_workspace
from .contracts import LABELS, PERSONAS, STAGES, require
from .continuity import DATABASE, backup_workspace, restore_workspace, user_service
from .credentials import ProviderCredentials
from .engine import Lead, TERMINAL, check_provider, discover_models, prepare_provider
from .extensions import Extensions
from .integrations import environment_provider, foundry_project_endpoint, initial_provider, provider_key, provider_settings
from .preflight import check_workspace
from .sandbox import ContainerCapacity, Docker, detected_capacity, execution_plan, execution_policy
from .server import main as serve
from .server import workspace_lock
from .store import Store
from .terminal import Terminal

IDENTIFIER = re.compile(r"[0-9a-f]{16}\Z")
MISSING = object()
PROVIDER_NAMES = {"demo": "Offline demo", "foundry": "Microsoft Foundry", "azure-gateway": "Azure AI Gateway",
                  "compatible": "Compatible Chat Completions", "local": "Local model",
                  **{kind: name for kind, (name, _) in AGENT_CLIS.items()}}
SETUP_CHOICES = {"1": "claude-code", "2": "github-copilot", "3": "foundry", "4": "local"}
LOCAL_MODEL_ENDPOINT = "http://127.0.0.1:11434/v1"
SESSION_STAGES = {"lead": "Planning", **LABELS}


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
                    "Content-Type": "application/json", "X-Aegis-Token": self.token, "X-Omni-Token": self.token,
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

    def create(self, name, repository=None, branch=None):
        if repository or branch:
            capabilities = self.about.get("capabilities", {})
            require(capabilities.get("github_projects", capabilities.get("github_racedays")),
                    "Restart the Workspace server to connect a GitHub repository")
        return self.request("/api/projects", {"name": name, "github_url": repository, "github_branch": branch})

    def publish(self, session):
        return self.request(f"/api/sessions/{session}/publish", {})

    def budget(self, project, limit=MISSING):
        capabilities = self.about.get("capabilities", {})
        require(capabilities.get("project_budgets", capabilities.get("raceday_budgets")), "Restart Aegis to use project budgets")
        body = MISSING if limit is MISSING else {"budget_usd": limit}
        return self.request(f"/api/projects/{project}/budget", body)

    def sessions(self, project):
        return self.request(f"/api/projects/{project}/sessions")["sessions"]

    def status(self, session):
        return self.request(f"/api/sessions/{session}")

    def events(self, session, **filters):
        query = urlencode({key: value for key, value in filters.items() if value is not None})
        return self.request(f"/api/sessions/{session}/telemetry?{query}")

    def configure(self, settings):
        return self.request("/api/provider", settings)

    def provider(self):
        return self.request("/api/workspace")["provider"]

    def models(self, settings, key):
        return self.request("/api/provider/models", {**settings, "api_key": key})

    def credential(self, settings, key, *, remember):
        return self.request("/api/provider/credential", {**settings, "api_key": key,
                            "remember_key": remember, "forget_key": not remember})

    def extensions(self, check):
        return self.request("/api/extensions/check", {}) if check else self.request("/api/extensions")

    def execution_settings(self, data=MISSING):
        return self.request("/api/execution", data)

    def pressure(self):
        return self.request("/api/execution/pressure")

    def preflight(self, models):
        return self.request("/api/doctor/full", {"models": models})

    def provider_test(self):
        return self.request("/api/provider/test", {})

    def run(self, project, prompt, sample):
        return self.request(f"/api/projects/{project}/sessions", {"prompt": prompt, "sample": sample})["id"]

    def chat(self, session, message):
        result = self.request(f"/api/sessions/{session}/messages", {"text": message})
        message_id = result.get("message_id")
        require(isinstance(message_id, int), "Restart the dashboard with this version to receive terminal chat replies")
        return message_id

    def chat_reply(self, session, message_id):
        return self.request(f"/api/sessions/{session}/messages?reply_to={message_id}")["reply"]

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

    def create(self, name, repository=None, branch=None):
        if self.lead is None and not (repository or branch):
            return self.store.project(name, "")
        return self.lead.create_project(name, repository=repository, branch=branch)

    def publish(self, session):
        return self.lead.publish(session)

    def budget(self, project, limit=MISSING):
        if limit is MISSING:
            return self.store.budget(project)
        with workspace_lock(self.store.directory):
            return self.store.set_budget(project, limit)

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
        return self.lead.configure(settings)

    def provider(self):
        if self.lead:
            return self.lead.provider()
        return self.store.setting("provider") or initial_provider(require_model=False)

    def models(self, settings, key):
        return discover_models(provider_settings(settings, require_model=False), self.credentials, key)

    def credential(self, settings, key, *, remember):
        target = provider_settings(settings, require_model=False)
        return self.credentials.update(target, key=key, remember=remember, forget=not remember)

    def extensions(self, check):
        return Extensions(self.store.directory, self.store.redact).describe(check)

    def execution_settings(self, data=MISSING):
        if data is MISSING:
            policy = execution_policy(self.store.setting("execution"))
        else:
            policy = execution_policy(data)
            self.store.set("execution", policy)
        return execution_plan(policy, detected_capacity(Docker()))

    def pressure(self):
        docker = Docker()
        capacity = ContainerCapacity(lambda: docker.external_usage(None))
        capacity.set_engine_capacity(detected_capacity(docker))
        return capacity.report()

    def preflight(self, models):
        settings = initial_provider(require_model=False) if environment_provider() else self.provider()
        provider_check = self.provider_test if models and settings["kind"] != "demo" else None
        return check_workspace(Docker(), self.store, provider_check)

    def provider_test(self):
        if self.lead:
            return self.lead.test_provider()
        settings = initial_provider() if environment_provider() else self.provider()
        key = provider_key(settings["kind"]) or self.credentials.get(settings)
        self.store.redact.add(key)
        try:
            return check_provider(prepare_provider(settings, self.credentials, key), key)
        except (ValueError, RuntimeError) as exc:
            raise ValueError(self.store.redact(str(exc))) from None

    def run(self, project, prompt, sample):
        return self.lead.launch(project, prompt, sample=sample)

    def chat(self, session, message):
        return self.lead.chat(session, message)

    def chat_reply(self, session, message_id):
        return self.store.chat_reply(session, message_id)

    def cancel(self, session):
        if self.lead:
            self.lead.cancel(session)
        else:
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
    elif args.command in ("chat", "run", "retry", "preview", "new-project", "publish"):
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
    require(0 < len(name.strip()) <= 100, "Enter a Project name up to 100 characters")
    return backend.create(name.strip())["id"]


def configure_environment_provider(backend):
    if environment_provider():
        settings = initial_provider()
        backend.configure({**settings, "api_key": provider_key(settings["kind"])})


class ChatShell:
    def __init__(self, backend, args):
        self.backend = backend
        self.terminal = Terminal(not args.no_animation)
        self.project = args.project
        self.session = None
        self.use_environment_provider = bool(environment_provider())
        if args.session:
            self.attach(args.session)

    def attach(self, sid):
        require(IDENTIFIER.fullmatch(sid), "Enter a valid session ID")
        session = self.backend.status(sid)
        self.project = session["project_id"]
        self.session = sid

    def start(self, request, *, demo=False):
        if not demo:
            require(request, "Enter a request after /run")
            if self.use_environment_provider:
                configure_environment_provider(self.backend)
            settings = self.backend.provider()
            require(settings["kind"] != "demo",
                    "Use /setup to choose a model provider or /demo to try Fieldwork offline")
            require(settings["kind"] != "foundry" or settings.get("has_key") is not False,
                    "Microsoft Foundry needs a credential. Use /setup to enter it")
        project = self.project or ("Workshop demo" if demo else "Workspace")
        identifier = project_id(self.backend, project, create=True)
        self.session = self.backend.run(identifier, request, demo)
        self.project = identifier
        self.terminal.write(f"{self.session}. Ask the lead agent or use /watch.", label="Queued session ", tone="gold")

    def ask(self, message):
        question = self.backend.chat(self.session, message)
        deadline = time.monotonic() + 45
        with self.terminal.waiting("Waiting for Lead Chat") as pause:
            while time.monotonic() < deadline:
                reply = self.backend.chat_reply(self.session, question)
                if reply:
                    self.terminal.write(reply["text"], label="Lead Agent > ")
                    return
                pause(0.4)
        self.terminal.write("The lead agent is working on your question. Check /messages for the reply.")

    def show_status(self, *, watch=False):
        previous = None
        with (self.terminal.waiting("Watching the run") if watch else nullcontext()) as pause:
            while True:
                session = self.backend.status(self.session)
                stage = SESSION_STAGES.get(session["stage"], SESSION_STAGES["lead"])
                passed = sum(gate["state"] == "passed" for gate in session["gates"])
                snapshot = (session["state"], stage, passed)
                if snapshot != previous:
                    self.terminal.status(session["id"], session["state"], stage, passed)
                    previous = snapshot
                if not watch or session["state"] in TERMINAL:
                    for field in ("error", "bundle", "preview"):
                        if session.get(field):
                            self.terminal.write(f"{field.title()}  {session[field]}")
                    return
                pause(0.5)

    def provider_status(self, *, test=False):
        if test and self.use_environment_provider:
            configure_environment_provider(self.backend)
        try:
            settings = (initial_provider(require_model=False) if self.use_environment_provider and not test
                        else self.backend.provider())
        except ValueError as exc:
            self.terminal.write(f"{exc}. Use /setup for guided provider setup.", tone="coral")
            return
        if settings["kind"] == "demo":
            self.terminal.write("No model provider configured. Use /setup to choose a provider or /demo to try Fieldwork offline.")
        elif settings["kind"] == "foundry" and settings.get("has_key") is False:
            self.terminal.write("Microsoft Foundry needs a credential. Use /setup to enter it.")
        elif test:
            result = self.backend.provider_test()
            self.terminal.write(f"{PROVIDER_NAMES[result['kind']]} responded via {result['model']}.")
        else:
            default = "CLI default model" if settings["kind"] in AGENT_CLIS else "automatic deployment routing"
            details = settings.get("model") or default
            self.terminal.write(f"Provider  {PROVIDER_NAMES[settings['kind']]} · {details}")
            self.terminal.write("Use /run to submit work, /provider test to check the connection, or /setup to change it.")

    def setup_foundry(self):
        self.terminal.write("Connect Microsoft Foundry", tone="cyan")
        self.terminal.write("Open https://ai.azure.com, select your project, then copy the Project endpoint and API key from Home.")
        endpoint = input("Project endpoint (Enter to cancel): ").strip()
        if not endpoint:
            return
        settings = provider_settings({"kind": "foundry", "endpoint": endpoint}, require_model=False)
        if foundry_project_endpoint(settings):
            self.terminal.write("Aegis will find the project's chat deployments and choose one for each role.")
        else:
            settings["model"] = input("Deployment name: ").strip()
            settings = provider_settings(settings)
        self.terminal.write("The key entry is hidden.")
        key = prompt_credential().strip()
        return (settings, key) if key else None

    def setup_local_model(self):
        self.terminal.write("Start your local OpenAI-compatible model server on the machine running Aegis.")
        endpoint = input(f"API base URL [{LOCAL_MODEL_ENDPOINT}]: ").strip() or LOCAL_MODEL_ENDPOINT
        settings = provider_settings({"kind": "local", "endpoint": endpoint}, require_model=False)
        model = input("Model ID (Enter to discover): ").strip()
        key = prompt_credential("API key (Enter if your local server doesn't require one): ").strip()
        if not model:
            models = self.backend.models(settings, key)["models"]
            require(models, "Start or download a model on your local server, then try /setup again")
            if len(models) == 1:
                model = models[0]["id"]
            else:
                self.terminal.write("Available model IDs\n" + "\n".join(item["id"] for item in models))
                model = input("Model ID (Enter to cancel): ").strip()
                if not model:
                    return None
        return {**settings, "model": model}, key

    def setup_provider(self, choice):
        if not choice:
            self.terminal.write("Choose a model provider", tone="cyan")
            for option, kind in SETUP_CHOICES.items():
                self.terminal.write(f"  {option}. {PROVIDER_NAMES[kind]}")
            choice = input("Selection (Enter to cancel): ").strip()
        if not choice:
            return
        kind = SETUP_CHOICES.get(choice, choice)
        require(kind in SETUP_CHOICES.values(), "Choose a provider listed in /setup")
        if kind == "foundry":
            connection = self.setup_foundry()
        elif kind == "local":
            connection = self.setup_local_model()
        else:
            self.terminal.write(f"Install {PROVIDER_NAMES[kind]} on the machine running Aegis.")
            login = "Run `claude auth login`" if kind == "claude-code" else "Run `copilot` and enter `/login`"
            self.terminal.write(f"{login} in another terminal if you need to sign in.")
            model = input("Model ID (Enter to use the CLI default): ").strip()
            connection = {"kind": kind, "model": model}, ""
        if connection is None:
            return
        settings, key = connection
        remember = bool(key) and self.backend.provider().get("keyring_available", False) and (
            input("Remember the credential in the system keyring? [y/N]: ").strip().lower() in ("y", "yes"))
        self.terminal.write("Checking the connection with a short model request…")
        configured = self.backend.configure({**settings, "api_key": key, "remember_key": remember, "test_connection": True})
        self.use_environment_provider = False
        result = configured.get("connection_test") or self.backend.provider_test()
        self.terminal.write(f"{PROVIDER_NAMES[kind]} responded via {result['model']}.")
        if remember:
            self.terminal.write("Credential saved in the system keyring.")
        elif key:
            self.terminal.write("The credential stays in the running workspace process.")
        self.terminal.write("Enter what you want to build, or use /run to start a new request here.")

    def command(self, command, arg):
        if command in ("/status", "/watch", "/messages", "/cancel", "/retry"):
            require(self.session, "Start a session or select one with /resume SESSION_ID")
        if command in ("/status", "/watch"):
            self.show_status(watch=command == "/watch")
        elif command == "/run":
            self.start(arg)
        elif command == "/demo":
            self.start("", demo=True)
        elif command == "/setup":
            self.setup_provider(arg)
        elif command == "/provider":
            require(arg in ("", "test"), "Use /provider or /provider test")
            self.provider_status(test=arg == "test")
        elif command == "/resume":
            self.attach(arg)
            self.show_status()
        elif command == "/project":
            require(0 < len(arg) <= 100, "Enter a Project name or ID")
            self.project, self.session = arg, None
            self.terminal.write(f"Project set to {arg}. Enter a brief to start a session.")
        elif command == "/projects":
            projects = self.backend.projects()
            for project in projects:
                self.terminal.write(f"{project['id']}  {project['name']}")
            if not projects:
                self.terminal.write("No Projects yet.")
        elif command == "/sessions":
            require(self.project, "Select a Project with /project NAME or ID")
            sessions = self.backend.sessions(project_id(self.backend, self.project))
            for session in sessions[:12]:
                request = " ".join(session["prompt"].split())[:72]
                self.terminal.write(f"{session['id']}  {session['state']:9}  {request}")
            if not sessions:
                self.terminal.write("No sessions yet.")
        elif command == "/messages":
            for message in self.backend.status(self.session)["messages"][-12:]:
                engineer = message["speaker"] == "lead"
                speaker = "Lead Agent" if engineer else message["speaker"].title()
                self.terminal.write(message["text"], label=f"{speaker} > ", tone="mint" if engineer else "lilac")
        elif command == "/cancel":
            self.backend.cancel(self.session)
            self.terminal.write("Cancellation requested.")
        elif command == "/retry":
            self.backend.retry(self.session)
            self.terminal.write(f"Queued {self.session} to retry.")
        elif command == "/help":
            self.terminal.write("""Enter a brief to start a session. With a session selected, plain text goes to Lead Chat.
  /run REQUEST     Start a new session or change the requirements
  /demo            Start the offline Fieldwork demo
  /setup           Choose and connect a model provider
  /provider        Show the selected provider or use /provider test
  /project NAME    Choose a Project for your next session
  /projects        List Projects
  /sessions        List recent sessions in the current project
  /resume ID       Select a previous session for Lead Chat
  /status /watch   Check the selected session or watch it finish
  /messages        Read Lead Chat and session updates
  /cancel /retry   Request cancellation or retry the selected session
  exit, /exit      Leave the terminal""")
        else:
            self.terminal.write("Unknown command. Enter /help for commands.")

    def run(self):
        readline_enabled = True
        try:
            import readline  # Enables native cursor movement and in-memory input history.
        except ImportError:
            readline_enabled = False
        self.terminal.banner()
        if self.session:
            self.terminal.write("Ask the lead agent about this session.")
            self.show_status()
        else:
            if self.project:
                self.terminal.write(f"Project  {self.project}")
            self.provider_status()
        while True:
            try:
                line = input(self.terminal.prompt(self.session is not None, readline=readline_enabled)).strip()
                if not line:
                    continue
                if line.casefold() == "exit":
                    return
                if line.startswith("/"):
                    command, *args = line.split(maxsplit=1)
                    if command in ("/exit", "/quit"):
                        return
                    self.command(command, args[0] if args else "")
                elif self.session:
                    self.ask(line)
                else:
                    self.start(line)
            except EOFError:
                self.terminal.write()
                return
            except KeyboardInterrupt:
                self.terminal.write("\nType exit to leave or /cancel to stop the selected run.")
            except (ValueError, OSError, RuntimeError, http.client.HTTPException, sqlite3.Error) as exc:
                self.terminal.write(exc, label="Error: ", tone="coral")


def progress(backend, sid):
    previous = None
    while True:
        session = backend.status(sid)
        current = (session["state"], session["stage"],
                   tuple((gate["stage"], gate["state"], (gate["details"] or {}).get("attempt"))
                         for gate in session["gates"]))
        if current != previous:
            stage = SESSION_STAGES.get(session["stage"], SESSION_STAGES["lead"])
            gate = next((gate for gate in session["gates"] if gate["stage"] == session["stage"]), None)
            attempt = (gate.get("details") or {}).get("attempt") if gate else None
            suffix = f" · attempt {attempt}" if attempt else ""
            print(f"{stage} · {session['state']}{suffix}", file=sys.stderr, flush=True)
            previous = current
        if session["state"] in TERMINAL:
            return session
        time.sleep(0.4)


def print_status(session):
    label = SESSION_STAGES.get(session["stage"], SESSION_STAGES["lead"])
    print(f"{session['id']}  {session['state']}  {label}")
    print(f"Branch  {session['branch']}")
    if session.get("github_commit"):
        print(f"GitHub commit  {session['github_commit']}")
    if session.get("github_published"):
        print(f"GitHub branch  {session['github_published']}")
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
                action = " · ".join(filter(None, (event["tool"] or event["kind"], event.get("model"))))
                print(f"{event['id']:>5}  {event['time']}  {actor}  {action}  {excerpt}", flush=True)

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


def prompt_credential(prompt="Provider credential: "):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except (getpass.GetPassWarning, EOFError):
            raise ValueError("Use an interactive terminal or supply the credential with --from-env") from None


def run_action(args, backend):
    json_output = getattr(args, "json", False)
    if args.command == "budget":
        project = project_id(backend, args.project)
        limit = None if args.clear else getattr(args, "limit", MISSING)
        result = backend.budget(project, limit)
        if json_output:
            print(json.dumps(result, ensure_ascii=False))
        else:
            cap = result["budget_usd"]
            print(f"Model budget  ${cap:.2f}" if cap is not None else "Model budget  Unset")
            print(f"Model spend   ${result['model_cost']:.4f} (${result['vendor_cost']:.4f} in vendor charges)")
            if cap is not None:
                print(f"Remaining     ${result['remaining_usd']:.4f}" + (" · Budget reached" if result["reached"] else ""))
            if result["unpriced_calls"]:
                print(f"Unpriced model calls  {result['unpriced_calls']}")
            if result["budget_hold"]:
                print(result["budget_hold"])
        return 0
    if args.command == "doctor":
        result = backend.preflight(not args.local_only)
        if json_output:
            print(json.dumps(result, indent=2))
        else:
            for item in result["checks"]:
                print(f"{item['status']:<7} {item['name']} · {item['detail']}")
        return int(not result["ready"])
    if args.command == "execution":
        if args.pressure:
            require(not args.apply, "Choose either applying settings or viewing pressure")
            print(json.dumps(backend.pressure(), indent=2))
            return 0
        data = MISSING
        if args.apply == "-":
            data = json.loads(sys.stdin.read(40_001))
        elif args.apply:
            with Path(args.apply).open(encoding="utf-8") as source:
                data = json.loads(source.read(40_001))
        if data is not MISSING:
            require(isinstance(data, dict), "Execution settings must be a JSON object")
        policy = backend.execution_settings(data)
        if json_output:
            print(json.dumps(policy, indent=2))
        else:
            capacity = policy["capacity"]
            if capacity:
                print(f"Docker engine  {capacity['cpu']} CPU  {capacity['ram']} MiB")
            else:
                print("Docker unavailable. Showing reference sizes.")
            print(f"Concurrent sessions  {policy['sessions']}")
            if policy.get("adaptive_sessions"):
                print(f"Adaptive concurrency  On · local limit {policy.get('effective_sessions', 'resolved by Workspace')}")
            if policy.get("deep_research"):
                print("Deep Research  On · up to six evidence rounds per attempt")
            if "model_slots" in policy:
                print(f"Maximum simultaneous model requests  {policy['model_slots']}")
            print("Independent reviewers per gate  " + ("2" if policy["parallel_reviews"] else "1"))
            for task, limits in policy["limits"].items():
                sizing = "custom" if task in policy["overrides"] else "auto"
                print(f"{task:<14} {limits['cpu']:g} CPU  {limits['ram']} MiB  {limits['pids']} processes  {sizing}")
        return 0
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
            require(settings["kind"] not in AGENT_CLIS, "This provider uses its own CLI sign-in")
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
        if isinstance(backend, RemoteWorkspace):
            configure_environment_provider(backend)
        result = backend.provider_test()
        print(json.dumps(result) if json_output else f"Model responded via {result['kind']}  {result['model']}")
        return 0
    if args.command == "new-project":
        project = backend.create(args.name, args.github, args.branch)
        print(json.dumps(project, ensure_ascii=False) if json_output else f"{project['id']}  {project['name']}")
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
        require(args.demo or getattr(args, "project", None), "Specify --project for a custom session")
        if not args.demo:
            configure_environment_provider(backend)
        project = project_id(backend, args.project or "Workshop demo", create=True)
        sid = backend.run(project, prompt, args.demo)
        if args.detach:
            print(json.dumps({"id": sid}) if json_output else sid)
            return 0
        return wait_command(backend, sid, json_output)
    sid = args.session
    require(IDENTIFIER.fullmatch(sid), "Enter a valid session ID")
    if args.command == "publish":
        published = backend.publish(sid)
        print(json.dumps(published, ensure_ascii=False) if json_output else published["url"])
        return 0
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
                        help="Connect to an explicit loopback Aegis port")
    shared.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Machine-readable output")
    parser = argparse.ArgumentParser(prog="aegis", description=__doc__, parents=[shared])
    parser.add_argument("--port", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--doctor", action="store_true", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    options = {
        "chat": "Open the interactive terminal and Lead Chat",
        "serve": "Start Workspace and its loopback API", "doctor": "Check Docker and the role images",
        "extensions": "List or check configured Skills and MCP tools",
        "execution": "Show or apply task limits and parallel execution settings",
        "backup": "Back up the stopped workspace database, Git history, and builds",
        "restore": "Restore a backup into a new workspace directory",
        "service": "Generate an optional systemd user service",
        "models": "List Foundry project deployments or model IDs from other providers",
        "credential": "Save or forget the provider credential in the system keyring",
        "provider-test": "Send a connectivity check to the configured model",
        "new-project": "Create a Project and optionally connect a GitHub repository",
        "budget": "Show, set, or clear a Project model budget in USD",
        "run": "Start a session or the built-in demo", "projects": "List Projects", "sessions": "List sessions",
        "status": "Show session and build status", "logs": "Read or follow filtered session telemetry",
        "retry": "Resume at the first unapproved stage", "cancel": "Request cancellation",
        "preview": "Start or stop a completed web preview",
        "publish": "Push an approved session commit to its own GitHub branch",
    }
    for name, explanation in options.items():
        command = commands.add_parser(name, help=explanation, parents=[shared])
        command.set_defaults(command=name)
        if name == "serve":
            command.add_argument("--port", type=int, default=argparse.SUPPRESS)
        if name == "chat":
            selection = command.add_mutually_exclusive_group()
            selection.add_argument("--project", metavar="PROJECT",
                                   help="Project name or ID for new sessions")
            selection.add_argument("--session", metavar="SESSION_ID",
                                   help="Resume Lead Chat for a session")
            command.add_argument("--no-animation", action="store_true", help="Show a static logo and plain wait messages")
        if name == "extensions":
            command.add_argument("--check", action="store_true", help="Connect and discover approved MCP tools")
        if name == "execution":
            command.add_argument("--apply", metavar="FILE", help="Apply execution settings from JSON or '-' for stdin")
            command.add_argument("--pressure", action="store_true", help="Show Docker usage and workspace reservations")
        if name == "doctor":
            command.add_argument("--full", action="store_true", help="Probe workspace mounts, caches, and the model provider")
            command.add_argument("--local-only", action="store_true", help="Skip the model request during full preflight")
        if name == "backup":
            command.add_argument("output", type=Path, help="New backup ZIP path")
        if name == "restore":
            command.add_argument("archive", type=Path, help="Backup ZIP to restore")
        if name == "service":
            command.add_argument("--write", type=Path, help="Write to a new unit file")
            command.add_argument("--docker-context", default=os.environ.get("DOCKER_CONTEXT"), help="Pin the Docker context in the unit")
            command.add_argument("--port", type=int, default=argparse.SUPPRESS)
        if name == "credential":
            command.add_argument("action", choices=("save", "forget"))
            command.add_argument("--from-env", action="store_true", help="Save an existing provider environment credential")
        if name in ("run", "sessions"):
            command.add_argument("--project", metavar="PROJECT",
                                 help="Project name or ID")
        if name == "run":
            command.add_argument("prompt", nargs="?")
            command.add_argument("--prompt-file", help="Read the request from a file or '-' for stdin")
            command.add_argument("--demo", action="store_true", help="Run the bundled sample without an API key")
        if name == "new-project":
            command.add_argument("name")
            command.add_argument("--github", metavar="URL", help="GitHub HTTPS repository URL")
            command.add_argument("--branch", help="Source branch. Defaults to the repository default branch.")
        if name == "budget":
            command.add_argument("--project", dest="project", required=True, metavar="PROJECT")
            budget = command.add_mutually_exclusive_group()
            budget.add_argument("--limit", type=float, default=argparse.SUPPRESS, metavar="USD")
            budget.add_argument("--clear", action="store_true")
        if name in ("status", "logs", "retry", "cancel", "preview", "publish"):
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
    args.data = getattr(args, "data", default_workspace()).resolve()
    if (args.command is None and not hasattr(args, "port") and not getattr(args, "json", False)
            and sys.stdin.isatty() and sys.stdout.isatty()):
        args.command, args.project, args.session, args.no_animation = "chat", None, None, False
    port = getattr(args, "port", 8765)
    try:
        if args.doctor or args.command == "doctor" and not args.full:
            Docker().doctor()
            print("Docker and Aegis images are ready.")
            return
        if args.command in (None, "serve"):
            serve(["--port", str(port), "--data", str(args.data)])
            return
        if args.command == "backup":
            require((args.data / DATABASE).is_file(), "The workspace database does not exist")
            with workspace_lock(args.data):
                path = backup_workspace(args.data, args.output.absolute())
            print(path)
            return
        if args.command == "restore":
            print(restore_workspace(args.archive.resolve(strict=True), args.data))
            return
        if args.command == "service":
            content = user_service(sys.argv[0], args.data, port, args.docker_context)
            if args.write:
                with args.write.open("x", encoding="utf-8") as unit:
                    unit.write(content)
                print(args.write.absolute())
            else:
                print(content, end="")
            return
        with workspace(args) as backend:
            if args.command == "chat":
                ChatShell(backend, args).run()
                return
            result = run_action(args, backend)
        raise SystemExit(result)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (ValueError, OSError, RuntimeError, http.client.HTTPException, sqlite3.Error, BadZipFile) as exc:
        parser.exit(1, f"aegis: {exc}\n")
