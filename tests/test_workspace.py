import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZipFile

from omni import demo
from omni.contracts import STAGES, manifest, validate
from omni.engine import Lead, TERMINAL
from omni.integrations import fetch_source, packages, provider_settings
from omni.sandbox import Docker

ROOT = Path(__file__).resolve().parents[1]


class PreviewFixture:
    def __init__(self):
        self.url = "http://127.0.0.1:8001"
        self.closed = threading.Event()
        self.on_close = None

    def close(self):
        self.closed.set()
        if self.on_close:
            self.on_close(self)


class DockerFixture:
    """Test double. Only smoke.py exercises the actual container runtime."""
    def __init__(self, failures=0):
        self.failures = failures
        self.names = []
        self.executions = []

    def doctor(self):
        pass

    def clean_orphans(self, _):
        pass

    @contextmanager
    def role(self, name, workspace, role, timeout):
        self.names.append((name, role))
        yield lambda tool, arguments, remaining: self.invoke_in_worker(role, tool, arguments, remaining)

    @staticmethod
    def invoke_in_worker(role, tool, arguments, timeout):
        output = subprocess.run([sys.executable, str(ROOT / "containers" / "role.py"), role],
                                input=json.dumps({"tool": tool, "arguments": arguments}) + "\n", text=True, capture_output=True, check=True,
                                env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=timeout)
        ready, content = output.stdout.split("\n", 1)
        if ready != "READY":
            raise AssertionError("Worker handshake failed")
        answer = json.loads(content)
        if error := answer.get("error"):
            raise ValueError(error)
        return answer["result"]

    def execute(self, name, workspace, role, source, checks, timeout, log):
        self.executions.append(role)
        if role == "qa":
            assert (source / "index.html").exists()
            assert json.loads((checks / "journeys.json").read_text())
            if self.failures > 0:
                self.failures -= 1
                log("Browser test fixture failed. bearer sample-sensitive-value")
                raise TimeoutError("Browser fixture expired")
        log("Execution fixture completed")
        return {"execution": "fixture", "exitCode": 0}

    def preview(self, name, workspace, directory, lifetime):
        return PreviewFixture()


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def lead(self, docker, **kwargs):
        instance = Lead(Path(self.temporary.name), docker=docker, **kwargs)
        self.addCleanup(instance.close)
        return instance

    @staticmethod
    def completed(lead, sid):
        expires = time.monotonic() + 15
        while time.monotonic() < expires:
            result = lead.get(sid)
            if result["state"] in TERMINAL:
                return lead.store.summary(sid)
            time.sleep(0.02)
        raise TimeoutError("Session fixture stalled")

    def test_pipeline_retries_qa_and_merges_its_verified_build(self):
        docker = DockerFixture(failures=1)
        lead = self.lead(docker)
        project = lead.store.project("Workshop", "")
        sid = lead.launch(project["id"], "", sample=True)
        result = self.completed(lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        self.assertEqual([gate["stage"] for gate in result["gates"]], ["lead", *STAGES])
        self.assertTrue(all(gate["state"] == "passed" and gate["commit_sha"] for gate in result["gates"]))
        self.assertEqual(docker.executions, ["deploy", "qa", "qa", "deploy"])
        self.assertEqual(len(docker.names), len({name for name, _ in docker.names}))
        qa = [agent for agent in result["agents"] if agent["role"] == "qa"]
        self.assertEqual([agent["status"] for agent in qa], ["Retried", "Success"])
        log = lead.store.rows("SELECT payload FROM events WHERE attempt_id=? AND kind='execution_log'", (qa[0]["id"],))
        self.assertIn("[REDACTED]", log[0]["payload"]["output"])
        branch = lead.repo.git(lead.repo.root, "branch", "--merged", "main").stdout
        self.assertIn(result["branch"], branch)
        main = lead.repo.root
        self.assertTrue((main / f"projects/{project['id']}/app/index.html").is_file())
        self.assertEqual(len(list((main / f".omni/sessions/{sid}/audit").glob("*.json"))), 1)
        with ZipFile(result["bundle"]) as artifact:
            self.assertIn("index.html", artifact.namelist())
            self.assertEqual(json.loads(artifact.read(".omni-build.json"))["session"], sid)
        self.assertTrue(result["preview"])
        lead.change_preview(sid, False)
        self.assertIsNone(lead.get(sid)["preview"])
        self.assertTrue(lead.change_preview(sid, True))

    def test_exhausted_qa_gate_prevents_release_and_can_resume(self):
        docker = DockerFixture(failures=3)
        lead = self.lead(docker)
        project = lead.store.project("Workshop", "")
        sid = lead.launch(project["id"], "", sample=True)
        result = self.completed(lead, sid)
        self.assertEqual(result["state"], "failed")
        self.assertEqual([gate["stage"] for gate in result["gates"]], ["lead", *STAGES[:-1]])
        self.assertFalse(result["bundle"])
        self.assertFalse((lead.repo.root / "projects").is_dir())
        lead.retry(sid)
        resumed = self.completed(lead, sid)
        self.assertEqual(resumed["state"], "completed", resumed["error"])
        self.assertEqual([gate["commit_sha"] for gate in resumed["gates"][:5]],
                         [gate["commit_sha"] for gate in result["gates"][:5]])
        self.assertEqual(len([agent for agent in resumed["agents"] if agent["role"] == "research"]), 1)

    def test_telemetry_filters_role_stage_and_attempt_across_retries(self):
        lead = self.lead(DockerFixture(failures=1))
        project = lead.store.project("Trace filters", "")
        sid = lead.launch(project["id"], "", sample=True)
        result = self.completed(lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        review = lead.store.telemetry(sid, role="review", stage="research")["events"]
        self.assertTrue(review)
        self.assertTrue(all(item["role"] == "review" and item["stage"] == "research" for item in review))
        qa_attempts = [agent for agent in result["agents"] if agent["role"] == "qa"]
        retry = qa_attempts[0]["id"]
        retry_events = lead.store.telemetry(sid, role="qa", stage="qa", attempt=retry)["events"]
        self.assertTrue(any(item["kind"] == "execution_log" and "[REDACTED]" in item["excerpt"]
                            for item in retry_events))
        self.assertEqual({item["attempt_id"] for item in retry_events}, {retry})
        self.assertFalse(lead.store.telemetry(sid, stage="research", attempt=retry)["events"])
        stage = lead.store.telemetry(sid, stage="research")["events"]
        self.assertTrue(any(item["kind"] == "gate_passed" and item["attempt_id"] is None for item in stage))
        first = lead.store.telemetry(sid, limit=3)
        self.assertEqual(first["events"][0]["id"], result["event_revision"])
        after = lead.store.telemetry(sid, before=first["before"], limit=3)
        self.assertEqual(len(after["events"]), 3)
        self.assertLess(after["events"][0]["id"], first["events"][-1]["id"])

    def test_live_contract_rejects_a_gate_before_starting_next_stage(self):
        calls = []
        rejected = False
        checked_source = False

        def model(config, key, persona, data, timeout):
            nonlocal rejected, checked_source
            self.assertEqual(key, "test-provider-key-value")
            stage = data["stage"]
            if "independent QA reviewer" in persona:
                if stage == "architecture" and not rejected:
                    rejected = True
                    value = {"passed": False, "checks": ["API"], "feedback": "Clarify the isolation boundary."}
                else:
                    value = {"passed": True, "checks": ["Contract"], "feedback": "Accepted."}
            elif "Propose the next high-value evidence" in persona:
                value = {"done": bool(data["evidence"]), "urls": [] if data["evidence"] else [demo.SOURCE], "packages": []}
            elif stage == "development" and not checked_source:
                checked_source = True
                value = {"tool_call": {"name": "source_check", "draft": demo.EXAMPLES["development"]}}
            else:
                value = copy.deepcopy(demo.EXAMPLES[stage])
                if stage == "development":
                    self.assertIn("app.js", data["tool_feedback"]["result"]["syntax_checked"])
                calls.append(stage)
            return json.dumps(value), (13, 9)

        lead = self.lead(DockerFixture(), model=model, scan=lambda *_: [],
                         fetch=lambda *args: {"url": args[0], "excerpt": "Verified source fixture."})
        lead.configure({"kind": "compatible", "endpoint": "http://127.0.0.1:8181/v1",
                        "model": "fixture", "input_rate": 2, "output_rate": 4, "api_key": "test-provider-key-value"})
        project = lead.store.project("Workshop", "")
        sid = lead.launch(project["id"], demo.PROMPT)
        result = self.completed(lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        self.assertEqual(calls, ["lead", "research", "architecture", "architecture", "design", "development", "qa", "deploy"])
        self.assertGreater(result["tokens"], 0)
        self.assertGreater(result["cost"], 0)
        self.assertNotIn("test-provider-key-value", repr(lead.store.rows("SELECT * FROM events")))

    def test_cli_can_read_and_filter_a_completed_workspace(self):
        lead = self.lead(DockerFixture())
        project = lead.store.project("CLI fixture", "")
        sid = lead.launch(project["id"], "", sample=True)
        self.assertEqual(self.completed(lead, sid)["state"], "completed")
        common = [sys.executable, "-m", "omni", "--data", self.temporary.name, "--json"]
        for arguments, check in [
            (["status", sid], lambda values: self.assertEqual(values["state"], "completed")),
            (["sessions", "--project", project["id"]], lambda values: self.assertEqual(values[0]["id"], sid)),
        ]:
            reply = subprocess.run([*common, *arguments], check=True, capture_output=True, text=True, cwd=ROOT)
            check(json.loads(reply.stdout))
        reply = subprocess.run([*common, "logs", sid, "--role", "development", "--stage", "development"],
                               check=True, capture_output=True, text=True, cwd=ROOT)
        events = [json.loads(line) for line in reply.stdout.splitlines()]
        self.assertEqual({item["role"] for item in events}, {"development"})
        self.assertTrue(any(item["tool"] == "role.source_check" for item in events))
        with self.assertRaisesRegex(ValueError, "outside this role"):
            DockerFixture.invoke_in_worker("design", "source_check", {}, 12)

    def test_memory_deduplicates_reinforces_and_keeps_proven_links(self):
        lead = self.lead(DockerFixture())
        store = lead.store
        first = store.project("One", "")
        second = store.project("Two", "")
        sid = store.session(first["id"], demo.PROMPT, {"kind": "demo"})
        store.update(sid, state="completed")
        store.remember(first["id"], [("decision", "Bounded retry", "Respect bounded retry and preserve diagnostic context.")])
        store.remember(second["id"], [("decision", "Same record", "respect  bounded retry and preserve diagnostic context.")])
        store.remember(second["id"], [("fix", "Retries", "Apply bounded retry and preserve diagnostic context after a failure.")],
                       problem="Timeout during verification")
        node = store.retrieve("bounded retry diagnostic")[0]["id"]
        for _ in range(2):
            store.remember(first["id"], [], [node], sid, "qa")
        self.assertEqual(store.memory_health()["applications"], 1)
        self.assertEqual(store.row("SELECT weight FROM memory_nodes WHERE id=?", (node,))["weight"], 2)
        self.assertGreater(store.dream()["synthesized"], 0)
        self.assertGreater(store.memory_health()["links"], 1)

    def test_contract_guards_paths_dependencies_and_browser_targets(self):
        files = {**demo.FILES, "../escape": "content"}
        with self.assertRaises(ValueError):
            manifest(files)
        with self.assertRaises(ValueError):
            packages({"requirements.txt": "pkg>=1"})
        self.assertEqual(packages({"requirements.txt": "pkg==1.2.0"}), [("PyPI", "pkg", "1.2.0")])
        with self.assertRaises(ValueError):
            fetch_source("http://127.0.0.1/")
        with self.assertRaises(ValueError):
            provider_settings({"kind": "foundry", "model": "test", "endpoint": "https://example.com/openai/v1"})
        qa = copy.deepcopy(demo.EXAMPLES["qa"])
        qa["journeys"][0]["steps"][0]["path"] = "//example.com"
        with self.assertRaises(ValueError):
            validate("qa", qa)
        docker = Docker.args("test", "test", "qa")
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--user=10001:10001", "--shm-size=256m"):
            self.assertIn(flag, docker)
