"""Resource-limited, networkless containers and loopback-only preview relay."""
import base64
import contextlib
import json
import math
import os
import re
import secrets
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
from .preflight import CACHES
from .role_tools import FRAME_LIMIT, ROLE_TOOLS

IMAGE_PREFIX = "rittaiharness"
TASK_PROFILES = {**PROFILES, "build": ("runtime", 1, 512, 128),
                 "qa_browser": ("qa", 2, 1536, 256), "qa_artifact": ("runtime", 1, 512, 128),
                 "release": ("runtime", 1, 512, 128), "preview": ("runtime", 1, 512, 128),
                 "mcp": ("runtime", 0.5, 256, 64)}
MAX_SESSIONS = 8
LOG_LIMIT = 1_000_000
PREVIEW_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
               "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
DOCKER_TIMEOUT = 12


def execution_policy(data=None):
    if data is None:
        data = {}
    require(isinstance(data, dict) and set(data) <= {"sessions", "parallel_reviews", "overrides", "limits", "capacity"},
            "Invalid execution settings")
    sessions = data.get("sessions", 3)
    reviews = data.get("parallel_reviews", False)
    legacy = "overrides" not in data
    overrides = data.get("limits" if legacy else "overrides", {})
    require(type(sessions) is int and 1 <= sessions <= MAX_SESSIONS, f"Concurrent sessions must be between 1 and {MAX_SESSIONS}")
    require(isinstance(reviews, bool), "Parallel reviews must be enabled or disabled")
    require(isinstance(overrides, dict) and set(overrides) <= TASK_PROFILES.keys(), "Unknown container task")
    custom = {}
    for task, override in overrides.items():
        require(isinstance(override, dict) and set(override) <= {"cpu", "ram", "pids"}, f"Invalid limits for {task}")
        _, base_cpu, base_ram, base_pids = TASK_PROFILES[task]
        defaults = {"cpu": base_cpu, "ram": base_ram, "pids": base_pids}
        cpu, ram, pids = (override.get(key, defaults[key]) for key in ("cpu", "ram", "pids"))
        require(type(cpu) in (int, float) and 0.1 <= cpu <= 64, f"{task} CPUs must be between 0.1 and 64")
        require(type(ram) is int and 64 <= ram <= 262144, f"{task} memory must be between 64 and 262144 MiB")
        require(type(pids) is int and 16 <= pids <= 4096, f"{task} process limit must be between 16 and 4096")
        # Older releases persisted the full default table when the operator saved settings.
        changed = {key: value for key, value in override.items() if not legacy or value != defaults[key]}
        if changed:
            custom[task] = changed
    return {"sessions": sessions, "parallel_reviews": reviews, "overrides": custom}


def execution_plan(policy, capacity):
    sizing_floors = {"worker": (0.25, 96), "runtime": (0.5, 320), "qa": (0.75, 768)}
    limits = {}
    for task, (image, cpu, ram, pids) in TASK_PROFILES.items():
        if capacity is not None:
            min_cpu, min_ram = sizing_floors[image]
            if task in ("development", "mcp"):
                min_cpu = 0.25 if task == "mcp" else min_cpu
                min_ram = 192
            cpu = round(max(min_cpu, cpu * min(2, capacity["cpu"] / 8)), 2)
            ram = max(min_ram, round(ram * min(2, capacity["ram"] / 4096) / 32) * 32)
        limits[task] = {"cpu": cpu, "ram": ram, "pids": pids, **policy["overrides"].get(task, {})}
    return {**policy, "capacity": capacity, "limits": limits}


def detected_capacity(docker):
    try:
        return docker.engine_capacity()
    except (OSError, RuntimeError, ValueError):
        return None


def profile(task, policy=None):
    image, cpu, ram, pids = TASK_PROFILES[task]
    limits = policy["limits"][task] if policy else {"cpu": cpu, "ram": ram, "pids": pids}
    return {"image": f"{IMAGE_PREFIX}-{image}:local", "task": task, **limits,
            "network": "none", "root": "read-only", "user": "10001:10001",
            "tools": [ROLE_TOOLS[task][0]] if task in ROLE_TOOLS else []}


