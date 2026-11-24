"""Run the role's allowlisted tools over a bounded JSON-line pipe."""
import json
import subprocess
import sys

from omni.role_tools import FRAME_LIMIT, ROLE_TOOLS, invoke


def main():
    role = sys.argv[1]
    if role not in ROLE_TOOLS:
        raise ValueError("Unknown agent persona")
    print("READY", flush=True)
    while raw := sys.stdin.buffer.readline(FRAME_LIMIT + 1):
        if len(raw) > FRAME_LIMIT or not raw.endswith(b"\n"):
            raise ValueError("Agent frame exceeded transport limit")
        try:
            message = json.loads(raw)
            result = {"result": invoke(role, message["tool"], message["arguments"])}
        except (ValueError, SyntaxError, OSError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"[:1800]}
        encoded = json.dumps(result, ensure_ascii=False).encode()
        if len(encoded) >= FRAME_LIMIT:
            encoded = b'{"error":"Tool response exceeded transport limit"}'
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
