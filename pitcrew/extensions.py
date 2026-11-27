"""Workspace-owned Skill files and role-scoped MCP permissions."""
import http.client
import json
import os
import re
import tempfile
import time
import tomllib
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .config import environment
from .contracts import INJECT, PERSONAS, require
from .mcp import Client, remaining

NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
ENV = re.compile(r"[A-Z_][A-Z_0-9]{0,63}\Z")
PRIVILEGED_TOOL = re.compile(r"(?:^|[_.-])(?:shell|terminal|exec|exploit|exfiltrate|intrude)(?:$|[_.-])", re.I)
ROLE_SKILL_LIMIT = 36_000
RESULT_LIMIT = 12_000
KEY_HEADERS = ("api-key", "x-api-key", "Ocp-Apim-Subscription-Key")
BROWSER_SKILLS = "browser-skills.json"
BROWSER_SKILL_BYTES = 1_600_000


def configuration(directory):
    override = environment("PITCREW_EXTENSIONS")
    return (Path(override).resolve(), False) if override else (directory / "extensions.toml", True)


def bounded_text(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, f"{path.name} exceeds its size limit")
    return data.decode("utf-8")


def roles(value):
    require(isinstance(value, list) and value and all(isinstance(role, str) and role in PERSONAS for role in value),
            "Extension roles must name factory roles")
    return list(dict.fromkeys(value))


def local_path(root, value):
    require(isinstance(value, str) and 0 < len(value) <= 1000, "Extension path is required")
    return (root / value).resolve(strict=True)


def skill_name(text):
    meta = text.split("\n---\n", 1)[0]
    match = re.search(r"^name:[ \t]*([a-z0-9-]+)[ \t]*$", meta, re.M)
    require(text.startswith("---\n") and "\n---\n" in text and match and NAME.fullmatch(match[1])
            and len(match[1]) <= 64 and re.search(r"^description:", meta, re.M),
            "SKILL.md needs Agent Skills name and description frontmatter")
    return match[1]


def browser_skill(files, grants):
    require(isinstance(files, dict) and "SKILL.md" in files and len(files) <= 9,
            "Upload SKILL.md and up to eight text reference files")
    for path, content in files.items():
        parts = path.split("/")
        require(len(path) <= 200 and all(part and not part.startswith(".") for part in parts)
                and not re.search(r"[\\\x00-\x1f\x7f]", path)
                and PurePosixPath(path).as_posix() == path, "Use relative Skill filenames without hidden or parent folders")
        require(isinstance(content, str) and "\0" not in content
                and len(content.encode()) <= (22_000 if path == "SKILL.md" else 9000),
                f"{path} must be UTF-8 text within its size limit")
    text = files["SKILL.md"].replace("\r\n", "\n")
    return {"name": skill_name(text), "roles": roles(grants), "text": text,
            "resources": {key: value for key, value in files.items() if key != "SKILL.md"}}


def atomic_text(path, content):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def skill_from(root, item):
    require(isinstance(item, dict) and set(item) <= {"path", "roles", "resources"}, "Invalid Skill entry")
    directory = local_path(root, item.get("path"))
    if directory.is_file():
        require(directory.name == "SKILL.md", "A Skill path must point to a directory or SKILL.md")
        directory = directory.parent
    require(directory.is_dir(), "Skill directory does not exist")
    file = (directory / "SKILL.md").resolve(strict=True)
    require(file.is_relative_to(directory), "SKILL.md must resolve inside its Skill directory")
    text = bounded_text(file, 22_000).replace("\r\n", "\n")
    name = skill_name(text)
    selected = item.get("resources", [])
    require(isinstance(selected, list) and len(selected) <= 8, "Select at most eight Skill resources")
    resources = {}
    for relative in selected:
        require(isinstance(relative, str) and not Path(relative).is_absolute()
                and all(not part.startswith(".") for part in Path(relative).parts), "Use a relative Skill resource path")
        resource = local_path(directory, relative)
        require(resource.is_relative_to(directory) and resource.is_file(), "Skill resource must stay inside its Skill directory")
        resources[relative] = bounded_text(resource, 9000)
    return {"name": name, "roles": roles(item.get("roles")), "text": text, "resources": resources}


