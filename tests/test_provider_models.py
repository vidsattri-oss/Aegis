import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omni.cli import main
from omni.credentials import ProviderCredentials
from omni.engine import discover_models
from omni.integrations import complete, list_provider_models, provider_settings
from omni.store import Store

FOUNDRY_RESOURCE = "https://fixture.services.ai.azure.com"
FOUNDRY_PROJECT = FOUNDRY_RESOURCE + "/api/projects/trial"
DEPLOYMENTS = FOUNDRY_PROJECT + "/deployments?api-version=v1&deploymentType=ModelDeployment"


class KeyringFixture:
    available = True

    def __init__(self):
        self.values = {}

    def store(self, slot, key):
        self.values[slot] = key

    def get(self, slot):
        return self.values.get(slot)

    def forget(self, slot):
        self.values.pop(slot, None)


class ProviderModelsTest(unittest.TestCase):
    def test_foundry_discovery_lists_deployment_names_for_inference(self):
        next_page = DEPLOYMENTS + "&skipToken=next"
        first = {"value": [{"type": "ModelDeployment", "name": "team-reasoning", "modelName": "base-model"},
                           {"type": "OtherDeployment", "name": "skip-me"}], "nextLink": next_page}
        second = {"value": [{"type": "ModelDeployment", "name": "team-chat", "modelName": "another-base"}]}
        with tempfile.TemporaryDirectory() as directory:
            credentials = ProviderCredentials(Store(directory), backend=KeyringFixture())
            settings = provider_settings({"kind": "foundry", "endpoint": FOUNDRY_PROJECT + "/"}, require_model=False)
            for key, headers in (
                ("fixture-provider-secret", {"api-key": "fixture-provider-secret"}),
                ("eyJhbGciOiJub25lIn0.eyJzdWIiOiJmaXh0dXJlIn0.token", {"Authorization":
                 "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJmaXh0dXJlIn0.token"}),
            ):
                with self.subTest(header=next(iter(headers))):
                    pages = [(json.dumps(page).encode(), "application/json") for page in (first, second)]
                    with patch("omni.integrations.request", side_effect=pages) as request:
                        result = discover_models(settings, credentials, key)
                    self.assertEqual([call.args[0] for call in request.call_args_list], [DEPLOYMENTS, next_page])
                    self.assertTrue(all(call.kwargs["headers"] == headers for call in request.call_args_list))
                    self.assertEqual([model["id"] for model in result["models"]], ["team-chat", "team-reasoning"])
                    self.assertFalse(credentials.status(settings)["has_key"])
            with self.assertRaisesRegex(ValueError, "Deployment or model is required"):
                provider_settings(settings)
            response_chat = b'{"choices":[{"message":{"content":"OK"}}]}'
            with patch("omni.integrations.request", return_value=(response_chat, "application/json")) as request:
                complete({**settings, "model": result["models"][0]["id"]}, "fixture-provider-secret", "Test", {}, 5)
            self.assertEqual(request.call_args.kwargs["payload"]["model"], "team-chat")

    def test_foundry_discovery_does_not_call_the_catalog_or_external_continuation(self):
        resource = provider_settings({"kind": "foundry", "endpoint": FOUNDRY_RESOURCE}, require_model=False)
        project = provider_settings({"kind": "foundry", "endpoint": FOUNDRY_PROJECT}, require_model=False)
        response = {"value": [], "nextLink": "https://elsewhere.invalid/deployments?api-version=v1"}
        with patch("omni.integrations.request", return_value=(json.dumps(response).encode(), "application/json")) as request:
            with self.assertRaisesRegex(ValueError, "Paste the Foundry project endpoint"):
                list_provider_models(resource, "fixture-provider-secret")
            request.assert_not_called()
            with self.assertRaisesRegex(ValueError, "invalid deployment continuation"):
                list_provider_models(project, "fixture-provider-secret")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], DEPLOYMENTS)

    def test_cli_models_prints_foundry_deployment_names(self):
        config = {"OMNI_PROVIDER": "foundry", "OMNI_ENDPOINT": FOUNDRY_PROJECT, "OMNI_API_KEY": "fixture-key"}
        response = b'{"value":[{"type":"ModelDeployment","name":"team-chat","modelName":"foundation-model"}]}'
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, config, clear=True), redirect_stdout(stdout):
            with patch("omni.integrations.request", return_value=(response, "application/json")) as request:
                with self.assertRaises(SystemExit) as exit_status:
                    main(["--data", directory, "models"])
            self.assertEqual(exit_status.exception.code, 0)
        self.assertEqual(request.call_args.args[0], DEPLOYMENTS)
        self.assertEqual(stdout.getvalue(), "team-chat\n")

    def test_saved_credentials_survive_restart_and_are_scoped_by_endpoint(self):
        provider = provider_settings({"kind": "compatible", "endpoint": "https://model.example/v1", "model": "alias"})
        key = "fixture-local-keyring-secret"
        keyring = KeyringFixture()
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            store = Store(directory)
            credentials = ProviderCredentials(store, backend=keyring)
            credentials.update(provider, key=key)
            self.assertFalse(credentials.status(provider)["credential_saved"])
            credentials.update(provider, remember=True)
            self.assertEqual(list(keyring.values.values()), [key])
            for path in Path(directory).glob("*.sqlite3*"):
                self.assertNotIn(key.encode(), path.read_bytes())
            restarted = ProviderCredentials(Store(directory), backend=keyring)
            self.assertEqual(restarted.get({**provider, "model": "another-deployment"}), key)
            self.assertIsNone(restarted.get({**provider, "endpoint": "https://other.example/v1"}))
            self.assertIsNone(ProviderCredentials(Store(other), backend=keyring).get(provider))
            restarted.update(provider, forget=True)
            self.assertFalse(restarted.status(provider)["has_key"])
            self.assertIsNone(restarted.get(provider))
            self.assertEqual(keyring.values, {})
