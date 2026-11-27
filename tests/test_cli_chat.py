import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aegis.cli import ChatShell, LocalWorkspace, parser_for_cli
from aegis.engine import Lead
from aegis.integrations import provider_settings
from aegis.store import Store
from aegis.terminal import SHINE_FRAMES, TWINKLE, Terminal
from test_provider_models import FOUNDRY_PROJECT, FOUNDRY_RESOURCE, KeyringFixture
from test_workspace import DockerFixture


class CliChatTest(unittest.TestCase):
    def test_foundry_setup_discovers_and_checks_a_deployment_before_a_run(self):
        key = "fixture-foundry-credential"
        replies = []

        def model(settings, credential, persona, context, timeout):
            replies.append((settings["model"], credential))
            return "OK", (1, 1)

        deployments = {"value": [{"type": "ModelDeployment", "name": "team-chat"}]}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AEGIS_PROVIDER": "demo"}):
            keyring = KeyringFixture()
            directory = Path(temp)
            with closing(Lead(directory, model=model, keyring=keyring)) as lead, redirect_stdout(io.StringIO()) as output:
                backend = LocalWorkspace(directory, lead)
                chat = ChatShell(backend, SimpleNamespace(no_animation=True, project="Workshop", session=None))
                inputs = ["/setup", "3", FOUNDRY_PROJECT, "y", "/provider test", "/run Build a local planner", "/exit"]
                with (patch("builtins.input", side_effect=inputs),
                      patch("aegis.cli.prompt_credential", return_value=key),
                      patch("aegis.integrations.request", return_value=(json.dumps(deployments).encode(), "application/json")),
                      patch.object(backend, "run", return_value="a" * 16) as run):
                    chat.run()

                self.assertIn("Use /setup to choose a provider", output.getvalue())
                for name in ("Claude Code", "GitHub Copilot CLI", "Microsoft Foundry", "Local model"):
                    self.assertIn(name, output.getvalue())
                self.assertIn("copy the Project endpoint and API key from Home", output.getvalue())
                self.assertIn("Microsoft Foundry responded via team-chat", output.getvalue())
                self.assertNotIn(key, output.getvalue())
                self.assertEqual(replies, [("team-chat", key)] * 2)
                self.assertEqual(lead.provider()["kind"], "foundry")
                self.assertEqual(list(keyring.values.values()), [key])
                run.assert_called_once_with(chat.project, "Build a local planner", False)

    def test_failed_foundry_setup_keeps_the_previous_provider(self):
        key = "fixture-rejected-credential"

        def reject_model(settings, credential, persona, context, timeout):
            raise RuntimeError(f"Credential rejected: {credential}")

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AEGIS_PROVIDER": "demo"}):
            directory = Path(temp)
            keyring = KeyringFixture()
            with closing(Lead(directory, model=reject_model, keyring=keyring)) as lead, redirect_stdout(io.StringIO()) as output:
                lead.configure({"kind": "compatible", "endpoint": "https://existing.example/v1", "model": "existing"})
                chat = ChatShell(LocalWorkspace(directory, lead), SimpleNamespace(no_animation=True, project=None, session=None))
                with (patch("builtins.input", side_effect=["/setup foundry", FOUNDRY_RESOURCE, "team-chat", "y", "/exit"]),
                      patch("aegis.cli.prompt_credential", return_value=key)):
                    chat.run()

                self.assertIn("Credential rejected", output.getvalue())
                self.assertNotIn(key, output.getvalue())
                self.assertEqual(lead.provider()["model"], "existing")
                self.assertEqual(keyring.values, {})

    def test_local_setup_discovers_a_model_and_uses_the_loopback_api(self):
        model_list = b'{"data":[{"id":"local-chat"}]}'
        reply = b'{"choices":[{"message":{"content":"OK"}}]}'
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AEGIS_PROVIDER": "demo"}):
            directory = Path(temp)
            with closing(Lead(directory)) as lead, redirect_stdout(io.StringIO()) as output:
                chat = ChatShell(LocalWorkspace(directory, lead), SimpleNamespace(no_animation=True, project=None, session=None))
                with (patch("builtins.input", side_effect=["/setup", "4", "", "", "/exit"]),
                      patch("aegis.cli.prompt_credential", return_value=""),
                      patch("aegis.integrations.request", side_effect=[(model_list, "application/json"),
                                                                      (reply, "application/json")]) as request):
                    chat.run()

                self.assertIn("Local model responded via local-chat", output.getvalue())
                self.assertEqual(lead.provider()["kind"], "local")
                self.assertEqual([call.args[0] for call in request.call_args_list],
                                 ["http://127.0.0.1:11434/v1/models", "http://127.0.0.1:11434/v1/chat/completions"])
                self.assertEqual(request.call_args.kwargs["payload"]["model"], "local-chat")
                with self.assertRaisesRegex(ValueError, "loopback URL"):
                    provider_settings({"kind": "local", "endpoint": "https://remote.example/v1", "model": "other"})

    def test_coding_cli_choices_check_the_signed_in_cli_in_an_empty_workspace(self):
        for choice, kind, binary, required in (
            ("1", "claude-code", "claude", ["--restricted", "--tools", "--disallowed-tools", "--no-session-persistence"]),
            ("2", "github-copilot", "copilot", ["--available-tools=view", "--deny-tool=view", "--sync-session=off"]),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                executable = directory / binary
                response = '{"result":"OK","usage":{"input_tokens":1,"output_tokens":1}}' if binary == "claude" else "OK"
                executable.write_text(f"#!{sys.executable}\n"
                                      "import os, sys\n"
                                      f"assert set({required!r}).issubset(sys.argv)\n"
                                      "assert '--model=team-model' in sys.argv\n"
                                      "assert not os.listdir()\n"
                                      "assert 'AEGIS_API_KEY' not in os.environ\n"
                                      "assert 'Aegis provider connectivity' in sys.stdin.read()\n"
                                      f"print({response!r})\n")
                executable.chmod(0o700)
                env = {"PATH": os.pathsep.join((str(directory), os.environ.get("PATH", ""))),
                       "AEGIS_PROVIDER": "demo", "AEGIS_API_KEY": "unrelated-fixture-secret"}
                with (patch.dict(os.environ, env), closing(Lead(directory / "workspace")) as lead,
                      redirect_stdout(io.StringIO()) as output,
                      patch("builtins.input", side_effect=["/setup", choice, "team-model", "/provider test", "/exit"])):
                    chat = ChatShell(LocalWorkspace(directory, lead), SimpleNamespace(no_animation=True, project=None, session=None))
                    chat.run()
                    self.assertEqual(lead.provider()["kind"], kind)
                    self.assertEqual(output.getvalue().count("responded via team-model"), 2)
                    self.assertNotIn("unrelated-fixture-secret", output.getvalue())

    def test_demo_run_and_conversation_from_the_terminal(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AEGIS_PROVIDER": "demo"}):
            directory = Path(temp)
            with closing(Lead(directory, docker=DockerFixture())) as lead, redirect_stdout(io.StringIO()) as output:
                args = SimpleNamespace(no_animation=True, project="Workshop", session=None)
                chat = ChatShell(LocalWorkspace(directory, lead), args)
                with (patch("builtins.input", side_effect=["/demo", "/watch", "How many approvals passed?", "exit"]),
                      patch.object(chat, "ask", wraps=chat.ask) as ask):
                    chat.run()
                ask.assert_called_once_with("How many approvals passed?")
                text = output.getvalue()
                self.assertIn("Aegis", text)
                self.assertIn(".--------------.", text)
                self.assertIn("completed", text)
                self.assertIn("Lead Agent > The session is completed.", text)
                self.assertIn("7 of 7 gates", text)
                self.assertNotIn("\x1b", text)

    def test_plain_exit_does_not_start_a_session(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with closing(Lead(directory, docker=DockerFixture())) as lead, redirect_stdout(io.StringIO()):
                args = SimpleNamespace(no_animation=True, project=None, session=None)
                chat = ChatShell(LocalWorkspace(directory, lead), args)
                with (patch("builtins.input", side_effect=["  EXIT  ", "/exit"]),
                      patch.object(chat, "start") as start):
                    chat.run()
                start.assert_not_called()

    def test_cli_uses_project_and_session_commands(self):
        parser = parser_for_cli()
        self.assertIn("usage: aegis", parser.format_help())
        for name in ("projects", "sessions", "extensions", "execution"):
            self.assertEqual(parser.parse_args([name]).command, name)
        session = "0123456789abcdef"
        self.assertEqual(parser.parse_args(["logs", session]).session, session)
        self.assertEqual(parser.parse_args(["run", "--project", "Workshop"]).project, "Workshop")
        self.assertEqual(parser.parse_args(["chat", "--session", session]).session, session)
        self.assertEqual(parser.parse_args(["new-project", "Workshop"]).name, "Workshop")

    def test_banner_sweeps_before_twinkle_and_fits_a_narrow_terminal(self):
        terminal = Terminal(animation=False)
        terminal.palette = {}
        early = terminal.banner_frame(2, 80)
        later = terminal.banner_frame(5, 80)
        self.assertNotEqual(early, later)
        self.assertNotIn("*", "".join(later))
        sparkle = terminal.banner_frame(SHINE_FRAMES + TWINKLE.index("*"), 80)
        self.assertTrue(sparkle[1].endswith("*"))
        self.assertNotIn("*", "".join(sparkle[2:]))
        self.assertEqual(terminal.banner_frame(SHINE_FRAMES, 20), ["Aegis", "/help for commands"])

    def test_animated_terminal_restores_the_cursor_and_removes_control_characters(self):
        palettes = (("xterm", "", "93"), ("xterm-256color", "", "38;5;221"),
                    ("xterm-256color", "truecolor", "38;2;255;200;98"))
        for term, color, gold in palettes:
            with self.subTest(term=term, color=color), patch.dict(os.environ, {"TERM": term, "COLORTERM": color}, clear=True):
                with patch("sys.stdout.isatty", return_value=True):
                    terminal = Terminal(animation=True)
                self.assertEqual(terminal.palette["gold"], gold)
                self.assertIn("lead>", terminal.prompt(True, readline=True))

        with (redirect_stdout(io.StringIO()) as output,
              patch("aegis.terminal.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))),
              patch("aegis.terminal.time.sleep")):
            terminal.banner()
            with terminal.waiting("Checks\x1b ready") as pause:
                with patch("aegis.terminal.time.monotonic", side_effect=[0.0, 0.0, 1.0]):
                    pause(0.1)
        self.assertIn("\033[?25l", output.getvalue())
        self.assertIn("\033[?25h", output.getvalue())
        self.assertIn("Checks ready", output.getvalue())

    def test_cli_generates_a_service_and_restores_a_stopped_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, restored = root / "workspace", root / "restored"
            project = Store(data).project("Service fixture", "")
            archive, unit = root / "backup.zip", root / "aegis.service"
            base = [sys.executable, "-m", "omni"]
            commands = ((data, ("service", "--port", "8766", "--docker-context", "desktop-linux", "--write", str(unit))),
                        (data, ("backup", str(archive))), (restored, ("restore", str(archive))))
            for directory, args in commands:
                result = subprocess.run([*base, "--data", str(directory), *args], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Environment="DOCKER_CONTEXT=desktop-linux"', unit.read_text(encoding="utf-8"))
            self.assertIn('"--port" "8766"', unit.read_text(encoding="utf-8"))
            self.assertEqual(Store(restored).projects()[0]["id"], project["id"])

    def test_follow_up_gets_the_previous_turn_and_its_own_reply(self):
        requests = []

        def model(settings, key, persona, context, timeout):
            requests.append(context)
            return '{"answer":"The last gate passed."}', (0, 0)

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AEGIS_PROVIDER": "demo"}):
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
