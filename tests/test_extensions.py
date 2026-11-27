import io
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import test_workspace
from aegis import demo
from aegis.engine import Lead
from aegis.extensions import Extensions
from aegis.integrations import complete, initial_provider, provider_key, provider_settings
from aegis.mcp import Client, StreamableHTTP
from aegis.store import Redactor

GATEWAY = "https://example.eastus2-01.ai.gateway.azure.com/default/models/openai/v1/chat/completions"

STDIO_SERVER = '''
import json, sys

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if "id" not in msg:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
    elif method == "tools/list":
        if msg["params"].get("cursor"):
            result = {"tools": [{"name": "search_docs", "inputSchema": {"type": "object"},
                                 "annotations": {"readOnlyHint": True}}]}
        else:
            result = {"tools": [{"name": "edit_docs", "inputSchema": {"type": "object"},
                                 "annotations": {"readOnlyHint": False}}], "nextCursor": "next"}
    elif method == "tools/call":
        assert msg["params"]["name"] == "search_docs"
        assert msg["params"]["arguments"] == {"query": "runtime tests"}
        result = {"content": [{"type": "text", "text": "Verified local tool fixture."}]}
    else:
        raise ValueError(method)
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
'''


class ExtensionTest(unittest.TestCase):
    def test_gateway_uses_exact_endpoint_alias_and_runtime_bearer_key(self):
        with patch.dict(os.environ, {"AEGIS_PROVIDER": "azure-gateway", "AI_GATEWAY_CHAT_URL": GATEWAY,
                                     "AI_GATEWAY_MODEL": "project/test-alias", "AI_GATEWAY_API_KEY": "fixture-gateway-key"}):
            settings = initial_provider()
            key = provider_key(settings["kind"])
        response = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 1}}
        with patch("aegis.integrations.request", return_value=(json.dumps(response).encode(), "application/json")) as request:
            self.assertEqual(complete(settings, key, "Test connectivity", {"check": True}, 5), ("OK", (12, 1)))
        self.assertEqual(request.call_args.args, (GATEWAY,))
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bearer fixture-gateway-key"})
        self.assertEqual(request.call_args.kwargs["payload"]["model"], "project/test-alias")
        with self.assertRaisesRegex(ValueError, "runtime key"):
            complete(settings, None, "Test", {}, 5)
        with self.assertRaises(ValueError):
            provider_settings({"kind": "azure-gateway", "endpoint": GATEWAY + "?key=secret", "model": "alias"})

    def test_streamable_http_negotiates_the_session_and_reads_sse(self):
        calls = []

        class Response(io.BytesIO):
            def __init__(self, content, status=200, headers=None):
                super().__init__(content)
                self.size = len(content)
                self.status = status
                self.headers = headers or {}

            def getheader(self, key, default=""):
                return self.headers.get(key, default)

            def isclosed(self):
                return self.tell() == self.size

        class Socket:
            def __init__(self, response):
                self.response = response

            def settimeout(self, _):
                if self.response.isclosed():
                    raise OSError("HTTP/1.0 closed its socket after the last chunk")

        @contextmanager
        def exchange(transport, method, deadline, message=None):
            calls.append((method, message, transport.session_id, transport.version))
            rpc_method = message.get("method") if message else None
            headers = {}
            if rpc_method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
                headers["Mcp-Session-Id"] = "session-fixture"
            elif rpc_method == "tools/call":
                result = {"content": [{"type": "text", "text": "SSE evidence"}]}
            else:
                response = Response(b"", 202)
                yield response, Socket(response)
                return
            raw = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
            if rpc_method == "tools/call":
                raw = b": keepalive\r\ndata: " + raw + b"\r\n\r\n"
                headers["Content-Type"] = "text/event-stream"
            else:
                headers["Content-Type"] = "application/json"
            response = Response(raw, headers=headers)
            yield response, Socket(response)

        client = Client({"url": "https://gateway.example/mcp", "token_env": "MCP_KEY"}, {"MCP_KEY": "fixture-token"})
        self.assertEqual(client.transport.headers["Authorization"], "Bearer fixture-token")
        with patch.object(StreamableHTTP, "request", exchange):
            deadline = time.monotonic() + 3
            client.initialize(deadline)
            result = client.invoke("docs", {"query": "test"}, deadline)
            client.close()
        self.assertEqual(result["content"][0]["text"], "SSE evidence")
        self.assertEqual([method for method, *_ in calls], ["POST", "POST", "POST", "DELETE"])
        self.assertEqual(calls[1][2:], ("session-fixture", "2025-06-18"))

    def test_skill_resources_and_mcp_calls_are_scoped_during_a_factory_run(self):
        requested = False
        seen = []
        specialist_seen = []

        def model(settings, key, persona, data, timeout):
            nonlocal requested
            stage = data["stage"]
            if "read-only software delivery specialist" in persona:
                self.assertIn("Fixture lean guidance", persona)
                self.assertIn("Fixture checklist resource", persona)
                self.assertIn("Fixture development-only rule", persona)
                self.assertNotIn("mcp_tools", data)
                specialist_seen.append(stage)
                answer = {"summary": "Check the navigation", "findings": ["Use keyboard navigation"],
                          "recommendations": ["Keep focus visible"]}
            elif "independent QA reviewer" in persona:
                self.assertIn("Fixture lean guidance", persona)
                self.assertEqual("Fixture development-only rule" in persona, stage == "development")
                answer = {"passed": True, "checks": ["Reviewed"], "feedback": "Accepted"}
            elif stage == "research" and "Propose the next high-value evidence" in persona:
                self.assertNotIn("Fixture lean guidance", persona)
                answer = {"done": bool(data["evidence"]), "urls": [] if data["evidence"] else [demo.SOURCE], "packages": []}
            elif stage == "development":
                self.assertIn("Fixture lean guidance", persona)
                self.assertIn("Fixture checklist resource", persona)
                self.assertEqual(set(data["mcp_tools"]), {"fixture.search_docs"})
                if not requested:
                    requested = True
                    answer = {"mcp_call": {"name": "fixture.search_docs", "arguments": {"query": "runtime tests"}}}
                else:
                    seen.append(data["mcp_evidence"][0]["untrusted_tool_result"]["output"])
                    answer = demo.EXAMPLES[stage]
            else:
                self.assertNotIn("mcp_tools", data)
                answer = demo.EXAMPLES[stage]
                if stage == "lead":
                    answer = {**answer, "specialists": [{"name": "Accessibility", "persona": "Keyboard focus",
                              "assignment": "Identify a focus check", "criterion": "AC1", "stage": "development",
                              "paths": [], "deliverable": "An actionable focus requirement"}]}
            return json.dumps(test_workspace.release_verdict(data, answer)), (1, 1)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "lean"
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: lean\ndescription: Review code\n---\nFixture lean guidance\n")
            (skill / "check.md").write_text("Fixture checklist resource\n")
            development_skill = root / "build-guidance"
            development_skill.mkdir()
            (development_skill / "SKILL.md").write_text("---\nname: build-guidance\ndescription: Build policies\n---\nFixture development-only rule\n")
            (root / "server.py").write_text(STDIO_SERVER)
            config = root / "extensions.toml"
            config.write_text(f'''[[skills]]
path = "lean"
roles = ["development", "review"]
resources = ["check.md"]

[[skills]]
path = "build-guidance"
roles = ["development"]

[[mcp]]
name = "fixture"
isolation = "host"
command = [{json.dumps(os.sys.executable)}, "server.py"]
[mcp.tools]
search_docs = ["development"]
edit_docs = ["development"]
''')
            with patch.dict(os.environ, {"AEGIS_EXTENSIONS": str(config), "AEGIS_PROVIDER": "demo"}):
                extensions = Extensions(root, Redactor())
                summary = extensions.describe(check=True)
                self.assertEqual(summary["mcp"][0]["available_tools"], ["search_docs"])
                self.assertEqual(summary["mcp"][0]["unavailable_tools"], ["edit_docs"])
                self.assertFalse(extensions.server_grants("research"))
                with extensions.tools("development", time.monotonic() + 3, lambda *_: None) as tools:
                    with self.assertRaisesRegex(ValueError, "outside this role"):
                        extensions.invoke(tools, "fixture.edit_docs", {}, time.monotonic() + 3)
                lead = Lead(root, docker=test_workspace.DockerFixture(), model=model, scan=lambda *_: [],
                            fetch=lambda *args: {"url": args[0], "excerpt": "Verified source fixture."})
                try:
                    lead.configure({"kind": "compatible", "endpoint": "http://127.0.0.1:8181/v1", "model": "fixture"})
                    project = lead.store.project("MCP and Skills", "")
                    sid = lead.launch(project["id"], demo.PROMPT)
                    result = test_workspace.WorkspaceTest.completed(lead, sid)
                    self.assertEqual(result["state"], "completed", result["error"])
                    self.assertEqual(seen, ["Verified local tool fixture."])
                    self.assertEqual(specialist_seen, ["development"])
                    specialist = next(agent for agent in result["agents"] if agent["role"] == "specialist")
                    self.assertEqual(specialist["profile"]["skills"], ["lean", "build-guidance"])
                    self.assertEqual(specialist["profile"]["mcp_tools"], [])
                    logs = lead.store.telemetry(sid, stage="development", role="development")["events"]
                    self.assertTrue(any(log["tool"] == "mcp.fixture.search_docs" for log in logs))
                finally:
                    lead.close()
            config.write_text(config.read_text().replace('"check.md"', '"../server.py"'))
            with patch.dict(os.environ, {"AEGIS_EXTENSIONS": str(config)}):
                with self.assertRaisesRegex(ValueError, "relative Skill resource"):
                    Extensions(root, Redactor())
