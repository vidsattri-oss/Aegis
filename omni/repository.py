"""Managed monorepo and atomic stage commits in session worktrees."""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .contracts import require, safe_path
from .store import now


class Repository:
    def __init__(self, directory):
        self.root = directory / "monorepo"
        self.worktrees = directory / "worktrees"
        self.bundles = directory / "builds"
        self.staging = directory / "staging"
        self.lock = threading.RLock()
        for path in (self.worktrees, self.bundles, self.staging):
            path.mkdir(exist_ok=True)
        if not (self.root / ".git").is_dir():
            self.root.mkdir(exist_ok=True)
            self.git(self.root, "init", "--initial-branch=main")
        self.git(self.root, "config", "user.name", "RittaiHarness Lead")
        self.git(self.root, "config", "user.email", "lead@rittaiharness.local")
        self.git(self.root, "config", "core.hooksPath", "/dev/null")
        self.git(self.root, "config", "commit.gpgSign", "false")
        if self.git(self.root, "rev-parse", "--verify", "HEAD", check=False).returncode:
            self.git(self.root, "commit", "--allow-empty", "-m", "Initialize RittaiHarness monorepo")

    @staticmethod
    def git(cwd, *args, check=True):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0")
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                                errors="replace", timeout=45)
        if check and result.returncode:
            raise RuntimeError(f"Git {args[0]} failed: {result.stderr.strip()[-1500:]}")
        return result

    def checkout(self, session):
        branch = session["branch"]
        path = self.worktrees / session["id"]
        with self.lock:
            if not (path / ".git").exists():
                exists = self.git(self.root, "show-ref", "--verify", "--quiet", "refs/heads/" + branch,
                                  check=False).returncode == 0
                args = [str(path), branch] if exists else ["-b", branch, str(path), "main"]
                self.git(self.root, "worktree", "add", *args)
        return path

    @staticmethod
    def artifact_path(session, stage):
        return f".omni/sessions/{session['id']}/{stage}.json"

    @staticmethod
    def project_path(session):
        return f"projects/{session['project_id']}/app"

    def saved_stage(self, tree, session, stage):
        path = self.artifact_path(session, stage)
        saved = self.git(tree, "show", f"HEAD:{path}", check=False)
        if saved.returncode:
            return None
        sha = self.git(tree, "log", "-1", "--format=%H", "--", path).stdout.strip()
        return sha, json.loads(saved.stdout)

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def write_files(directory, files):
        directory.mkdir(exist_ok=True, parents=True)
        for path, content in files.items():
            target = directory / safe_path(path)
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_text(content, encoding="utf-8")

    def stage_source(self, session, files):
        directory = self.staging / session["id"]
        if directory.exists():
            shutil.rmtree(directory)
        self.write_files(directory, files)
        return directory

    def prior_source(self, tree, session):
        directory = tree / self.project_path(session)
        result = {}
        total = 0
        if directory.exists():
            for path in sorted(directory.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    size = path.stat().st_size
                    if len(result) >= 120 or total + size > 1_250_000:
                        break
                    result[path.relative_to(directory).as_posix()] = path.read_text(encoding="utf-8")
                    total += size
        return result

    def review(self, tree, session, stage, reviewer, verdict):
        with self.lock:
            relative = f".omni/sessions/{session['id']}/reviews/{stage}-{reviewer}.json"
            self.write_json(tree / relative, {"agent": reviewer, "stage": stage, "verdict": verdict, "at": now()})
            outcome = "review passed" if verdict["passed"] else "review rejected"
            return self.commit(tree, [relative], stage, reviewer, outcome)

    def approve(self, tree, session, stage, artifact, details, attempt):
        with self.lock:
            already = self.saved_stage(tree, session, stage)
            if already:
                return already[0]
            paths = [self.artifact_path(session, stage)]
            self.write_json(tree / paths[0], {"artifact": artifact, "details": details, "agent": attempt, "at": now()})
            if stage == "development":
                relative = self.project_path(session)
                app_dir = tree / relative
                if app_dir.exists():
                    shutil.rmtree(app_dir)
                self.write_files(app_dir, artifact["files"])
                paths.append(relative)
            return self.commit(tree, paths, stage, attempt, "passed")

    def audit(self, tree, session, stage, attempt, payload):
        with self.lock:
            relative = f".omni/sessions/{session['id']}/audit/{attempt}.json"
            self.write_json(tree / relative, payload)
            return self.commit(tree, [relative], stage, attempt, "failed")

    def commit(self, tree, paths, stage, attempt, outcome):
        self.git(tree, "add", "-A", "-f", "--", *paths)
        self.git(tree, "commit", "--only", "-m", f"{stage}: {outcome}",
                 "-m", f"Agent-ID: {attempt}\nStage: {stage}\nOutcome: {outcome}\nRecorded-At: {now()}",
                 "--", *paths)
        return self.git(tree, "rev-parse", "HEAD").stdout.strip()

    def merge(self, tree, session):
        with self.lock:
            branch = session["branch"]
            result = self.git(self.root, "merge", "--no-ff", "-m", f"Merge approved session {session['id']}",
                              branch, check=False)
            if result.returncode:
                self.git(self.root, "merge", "--abort", check=False)
                raise RuntimeError(f"Git merge failed: {result.stderr.strip() or result.stdout.strip()}")
            return self.git(self.root, "rev-parse", "HEAD").stdout.strip()

    def bundle(self, session, files, commit):
        destination = self.bundles / f"{session['id']}.zip"
        temporary = destination.with_suffix(".tmp")
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                archive.writestr(safe_path(name), content)
            archive.writestr(".omni-build.json", json.dumps({"session": session["id"], "commit": commit}, indent=2))
        temporary.replace(destination)
        return destination
