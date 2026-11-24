"""Resource-limited, networkless containers and loopback-only preview relay."""
import base64
import contextlib
import json
import os
import re
import selectors
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import PROFILES, require
from .role_tools import FRAME_LIMIT, ROLE_TOOLS

IMAGE_PREFIX = "rittaiharness"
LOG_LIMIT = 1_000_000
PREVIEW_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
               "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
DOCKER_TIMEOUT = 12


def profile(role):
    image, cpu, ram, pids = PROFILES[role]
    return {"image": f"{IMAGE_PREFIX}-{image}:local", "cpu": cpu, "ram": ram, "pids": pids,
            "network": "none", "root": "read-only", "user": "10001:10001",
            "tools": [ROLE_TOOLS[role][0]]}


def run_command(args, timeout=DOCKER_TIMEOUT, data=None, check=True):
    try:
        result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Docker {args[1]} exceeded its deadline") from exc
    if check and result.returncode:
        output = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"Docker {args[1]} failed: {output[-2000:]}")
    return result


class RoleChannel:
    """Bounded duplex transport for trusted role entry points."""

    def __init__(self, process, stderr=None, limit=FRAME_LIMIT):
        self.process = process
        self.stderr = stderr
        self.limit = limit
        self.buffer = bytearray()
        os.set_blocking(process.stdin.fileno(), False)

    def read(self, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while (end := self.buffer.find(b"\n")) < 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Role container did not respond before its deadline")
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    trace = ""
                    if self.stderr:
                        self.stderr.seek(max(0, self.stderr.seek(0, os.SEEK_END) - 1600))
                        trace = self.stderr.read(1600).decode(errors="replace").strip()
                    raise RuntimeError("Tool process closed its pipe" + (f": {trace}" if trace else ""))
                self.buffer.extend(chunk)
                require(len(self.buffer) <= self.limit, "Tool response exceeded its transport limit")
            frame = bytes(self.buffer[:end])
            del self.buffer[:end + 1]
            return frame

    def write(self, data, deadline):
        require(len(data) <= self.limit, "Tool request exceeded its transport limit")
        pending = memoryview(data)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Tool process did not accept its request before its deadline")
                pending = pending[os.write(self.process.stdin.fileno(), pending):]

    def call(self, tool, arguments, timeout):
        data = json.dumps({"tool": tool, "arguments": arguments}, ensure_ascii=False).encode() + b"\n"
        deadline = time.monotonic() + timeout
        self.write(data, deadline)
        response = json.loads(self.read(deadline))
        if error := response.get("error"):
            raise ValueError(f"{tool}: {error}")
        return response["result"]


class Docker:
    def doctor(self, images=True):
        run_command(["docker", "info", "--format", "{{.ServerVersion}}"])
        if images:
            for target in ("worker", "qa", "runtime"):
                run_command(["docker", "image", "inspect", f"{IMAGE_PREFIX}-{target}:local"])

    def remove(self, name):
        run_command(["docker", "rm", "-f", name], check=False)

    def clean_orphans(self, identifier):
        result = run_command(["docker", "ps", "-aq", "--filter", f"label=omniharness.workspace={identifier}"])
        for container in result.stdout.decode().splitlines():
            if re.fullmatch(r"[0-9a-f]{12,64}", container):
                self.remove(container)

    @staticmethod
    def args(name, workspace, role, *, source=None, checks=None, tag=None, detached=False):
        attributes = profile(role)
        cmd = ["docker", "run", "--pull=never", "--rm", "--init", "--name", name,
               "--label", f"omniharness.workspace={workspace}", "--network=none", "--read-only",
               "--cap-drop=ALL", "--security-opt=no-new-privileges=true", "--user=10001:10001",
               f"--cpus={attributes['cpu']}", f"--memory={attributes['ram']}m",
               f"--memory-swap={attributes['ram']}m", f"--pids-limit={attributes['pids']}",
               "--ulimit", "nofile=1024:1024", "--log-driver=none",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=192m,mode=1777",
               "--tmpfs", "/work:rw,nosuid,nodev,size=512m,mode=0700,uid=10001,gid=10001"]
        cmd += ["-d"] if detached else ["-i"]
        if role == "qa":
            cmd += ["--shm-size=256m"]
        if source is not None:
            mounts = [(source, "/input"), (checks, "/checks"),
                      (os.environ.get("OMNI_WHEELHOUSE"), "/wheels"),
                      (os.environ.get("OMNI_NPM_CACHE"), "/npm-input")]
            for directory, destination in mounts:
                if directory:
                    absolute = str(Path(directory).resolve(strict=True))
                    require("," not in absolute and "\n" not in absolute, "Unsupported bind mount path")
                    cmd += ["--mount", f"type=bind,src={absolute},dst={destination},readonly"]
        return [*cmd, tag or attributes["image"]]

    @contextmanager
    def role(self, name, workspace, role, timeout):
        cmd = self.args(name, workspace, role) + ["python", "/opt/harness/role.py", role]
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, bufsize=0)
            channel = RoleChannel(process, stderr)
            try:
                marker = channel.read(time.monotonic() + min(timeout, DOCKER_TIMEOUT))
                require(marker == b"READY", "Invalid role container handshake")
                yield channel.call
            finally:
                process.stdin.close()
                if process.poll() is None:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
                with contextlib.suppress(Exception):
                    self.remove(name)

    def execute(self, name, workspace, role, source, checks, timeout, log):
        command = self.args(name, workspace, role, source=source, checks=checks)
        action = "qa" if role == "qa" else "build"
        command += ["python", "/opt/harness/runtime.py", action]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        tail = bytearray()
        total = 0
        with selectors.DefaultSelector() as streams:
            streams.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not streams.select(remaining):
                        raise TimeoutError(f"{action} container exceeded its deadline")
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        break
                    total += len(chunk)
                    tail.extend(chunk[:max(0, LOG_LIMIT - len(tail))])
                    require(total <= LOG_LIMIT, "Container log exceeded its hard output limit")
                if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                    raise RuntimeError(f"{action} failed (exit {process.returncode}): "
                                       + tail[-1800:].decode(errors="replace").strip())
            finally:
                if tail:
                    log(tail.decode(errors="replace"))
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
                with contextlib.suppress(Exception):
                    self.remove(name)
        return {"execution": action, "exitCode": 0}

    def preview(self, name, workspace, directory, lifetime=3600):
        return Preview(self, name, workspace, directory, lifetime)


