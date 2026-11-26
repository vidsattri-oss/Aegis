import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import test_workspace
from pitcrew import cli
from pitcrew.engine import Lead
from pitcrew.server import WorkspaceServer, workspace_lock


class PitWallAPITest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            key: value for key, value in os.environ.items()
            if not key.startswith(("PITCREW_", "OMNI_", "AI_GATEWAY_"))
        }, clear=True)
        self.enterContext(environment)
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.docker = test_workspace.DockerFixture()
        self.docker.engine_capacity = lambda: {"cpu": 4, "ram": 4096}
        self.lead = self.enterContext(closing(Lead(self.data, docker=self.docker)))
        self.server = self.enterContext(WorkspaceServer(("127.0.0.1", 0), self.lead))
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.port = str(self.server.server_port)
        (self.data / ".control-port").write_text(self.port, encoding="ascii")
        self.remote = cli.RemoteWorkspace(int(self.port))

    def request(self, path, *, method="GET", data=None, headers=None):
        with closing(http.client.HTTPConnection("127.0.0.1", int(self.port), timeout=5)) as client:
            client.request(method, path, data, headers=headers or {})
            with client.getresponse() as response:
                return response.status, response.headers, response.read()

    def post(self, path, value):
        return self.request(path, method="POST", data=json.dumps(value),
                            headers={"Content-Type": "application/json", "X-Pitcrew-Token": self.remote.token})

    def command(self, *args, code=0):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                cli.main(["--data", str(self.data), *args])
            except SystemExit as exc:
                self.assertEqual(exc.code, code, errors.getvalue())
            else:
                self.assertEqual(code, 0)
        return output.getvalue(), errors.getvalue()

    def test_assets_and_mutations_require_valid_local_requests(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Pit Crew Harness", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/app.js", method="HEAD")[2], b"")
        self.assertEqual(self.request("/api/workspace", headers={"Host": "example.invalid"})[0], 403)
        self.assertEqual(self.request("/api/projects", method="POST", data="{}")[0], 403)
        auth = {"X-Pitcrew-Token": self.remote.token}
        self.assertEqual(self.request("/api/projects", method="POST", data="{}",
                                      headers=auth | {"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request("/api/projects", method="POST", data="{}", headers=auth)[0], 415)
        self.assertEqual(self.post("/api/projects", ["not an object"])[0], 400)
        self.assertEqual(self.post("/api/projects", {"name": ""})[0], 400)
        self.assertEqual(self.request("/unknown")[0], 404)
        self.assertEqual(self.post("/unknown", {})[0], 404)
        with workspace_lock(self.data), self.assertRaisesRegex(SystemExit, "already open"):
            with workspace_lock(self.data):
                self.fail("Two writers acquired the workspace lock")

    def test_remote_cli_runs_a_lap_and_reads_its_attempts_and_build(self):
        project = json.loads(self.command("new-raceday", "Practice", "--json")[0])
        self.assertIn("Practice", self.command("racedays")[0])
        sid = json.loads(self.command("run", "--demo", "--detach", "--raceday", "Practice", "--json")[0])["id"]
        result = test_workspace.WorkspaceTest.completed(self.lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        path = f"/api/sessions/{sid}"
        attempt = result["agents"][0]["id"]
        agent_url = f"{path}/agents/{attempt}"
        self.assertEqual(self.remote.request(agent_url + "?metadata=1")["agent"]["role"], "lead")
        self.assertTrue(self.remote.request(agent_url + "?after=0")["events"])
        self.assertEqual(self.request(f"{path}/agents/{'f' * 16}")[0], 404)
        self.assertEqual(self.request(f"{path}/telemetry?role=unknown")[0], 400)
        self.assertEqual(self.request(f"{path}/telemetry?before=2&after=1")[0], 400)
        self.assertEqual(self.request(f"{path}/telemetry?event=999999")[0], 404)
        event = self.remote.events(sid, role="lead")["events"][0]
        self.assertIn(f"{event['id']}", self.command("telemetry", sid, "--event", str(event["id"]))[0])
        self.assertIn("lead", self.command("telemetry", sid, "--role", "lead")[0].lower())
        self.assertIn(sid, self.command("status", sid)[0])
        self.assertIn(sid, self.command("laps", "--raceday", project["id"])[0])
        status, headers, body = self.request(f"{path}/build")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertTrue(body.startswith(b"PK"))
        self.assertIn("Preview stopped", self.command("preview", sid, "--stop")[0])
        self.assertIn("http://127.0.0.1", self.command("preview", sid)[0])
        self.assertEqual(self.post(f"{path}/publish", {})[0], 400)
        question = self.remote.chat(sid, "Is the lap complete?")
        self.lead.chats.submit(lambda: None).result(timeout=5)
        self.assertIn("completed", self.remote.chat_reply(sid, question)["text"])

    def test_workspace_settings_and_queued_lap_controls(self):
        project = self.remote.create("Controls")
        lead = self.lead
        data = {"kind": "compatible", "endpoint": "http://127.0.0.1:8181/v1", "model": "fixture"}
        with patch.object(lead, "model", return_value=("OK", (0, 0))):
            self.assertEqual(self.remote.configure(data)["model"], "fixture")
            self.assertIn("fixture", self.command("provider-test")[0])
        with patch("pitcrew.engine.list_provider_models", return_value={"models": [{"id": "fixture"}]}):
            self.assertEqual(self.command("models")[0].strip(), "fixture")
        self.assertIn("removed", self.command("credential", "forget")[0])
        policy = self.data / "limits.json"
        policy.write_text(json.dumps({"sessions": 2, "parallel_reviews": True, "adaptive_sessions": True,
                                      "deep_research": True}), encoding="utf-8")
        setup, _ = self.command("pit-setup", "--apply", str(policy))
        self.assertIn("Concurrent laps  2", setup)
        self.assertIn("Adaptive concurrency  On", setup)
        self.assertIn("Autoresearch Beast Mode  On", setup)
        self.assertIn("reserved", json.loads(self.command("pit-setup", "--pressure")[0]))
        self.assertIn("Docker", self.command("doctor", "--full", "--local-only")[0])
        self.assertEqual(self.request("/api/doctor")[0], 200)
        self.assertIn("No Skills", self.command("toolbox", "--check")[0])
        self.assertIn("callback", self.remote.request("/api/extensions/config"))
        self.assertEqual(self.post("/api/extensions/config", {"text": "", "base": ""})[0], 200)
        skill = {"files": {"SKILL.md": "---\nname: checks\ndescription: Checks\n---\nConfirm focus order."},
                 "roles": ["research"]}
        self.assertEqual(self.post("/api/extensions/skill", skill)[0], 200)
        self.assertIn("SKILL.md", self.remote.request("/api/extensions/skill?name=checks")["files"])
        self.assertIn("playbooks", self.remote.request(f"/api/memory/playbooks?project={project['id']}"))
        self.assertIn("records", self.remote.request("/api/memory/records?q=checks"))
        self.assertEqual(self.post("/api/memory/dream", {})[0], 200)
        self.assertTrue(self.remote.request("/api/memory/history")["history"])
        with patch.object(lead, "dispatch_sessions"):
            sid = self.remote.run(project["id"], "Review the board", False)
            self.assertEqual(self.post("/api/memory/dream", {})[0], 409)
            self.assertEqual(self.request(f"/api/sessions/{sid}/build")[0], 404)
            self.assertIn("Cancellation requested", self.command("cancel", sid)[0])
            self.assertEqual(self.remote.status(sid)["state"], "cancelled")
            self.assertEqual(json.loads(self.command("retry", sid, "--detach", "--json")[0])["id"], sid)
            self.assertEqual(self.remote.status(sid)["state"], "queued")
            self.assertEqual(self.remote.sessions(project["id"])[0]["id"], sid)
