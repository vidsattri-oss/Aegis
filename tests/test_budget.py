import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import test_workspace
from aegis import demo, integrations
from aegis.agent_cli import complete_with_cli
from aegis.azure_prices import azure_token_rates
from aegis.contracts import PERSONAS
from aegis.engine import BudgetPaused, Lead, model_for_role, with_model_pool
from aegis.store import Store, now, uid
from aegis.usage import MeteredModelError, Usage


ROUTER = {"kind": "compatible", "endpoint": "https://openrouter.ai/api/v1", "model": "fixture/model"}
INPUT_METER = "a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12
OUTPUT_METER = "b" * 8 + "-bbbb-bbbb-bbbb-" + "b" * 12


def azure_meter(identifier, direction, cost, *, model="gpt 4.1"):
    sku = f"{model} {direction} Data Zone"
    return {"meterId": identifier, "skuName": sku, "meterName": f"{sku} Tokens", "unitOfMeasure": "1K",
            "type": "Consumption", "armRegionName": "eastus", "currencyCode": "USD", "tierMinimumUnits": 0,
            "retailPrice": cost, "effectiveStartDate": "2025-04-01T00:00:00Z"}


class VendorUsageTest(unittest.TestCase):
    def test_azure_retail_prices_use_exact_token_meters_and_match_skus(self):
        incoming = azure_meter(INPUT_METER, "Inp", 0.002)
        outgoing = azure_meter(OUTPUT_METER, "Outp", 0.008)
        with patch("aegis.azure_prices.request", return_value=(json.dumps({"Items": [incoming, outgoing]}).encode(), "application/json")) as fetch:
            rates = azure_token_rates("eastus", INPUT_METER, OUTPUT_METER)
        self.assertEqual((rates["input_rate"], rates["output_rate"], rates["rate_source"]), (2.0, 8.0, "azure_retail"))
        url = urlsplit(fetch.call_args.args[0])
        self.assertEqual(url.hostname, "prices.azure.com")
        self.assertIn(OUTPUT_METER, parse_qs(url.query)["$filter"][0])
        self.assertNotIn("headers", fetch.call_args.kwargs)
        mismatch = azure_meter(OUTPUT_METER, "Outp", 0.008, model="gpt 4.1 mini")
        with patch("aegis.azure_prices.request", return_value=(json.dumps({"Items": [incoming, mismatch]}).encode(), "application/json")):
            with self.assertRaisesRegex(ValueError, "same model and deployment SKU"):
                azure_token_rates("eastus", INPUT_METER, OUTPUT_METER)
        with self.assertRaisesRegex(ValueError, "region code"):
            azure_token_rates("eastus'", INPUT_METER, OUTPUT_METER)

    def test_foundry_vendor_budget_block_preserves_the_zero_charge(self):
        provider = integrations.provider_settings({"kind": "foundry", "endpoint": "https://fixture.services.ai.azure.com", "model": "fixture"})
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 429
        response.getheader.return_value = "budget_exceeded"
        with patch.object(integrations.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(MeteredModelError, "vendor budget") as raised:
                integrations.complete(provider, "fixture-key", "Plan", {}, 3)
        self.assertTrue(raised.exception.budget_reached)
        self.assertEqual(raised.exception.usage.price(provider), (0, "vendor_blocked"))
        connection.close.assert_called_once()

    def test_openrouter_charge_survives_an_unusable_model_response(self):
        settings = integrations.provider_settings(ROUTER | {"input_rate": 999, "output_rate": 999})
        response = {"id": "gen-fixture", "usage": {"cost": 0.07, "prompt_tokens": 100, "completion_tokens": 20},
                    "choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
        with patch.object(integrations, "request", return_value=(json.dumps(response).encode(), "application/json")):
            with self.assertRaises(MeteredModelError) as raised:
                integrations.complete(settings, "fixture-key", "Plan", {}, 3)
        usage = raised.exception.usage
        self.assertEqual(usage, (100, 20))
        self.assertEqual(usage.price(settings), (0.07, "vendor_charge"))
        self.assertEqual(usage.request_id, "gen-fixture")

    def test_openrouter_byok_and_token_only_gateway_have_distinct_price_sources(self):
        response = {"id": "gen-fixture", "usage": {"cost": 0.02, "prompt_tokens": 100, "completion_tokens": 20,
                                                   "is_byok": True, "cost_details": {"upstream_inference_cost": 0.18}},
                    "choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]}
        with patch.object(integrations, "request", return_value=(json.dumps(response).encode(), "application/json")):
            settings = integrations.provider_settings(ROUTER)
            _, usage = integrations.complete(settings, "fixture-key", "Plan", {}, 3)
            amount, basis = usage.price(settings)
            self.assertAlmostEqual(amount, 0.20)
            self.assertEqual(basis, "vendor_estimate")
            gateway = integrations.provider_settings(ROUTER | {
                "endpoint": "https://fixture.example/v1", "input_rate": 10, "output_rate": 50,
            })
            _, usage = integrations.complete(gateway, "fixture-key", "Plan", {}, 3)
            self.assertEqual(usage.price(gateway), (0.002, "token_rates"))

    def test_claude_report_preserves_cost_without_claiming_an_invoice_charge(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "claude"
            result = {"result": "OK", "session_id": "fixture", "total_cost_usd": 0.12,
                      "usage": {"input_tokens": 123, "output_tokens": 20}}
            executable.write_text(f"#!{sys.executable}\nimport sys\n"
                                  "assert sys.argv[sys.argv.index('--max-budget-usd') + 1] == '0.50000000'\n"
                                  f"print({json.dumps(result)!r})\n", encoding="utf-8")
            executable.chmod(0o700)
            settings = integrations.provider_settings({"kind": "claude-code"})
            with patch.dict(os.environ, {"PATH": folder + os.pathsep + os.environ["PATH"]}):
                text, usage = complete_with_cli(settings | {"remaining_model_budget": 0.5}, "Plan", {}, 3)
            self.assertEqual(text, "OK")
            self.assertEqual(usage.price(settings), (0.12, "vendor_estimate"))
            self.assertEqual(usage.request_id, "fixture")


class ProjectBudgetTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def lead(self, model):
        instance = self.enterContext(closing(Lead(self.data, docker=test_workspace.DockerFixture(), model=model,
                                                  fetch=lambda url, *_: {"url": url, "excerpt": "A verified fixture."})))
        instance.configure(ROUTER)
        return instance

    def attempt(self, lead, session):
        attempt = uid()
        lead.store.write("""INSERT INTO attempts(id,session_id,stage,role,number,status,profile,started,persona,input)
                         VALUES (?,?,'lead','lead',1,'Running','{}',?,'','{}')""", (attempt, session, now()))
        return attempt

    def test_vendor_cost_pauses_at_project_limit_and_retry_skips_approved_gates(self):
        calls = []

        def model(settings, key, persona, data, timeout):
            role = next(role for role, instruction in PERSONAS.items() if persona.startswith(instruction))
            calls.append(role)
            if role == "review":
                draft = {"passed": True, "checks": ["Execution evidence checked"], "feedback": "Approved"}
            elif role == "research" and "round" in data:
                draft = {"urls": [demo.SOURCE]} if data["round"] == 1 else {"done": True}
            else:
                draft = copy.deepcopy(demo.EXAMPLES[role])
            return json.dumps(test_workspace.release_verdict(data, draft)), Usage((5, 2), 0.013, "vendor_charge")

        lead = self.lead(model)
        project = lead.create_project("Metered Board")["id"]
        other = lead.create_project("Independent Board")["id"]
        lead.budget(project, 0.02)
        sid = lead.launch(project, "Build the Fieldwork board")
        stopped = test_workspace.WorkspaceTest.completed(lead, sid)
        self.assertEqual(stopped["state"], "budget_paused")
        self.assertEqual(calls, ["lead", "review"])
        self.assertEqual(stopped["gates"][0]["state"], "passed")
        self.assertEqual(lead.budget(project)["vendor_cost"], 0.026)
        self.assertEqual(lead.budget(other)["model_cost"], 0)
        self.assertTrue(lead.budget(project)["reached"])
        with self.assertRaisesRegex(BudgetPaused, "budget reached"):
            lead.retry(sid)
        with self.assertRaisesRegex(BudgetPaused, "budget reached"):
            lead.launch(project, "Send a new request")
        radio = lead.chat(sid, "Why is the session paused?")
        lead.chats.submit(lambda: None).result(timeout=5)
        self.assertIn("budget reached", lead.store.chat_reply(sid, radio)["text"])
        self.assertEqual(calls, ["lead", "review"])
        lead.budget(project, 1)
        lead.retry(sid)
        completed = test_workspace.WorkspaceTest.completed(lead, sid)
        self.assertEqual(completed["state"], "completed", completed["error"])
        self.assertEqual(calls.count("lead"), 1)
        self.assertEqual(Store(self.data).budget(project)["model_cost"], round(0.013 * len(calls), 8))

    def test_optional_unpriced_specialist_is_skipped_but_the_raceday_cap_still_stops_work(self):
        model = Mock(side_effect=AssertionError("Unpriced requests must be blocked"))
        lead = self.lead(model)
        project = lead.create_project("Optional advisor budget")["id"]
        lead.budget(project, 1)
        settings = integrations.provider_settings({"kind": "compatible", "endpoint": "http://127.0.0.1:8181/v1",
                                                   "model": "fixture"})
        sid = lead.store.session(project, "Check navigation", settings)
        session = lead.get(sid)
        session["execution"] = lead.execution_settings()
        plan = {"criteria": [{"id": "AC1", "description": "Works with keyboard"}],
                "specialists": [{"name": "Navigation", "persona": "Accessibility", "assignment": "Identify a focus check",
                                 "stage": "qa", "criterion": "AC1", "paths": [], "deliverable": "One test"}]}
        data = {"approved": {}}
        self.assertEqual(lead.delegate(session, plan, data, 1, time.monotonic() + 6, False, "parent", stage="qa"), [])
        skipped = lead.store.row("SELECT payload FROM events WHERE session_id=? AND kind='specialist_skipped'", (sid,))
        self.assertIn("Set rates", skipped["payload"]["message"])
        self.assertEqual(lead.store.summary(sid)["agents"][0]["status"], "Skipped")
        lead.budget(project, 0)
        with self.assertRaisesRegex(BudgetPaused, "budget reached"):
            lead.delegate(session, plan, data, 1, time.monotonic() + 6, False, "parent", stage="qa")
        model.assert_not_called()

    def test_team_radio_gets_next_slot_without_bypassing_a_raceday_budget(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def model(_settings, _key, _persona, data, _timeout):
            if "question" in data:
                calls.append("radio")
                self.assertEqual(data["state"], "running")
                return '{"answer":"Development is running."}', Usage((1, 1), 0.01, "vendor_charge")
            calls.append(data["stage"])
            if data["stage"] == "development":
                started.set()
                self.assertTrue(release.wait(5))
            return "{}", Usage((1, 1), 0.01, "vendor_charge")

        lead = self.lead(model)
        lead.configure_execution({"model_slots": 1})
        project = lead.create_project("Live radio")
        lead.budget(project["id"], 1)
        settings = lead.provider()
        sid = lead.store.session(project["id"], "Build", settings)
        lead.store.update(sid, state="running", stage="development")
        until = time.monotonic() + 8
        with ThreadPoolExecutor(2) as workers:
            first = workers.submit(lead.call_model, sid, settings, "", "", {"stage": "development"}, until,
                                   attempt=self.attempt(lead, sid))
            try:
                self.assertTrue(started.wait(2))
                question = lead.chat(sid, "What is running?")
                deadline = time.monotonic() + 2
                while lead.radio_waiting is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNotNone(lead.radio_waiting)
                queued = workers.submit(lead.call_model, sid, settings, "", "", {"stage": "qa"}, until,
                                        attempt=self.attempt(lead, sid))
            finally:
                release.set()
            first.result(timeout=3)
            queued.result(timeout=3)
        lead.chats.submit(lambda: None).result(timeout=3)
        self.assertEqual(lead.store.chat_reply(sid, question)["text"], "Development is running.")
        self.assertEqual(calls, ["development", "radio", "qa"])
        self.assertEqual(lead.budget(project["id"])["vendor_cost"], 0.03)

    def test_parallel_requests_see_first_cost_before_admission(self):
        started = threading.Event()
        release = threading.Event()
        invoked = []

        def model(*args):
            invoked.append(1)
            started.set()
            self.assertTrue(release.wait(5))
            return "OK", Usage((1, 1), 0.10, "vendor_charge")

        lead = self.lead(model)
        project = lead.create_project("Concurrent meter")["id"]
        lead.budget(project, 0.05)
        settings = integrations.provider_settings(ROUTER)
        sid = lead.store.session(project, "Run workers", settings)
        attempts = [self.attempt(lead, sid) for _ in range(2)]
        deadline = time.monotonic() + 10
        with ThreadPoolExecutor(max_workers=2) as workers:
            future = workers.submit(lead.call_model, sid, settings, "", "", {}, deadline, attempt=attempts[0])
            try:
                self.assertTrue(started.wait(2))
                waiting = workers.submit(lead.call_model, sid, settings, "", "", {}, deadline, attempt=attempts[1])
            finally:
                release.set()
            future.result(timeout=3)
            with self.assertRaisesRegex(BudgetPaused, "budget reached"):
                waiting.result(timeout=3)
        self.assertEqual(len(invoked), 1)
        self.assertEqual(lead.budget(project)["model_cost"], 0.10)

    def test_unpriced_and_interrupted_calls_place_a_hold_on_the_raceday(self):
        calls = []

        def model(*args):
            calls.append(1)
            return "OK", Usage((None, None))

        settings = integrations.provider_settings(ROUTER)
        with closing(Lead(self.data, docker=test_workspace.DockerFixture(), model=model)) as lead:
            project = lead.create_project("Unpriced meter")["id"]
            lead.budget(project, 0.10)
            sid = lead.store.session(project, "Review the report", settings)
            attempt = self.attempt(lead, sid)
            for _ in range(2):
                with self.assertRaisesRegex(BudgetPaused, "no usable cost"):
                    lead.call_model(sid, settings, "", "", {}, time.monotonic() + 4, attempt=attempt)
            self.assertEqual(len(calls), 1)
            self.assertEqual(lead.budget(project)["unpriced_calls"], 1)
            self.assertTrue(lead.budget(project)["budget_hold"])
            lead.budget(project, 0.10)
            self.assertIsNone(lead.budget(project)["budget_hold"])
            lead.store.write("UPDATE attempts SET metering_pending=1 WHERE id=?", (attempt,))
            lead.store.update(sid, state="cancelled")
        with closing(Lead(self.data, docker=test_workspace.DockerFixture(), model=model)) as restarted:
            self.assertIn("interrupted", restarted.budget(project)["budget_hold"])
            self.assertEqual(restarted.budget(project)["unpriced_calls"], 2)
            without_cap = restarted.budget(project, None)
            self.assertIsNone(without_cap["remaining_usd"])
            self.assertIsNone(without_cap["budget_hold"])

    def test_claude_budget_stop_records_usage_and_passes_the_remaining_amount(self):
        def run_vendor(settings, *args):
            self.assertEqual(settings["remaining_model_budget"], 0.25)
            raise MeteredModelError("Claude Code reached the remaining Project budget", Usage((30, 10), 0.26, "vendor_estimate"),
                                    budget_reached=True)

        lead = self.lead(run_vendor)
        project = lead.create_project("Claude meter")["id"]
        lead.budget(project, 0.25)
        settings = integrations.provider_settings({"kind": "claude-code"})
        sid = lead.store.session(project, "Review the report", settings)
        with self.assertRaisesRegex(BudgetPaused, "Claude Code reached"):
            lead.call_model(sid, settings, "", "", {}, time.monotonic() + 4, attempt=self.attempt(lead, sid))
        budget = lead.budget(project)
        self.assertEqual(budget["vendor_estimate_cost"], 0.26)
        self.assertTrue(budget["reached"])

    def test_import_prices_one_foundry_deployment_and_leaves_unpriced_roles_blocked(self):
        lead = self.lead(lambda *_: ("OK", Usage((2500, 1000))))
        foundry = integrations.provider_settings({"kind": "foundry",
                                                   "endpoint": "https://fixture.services.ai.azure.com/api/projects/test"})
        models = [{"id": model, "chat": True} for model in ("gpt-deployment", "partner-deployment")]
        lead.store.set("provider", with_model_pool(foundry, models, {"lead": "gpt-deployment", "review": "partner-deployment"}))
        published = {"input_rate": 2, "output_rate": 8, "rate_source": "azure_retail", "region": "eastus"}
        with patch("aegis.engine.azure_token_rates", return_value=published):
            result = lead.import_azure_prices({"model": "gpt-deployment", "region": "eastus",
                                              "input_meter": INPUT_METER, "output_meter": OUTPUT_METER})
        self.assertEqual(result["rates"], published)
        saved = lead.store.setting("provider")
        project = lead.create_project("Foundry meter")["id"]
        lead.budget(project, 0.50)
        sid = lead.store.session(project, "Review the report", saved)
        attempt = self.attempt(lead, sid)
        lead.call_model(sid, model_for_role(saved, "lead", sid), "", "", {}, time.monotonic() + 4, attempt=attempt)
        with self.assertRaisesRegex(BudgetPaused, "partner-deployment"):
            lead.call_model(sid, model_for_role(saved, "review", sid), "", "", {}, time.monotonic() + 4, attempt=attempt)
        self.assertEqual(lead.budget(project)["vendor_estimate_cost"], 0.013)
        self.assertIsNone(lead.import_azure_prices({"model": "gpt-deployment", "clear": True})["rates"])
        self.assertEqual(lead.store.setting("provider")["model_rates"], {})