def server_from(root, item):
    require(isinstance(item, dict) and set(item) <= {"name", "url", "command", "cwd", "env",
                                                      "token_env", "api_key_env", "key_header", "tools", "isolation", "oauth"},
            "Invalid MCP server entry")
    name = item.get("name")
    require(isinstance(name, str) and len(name) <= 48 and NAME.fullmatch(name), "Invalid MCP server name")
    tools = item.get("tools")
    require(isinstance(tools, dict) and 1 <= len(tools) <= 24, "List approved MCP tools with role assignments")
    require(all(TOOL.fullmatch(tool) and not PRIVILEGED_TOOL.search(tool) for tool in tools),
            "Invalid or privileged MCP tool name")
    config = {"name": name, "tools": {tool: roles(grants) for tool, grants in tools.items()}}
    require(("url" in item) != ("command" in item), "Choose one MCP transport per server")
    if "url" in item:
        url = item["url"]
        require(isinstance(url, str) and 0 < len(url) <= 1000 and not re.search(r"\s", url), "Invalid MCP URL")
        location = urlsplit(url)
        loopback = location.hostname in ("localhost", "127.0.0.1", "::1")
        require(location.hostname and not any((location.username, location.password, location.query, location.fragment))
                and (location.scheme == "https" or location.scheme == "http" and loopback),
                "MCP URL must use HTTPS or loopback HTTP without embedded credentials")
        require(not {"cwd", "env", "isolation"} & item.keys(), "Process options apply only to a stdio MCP server")
        env = [item[key] for key in ("token_env", "api_key_env") if key in item]
        key_header = item.get("key_header", "api-key")
        require(key_header in KEY_HEADERS, "Unsupported MCP credential header")
        config.update(url=url, token_env=item.get("token_env"), api_key_env=item.get("api_key_env"), key_header=key_header)
        if "oauth" in item:
            oauth = item["oauth"]
            require(location.scheme == "https" and not env and isinstance(oauth, dict)
                    and set(oauth) <= {"client_id", "scopes"}, "MCP OAuth requires HTTPS and its own authentication configuration")
            client_id, scopes = oauth.get("client_id"), oauth.get("scopes", [])
            require(client_id is None or isinstance(client_id, str) and 0 < len(client_id) <= 1000
                    and not re.search(r"[\x00-\x20\x7f]", client_id), "Invalid OAuth client ID")
            require(isinstance(scopes, list) and len(scopes) <= 24 and all(
                isinstance(scope, str) and re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]{1,180}", scope) for scope in scopes),
                "Enter OAuth scopes as a list of individual scope names")
            config["oauth"] = {"client_id": client_id, "scopes": scopes}
    else:
        command = item["command"]
        require(isinstance(command, list) and 1 <= len(command) <= 16 and
                all(isinstance(part, str) and part and len(part) <= 1000 and "\0" not in part for part in command),
                "MCP command must be an explicit argument array")
        require(not {"token_env", "api_key_env", "key_header", "oauth"} & item.keys(), "HTTP credentials apply only to an HTTP MCP server")
        cwd = local_path(root, item.get("cwd", "."))
        require(cwd.is_dir(), "MCP working directory must exist")
        env = item.get("env", [])
        isolation = item.get("isolation", "sandbox")
        require(isolation in ("sandbox", "host"), "Choose sandbox or trusted host execution for stdio MCP")
        if isolation == "sandbox":
            bundles = root / "mcp"
            require(cwd.is_relative_to(bundles) and cwd != bundles, "Sandboxed stdio MCP needs a dedicated cwd under mcp/")
            require(command[0] in ("python", "python3", "node"), "Sandboxed stdio MCP needs python, python3, or node")
            require(env == [], "Sandboxed stdio MCP runs without host credential variables")
        config.update(command=command, cwd=cwd, isolation=isolation)
    require(isinstance(env, list) and len(env) <= 16 and
            all(isinstance(variable, str) and ENV.fullmatch(variable) for variable in env),
            "MCP credentials must use named environment variables")
    config["env"] = env
    return config


