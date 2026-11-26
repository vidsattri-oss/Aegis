import base64
import hashlib
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from pitcrew.engine import Lead
from pitcrew.extensions import Extensions, server_from
from pitcrew.mcp_oauth import MCPOAuth, discover
from pitcrew.store import Store


class WorkspaceToolsTest(unittest.TestCase):
    def test_browser_edits_config_and_multiple_skill_files(self):
        config = '[[mcp]]\nname = "example"\nurl = "https://example.org/mcp"\noauth = {client_id = "client"}\n[mcp.tools]\nsearch = ["research"]\n'
        files = {"SKILL.md": "---\nname: workshop\ndescription: Workshop guidance\n---\nUse the references.",
                 "references/checks.md": "Check the acceptance criteria.", "notes.txt": "Confirm readiness."}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PITCREW_PROVIDER": "demo"}, clear=True):
            with closing(Lead(Path(temp))) as lead:
                lead.save_extensions({"text": config, "base": ""})
                with self.assertRaisesRegex(ValueError, "configuration changed"):
                    lead.save_extensions({"text": "", "base": ""})
                lead.save_skill({"files": files, "roles": ["research"]})
                previous = lead.skill_files("workshop")
                changed = dict(files, **{"references/checks.md": "Review the acceptance criteria."})
                lead.save_skill({"original": "workshop", "base": previous, "files": changed, "roles": ["research"]})
                with self.assertRaisesRegex(ValueError, "relative Skill filenames"):
                    lead.save_skill({"files": files | {"../private.txt": "invalid"}, "roles": ["research"]})
                runtime = Extensions(lead.store.directory, lead.store.redact)
                self.assertIn("Review the acceptance criteria.", runtime.guidance("research"))
                self.assertIn("Confirm readiness.", runtime.guidance("research"))
                self.assertEqual(runtime.guidance("qa"), "")
                self.assertEqual(runtime.servers[0]["oauth"]["client_id"], "client")

    def test_dreams_preserve_history_and_make_memory_links_searchable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            first, second = (store.project(name, "")["id"] for name in ("First", "Second"))
            store.remember(first, [("decision", "Verify launcher", "Verify isolated launcher during browser checks.")])
            store.remember(second, [("decision", "Confirm launcher", "Verify isolated launcher after reopening.")])
            store.dream()
            store.dream()
            self.assertEqual(len(store.dream_history(0)["history"]), 2)
            records = store.memory_browser("launcher", 0)["records"]
            self.assertTrue(any(item["kind"] == "cross-project" and item["links"] for item in records))
            self.assertFalse(store.memory_browser("unrelated", 0)["records"])

    def test_oauth_binds_pkce_state_and_tokens_to_the_mcp_resource(self):
        server = server_from(Path("/tmp"), {"name": "workshop", "url": "https://mcp.example/mcp",
                             "oauth": {"client_id": "client", "scopes": ["read"]}, "tools": {"search": ["research"]}})
        oauth = MCPOAuth(set())
        callback = "http://127.0.0.1:8765/oauth/mcp/callback"
        metadata = {"issuer": "https://auth.example", "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token"}

        def redeem(url, *, data, form):
            self.assertEqual(url, metadata["token_endpoint"])
            self.assertTrue(form)
            self.assertEqual(data["resource"], server["url"])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).decode().rstrip("=")
            self.assertEqual(challenge, authorization["code_challenge"][0])
            return {"access_token": "example-token", "token_type": "Bearer", "expires_in": 300}

        with (patch("pitcrew.mcp_oauth.discover", return_value=(metadata, "")),
              patch("pitcrew.mcp_oauth.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]),
              patch("pitcrew.mcp_oauth.exchange", side_effect=redeem)):
            authorization = parse_qs(urlsplit(oauth.begin(server, callback)).query)
            self.assertEqual(authorization["redirect_uri"], [callback])
            self.assertEqual(authorization["scope"], ["read"])
            with self.assertRaisesRegex(ValueError, "expired"):
                oauth.finish({"state": ["wrong"], "code": ["code"]})
            result = {"state": authorization["state"], "code": ["code"]}
            oauth.finish(result)
            self.assertEqual(oauth.access_token(server), "example-token")
            with self.assertRaisesRegex(ValueError, "expired"):
                oauth.finish(result)
            oauth.disconnect(server)
            self.assertFalse(oauth.connected(server))

    def test_oauth_rejects_unverified_issuer(self):
        with patch("pitcrew.mcp_oauth.exchange", side_effect=[
            'Bearer resource_metadata="https://mcp.example/metadata"',
            {"resource": "https://mcp.example/mcp", "authorization_servers": ["https://auth.example"]},
            {"issuer": "https://unexpected.example", "code_challenge_methods_supported": ["S256"]},
        ]):
            with self.assertRaisesRegex(ValueError, "does not match"):
                discover("https://mcp.example/mcp")