class Preview:
    def __init__(self, docker, name, workspace, directory, lifetime):
        self.docker, self.name = docker, name
        self.lifetime = lifetime
        self.server = None
        self.timer = None
        self.on_close = None
        self.closed = threading.Event()
        cmd = docker.args(name, workspace, "deploy", source=directory, detached=True)
        try:
            run_command([*cmd, "python", "/opt/harness/runtime.py", "serve"])
            deadline = time.monotonic() + 25
            while True:
                try:
                    status = self.relay("GET", "/", {}, b"")["status"]
                    if 200 <= status < 400:
                        break
                except (OSError, ValueError, RuntimeError):
                    if time.monotonic() >= deadline:
                        raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Preview runtime did not return a healthy response")
                time.sleep(0.35)
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
            self.url = "http://127.0.0.1:" + str(self.server.server_port)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.timer = threading.Timer(lifetime, self.close)
            self.timer.daemon = True
            self.timer.start()
        except Exception:
            with contextlib.suppress(Exception):
                docker.remove(name)
            raise

    def relay(self, method, path, headers, body):
        raw = json.dumps({"method": method, "path": path, "headers": headers,
                          "body": base64.b64encode(body).decode()}).encode()
        result = run_command(["docker", "exec", "-i", self.name, "python", "/opt/harness/runtime.py", "relay"],
                             data=raw)
        require(len(result.stdout) <= 11_000_000, "Preview response exceeded its limit")
        return json.loads(result.stdout)

    def handler(self):
        preview = self
        slots = threading.BoundedSemaphore(6)

        class Handler(BaseHTTPRequestHandler):
            timeout = 12

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.forward()

            do_HEAD = do_GET
            do_POST = do_GET
            do_PUT = do_GET
            do_PATCH = do_GET
            do_DELETE = do_GET
            do_OPTIONS = do_GET

            def forward(self):
                if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                    self.send_error(403)
                    return
                origin = self.headers.get("Origin")
                if origin and origin != preview.url:
                    self.send_error(403)
                    return
                if not slots.acquire(blocking=False):
                    self.send_error(503)
                    return
                try:
                    self.route()
                finally:
                    slots.release()

            def route(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    require(0 <= length <= 1_000_000, "Preview request too large")
                    require(not self.headers.get("Transfer-Encoding"), "Chunked preview requests are unsupported")
                    require(self.path.startswith("/") and not self.path.startswith("//") and "\\" not in self.path,
                            "Expected a local preview path")
                    incoming = {key: self.headers[key] for key in ("Accept", "Content-Type") if key in self.headers}
                    result = preview.relay(self.command, self.path, incoming, self.rfile.read(length))
                    body = base64.b64decode(result["body"], validate=True)
                except (OSError, ValueError, RuntimeError, KeyError):
                    self.send_error(502, "Preview runtime is unavailable")
                    return
                try:
                    self.send_response(result["status"])
                    for key, value in result["headers"]:
                        if any(char in value for char in ("\r", "\n")):
                            continue
                        if key.lower() == "location":
                            location = urlsplit(value)
                            if location.netloc == "127.0.0.1:8080":
                                value = (location.path or "/") + ("?" + location.query if location.query else "")
                            elif location.scheme or location.netloc or "\\" in value:
                                continue
                        self.send_header(key, value)
                    self.send_header("Content-Security-Policy", PREVIEW_CSP)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.timer:
            self.timer.cancel()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        with contextlib.suppress(Exception):
            self.docker.remove(self.name)
        if self.on_close:
            self.on_close(self)
