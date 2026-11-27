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
from unittest.mock import patch
from zipfile import ZipFile

from pitcrew import demo
from pitcrew.contracts import PERSONAS, STAGES, manifest, validate
from pitcrew.engine import Lead, TERMINAL
from pitcrew.integrations import fetch_source, packages, provider_settings
from pitcrew.repository import Repository, github_url
from pitcrew.sandbox import ContainerCapacity, Docker, execution_plan, execution_policy, lap_profile, profile

ROOT = Path(__file__).resolve().parents[1]


def release_verdict(data, verdict):
    if data.get("release_source") and verdict["passed"]:
        verdict["security"] = {"checks": ["Verified the release security evidence and source fixture."], "residual_risks": []}
    return verdict


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
        return self.engine_capacity()

    def engine_capacity(self):
        return None

    def external_usage(self, workspace):
        return None

    def probe_mounts(self, directory):
        pass

    def clean_orphans(self, _):
        pass

    @contextmanager
    def role(self, name, workspace, role, timeout, *, resources=None):
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

    def execute(self, name, workspace, role, source, checks, timeout, log, *, resources=None):
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

    def preview(self, name, workspace, directory, lifetime, *, resources=None):
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
        expires = time.monotonic() + 45
        while time.monotonic() < expires:
            result = lead.get(sid)
            if result["state"] in TERMINAL:
                return lead.store.summary(sid)
            time.sleep(0.02)
        raise TimeoutError(f"Session fixture stalled in {result['stage']} ({result['state']})")

    def test_lead_sizes_stay_within_operator_limits_and_use_standard_after_failure(self):
        plan = copy.deepcopy(demo.EXAMPLES["lead"])
        plan["crew_notes"] = {"development": "Keep the example data deterministic."}
        plan["sandbox_sizes"] = {"build": "compact", "specialist": "compact"}
        validate("lead", plan)
        policy = execution_plan(execution_policy({"overrides": {"build": {"ram": 384}}}), None)
        normal = profile("build", policy)
        compact = lap_profile("build", policy, plan)
        self.assertLess(compact["cpu"], normal["cpu"])
        self.assertEqual({**compact, "cpu": normal["cpu"]}, normal)
        self.assertEqual(lap_profile("build", policy, plan, 2), normal)
        flags = Docker.args("runner", "workspace", "build", resources=compact)
        self.assertIn("--network=none", flags)
        self.assertIn("--read-only", flags)
        with self.assertRaisesRegex(ValueError, "compact or standard"):
            validate("lead", {**plan, "sandbox_sizes": {"preview": "compact"}})

    def test_release_blocks_new_dependency_advisories_before_execution(self):
        coordinates = [("PyPI", "sample-package", "1.0")]

        def scan(dependencies, timeout):
            self.assertEqual(dependencies, coordinates)
            self.assertGreater(timeout, 0)
            return [{"package": "sample-package", "version": "1.0", "id": "OSV-fixture"}]

        docker = DockerFixture()
        lead = self.lead(docker, scan=scan)
        project = lead.create_project("Release advisory check")
        sid = lead.store.session(project["id"], "Build", lead.provider())
        with patch("pitcrew.engine.packages", return_value=coordinates):
            with self.assertRaisesRegex(ValueError, "Release blocked.*OSV-fixture"):
                lead.verify(lead.get(sid), "deploy", {}, {"development": {"files": demo.FILES}},
                            {"id": None}, time.monotonic() + 3, True, profile("release"))
        self.assertEqual(docker.executions, [])

    def test_specialists_run_concurrently_and_report_in_plan_order(self):
        lead = self.lead(DockerFixture())
        project = lead.create_project("Parallel crew")
        sid = lead.store.session(project["id"], "Improve the task board", provider_settings({"kind": "demo"}))
        session = lead.get(sid)
        session["execution"] = lead.execution_settings()
        assignments = [{"name": name, "persona": "Review a specific user journey", "assignment": assignment}
                       for name, assignment in (("Keyboard", "Inspect focus order"), ("State", "Inspect persistence"))]
        plan = {"specialists": assignments, "criteria": [{"id": "AC1", "description": "The board works"}]}
        both_started = threading.Barrier(2, timeout=5)
        second_finished = threading.Event()
        retry = {"message": "The first plan missed focus restoration"}

        def ask(session, role, data, agent, until, sample):
            index = assignments.index(data["specialist"])
            self.assertIn("attempt 2", agent["persona"])
            self.assertEqual(data["retry_context"], retry)
            other = assignments[1 - index]
            self.assertEqual(data["other_assignments"], [{"name": other["name"], "assignment": other["assignment"]}])
            self.assertEqual(data["criteria"], plan["criteria"])
            self.assertEqual(data["repository"], {"commit": "pinned"})
            both_started.wait()
            if index == 0:
                self.assertTrue(second_finished.wait(5))
            return {"summary": data["specialist"]["name"], "findings": ["Finding"], "recommendations": ["Check"]}

        def record(*args):
            result = submit(*args)
            if result["summary"] == "State":
                second_finished.set()
            return result

        submit = lead.role_tool
        with patch.object(lead, "ask", side_effect=ask), patch.object(lead, "role_tool", side_effect=record):
            reports = lead.delegate(session, plan, {"retry_context": retry, "repository": {"commit": "pinned"}},
                                    2, time.monotonic() + 12, True, "lead-attempt")
        self.assertEqual([report["name"] for report in reports], ["Keyboard", "State"])
        self.assertEqual(len({report["attempt"] for report in reports}), 2)
        self.assertFalse(lead.capacity.report()["holds"])
        events = lead.store.rows("SELECT payload FROM events WHERE session_id=? AND kind='delegated'", (sid,))
        self.assertEqual({event["payload"]["parent"] for event in events}, {"lead-attempt"})

    def test_failed_specialist_round_joins_other_worker_before_retry(self):
        lead = self.lead(DockerFixture())
        project = lead.create_project("Specialist recovery")
        sid = lead.store.session(project["id"], "Review the board", provider_settings({"kind": "demo"}))
        session = lead.get(sid)
        session["execution"] = lead.execution_settings()
        plan = {"specialists": [{"name": name, "assignment": name} for name in ("First", "Second")]}
        failed = threading.Event()
        cleaned_up = threading.Event()

        def specialist(session, plan, data, assignment, *args):
            if assignment["name"] == "First":
                failed.set()
                raise ValueError("First assessment failed")
            self.assertTrue(failed.wait(5))
            cleaned_up.set()
            return {"name": "Second"}

        with patch.object(lead, "run_specialist", side_effect=specialist):
            with self.assertRaisesRegex(ValueError, "First assessment failed"):
                lead.delegate(session, plan, {}, 1, time.monotonic() + 12, True, "lead-attempt")
        self.assertTrue(cleaned_up.is_set())

    def test_pipeline_retries_qa_and_merges_its_verified_build(self):
        docker = DockerFixture(failures=1)
        lead = self.lead(docker)
        project = lead.store.project("Workshop", "")
        sid = lead.launch(project["id"], "", sample=True)
        result = self.completed(lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        self.assertEqual([gate["stage"] for gate in result["gates"]], ["lead", *STAGES])
        self.assertTrue(all(gate["state"] == "passed" and gate["commit_sha"] for gate in result["gates"]))
        release = result["gates"][-1]["details"]
        self.assertEqual(release["evidence"]["security_review"]["isolation"]["network"], "none")
        self.assertEqual(release["evidence"]["security_review"]["mode"], "practice")
        self.assertIn("security", release["reviews"][0]["review"])
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

    def test_github_laps_commit_to_source_history_and_learn_from_verified_work(self):
        root = Path(self.temporary.name)
        upstream = root / "upstream"
        source = root / "source"
        source.mkdir()
        git = Repository.git
        git(root, "init", "--bare", "--initial-branch=main", str(upstream))
        git(source, "init", "--initial-branch=main")
        Repository.write_files(source, {**demo.FILES, "README.md": "Keep this file.\n", "obsolete.txt": "Remove this."})
        (source / "asset.bin").write_bytes(b"\x00\xff")
        git(source, "add", "-A")
        git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-m", "Base project")
        base = git(source, "rev-parse", "HEAD").stdout.strip()
        git(source, "push", str(upstream), "HEAD:refs/heads/main")
        url = "https://github.com/example/fieldwork.git"
        observed = {}
        specialist = {"name": "Acceptance specialist", "persona": "Focus on observable user flows.",
                      "assignment": "Read the repo and suggest acceptance checks."}

        def model(settings, key, persona, data, timeout):
            role = next(role for role, instruction in PERSONAS.items() if persona.startswith(instruction))
            self.assertEqual(data["repository"]["url"], url)
            observed[role] = data["agent_playbook"]["revision"]
            if role == "research" and "round" in data:
                value = {"urls": [demo.SOURCE] if data["round"] == 1 else [], "done": True}
            elif role == "review":
                value = {"passed": True, "checks": ["Verified acceptance evidence"], "feedback": "Approved."}
            elif role == "specialist":
                self.assertEqual(data["specialist"], specialist)
                value = {"summary": "Use the existing UI.", "findings": ["The project includes a README."],
                         "recommendations": ["Test the changed title."]}
            else:
                value = copy.deepcopy(demo.EXAMPLES[role])
                if role == "lead":
                    value["specialists"] = [specialist]
                if role == "development":
                    prior = data["previous_source"]
                    self.assertEqual(prior["README.md"], "Keep this file.\n")
                    self.assertNotIn("asset.bin", prior)
                    value["files"] = {"index.html": prior["index.html"] + "\n<!-- approved edit -->\n"}
                    value["remove"] = ["obsolete.txt"] if "obsolete.txt" in prior else []
            return json.dumps(release_verdict(data, value)), (0, 0)

        def local_transport(cwd, *args, **options):
            return git(cwd, *(str(upstream) if arg == url else arg for arg in args), **options)

        docker = DockerFixture()
        lead = self.lead(docker, model=model, scan=lambda *_: [],
                         fetch=lambda *args: {"url": args[0], "excerpt": "Verified source fixture."})
        lead.configure({"kind": "compatible", "endpoint": "http://127.0.0.1:8181/v1", "model": "fixture"})
        with patch.object(lead.repo, "git", side_effect=local_transport):
            race = lead.create_project("GitHub fixture", repository=url.removesuffix(".git"), branch="main")
            lead.store.remember(race["id"], [("decision", "Project scope", "Use a secluded build fixture.")])
            self.assertEqual(lead.store.retrieve("secluded"), [])
            self.assertEqual(len(lead.store.retrieve("secluded", project=race["id"])), 1)
            sid = lead.launch(race["id"], "Update the existing Fieldwork page.")
            first = self.completed(lead, sid)
            self.assertEqual(first["state"], "completed", first["error"])
            self.assertEqual(first["github_base"], base)
            self.assertEqual(set(observed), set(PERSONAS))
            self.assertIn("specialist", [role for _, role in docker.names])
            commit = first["github_commit"]
            self.assertEqual(git(lead.repo.root, "rev-parse", commit + "^").stdout.strip(), base)
            self.assertEqual(git(lead.repo.root, "show", commit + ":README.md").stdout, "Keep this file.\n")
            self.assertEqual(git(lead.repo.root, "show", commit + ":asset.bin", binary=True).stdout, b"\x00\xff")
            self.assertNotIn("obsolete.txt", git(lead.repo.root, "ls-tree", "-r", "--name-only", commit).stdout)
            self.assertFalse(git(upstream, "branch", "--list", "pitcrew/*").stdout.strip())
            published = lead.publish(sid)
            self.assertEqual(git(upstream, "rev-parse", published["branch"]).stdout.strip(), commit)
            self.assertEqual(git(upstream, "rev-parse", "main").stdout.strip(), base)
            self.assertEqual(lead.publish(sid), published)
            self.assertEqual(lead.dream()["playbooks_revised"], len(PERSONAS))
            self.assertEqual(lead.dream()["playbooks_revised"], 0)
            self.assertEqual(lead.store.playbook(lead.create_project("Isolated")["id"], "development")["revision"], 0)
            second = self.completed(lead, lead.launch(race["id"], "Make another edit to the existing page."))
            self.assertEqual(second["state"], "completed", second["error"])
            self.assertEqual(second["github_base"], commit)
            self.assertTrue(all(revision == 1 for revision in observed.values()), observed)

    def test_github_repository_urls_cannot_supply_credentials_or_another_host(self):
        for value in ("file:///tmp/repo", "https://github.com.evil.test/user/repo",
                      "https://token@github.com/user/repo", "https://github.com/user/repo?token=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                github_url(value)

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
                if stage == "deploy":
                    self.assertIn("index.html", data["release_source"])
                    report = data["evidence"]["security_review"]
                    self.assertTrue(all(row["status"] == "fetched" for row in report["guidance"]))
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
            return json.dumps(release_verdict(data, value)), (13, 9)

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
        self.assertEqual(result["gates"][-1]["details"]["evidence"]["security_review"]["mode"], "release")

    def test_cli_can_read_and_filter_a_completed_workspace(self):
        lead = self.lead(DockerFixture())
        project = lead.store.project("CLI fixture", "")
        sid = lead.launch(project["id"], "", sample=True)
        self.assertEqual(self.completed(lead, sid)["state"], "completed")
        common = [sys.executable, "-m", "pitcrew", "--data", self.temporary.name, "--json"]
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

    def test_concurrent_sessions_skip_a_project_already_using_a_worker(self):
        lead = self.lead(DockerFixture())
        lead.configure_execution({"sessions": 2})
        first = lead.store.project("Project one", "")
        second = lead.store.project("Project two", "")
        first_started, another_started, release = threading.Event(), threading.Event(), threading.Event()

        def run_fixture(session):
            if session["project_id"] == first["id"]:
                first_started.set()
                self.assertTrue(release.wait(5))
            else:
                another_started.set()
            lead.store.update(session["id"], state="completed")

        with patch.object(lead, "_run", side_effect=run_fixture):
            try:
                first_session = lead.launch(first["id"], "", sample=True)
                self.assertTrue(first_started.wait(3))
                queued = lead.launch(first["id"], "", sample=True)
                other = lead.launch(second["id"], "", sample=True)
                self.assertTrue(another_started.wait(3))
                self.assertEqual(lead.get(queued)["state"], "queued")
                lead.cancel(queued)
                self.assertEqual(lead.get(queued)["state"], "cancelled")
                self.assertEqual(self.completed(lead, other)["state"], "completed")
            finally:
                release.set()
            self.assertEqual(self.completed(lead, first_session)["state"], "completed")

    def test_parallel_reviews_both_have_to_pass_a_gate(self):
        docker = DockerFixture()
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        votes = 0
        original = docker.role

        @contextmanager
        def role(name, workspace, role_name, timeout, *, resources=None):
            with original(name, workspace, role_name, timeout, resources=resources) as invoke:
                if role_name != "review":
                    yield invoke
                    return
                barrier.wait()

                def review(tool, arguments, remaining):
                    nonlocal votes
                    if tool == "submit":
                        with lock:
                            votes += 1
                            reject = votes == 1
                        if reject:
                            arguments = {"draft": {"passed": False, "checks": ["Criteria"], "feedback": "Revise the plan."}}
                    return invoke(tool, arguments, remaining)

                yield review

        docker.role = role
        lead = self.lead(docker)
        lead.configure_execution({"parallel_reviews": True, "limits": {"review": {"ram": 192}}})
        project = lead.store.project("Parallel review fixture", "")
        sid = lead.launch(project["id"], "", sample=True)
        result = self.completed(lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        plan = result["gates"][0]
        self.assertEqual(plan["details"]["attempt"], 2)
        self.assertEqual({entry["focus"] for entry in plan["details"]["reviews"]}, {"Acceptance criteria", "Execution evidence"})
        self.assertEqual(votes, 16)
        self.assertTrue(all(agent["profile"]["ram"] == 192 for agent in result["agents"] if agent["role"] == "review"))

    def test_container_limits_and_reservations_keep_docker_flags_and_capacity(self):
        policy = execution_plan(execution_policy({"limits": {"qa_browser": {"cpu": 1.75, "ram": 896, "pids": 150}}}), None)
        resources = profile("qa_browser", policy)
        flags = Docker.args("test", "test", "qa_browser", resources=resources)
        for flag in ("--cpus=1.75", "--memory=896m", "--memory-swap=896m", "--pids-limit=150", "--shm-size=256m"):
            self.assertIn(flag, flags)
        self.assertNotIn("--shm-size=256m", Docker.args("test", "test", "qa"))
        with self.assertRaises(ValueError):
            execution_policy({"limits": {"qa_browser": {"privileged": True}}})
        capacity = ContainerCapacity()
        capacity.set_engine_capacity({"cpu": 2, "ram": 2048})
        waiting, acquired = threading.Event(), threading.Event()

        def other_task():
            with capacity.reserve([resources], lambda: 1, lambda _: waiting.set()):
                acquired.set()

        with capacity.reserve([resources], lambda: 1, lambda _: None):
            thread = threading.Thread(target=other_task)
            thread.start()
            self.assertTrue(waiting.wait(2))
            self.assertFalse(acquired.is_set())
            capacity.set_engine_capacity({"cpu": 4, "ram": 2048})
            self.assertTrue(acquired.wait(2))
        thread.join(2)
        self.assertTrue(acquired.is_set())
        self.assertEqual(capacity.used, (0, 0))

    def test_automatic_task_sizing_uses_engine_capacity_and_preserves_manual_fields(self):
        # Old settings included untouched reference values even when no limits were intentionally pinned.
        policy = execution_policy({"limits": {"qa_browser": {"cpu": 2, "ram": 1536, "pids": 180}}})
        self.assertEqual(policy["overrides"], {"qa_browser": {"pids": 180}})
        small = execution_plan(policy, {"cpu": 2, "ram": 2048})
        desktop = execution_plan(policy, {"cpu": 8, "ram": 3741})
        large = execution_plan(policy, {"cpu": 32, "ram": 32768})
        self.assertEqual(small["limits"]["qa_browser"], {"cpu": 0.75, "ram": 768, "pids": 180})
        self.assertEqual(desktop["limits"]["qa_browser"], {"cpu": 2, "ram": 1408, "pids": 180})
        self.assertEqual(large["limits"]["qa_browser"], {"cpu": 4, "ram": 3072, "pids": 180})
        self.assertEqual(small["limits"]["research"]["ram"], 96)
        self.assertEqual(execution_policy(large), policy)
        pinned = execution_policy({"overrides": {"qa_browser": {"ram": 1536}}})
        self.assertEqual(execution_plan(pinned, small["capacity"])["limits"]["qa_browser"]["ram"], 1536)

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
        docker = Docker.args("test", "test", "qa_browser")
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--user=10001:10001", "--shm-size=256m"):
            self.assertIn(flag, docker)
