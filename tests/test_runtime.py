import base64
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from containers import runtime
from aegis.sandbox import Docker, RoleChannel, run_command

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root / "input"
        self.source.mkdir()
        self.work = self.root / "work"
        self.enterContext(patch.multiple(runtime, SOURCE=self.source, WORK=self.work,
                                         VENV_PYTHON=self.root / "venv" / "bin" / "python"))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))

    def source_files(self, configuration, files):
        (self.source / "harness.json").write_text(json.dumps(configuration), encoding="utf-8")
        for name, content in files.items():
            (self.source / name).write_text(content, encoding="utf-8")
        return runtime.prepare()

    def test_python_artifact_builds_and_runs_its_own_tests(self):
        config = self.source_files({"runtime": "python", "mode": "artifact", "entry": "app.py"}, {
            "app.py": "def title():\n    return 'Fieldwork'\n\nif __name__ == '__main__':\n    print(title())\n",
            "test_app.py": "import unittest\nfrom app import title\n\nclass TestTitle(unittest.TestCase):\n"
                           "    def test_title(self):\n        self.assertEqual(title(), 'Fieldwork')\n",
        })
        runtime.qa(config)
        self.assertIn("All execution checks passed", self.output.getvalue())
        (self.work / "test_app.py").unlink()
        with self.assertRaises(subprocess.CalledProcessError):
            runtime.qa(config)

    def test_node_artifact_builds_and_runs_its_own_tests(self):
        config = self.source_files({"runtime": "node", "mode": "artifact", "entry": "app.cjs"}, {
            "app.cjs": "exports.total = (values) => values.reduce((sum, value) => sum + value, 0);\n",
            "app.test.cjs": "const test = require('node:test');\n"
                            "const assert = require('node:assert/strict');\n"
                            "const { total } = require('./app.cjs');\n"
                            "test('totals', () => assert.equal(total([2, 3]), 5));\n",
        })
        runtime.qa(config)
        self.assertIn("All execution checks passed", self.output.getvalue())

    def test_browser_checks_assert_page_behavior_for_a_web_build(self):
        config = self.source_files({"runtime": "static", "mode": "web"}, {
            "index.html": "<html><title>Fieldwork</title><h1>Make a plan</h1></html>",
            "app.js": "document.title = 'Fieldwork';",
        })
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        suite = [{"name": "Add a task", "steps": [
            {"action": "goto", "path": "/"}, {"action": "title", "value": "Fieldwork"},
            {"action": "visible", "selector": "h1"}, {"action": "text", "selector": "h1", "value": "Make a plan"},
            {"action": "fill", "selector": "input", "value": "Pack"}, {"action": "click", "selector": "button"},
        ]}]
        checks = self.root / "journeys.json"
        checks.write_text(json.dumps(suite), encoding="utf-8")
        page, locator, context, browser, playwright, expect = (Mock() for _ in range(6))
        page.locator.return_value = locator
        context.new_page.return_value = page
        browser.new_context.return_value = context
        playwright.chromium.launch.return_value = browser
        session = MagicMock()
        session.__enter__.return_value = playwright
        api = SimpleNamespace(expect=expect, sync_playwright=lambda: session)
        origin = f"http://127.0.0.1:{port}"
        original_path = runtime.Path
        with (patch.multiple(runtime, PORT=port, ORIGIN=origin), patch.dict(sys.modules, {"playwright.sync_api": api}),
              patch.object(runtime, "Path", side_effect=lambda name: checks if name == "/checks/journeys.json" else original_path(name))):
            runtime.qa(config)
        page.goto.assert_called_once_with(origin + "/", wait_until="domcontentloaded", timeout=9000)
        expect.return_value.to_have_title.assert_called_once_with("Fieldwork")
        expect.return_value.to_be_visible.assert_called_once()
        expect.return_value.to_contain_text.assert_called_once_with("Make a plan")
        locator.fill.assert_called_once_with("Pack")
        locator.click.assert_called_once()
        browser.close.assert_called_once()

    def test_relay_preserves_allowed_headers_and_bounds_response(self):
        request = {"method": "POST", "path": "/tasks", "headers": {"Content-Type": "text/plain"},
                   "body": base64.b64encode(b"task").decode()}
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 201
        response.read.return_value = b"created"
        response.getheaders.return_value = [("Content-Type", "text/plain"), ("Set-Cookie", "ignored=true")]
        with patch.object(runtime.http.client, "HTTPConnection", return_value=connection):
            with patch.object(sys, "stdin", io.StringIO(json.dumps(request))):
                runtime.relay()
            self.assertEqual(json.loads(self.output.getvalue()), {
                "status": 201, "headers": [["Content-Type", "text/plain"]], "body": "Y3JlYXRlZA==",
            })
            connection.request.assert_called_once_with("POST", "/tasks", b"task", headers=request["headers"])
            with patch.object(sys, "stdin", io.StringIO(json.dumps(request))), patch.object(runtime, "LIMIT", 2):
                with self.assertRaisesRegex(RuntimeError, "exceeded size limit"):
                    runtime.relay()
        self.assertEqual(connection.close.call_count, 2)


