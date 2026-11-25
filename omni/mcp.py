"""Bounded MCP client for explicitly configured stdio and Streamable HTTP servers."""
import contextlib
import http.client
import json
import os
import secrets
import signal
import subprocess
import time
from urllib.parse import urlsplit

from .contracts import require
from .integrations import public_socket
from .sandbox import Docker, RoleChannel

VERSION = "2025-11-25"
VERSIONS = {VERSION, "2025-06-18", "2025-03-26", "2024-11-05"}
LIMIT = 500_000


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("MCP request timed out")
    return seconds


class Stdio:
    def __init__(self, config, credentials, docker=None, workspace=None, resources=None):
        sandboxed = config.get("isolation") != "host"
        self.docker = (docker or Docker()) if sandboxed else None
        self.container = "rittaiharness-mcp-" + secrets.token_hex(8) if sandboxed else None
        if sandboxed:
            args = self.docker.args(self.container, workspace or self.container, "mcp", source=config["cwd"],
                                    resources=resources, caches=False)
            command = [*args[:-1], "--workdir=/input", args[-1], *config["command"]]
            options = {}
        else:
            env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if key in os.environ}
            env.update(credentials)
            command = config["command"]
            options = {"cwd": config["cwd"], "env": env}
        self.process = subprocess.Popen(command, **options, start_new_session=True,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        bufsize=0)
        self.pipe = RoleChannel(self.process, limit=LIMIT)

    def exchange(self, payload, deadline):
        self.pipe.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n", deadline)
        if "method" in payload and "id" in payload:
            while True:
                yield json.loads(self.pipe.read(deadline))

    def close(self):
        try:
            self.close_process()
        finally:
            if self.docker:
                self.docker.remove(self.container)

    def close_process(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.process.stdout.close()


class StreamableHTTP:
    def __init__(self, config, credentials):
        self.location = urlsplit(config["url"])
        self.headers = {"Accept": "application/json, text/event-stream", "Accept-Encoding": "identity"}
        if token := config.get("token_env"):
            self.headers["Authorization"] = "Bearer " + credentials[token]
        if token := credentials.get("oauth_access_token"):
            self.headers["Authorization"] = "Bearer " + token
        self.oauth = "oauth" in config
        if api_key := config.get("api_key_env"):
            self.headers[config["key_header"]] = credentials[api_key]
        self.session_id = None
        self.version = None

    @contextlib.contextmanager
    def request(self, method, deadline, message=None):
        headers = self.headers.copy()
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        body = None if message is None else json.dumps(message, ensure_ascii=False).encode()
        if body is not None:
            require(len(body) <= LIMIT, "MCP request exceeded its size limit")
            headers["Content-Type"] = "application/json"
        url = self.location
        connection = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        with contextlib.closing(connection(url.hostname, url.port, timeout=remaining(deadline))) as client:
            if self.oauth:
                client._create_connection = public_socket
            client.connect()
            sock = client.sock
            sock.settimeout(remaining(deadline))
            client.request(method, url.path or "/", body=body, headers=headers)
            sock.settimeout(remaining(deadline))
            with client.getresponse() as response:
                yield response, sock

    def exchange(self, payload, deadline):
        with self.request("POST", deadline, payload) as (response, sock):
            require(200 <= response.status < 300, f"MCP endpoint returned HTTP {response.status}")
            if payload.get("method") == "initialize" and (session := response.getheader("Mcp-Session-Id")):
                require(0 < len(session) <= 256 and all(32 < ord(char) < 127 for char in session),
                        "Invalid MCP session ID")
                self.session_id = session
            if response.status in (202, 204):
                return
            media = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
            require(media in ("application/json", "text/event-stream"), "Unsupported MCP response content type")
            buffer = bytearray()
            event_data = []
            used = 0
            while True:
                if response.isclosed():
                    chunk = b""
                else:
                    sock.settimeout(remaining(deadline))
                    chunk = response.read1(8192)
                used += len(chunk)
                require(used <= LIMIT, "MCP response exceeded its size limit")
                buffer.extend(chunk)
                if media == "text/event-stream":
                    while (end := buffer.find(b"\n")) >= 0:
                        line = bytes(buffer[:end]).removesuffix(b"\r")
                        del buffer[:end + 1]
                        if not line and event_data:
                            yield json.loads(b"\n".join(event_data))
                            event_data.clear()
                        elif line.startswith(b"data:"):
                            event_data.append(line[5:].lstrip(b" "))
                if not chunk:
                    if media == "application/json":
                        yield json.loads(buffer)
                    elif event_data:
                        yield json.loads(b"\n".join(event_data))
                    return

    def close(self):
        if self.session_id:
            with self.request("DELETE", time.monotonic() + 2):
                pass


class Client:
    def __init__(self, config, credentials, *, docker=None, workspace=None, resources=None):
        self.transport = (StreamableHTTP(config, credentials) if "url" in config
                          else Stdio(config, credentials, docker, workspace, resources))
        self.sequence = 0

    def exchange(self, method, params, deadline, *, notify=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            self.sequence += 1
            message["id"] = self.sequence
        with contextlib.closing(self.transport.exchange(message, deadline)) as responses:
            for response in responses:
                require(isinstance(response, dict) and response.get("jsonrpc") == "2.0", "Invalid MCP response")
                if "method" in response:
                    if "id" in response:
                        answer = {"jsonrpc": "2.0", "id": response["id"]}
                        if response["method"] == "ping":
                            answer["result"] = {}
                        else:
                            answer["error"] = {"code": -32601, "message": "Client capability unavailable"}
                        with contextlib.closing(self.transport.exchange(answer, deadline)) as reply:
                            for _ in reply:
                                pass
                    continue
                if response.get("id") != message.get("id"):
                    continue
                if error := response.get("error"):
                    require(isinstance(error, dict), "Invalid MCP RPC error")
                    raise ValueError(f"MCP {method} rejected: {str(error.get('message', 'Unknown error'))[:350]}")
                result = response.get("result")
                require(isinstance(result, dict), "MCP response must be an object")
                return result
        require(not message.get("id"), "MCP server returned no result")
        return {}

    def initialize(self, deadline):
        result = self.exchange("initialize", {"protocolVersion": VERSION, "capabilities": {},
                                             "clientInfo": {"name": "RittaiHarness", "version": "0.1.0"}}, deadline)
        version = result.get("protocolVersion")
        require(version in VERSIONS, "MCP server selected an unsupported protocol version")
        if isinstance(self.transport, StreamableHTTP):
            self.transport.version = version
        self.exchange("notifications/initialized", {}, deadline, notify=True)
        require("tools" in result.get("capabilities", {}), "MCP endpoint does not expose tools")

    def catalog(self, deadline, allowed):
        discovered = {}
        cursor = None
        for _ in range(10):
            result = self.exchange("tools/list", {"cursor": cursor} if cursor else {}, deadline)
            tools = result.get("tools")
            require(isinstance(tools, list), "Invalid MCP tool list")
            for tool in tools:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"] in allowed:
                    discovered[tool["name"]] = tool
            cursor = result.get("nextCursor")
            if not cursor or set(discovered) == set(allowed):
                return discovered
            require(isinstance(cursor, str) and len(cursor) <= 1024, "Invalid MCP list cursor")
        raise ValueError("MCP tool list exceeded its page limit")

    def invoke(self, name, arguments, deadline):
        return self.exchange("tools/call", {"name": name, "arguments": arguments}, deadline)

    def close(self):
        with contextlib.suppress(OSError, ValueError, RuntimeError, http.client.HTTPException):
            self.transport.close()
