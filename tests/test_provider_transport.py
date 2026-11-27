import json
import socket
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from aegis import integrations
from aegis.mcp import Client, VERSION


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/v1/models":
            self.reply(200, {"data": [{"id": "zeta"}, None, {"id": "alpha", "owned_by": "Fixture"}, {"id": ""}]})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        self.server.prompt = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.authorization = self.headers.get("Authorization")
        content = "```json\n{\"summary\":\"Ready\"}\n```"
        self.reply(200, {"choices": [{"finish_reason": self.server.stop_reason, "message": {"content": content}}],
                         "usage": {"prompt_tokens": 12, "completion_tokens": 6}})


class ProviderTransportTest(unittest.TestCase):
    def test_loopback_model_discovery_prompt_and_response_contracts(self):
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        settings = integrations.provider_settings({"kind": "local", "endpoint": endpoint, "model": "alpha"})
        server.stop_reason = "stop"
        self.assertEqual(integrations.list_provider_models(settings, "")["models"],
                         [{"id": "alpha", "owned_by": "Fixture"}, {"id": "zeta", "owned_by": ""}])
        response, tokens = integrations.complete(settings, "test-credential", "Summarize", {"request": "Plan a session"}, 3)
        self.assertEqual(json.loads(response), {"summary": "Ready"})
        self.assertEqual(tokens, (12, 6))
        self.assertEqual(server.authorization, "Bearer test-credential")
        self.assertEqual(server.prompt["model"], "alpha")
        self.assertEqual(json.loads(server.prompt["messages"][1]["content"]), {"request": "Plan a session"})
        server.stop_reason = "length"
        with self.assertRaisesRegex(RuntimeError, "stopped with length"):
            integrations.complete(settings, "", "Summarize", {}, 3)
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            integrations.request(endpoint + "/unknown")
        with self.assertRaisesRegex(ValueError, "size limit"):
            integrations.request(endpoint + "/models", limit=1)
        with self.assertRaisesRegex(ValueError, "External endpoints require HTTPS"):
            integrations.request(endpoint, public=True)

    def test_research_and_advisory_results_are_checked_before_use(self):
        url = "https://docs.python.org/fixture"
        paragraph = "The application stores each task in a list and displays the title on a local board."
        document = f"<article>{paragraph}</article><script>Hidden script</script>"
        with patch.object(integrations, "request", return_value=(document.encode(), "text/html")) as fetch:
            self.assertEqual(integrations.fetch_source(url, topic="application board"), {"url": url, "excerpt": paragraph})
            self.assertTrue(fetch.call_args.kwargs["public"])
        deps = integrations.packages({"requirements.txt": "demo==1.2.3  # pinned\n"})
        with patch.object(integrations, "request", return_value=(b'{"results":[{"vulns":[{"id":"TEST-1"}]}]}', "application/json")) as query:
            self.assertEqual(integrations.advisories(deps, 3), [{"package": "demo", "version": "1.2.3", "id": "TEST-1"}])
            self.assertEqual(query.call_args.kwargs["payload"]["queries"][0]["version"], "1.2.3")
        with patch.object(integrations, "request", return_value=(b'{"results":[]}', "application/json")):
            with self.assertRaisesRegex(ValueError, "Incomplete advisory response"):
                integrations.advisories(deps, 3)

    def test_public_socket_uses_only_validated_addresses_and_cleans_failed_connections(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, 443))
                     for host in ("1.1.1.1", "8.8.8.8")]
        first, second = Mock(), Mock()
        first.connect.side_effect = OSError("Fixture address unavailable")
        with (patch.object(socket, "getaddrinfo", return_value=addresses),
              patch.object(socket, "socket", side_effect=[first, second])):
            self.assertIs(integrations.public_socket(("docs.python.org", 443), 2, ("0.0.0.0", 0)), second)
        first.close.assert_called_once()
        second.bind.assert_called_once_with(("0.0.0.0", 0))
        second.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_mcp_http_handles_session_headers_pagination_and_event_streams(self):
        messages = []

        class MCPHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = request["method"]
                messages.append((method, dict(self.headers)))
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": VERSION, "capabilities": {"tools": {}}}
                elif method == "tools/list" and not request["params"]:
                    result = {"tools": [{"name": "unrequested"}], "nextCursor": "next"}
                elif method == "tools/list":
                    result = {"tools": [{"name": "lookup", "annotations": {"readOnlyHint": True}}]}
                else:
                    result = {"content": [{"type": "text", "text": request["params"]["arguments"]["query"]}]}
                payload = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                streamed = method == "tools/call"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if streamed else "application/json")
                if method == "initialize":
                    self.send_header("Mcp-Session-Id", "test-session")
                self.end_headers()
                self.wfile.write(b"data: " + payload + b"\r\n\r\n" if streamed else payload)

            def do_DELETE(self):
                messages.append(("DELETE", dict(self.headers)))
                self.send_response(204)
                self.end_headers()

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        token_env, api_key_env = "TEST_TOKEN", "TEST_KEY"
        config = {"url": f"http://127.0.0.1:{server.server_port}/mcp", "token_env": token_env,
                  "api_key_env": api_key_env, "key_header": "X-Test-Key"}
        with closing(Client(config, {token_env: "fixture-token", api_key_env: "fixture-key"})) as client:
            deadline = time.monotonic() + 5
            client.initialize(deadline)
            self.assertEqual(list(client.catalog(deadline, {"lookup"})), ["lookup"])
            self.assertEqual(client.invoke("lookup", {"query": "Boards"}, deadline)["content"],
                             [{"type": "text", "text": "Boards"}])
        self.assertEqual([method for method, _ in messages],
                         ["initialize", "notifications/initialized", "tools/list", "tools/list", "tools/call", "DELETE"])
        for _, headers in messages[1:]:
            self.assertEqual(headers["Mcp-Session-Id"], "test-session")
            self.assertEqual(headers["MCP-Protocol-Version"], VERSION)
            self.assertEqual(headers["Authorization"], "Bearer fixture-token")
            self.assertEqual(headers["X-Test-Key"], "fixture-key")
