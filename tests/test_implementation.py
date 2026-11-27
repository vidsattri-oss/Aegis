import json
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_workspace
from aegis import demo
from aegis.contracts import manifest, validate_workstreams
from aegis.engine import BudgetPaused, Lead
from aegis.usage import Usage


def implementation_plan():
    return {"interface": "Load app.js and styles.css from index.html. Keep the existing DOM IDs.", "tasks": [
        {"name": "Page structure", "assignment": "Build the entry page and runtime manifest.",
         "paths": ["index.html", "harness.json"], "read_paths": [], "criteria": ["AC1"]},
        {"name": "Board behavior", "assignment": "Implement interactions and styling.",
         "paths": ["app.js", "styles.css", "panorama.css"], "read_paths": ["index.html"], "criteria": ["AC2"]},
    ]}


class ImplementationTest(unittest.TestCase):
    def lead(self, **kwargs):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return self.enterContext(closing(Lead(directory, docker=test_workspace.DockerFixture(), **kwargs)))

    def setup_stage(self, lead):
        project = lead.create_project("Parallel Development Team")
        sid = lead.store.session(project["id"], "Build the board", lead.provider())
        session = lead.get(sid)
        session["execution"] = lead.execution_settings()
        architecture = {**demo.EXAMPLES["architecture"], "implementation": implementation_plan()}
        approved = {"lead": demo.EXAMPLES["lead"], "architecture": architecture, "design": demo.EXAMPLES["design"]}
        return session, approved

    def test_parallel_workers_integrate_disjoint_patches_without_another_model_author(self):
        lead = self.lead()
        barrier = threading.Barrier(2, timeout=10)
        repaired = []
        ask = lead.ask_model

        def model(session, role, data, agent, until, sample, *, instruction=None):
            draft = ask(session, role, data, agent, until, sample, instruction=instruction)
            if role == "architecture":
                draft["implementation"] = implementation_plan()
            if role == "development":
                task = data["workstream"]
                self.assertNotIn("Previous unrelated source", repr(data))
                self.assertEqual(agent["mcp"], {})
                if "patch_feedback" not in data:
                    barrier.wait()
                    if task["name"] == "Board behavior":
                        draft["files"]["index.html"] = "Out of scope"
                else:
                    repaired.append(data["patch_feedback"])
            return draft

        with patch.object(lead, "ask_model", side_effect=model):
            project = lead.create_project("Parallel completion")
            sid = lead.launch(project["id"], "", sample=True)
            result = test_workspace.WorkspaceTest.completed(lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        self.assertEqual(repaired, ["The patch writes outside its assigned files"])
        gate = next(gate for gate in result["gates"] if gate["stage"] == "development")
        evidence = gate["details"]["evidence"]["implementation"]
        self.assertFalse(evidence["fallback"])
        self.assertEqual([item["name"] for item in evidence["workstreams"]], ["Page structure", "Board behavior"])
        workers = [agent for agent in result["agents"] if "persona_name" in agent["profile"] and agent["role"] == "development"]
        self.assertEqual(len(workers), 2)
        self.assertTrue(all(agent["profile"]["network"] == "none" and agent["profile"]["root"] == "read-only" for agent in workers))

    def test_budget_pause_reuses_validated_patch_and_preserves_the_base(self):
        calls = []

        def model(_settings, _key, _persona, data, _timeout):
            task = data["workstream"]
            calls.append(task["name"])
            self.assertNotIn("README.md", data["previous_source"])
            self.assertLessEqual(set(data["previous_source"]), set(task["paths"] + task["read_paths"]))
            draft = {"summary": task["name"], "files": {path: demo.FILES[path] for path in task["paths"]}}
            return json.dumps(draft), Usage((2, 2), 0.02, "vendor_charge")

        lead = self.lead(model=model)
        lead.configure({"kind": "compatible", "endpoint": "https://openrouter.ai/api/v1", "model": "fixture"})
        session, approved = self.setup_stage(lead)
        lead.budget(session["project_id"], 0.02)
        base = {"README.md": "Preserve this file.\n", "index.html": demo.FILES["index.html"]}
        data = lead.context(session, "development", approved, base, None, [])
        with self.assertRaises(BudgetPaused):
            lead.implement_in_parallel(session, data, approved, base, "parent-1", time.monotonic() + 12, False, None)
        self.assertEqual(len(calls), 1)
        lead.budget(session["project_id"], 1)
        candidate, workstreams, errors = lead.implement_in_parallel(
            session, data, approved, base, "parent-2", time.monotonic() + 12, False, None)
        self.assertEqual(errors, [])
        self.assertEqual(candidate["files"], demo.FILES | {"README.md": base["README.md"]})
        self.assertEqual(sorted(calls), ["Board behavior", "Page structure"])
        self.assertEqual(sum(item["reused"] for item in workstreams), 1)
        self.assertEqual(lead.budget(session["project_id"])["vendor_cost"], 0.04)

    def test_failed_workstream_passes_valid_sibling_patch_to_the_fallback_author(self):
        lead = self.lead()
        ask = lead.ask_model
        partials = []

        def model(session, role, data, agent, until, sample, *, instruction=None):
            draft = ask(session, role, data, agent, until, sample, instruction=instruction)
            if role == "architecture":
                draft["implementation"] = implementation_plan()
            elif role == "development":
                task = data.get("workstream")
                if task and task["name"] == "Board behavior":
                    draft["files"]["index.html"] = "Outside board behavior scope"
                elif not task:
                    partial = data["partial_implementation"]
                    partials.append(partial)
                    self.assertEqual(partial["candidate"]["files"], {key: demo.FILES[key] for key in ("index.html", "harness.json")})
            return draft

        with patch.object(lead, "ask_model", side_effect=model):
            project = lead.create_project("Failed slice fallback")
            sid = lead.launch(project["id"], "", sample=True)
            result = test_workspace.WorkspaceTest.completed(lead, sid)
        self.assertEqual(result["state"], "completed", result["error"])
        self.assertEqual(len(partials), 1)
        self.assertIn("Board behavior: The patch writes outside its assigned files", partials[0]["errors"])
        gate = next(gate for gate in result["gates"] if gate["stage"] == "development")
        self.assertTrue(gate["details"]["evidence"]["implementation"]["fallback"])

    def test_rejects_conflicting_paths_and_protects_excluded_git_files(self):
        for path in ("INDEX.HTML", "index.html/inc.js"):
            plan = implementation_plan()
            plan["tasks"][1]["paths"] = [path]
            with self.assertRaisesRegex(ValueError, "paths overlap"):
                validate_workstreams(plan)
        with self.assertRaisesRegex(ValueError, "both a file and a directory"):
            manifest(demo.FILES | {"styles.css/part.css": "/* invalid tree */"})
        lead = self.lead()
        session, approved = self.setup_stage(lead)
        session.update(github_base="pinned", github_entries={"INDEX.HTML": ("120000", "excluded", 12)}, github_source={})
        data = lead.context(session, "development", approved, {}, None, [])
        with patch.object(lead, "implementation_patch") as spawn:
            candidate, workstreams, errors = lead.implement_in_parallel(
                session, data, approved, {}, "parent", time.monotonic() + 12, True, None)
        spawn.assert_not_called()
        self.assertIsNone(candidate)
        self.assertEqual(workstreams, [])
        self.assertIn("excluded files", errors[0])
        with self.assertRaisesRegex(ValueError, "excluded GitHub path"):
            lead.merge_github_edits(session, {"files": {"index.html": "outside the editable inventory"}})
