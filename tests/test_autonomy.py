import copy
import threading
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_workspace
from pitcrew import demo
from pitcrew.engine import Lead
from pitcrew.integrations import provider_settings
from pitcrew.sandbox import ExecutionFailed, adaptive_lap_limit, execution_plan, execution_policy


class AutonomyTest(unittest.TestCase):
    def lead(self, docker, **kwargs):
        directory = Path(self.enterContext(TemporaryDirectory()))
        return self.enterContext(closing(Lead(directory, docker=docker, **kwargs)))

    def test_adaptive_limit_accounts_for_heavy_qa_previews_and_missing_measurements(self):
        policy = execution_policy({"adaptive_sessions": True, "sessions": 6})
        capacity = {"cpu": 8, "ram": 4096}
        plan = execution_plan(policy, capacity)
        pressure = {"budget": {"cpu": 8, "ram": 3584}, "external": {"cpu": 0, "ram": 0}, "holds": []}
        self.assertEqual(adaptive_lap_limit(plan, pressure), 2)
        pressure["holds"] = [{"tasks": ["preview"], "cpu": 1, "ram": 512}]
        self.assertEqual(adaptive_lap_limit(plan, pressure), 1)
        pressure["external"] = None
        self.assertEqual(adaptive_lap_limit(plan, pressure), 1)
        self.assertEqual(adaptive_lap_limit({**plan, "adaptive_sessions": False}, pressure), 6)

    def test_scheduler_admits_more_work_when_pressure_recovers(self):
        class Docker(test_workspace.DockerFixture):
            busy = True

            def engine_capacity(self):
                return {"cpu": 8, "ram": 4096}

            def external_usage(self, workspace):
                return {"cpu": 4, "ram": 2304} if self.busy else {"cpu": 0, "ram": 0}

        docker = Docker()
        lead = self.lead(docker)
        lead.configure_execution({"adaptive_sessions": True, "sessions": 3})
        first, second = (lead.create_project(name)["id"] for name in ("First", "Second"))
        running = {project: threading.Event() for project in (first, second)}
        finish = threading.Event()

        def run(session):
            running[session["project_id"]].set()
            if not finish.wait(20):
                raise TimeoutError("Test did not release the lap")
            lead.store.update(session["id"], state="completed")

        with patch.object(lead, "_run", side_effect=run):
            try:
                first_lap = lead.launch(first, "", sample=True)
                self.assertTrue(running[first].wait(3))
                second_lap = lead.launch(second, "", sample=True)
                self.assertEqual(lead.get(second_lap)["state"], "queued")
                self.assertFalse(running[second].is_set())
                docker.busy = False
                self.assertTrue(running[second].wait(12))
                self.assertEqual(lead.execution_settings()["effective_sessions"], 2)
            finally:
                finish.set()
            for sid in (first_lap, second_lap):
                self.assertEqual(test_workspace.WorkspaceTest.completed(lead, sid)["state"], "completed")

    def test_model_slots_limit_requests_and_release_the_slot_after_failure(self):
        first_active = threading.Event()
        second_active = threading.Event()
        release = threading.Event()

        def model(settings, key, persona, data, timeout):
            if data == "first":
                first_active.set()
                if not release.wait(10):
                    raise TimeoutError("Fixture did not release the model call")
                raise RuntimeError("Provider disconnected")
            second_active.set()
            return "OK", (0, 0)

        lead = self.lead(test_workspace.DockerFixture(), model=model)
        lead.configure_execution({"model_slots": 1})
        with ThreadPoolExecutor(max_workers=2) as workers:
            deadline = time.monotonic() + 12
            first = workers.submit(lead.call_model, None, {}, "", "", "first", deadline)
            try:
                self.assertTrue(first_active.wait(3))
                second = workers.submit(lead.call_model, None, {}, "", "", "second", deadline)
                self.assertFalse(second_active.wait(0.4))
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "Provider disconnected"):
                first.result(timeout=3)
            self.assertEqual(second.result(timeout=3), ("OK", (0, 0)))
        self.assertEqual(lead.active_model_calls, 0)

    def test_autoresearch_rejects_fabricated_quotes_and_follows_the_remaining_gap(self):
        urls = ["https://docs.python.org/input", "https://docs.python.org/storage"]
        excerpts = ["The app checks keyboard input in an event handler.", "SQLite writes should happen in a transaction."]
        quotes = dict(zip(urls, excerpts, strict=True))
        observed = []

        def source(url, hosts, timeout, topic):
            return {"url": url, "excerpt": quotes[url]}

        def proof(criterion, index, quote=None):
            return {"criterion": criterion, "url": urls[index], "quote": quote or excerpts[index],
                    "reason": "This directly informs how to implement the criterion."}

        def ask(session, role, data, agent, until, sample, instruction=None):
            if "round" not in data:
                return {"summary": "Input and storage research", "findings": excerpts,
                        "decisions": ["Use transactions"], "sources": [urls[0]]}
            observed.append(list(data["gaps"]))
            return {
                1: {"urls": [urls[0]], "support": [proof("AC1", 0)]},
                2: {"urls": [urls[0], urls[1]], "support": [proof("AC1", 0), proof("AC2", 0, "This quote was invented and cannot be verified")]},
                3: {"done": True, "support": [proof("AC2", 1)]},
            }[data["round"]]

        lead = self.lead(test_workspace.DockerFixture(), fetch=source)
        project = lead.create_project("Research fixture")
        sid = lead.store.session(project["id"], "Build a task board", provider_settings({"kind": "demo"}))
        session = lead.get(sid)
        session["execution"] = execution_plan(execution_policy({"deep_research": True}), None)
        criteria = [{"id": "AC1", "description": "Keyboard use"}, {"id": "AC2", "description": "Durable state"}]
        data = {"request": session["prompt"], "approved": {"lead": {"criteria": criteria}}}
        deadline = time.monotonic() + 15
        with patch.object(lead, "ask", side_effect=ask):
            with lead.agent(session, "research", "research", 1, data, deadline) as agent:
                result = lead.research(session, data, agent, deadline, False)
        self.assertEqual(observed, [["AC1", "AC2"], ["AC1", "AC2"], ["AC2"]])
        self.assertEqual(result["research_coverage"], {"supported": [proof("AC1", 0), proof("AC2", 1)], "gaps": []})
        self.assertEqual(result["sources"], urls)
        rounds = lead.store.rows("SELECT payload FROM events WHERE kind='research_round' ORDER BY id")
        self.assertEqual([row["payload"]["remaining_criteria"] for row in rounds], [["AC1", "AC2"], ["AC2"], []])

    def test_failed_qa_checkpoint_resumes_development_and_preserves_the_test_plan(self):
        class Docker(test_workspace.DockerFixture):
            def __init__(self):
                super().__init__()
                self.qa_inputs = []

            def execute(self, name, workspace, role, source, checks, timeout, log, *, resources=None):
                if role == "qa":
                    self.qa_inputs.append((checks / "journeys.json").read_bytes())
                    if "<!-- Repair verified -->" not in (source / "index.html").read_text(encoding="utf-8"):
                        raise ExecutionFailed("Keyboard focus did not return after closing the panel")
                return super().execute(name, workspace, role, source, checks, timeout, log, resources=resources)

        docker = Docker()
        lead = self.lead(docker)
        project = lead.create_project("QA repair fixture")
        ask = lead.ask

        def model(session, role, data, agent, until, sample, **options):
            if role == "development" and "repair" in data:
                self.assertIn("Keyboard focus", data["repair"]["failure"])
                draft = copy.deepcopy(data["repair"]["candidate"])
                draft["files"]["index.html"] += "\n<!-- Repair verified -->\n"
                return draft
            return ask(session, role, data, agent, until, sample, **options)

        run_stage = lead.run_stage

        def interrupt(session, tree, stage, *args):
            if stage == "development" and session.get("repair"):
                raise RuntimeError("Interrupted after repair checkpoint")
            return run_stage(session, tree, stage, *args)

        with patch.object(lead, "ask", side_effect=model):
            with patch.object(lead, "run_stage", side_effect=interrupt):
                sid = lead.launch(project["id"], "", sample=True)
                first = test_workspace.WorkspaceTest.completed(lead, sid)
            self.assertEqual(first["state"], "failed")
            self.assertIn("repair checkpoint", first["error"])
            lead.retry(sid)
            result = test_workspace.WorkspaceTest.completed(lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        self.assertEqual(len(docker.qa_inputs), 2)
        self.assertEqual(docker.qa_inputs[0], docker.qa_inputs[1])
        self.assertEqual(len([agent for agent in result["agents"] if agent["role"] == "development"]), 2)
        self.assertIn("<!-- Repair verified -->", (lead.repo.root / f"projects/{project['id']}/app/index.html").read_text())
        self.assertEqual(len(lead.store.rows("SELECT id FROM events WHERE kind='repair_requested'")), 1)