class DockerTransportTest(unittest.TestCase):
    def test_worker_transport_validates_messages_and_closes_container(self):
        docker = Docker()
        popen = subprocess.Popen

        def launch_worker(command, **kwargs):
            self.assertIn("--network=none", command)
            self.assertIn("--read-only", command)
            return popen([sys.executable, str(ROOT / "containers" / "role.py"), "specialist"],
                         env={**os.environ, "PYTHONPATH": str(ROOT)}, **kwargs)

        valid = {"summary": "Check tab order", "findings": ["Focus should follow layout"],
                 "recommendations": ["Add a keyboard journey"]}
        with patch("aegis.sandbox.subprocess.Popen", side_effect=launch_worker), patch.object(docker, "remove") as remove:
            with docker.role("crew-worker", "workspace", "specialist", 5) as invoke:
                self.assertEqual(invoke("submit", {"draft": valid}, 5), valid)
                with self.assertRaisesRegex(ValueError, "submit"):
                    invoke("submit", {"draft": {}}, 5)
        remove.assert_called_once_with("crew-worker")

    def test_execution_collects_logs_and_cleans_failed_processes(self):
        docker = Docker()
        popen = subprocess.Popen
        # These children stand in for Docker and never execute application code.
        environment = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE_")}
        output = []
        with tempfile.TemporaryDirectory() as source, patch.object(docker, "remove") as remove:
            for program, error in (("print('build output')", None),
                                   ("print('failed output'); raise SystemExit(4)", RuntimeError),
                                   ("import time; time.sleep(5)", TimeoutError)):
                with self.subTest(program=program), patch("aegis.sandbox.subprocess.Popen",
                        side_effect=lambda command, **kwargs: popen([sys.executable, "-c", program],
                                                                     env=environment, **kwargs)):
                    arguments = ("crew-build", "workspace", "deploy", source, None, 3 if error != TimeoutError else 0.1,
                                 output.append)
                    if error:
                        with self.assertRaises(error):
                            docker.execute(*arguments)
                    else:
                        self.assertEqual(docker.execute(*arguments), {"execution": "build", "exitCode": 0})
        self.assertEqual([entry.splitlines()[-1] for entry in output], ["build output", "failed output"])
        self.assertEqual(remove.call_count, 3)

    def test_preview_forwards_only_local_requests_and_rewrites_redirects(self):
        docker = Docker()
        fixture = {"status": 200, "body": "RmllbGR3b3Jr", "headers": [["Content-Type", "text/plain"],
                                                                   ["Set-Cookie", "preview=fixture"], ["X-From-App", "fixture"]]}
        redirect = {"status": 302, "body": "", "headers": [["Location", "http://127.0.0.1:8080/board?view=tasks"],
                                                             ["X-Bad", "header\nvalue"]]}
        external = {"status": 302, "body": "", "headers": [["Location", "https://example.invalid/"]]}

        def docker_command(args, **kwargs):
            payload = kwargs.get("data")
            if not payload:
                return SimpleNamespace(stdout=b"")
            request = json.loads(payload)
            replies = {"/": fixture, "/redirect": redirect, "/other": external}
            return SimpleNamespace(stdout=json.dumps(replies.get(request["path"], {})).encode())

        with tempfile.TemporaryDirectory() as source, patch("aegis.sandbox.run_command", side_effect=docker_command) as command:
            preview = self.enterContext(closing(docker.preview("crew-preview", "workspace", source, lifetime=30)))
            with closing(http.client.HTTPConnection("127.0.0.1", preview.server.server_port, timeout=3)) as client:
                for method, path, headers, status, body in (
                    ("GET", "/", {}, 200, b"Fieldwork"), ("HEAD", "/", {}, 200, b""),
                    ("GET", "/redirect", {}, 302, b""), ("GET", "/other", {}, 302, b""),
                    ("GET", "/", {"Host": "example.invalid"}, 403, None),
                    ("GET", "/", {"Origin": "https://example.invalid"}, 403, None),
                    ("GET", "/", {"Service-Worker": "script"}, 403, None),
                    ("GET", "/missing", {}, 502, None),
                ):
                    with self.subTest(method=method, path=path, headers=headers):
                        client.request(method, path, headers=headers)
                        with client.getresponse() as response:
                            self.assertEqual(response.status, status)
                            data = response.read()
                            if body is not None:
                                self.assertEqual(data, body)
                            if status == 200:
                                self.assertEqual(response.getheader("Cross-Origin-Opener-Policy"), "same-origin")
                                self.assertIsNone(response.getheader("Set-Cookie"))
                                self.assertIsNone(response.getheader("X-From-App"))
                            if path == "/redirect":
                                self.assertEqual(response.getheader("Location"), "/board?view=tasks")
                                self.assertIsNone(response.getheader("X-Bad"))
                            elif path == "/other":
                                self.assertIsNone(response.getheader("Location"))
            preview.close()
            preview.close()
            self.assertTrue(preview.closed.is_set())
            self.assertEqual(command.call_args.args[0][:3], ["docker", "rm", "-f"])

    def test_pipe_response_limits_and_docker_command_errors(self):
        with subprocess.Popen([sys.executable, "-c", "print('long response')"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
            channel = RoleChannel(child, limit=4)
            with self.assertRaisesRegex(ValueError, "request exceeded"):
                channel.write(b"12345", time.monotonic() + 2)
            with self.assertRaisesRegex(ValueError, "response exceeded"):
                channel.read(time.monotonic() + 2)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            run_command([sys.executable, "-c", "raise SystemExit(4)"])
        with self.assertRaises(TimeoutError):
            run_command([sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.1)