class Extensions:
    def __init__(self, directory, redact, *, docker=None, workspace=None, resources=None,
                 oauth=None, config_text=None, browser_skills=None):
        self.path, self.config_writable = configuration(directory)
        self.redact = redact
        self.oauth = oauth
        self.docker, self.workspace, self.resources = docker, workspace, resources
        self.browser_path = directory / BROWSER_SKILLS
        self.config_text = config_text if config_text is not None else (
            bounded_text(self.path, 64_000) if self.path.exists() or not self.config_writable else "")
        data = tomllib.loads(self.config_text)
        require(set(data) <= {"skills", "mcp"}, "Unknown extension configuration key")
        for key in ("skills", "mcp"):
            entries = data.get(key, [])
            require(isinstance(entries, list) and len(entries) <= 12, f"Configure at most twelve {key} entries")
        local_skills = [skill_from(self.path.parent, item) for item in data.get("skills", [])]
        require(len({entry["name"] for entry in local_skills}) == len(local_skills), "Skill names must be unique")
        self.browser_skills = browser_skills if browser_skills is not None else (
            json.loads(bounded_text(self.browser_path, BROWSER_SKILL_BYTES)) if self.browser_path.exists() else {})
        managed = {name: browser_skill(item["files"], item["roles"]) for name, item in self.browser_skills.items()}
        require(all(name == item["name"] for name, item in managed.items()), "Invalid browser Skill name")
        self.skills = list(({item["name"]: item for item in local_skills} | managed).values())
        require(len(self.skills) <= 12, "Configure at most twelve Skills")
        self.servers = [server_from(self.path.parent, item) for item in data.get("mcp", [])]
        for entries in (self.skills, self.servers):
            require(len({entry["name"] for entry in entries}) == len(entries), "Extension names must be unique")
        for role in PERSONAS:
            require(len(self.server_grants(role)) <= 4, "A role can use at most four MCP servers")
            require(len(self.guidance(role).encode()) <= ROLE_SKILL_LIMIT, "Role Skill context exceeds its size limit")
        for server in self.servers:
            for variable in server["env"]:
                self.redact.add(os.environ.get(variable))

    def guidance(self, *roles):
        blocks = []
        for skill in self.skills:
            if any(role in skill["roles"] for role in roles):
                references = "\n\n".join(f"Resource {path}\n{text}" for path, text in skill["resources"].items())
                blocks.append(f"Skill {skill['name']}\n{skill['text']}\n{references}".rstrip())
        return self.redact("\n\n".join(blocks))

    def server_grants(self, role):
        return [(server, {tool for tool, grants in server["tools"].items() if role in grants})
                for server in self.servers if any(role in grants for grants in server["tools"].values())]

    def sandbox_profiles(self, role):
        return [self.resources for server, _ in self.server_grants(role) if server.get("isolation") == "sandbox"]

    def describe(self, check=False):
        result = {"config": str(self.path), "skills": [
            {key: skill[key] for key in ("name", "roles")} | {"resources": list(skill["resources"])}
            for skill in self.skills], "mcp": []}
        for server in self.servers:
            item = {"name": server["name"], "transport": "http" if "url" in server else "stdio",
                    "tools": server["tools"], "credentials_ready": all(os.environ.get(key) for key in server["env"])}
            if "isolation" in server:
                item["isolation"] = server["isolation"]
            if "oauth" in server:
                item["oauth"] = True
                item["credentials_ready"] = bool(self.oauth and self.oauth.connected(server))
            if check:
                try:
                    with self.connection(server, set(server["tools"]), time.monotonic() + 10) as (_, tools):
                        item["available_tools"] = sorted(tools)
                        item["unavailable_tools"] = sorted(set(server["tools"]) - set(tools))
                        item["ready"] = not item["unavailable_tools"]
                except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
                    item.update(ready=False, error=self.redact(str(exc)))
            result["mcp"].append(item)
        return result

    @contextmanager
    def connection(self, server, allowed, deadline):
        credentials = {variable: os.environ.get(variable) for variable in server["env"]}
        require(all(value and len(value) <= 8192 and "\r" not in value and "\n" not in value
                    for value in credentials.values()), "MCP server credential environment is incomplete or invalid")
        if "oauth" in server:
            require(self.oauth, "Connect this MCP server in the dashboard before using it")
            credentials["oauth_access_token"] = self.oauth.access_token(server)
        client = Client(server, credentials, docker=self.docker, workspace=self.workspace, resources=self.resources)
        try:
            client.initialize(deadline)
            catalog = {}
            for name, tool in client.catalog(deadline, allowed).items():
                annotations = tool.get("annotations", {})
                schema = tool.get("inputSchema")
                if (isinstance(annotations, dict) and annotations.get("readOnlyHint") is True
                        and annotations.get("destructiveHint") is not True and isinstance(schema, dict)
                        and schema.get("type") == "object" and len(json.dumps(schema)) <= 6500):
                    description = str(tool.get("description") or "")[:600]
                    if not INJECT.search(description) and not INJECT.search(json.dumps(schema)):
                        catalog[name] = self.redact({"description": description, "input_schema": schema})
            yield client, catalog
        finally:
            client.close()

    @contextmanager
    def tools(self, role, deadline, report):
        with ExitStack() as stack:
            available = {}
            for server, allowed in self.server_grants(role):
                try:
                    end = time.monotonic() + min(9, remaining(deadline))
                    client, catalog = stack.enter_context(self.connection(server, allowed, end))
                    for name, descriptor in catalog.items():
                        available[f"{server['name']}.{name}"] = (client, name, descriptor)
                    report(server["name"], {"tools": sorted(catalog), "unavailable": sorted(allowed - catalog.keys())})
                except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
                    report(server["name"], {"error": self.redact(str(exc))})
            yield available

    def invoke(self, available, name, arguments, deadline):
        require(isinstance(name, str) and name in available, "MCP tool is outside this role's approved catalog")
        require(isinstance(arguments, dict) and len(json.dumps(arguments).encode()) <= 9000,
                "MCP arguments must be a small JSON object")
        require(self.redact(arguments) == arguments, "MCP arguments contain a credential")
        client, tool, _ = available[name]
        result = client.invoke(tool, arguments, time.monotonic() + min(20, remaining(deadline)))
        text = [item["text"] for item in result.get("content", []) if isinstance(item, dict)
                and item.get("type") == "text" and isinstance(item.get("text"), str)]
        data = result.get("structuredContent")
        output = self.redact("\n".join(text) or (json.dumps(data, ensure_ascii=False) if data is not None else ""))
        return {"output": output[:RESULT_LIMIT], "truncated": len(output) > RESULT_LIMIT,
                "is_error": result.get("isError") is True}
