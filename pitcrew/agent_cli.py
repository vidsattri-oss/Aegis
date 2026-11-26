"""Use signed-in coding CLIs as model providers for the Lead's existing roles."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
from contextlib import suppress

from .contracts import MAX_BYTES, require


AGENT_CLIS = {"claude-code": ("Claude Code", "claude"), "github-copilot": ("GitHub Copilot CLI", "copilot")}
COMMON_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
              "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SSL_CERT_FILE", "SSL_CERT_DIR")
AUTH_ENV = {"claude-code": ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
            "github-copilot": ("COPILOT_HOME", "COPILOT_GITHUB_TOKEN")}


def complete_with_cli(settings, persona, data, timeout):
    kind = settings["kind"]
    name, binary = AGENT_CLIS[kind]
    executable = shutil.which(binary)
    require(executable, f"Install {name} on the machine running Pit Crew Harness and sign in there")
    if kind == "claude-code":
        options = ["-p", "--restricted", "--tools", "", "--disallowed-tools", "*", "--strict-mcp-config",
                   "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence", "--max-turns", "1",
                   "--output-format", "json"]
    else:
        # Permit no callable tools. Keep project and built-in integrations out of prompt mode.
        options = ["--silent", "--no-color", "--no-auto-update", "--no-custom-instructions", "--no-ask-user",
                   "--available-tools=view", "--deny-tool=view", "--disable-builtin-mcps", "--sync-session=off"]
    if settings["model"]:
        options.append("--model=" + settings["model"])
    environment = {key: os.environ[key] for key in (*COMMON_ENV, *AUTH_ENV[kind]) if key in os.environ}
    prompt = (persona + "\n\nRequest data (JSON):\n" + json.dumps(data, ensure_ascii=False)).encode()
    with tempfile.TemporaryDirectory(prefix="pitcrew-model-") as directory, tempfile.TemporaryFile() as output:
        with subprocess.Popen([executable, *options], cwd=directory, env=environment, stdin=subprocess.PIPE,
                              stdout=output, stderr=subprocess.DEVNULL, start_new_session=True) as process:
            try:
                process.communicate(prompt, timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                if isinstance(exc, KeyboardInterrupt):
                    raise
                raise TimeoutError(f"{name} did not respond before the request deadline") from None
            require(process.returncode == 0,
                    f"{name} exited with status {process.returncode}. Check its version and sign-in on the workspace host")
        output.seek(0)
        raw = output.read(MAX_BYTES * 2 + 1)
    require(len(raw) <= MAX_BYTES * 2, f"{name} response exceeded the output limit")
    if kind == "github-copilot":
        return raw.decode("utf-8"), (0, 0)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError(f"{name} returned an invalid automation response") from None
    require(isinstance(result, dict) and not result.get("is_error"), f"{name} reported an unsuccessful request")
    usage = result.get("usage") or {}
    require(isinstance(usage, dict), f"{name} returned invalid usage metadata")
    tokens = tuple(value if type(value) is int and value >= 0 else 0
                   for value in (usage.get("input_tokens"), usage.get("output_tokens")))
    return result.get("result"), tokens
