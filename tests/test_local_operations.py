import os
import tempfile
import time
import tarfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

from pitcrew import build
from pitcrew.config import default_workspace
from pitcrew.continuity import DATABASE, backup_workspace, restore_workspace
from pitcrew.extensions import server_from
from pitcrew.preflight import check_workspace
from pitcrew.repository import Repository
from pitcrew.sandbox import ContainerCapacity, Docker, profile
from pitcrew.store import Store


class LocalOperationsTest(unittest.TestCase):
    def test_packaging_includes_developer_workflow_and_excludes_compiled_files(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            package = source / "pitcrew"
            package.mkdir()
            (package / "cli.py").write_text("def main():\n    pass\n", encoding="utf-8")
            (package / "cached.pyc").touch()
            (package / "__pycache__").mkdir()
            (package / "__pycache__" / "data").touch()
            (source / ".coveragerc").touch()
            (source / "requirements-dev.txt").touch()
            with patch.object(build, "ROOT", source), patch("builtins.print"):
                build.main()
            with ZipFile(source / "dist" / "pitcrew") as executable:
                self.assertIn("__main__.py", executable.namelist())
                self.assertIn("pitcrew/cli.py", executable.namelist())
                self.assertNotIn("pitcrew/cached.pyc", executable.namelist())
            with tarfile.open(source / "dist" / "pitcrew-src.tar.gz") as archive:
                self.assertEqual(set(archive.getnames()), {".coveragerc", "requirements-dev.txt", "pitcrew/cli.py"})

    def test_default_workspace_outside_a_checkout_uses_user_data_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with (chdir(directory), patch.dict(os.environ, {}, clear=True),
                  patch("pitcrew.config.Path.home", return_value=directory / "home")):
                self.assertEqual(default_workspace(), directory / "home" / ".local/share/pitcrew")
                with patch.dict(os.environ, {"XDG_DATA_HOME": str(directory / "data")}):
                    self.assertEqual(default_workspace(), directory / "data/pitcrew")
                with patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
                    self.assertEqual(default_workspace(), directory / "home" / ".local/share/pitcrew")
                legacy = directory / ".omni-data"
                legacy.mkdir()
                self.assertEqual(default_workspace(), Path(".omni-data"))
                (directory / ".pitcrew-data").mkdir()
                self.assertEqual(default_workspace(), Path(".pitcrew-data"))
                with patch.dict(os.environ, {"PITCREW_DATA": str(directory / "explicit")}):
                    self.assertEqual(default_workspace(), directory / "explicit")

    def test_fresh_checkout_uses_checkout_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "Makefile").touch()
            (directory / "pitcrew").mkdir()
            (directory / "pitcrew" / "build.py").touch()
            with chdir(directory), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(default_workspace(), Path(".pitcrew-data"))

    def test_preflight_reports_mount_failure_and_keeps_model_calls_optional(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            docker = Mock()
            docker.doctor.return_value = {"cpu": 4, "ram": 2048}
            docker.probe_mounts.side_effect = RuntimeError("Permission denied")
            with patch.dict(os.environ, {"PITCREW_NPM_CACHE": ""}):
                result = check_workspace(docker, store)
            self.assertFalse(result["ready"])
            self.assertEqual(result["checks"][1]["detail"], "Permission denied")
            self.assertEqual(result["checks"][-1]["status"], "skipped")
            docker.probe_mounts.side_effect = None
            provider = Mock(return_value={"model": "local-fixture"})
            result = check_workspace(docker, store, provider)
            self.assertTrue(result["ready"])
            provider.assert_called_once_with()

    def test_pressure_counts_engine_usage_and_identifies_local_reservations(self):
        container, local = "a" * 64, "b" * 64
        data = (f'{{"Container":"{container}","CPUPerc":"125.00%","MemUsage":"1.25GiB / 4GiB"}}\n'
                f'{{"Container":"{local}","CPUPerc":"80.00%","MemUsage":"300MiB / 384MiB"}}').encode()
        with patch("pitcrew.sandbox.run_command", side_effect=[Mock(stdout=local.encode()), Mock(stdout=data)]):
            external = Docker().external_usage("local")
        self.assertEqual(external, {"cpu": 1.25, "ram": 1280, "containers": 1})
        pressure = ContainerCapacity(lambda: external)
        pressure.set_engine_capacity({"cpu": 2, "ram": 2048})
        with pressure.reserve([profile("mcp")], lambda: 2, self.fail, holder={"session_id": "test", "stage": "qa"}):
            info = pressure.report()
            self.assertEqual(info["holds"], [{"session_id": "test", "stage": "qa", "cpu": 0.5, "ram": 256, "tasks": ["mcp"]}])
            waiting = []
            # The next reservation fits by itself but must wait for the active lease and sampled Docker use.
            deadline = time.monotonic() + 0.01

            def remaining():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                return left

            with self.assertRaises(TimeoutError):
                with pressure.reserve([profile("mcp")], remaining, waiting.append):
                    self.fail("Docker capacity was overbooked")
            self.assertEqual(waiting, [info])
        self.assertEqual(pressure.report()["reserved"], {"cpu": 0, "ram": 0})

    def test_stdio_sandbox_requires_a_separate_bundle_and_no_host_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "mcp/docs").mkdir(parents=True)
            spec = {"name": "docs", "command": ["python", "server.py"], "cwd": "mcp/docs",
                    "tools": {"search_docs": ["research"]}}
            self.assertEqual(server_from(directory, spec)["isolation"], "sandbox")
            with self.assertRaisesRegex(ValueError, "dedicated cwd"):
                server_from(directory, {**spec, "cwd": "."})
            with self.assertRaisesRegex(ValueError, "without host credential"):
                server_from(directory, {**spec, "env": ["TOKEN"]})
            self.assertEqual(server_from(directory, {**spec, "isolation": "host", "env": ["TOKEN"]})["env"], ["TOKEN"])

    def test_backup_restores_data_git_history_and_builds_with_a_new_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            original = Store(directory / "original")
            repo = Repository(original.directory)
            project = original.project("Recovery example", "")
            sid = original.session(project["id"], "Create a fixture", {"kind": "demo"})
            session = original.row("SELECT * FROM sessions WHERE id=?", (sid,))
            tree = repo.checkout(session)
            (tree / "task.txt").write_text("Session source", encoding="utf-8")
            repo.git(tree, "add", "task.txt")
            repo.git(tree, "commit", "-m", "Session source")
            head = repo.git(tree, "rev-parse", "HEAD").stdout
            main = repo.git(repo.root, "rev-parse", "main").stdout
            build = repo.bundles / f"{sid}.zip"
            with ZipFile(build, "w") as archive:
                archive.writestr("task.txt", "Fixture artifact")
            original.update(sid, bundle=str(build), preview="http://127.0.0.1:1234/")
            original.set("keyring-fixture", True)
            (original.directory / "service.env").write_text("FIXTURE=private", encoding="utf-8")

            backup = backup_workspace(original.directory, directory / "backup.zip")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            with ZipFile(backup) as archive:
                self.assertNotIn("service.env", archive.namelist())
            with self.assertRaisesRegex(ValueError, "already exists"):
                backup_workspace(original.directory, backup)
            destination = directory / "restored"
            restored = Store(restore_workspace(backup, destination))
            self.assertNotEqual(restored.workspace_id(), original.workspace_id())
            self.assertIsNone(restored.setting("keyring-fixture"))
            resumed = restored.row("SELECT * FROM sessions WHERE id=?", (sid,))
            self.assertIsNone(resumed["preview"])
            self.assertEqual(resumed["bundle"], str(destination / "builds" / build.name))
            self.assertEqual(Path(resumed["bundle"]).read_bytes(), build.read_bytes())
            restored_repo = Repository(restored.directory)
            self.assertEqual(restored_repo.git(restored_repo.root, "rev-parse", "main").stdout, main)
            self.assertEqual(restored_repo.git(restored_repo.checkout(resumed), "rev-parse", "HEAD").stdout, head)
            with self.assertRaisesRegex(ValueError, "new workspace directory"):
                restore_workspace(backup, destination)

    def test_restore_accepts_verified_legacy_backup_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            store = Store(directory / "original")
            for marker_text in ("RaceHarness workspace backup\n", "OtherStudio workspace backup\n"):
                with self.subTest(marker_text=marker_text):
                    backup = directory / "legacy.zip"
                    with ZipFile(backup, "w") as archive:
                        archive.writestr("raceharness-backup-v1", marker_text)
                        archive.write(store.directory / DATABASE, DATABASE)
                    if marker_text.startswith("RaceHarness"):
                        restored = Store(restore_workspace(backup, directory / "restored"))
                        self.assertNotEqual(restored.workspace_id(), store.workspace_id())
                    else:
                        with self.assertRaisesRegex(ValueError, "backup marker"):
                            restore_workspace(backup, directory / "invalid")


if __name__ == "__main__":
    unittest.main()
