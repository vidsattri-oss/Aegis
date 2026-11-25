import io
import os
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omni.cli import ChatShell, LocalWorkspace
from omni.engine import Lead
from omni.integrations import provider_settings
from omni.store import Store
from test_workspace import DockerFixture


class CliChatTest(unittest.TestCase):
    def test_demo_run_and_conversation_from_the_terminal(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"OMNI_PROVIDER": "demo"}):
            directory = Path(temp)
            with closing(Lead(directory, docker=DockerFixture())) as lead, redirect_stdout(io.StringIO()) as output:
                args = SimpleNamespace(no_animation=True, project="Workshop", session=None)
                chat = ChatShell(LocalWorkspace(directory, lead), args)
                with patch("builtins.input", side_effect=["/demo", "/watch", "How many approvals passed?", "/exit"]):
                    chat.run()
                text = output.getvalue()
                self.assertIn("R I T T A I H A R N E S S", text)
                self.assertIn("completed", text)
                self.assertIn("Lead > The session is completed.", text)
                self.assertIn("7 of 7 gates", text)
                self.assertNotIn("\x1b", text)

    def test_follow_up_gets_the_previous_turn_and_its_own_reply(self):
        requests = []

        def model(settings, key, persona, context, timeout):
            requests.append(context)
            return '{"answer":"The last gate passed."}', (0, 0)

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"OMNI_PROVIDER": "demo"}):
            with closing(Lead(Path(temp), model=model)) as lead:
                store = lead.store
                project = store.project("Workshop", "")
                provider = provider_settings({"kind": "compatible", "endpoint": "https://example.org/v1", "model": "test"})
                session = store.session(project["id"], "Create a task board.", provider)
                first = lead.chat(session, "Did QA pass?")
                store.message(session, "lead", "Run finished.")
                second = lead.chat(session, "Did it pass on the first attempt?")
                lead.chats.shutdown(wait=True)

                self.assertEqual(store.chat_reply(session, first), {"text": "The last gate passed."})
                self.assertEqual(store.chat_reply(session, second), {"text": "The last gate passed."})
                self.assertEqual(requests[1]["conversation"],
                                 [{"question": "Did QA pass?", "answer": "The last gate passed."}])

    def test_opening_an_older_workspace_preserves_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            project = store.project("Workshop", "")
            session = store.session(project["id"], "Create a task board.", {"kind": "demo"})
            with store.db() as db:
                db.execute("DROP INDEX messages_reply")
                db.execute("ALTER TABLE messages DROP COLUMN reply_to")

            upgraded = Store(store.directory)
            self.assertEqual(upgraded.summary(session)["messages"][0]["text"], "Create a task board.")
            question = upgraded.message(session, "user", "What's the current stage?")
            upgraded.message(session, "lead", "Planning", reply_to=question)
            self.assertEqual(upgraded.chat_reply(session, question), {"text": "Planning"})