def used_mib(display):
    match = re.fullmatch(r"([0-9.]+)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)", display.split("/", 1)[0].strip())
    require(match, "Docker reported an unknown memory unit")
    amount, unit = match.groups()
    powers = {"B": 0, "k": 1, "K": 1, "M": 2, "G": 3, "T": 4}
    return float(amount) * (1024 if "i" in unit else 1000) ** powers[unit[0]] / (1024 ** 2)


class ContainerCapacity:
    """Account for overlapping container limits before an attempt starts."""

    def __init__(self, pressure_probe=None):
        self.condition = threading.Condition()
        self.limit = (float("inf"), float("inf"))
        self.used = (0, 0)
        self.held = {}
        self.pressure_probe = pressure_probe
        self.stats_lock = threading.Lock()
        self.sampled_at = 0
        self.external = None

    def set_engine_capacity(self, capacity):
        if capacity is not None:
            ram = capacity["ram"]
            with self.condition:
                self.limit = (capacity["cpu"], ram - min(512, ram // 8))
                self.condition.notify_all()

    def external_usage(self, refresh=False):
        if self.pressure_probe:
            with self.stats_lock:
                if refresh or time.monotonic() - self.sampled_at >= 5:
                    try:
                        self.external = self.pressure_probe()
                    except (OSError, RuntimeError, ValueError):
                        self.external = None
                    self.sampled_at = time.monotonic()
        return self.external

    def snapshot(self, external):
        return {"budget": {key: value if math.isfinite(value) else None for key, value in zip(("cpu", "ram"), self.limit)},
                "reserved": {"cpu": self.used[0] / 1000, "ram": self.used[1]},
                "holds": list(self.held.values()), "external": external}

    def report(self):
        external = self.external_usage()
        with self.condition:
            return self.snapshot(external)

    @contextmanager
    def reserve(self, profiles, remaining, waiting, *, holder=None):
        # Whole millicores avoid accumulating float rounding during repeated releases.
        request = (sum(round(item["cpu"] * 1000) for item in profiles), sum(item["ram"] for item in profiles))
        key = object()
        reported = False
        while True:
            external = self.external_usage()
            consumption = (math.ceil(external["cpu"] * 1000), external["ram"]) if external else (0, 0)
            with self.condition:
                limit = (self.limit[0] * 1000, self.limit[1])
                require(all(amount <= ceiling for amount, ceiling in zip(request, limit)),
                        f"Container tasks need {request[0] / 1000:g} CPUs and {request[1]} MiB. "
                        "Decrease custom limits or increase Docker capacity.")
                seconds = remaining()
                if all(used + amount + outside <= ceiling for used, amount, outside, ceiling in zip(
                        self.used, request, consumption, limit)):
                    self.used = tuple(used + amount for used, amount in zip(self.used, request))
                    self.held[key] = {**(holder or {}), "cpu": request[0] / 1000, "ram": request[1],
                                      "tasks": [item["task"] for item in profiles]}
                    break
                if not reported:
                    waiting(self.snapshot(external))
                    reported = True
                self.condition.wait(min(0.2, seconds))
        try:
            yield
        finally:
            with self.condition:
                self.used = tuple(used - amount for used, amount in zip(self.used, request))
                del self.held[key]
                self.condition.notify_all()


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
    def engine_capacity(self):
        output = run_command(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"]).stdout
        cpu, memory = map(int, output.split())
        require(cpu > 0 and memory > 0, "Docker engine reported invalid CPU or memory capacity")
        return {"cpu": cpu, "ram": memory // (1024 * 1024)}

    def doctor(self, images=True):
        capacity = self.engine_capacity()
        if images:
            for target in ("worker", "qa", "runtime"):
                run_command(["docker", "image", "inspect", f"{IMAGE_PREFIX}-{target}:local"])
        return capacity

    def remove(self, name):
        run_command(["docker", "rm", "-f", name], check=False)

    def clean_orphans(self, identifier):
        result = run_command(["docker", "ps", "-aq", "--filter", f"label=omniharness.workspace={identifier}"])
        for container in result.stdout.decode().splitlines():
            if re.fullmatch(r"[0-9a-f]{12,64}", container):
                self.remove(container)

    def external_usage(self, workspace):
        filters = ["--filter", f"label=omniharness.workspace={workspace}"] if workspace else []
        known = set(run_command(["docker", "ps", "--no-trunc", "-q", *filters]).stdout.decode().split()) if workspace else set()
        output = run_command(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=5).stdout
        count, cpu, ram = 0, 0.0, 0.0
        for line in output.splitlines():
            item = json.loads(line)
            require(isinstance(item, dict) and isinstance(item.get("MemUsage"), str) and
                    isinstance(item.get("CPUPerc"), str), "Invalid Docker stats entry")
            identifier = item.get("Container", "")
            require(isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{12,64}", identifier), "Invalid Docker stats entry")
            if any(candidate.startswith(identifier) for candidate in known):
                continue
            usage = float(item.get("CPUPerc", "0%").removesuffix("%")) / 100
            require(math.isfinite(usage) and usage >= 0, "Invalid Docker CPU usage")
            count += 1
            cpu += usage
            ram += used_mib(item["MemUsage"])
        return {"containers": count, "cpu": round(cpu, 3), "ram": math.ceil(ram)}

    def probe_mounts(self, directory):
        name = "rittaiharness-probe-" + secrets.token_hex(8)
        targets = [target for variable, target in CACHES.values() if os.environ.get(variable)]
        source = "from pathlib import Path\nimport sys\nassert Path('/input/probe').read_text() == sys.argv[1]\n"
        source += "for path in sys.argv[2:]:\n    next(Path(path).iterdir(), None)\n"
        with tempfile.TemporaryDirectory(prefix=".docker-probe-", dir=directory) as scratch:
            probe = Path(scratch)
            probe.chmod(0o755)
            (probe / "probe").write_text(name, encoding="utf-8")
            command = self.args(name, name, "lead", source=probe)
            try:
                run_command([*command, "python", "-I", "-c", source, name, *targets])
            except (OSError, ValueError, RuntimeError) as exc:
                raise RuntimeError(f"Docker could not read the workspace or configured cache mounts: {exc}") from exc
            finally:
                with contextlib.suppress(OSError, RuntimeError):
                    self.remove(name)

    @staticmethod
    def args(name, workspace, role, *, source=None, checks=None, tag=None, detached=False, resources=None, caches=True):
        attributes = resources or profile(role)
        cmd = ["docker", "run", "--pull=never", "--rm", "--init", "--name", name,
               "--label", f"omniharness.workspace={workspace}", "--network=none", "--read-only",
               "--cap-drop=ALL", "--security-opt=no-new-privileges=true", "--user=10001:10001",
               f"--cpus={attributes['cpu']}", f"--memory={attributes['ram']}m",
               f"--memory-swap={attributes['ram']}m", f"--pids-limit={attributes['pids']}",
               "--ulimit", "nofile=1024:1024", "--log-driver=none",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=192m,mode=1777",
               "--tmpfs", "/work:rw,nosuid,nodev,size=512m,mode=0700,uid=10001,gid=10001"]
        cmd += ["-d"] if detached else ["-i"]
        if attributes["image"] == f"{IMAGE_PREFIX}-qa:local":
            cmd += ["--shm-size=256m"]
        if source is not None:
            mounts = [(source, "/input"), (checks, "/checks")]
            if caches:
                mounts += [(os.environ.get(variable), destination) for variable, destination in CACHES.values()]
            for directory, destination in mounts:
                if directory:
                    absolute = str(Path(directory).resolve(strict=True))
                    require("," not in absolute and "\n" not in absolute, "Unsupported bind mount path")
                    cmd += ["--mount", f"type=bind,src={absolute},dst={destination},readonly"]
        return [*cmd, tag or attributes["image"]]

    @contextmanager
    def role(self, name, workspace, role, timeout, *, resources=None):
        cmd = self.args(name, workspace, role, resources=resources) + ["python", "/opt/harness/role.py", role]
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

    def execute(self, name, workspace, role, source, checks, timeout, log, *, resources=None):
        task = "qa_browser" if role == "qa" else "release"
        command = self.args(name, workspace, task, source=source, checks=checks, resources=resources)
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

    def preview(self, name, workspace, directory, lifetime=3600, *, resources=None):
        return Preview(self, name, workspace, directory, lifetime, resources=resources)


class Preview:
    def __init__(self, docker, name, workspace, directory, lifetime, *, resources=None):
        self.docker, self.name = docker, name
        self.lifetime = lifetime
        self.server = None
        self.timer = None
        self.on_close = None
        self.closed = threading.Event()
        cmd = docker.args(name, workspace, "preview", source=directory, detached=True, resources=resources)
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
