"""Managed monorepo and atomic stage commits in session worktrees."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .contracts import MAX_BYTES, require, safe_path
from .store import now

GITHUB_URL = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})/?\Z")
GITHUB_OPTIONS = ("-c", "http.followRedirects=false",
                  "-c", "credential.https://github.com.helper=!gh auth git-credential")


def github_url(value):
    match = GITHUB_URL.fullmatch(value) if isinstance(value, str) else None
    require(match, "Enter a GitHub HTTPS repository URL such as https://github.com/owner/name")
    owner, name = match.groups()
    name = name.removesuffix(".git")
    require(name and name not in (".", ".."), "Enter a GitHub repository name")
    return f"https://github.com/{owner}/{name}.git"


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
        self.git(self.root, "config", "user.name", "Pit Crew Harness Lead")
        self.git(self.root, "config", "user.email", "lead@pitcrew.local")
        self.git(self.root, "config", "core.hooksPath", "/dev/null")
        self.git(self.root, "config", "commit.gpgSign", "false")
        if self.git(self.root, "rev-parse", "--verify", "HEAD", check=False).returncode:
            self.git(self.root, "commit", "--allow-empty", "-m", "Initialize Pit Crew Harness monorepo")

    @staticmethod
    def git(cwd, *args, check=True, index=None, data=None, binary=False):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0")
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        result = subprocess.run(["git", *args], cwd=cwd, env=env, input=data, capture_output=True,
                                timeout=45, **({} if binary else {"encoding": "utf-8", "errors": "replace"}))
        if check and result.returncode:
            detail = result.stderr.decode("utf-8", "replace") if binary else result.stderr
            raise RuntimeError(f"Git {args[0]} failed: {detail.strip()[-1500:]}")
        return result

    @staticmethod
    def github_ref(project, suffix="head"):
        return f"refs/pitcrew/github/{project}/{suffix}"

    def import_github(self, project, url, branch):
        ref = self.github_ref(project)
        source = "HEAD"
        if branch:
            source = "refs/heads/" + branch
            require(len(branch) <= 200 and self.git(self.root, "check-ref-format", source, check=False).returncode == 0,
                    "Enter a valid GitHub branch name")
        self.git(self.root, *GITHUB_OPTIONS, "fetch", "--no-tags", "--no-recurse-submodules", "--",
                 github_url(url), f"{source}:{ref}")

    def github_head(self, project):
        return self.git(self.root, "rev-parse", "--verify", self.github_ref(project)).stdout.strip()

    def github_inventory(self, base):
        entries = {}
        for entry in self.git(self.root, "ls-tree", "-r", "-l", "-z", base).stdout.rstrip("\0").split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            mode, kind, blob, size = metadata.split()
            entries[name] = (mode, blob, int(size) if kind == "blob" else 0)
        return entries

    def github_source(self, entries, redact):
        source = {}
        total = 0
        for name, (mode, blob, size) in entries.items():
            if mode not in ("100644", "100755") or name.lower().endswith((".key", ".pem", ".p12")):
                continue
            if Path(name).name.lower() in ("credentials.json", "secrets.json"):
                continue
            if len(source) == 120 or total == MAX_BYTES:
                break
            if total + size > MAX_BYTES:
                continue
            try:
                safe_path(name)
                content = self.git(self.root, "cat-file", "blob", blob, binary=True).stdout.decode("utf-8")
            except (ValueError, UnicodeError):
                continue
            if "\0" not in content and redact(content) == content:
                source[name] = content
                total += size
        return source

    def commit_github(self, session, original, entries, files):
        base = session["github_base"]
        ref = self.github_ref(session["project_id"], "laps/" + session["id"])
        saved = self.git(self.root, "rev-parse", "--verify", ref, check=False)
        if saved.returncode == 0:
            return saved.stdout.strip()
        changed = {name: content for name, content in files.items() if original.get(name) != content}
        removed = original.keys() - files.keys()
        if not changed and not removed:
            return None
        with tempfile.TemporaryDirectory(prefix="git-index-", dir=self.staging) as directory:
            index = Path(directory) / "index"
            self.git(self.root, "read-tree", base, index=index)
            if removed:
                self.git(self.root, "update-index", "--force-remove", "--", *sorted(removed), index=index)
            for name, content in changed.items():
                safe_path(name)
                require(name not in entries or name in original, "Cannot replace an excluded GitHub path")
                mode = entries[name][0] if name in entries else "100644"
                blob = self.git(self.root, "hash-object", "-w", "--stdin", data=content.encode(), binary=True)
                self.git(self.root, "update-index", "--add", "--cacheinfo",
                         f"{mode},{blob.stdout.decode().strip()},{name}", index=index)
            tree = self.git(self.root, "write-tree", index=index).stdout.strip()
        commit = self.git(self.root, "commit-tree", tree, "-p", base, "-m",
                          f"Pit Crew: approved lap {session['id']}").stdout.strip()
        self.git(self.root, "update-ref", ref, commit, "0" * len(commit))
        return commit

    def advance_github(self, session, commit):
        ref = self.github_ref(session["project_id"])
        if self.github_head(session["project_id"]) != commit:
            self.git(self.root, "update-ref", ref, commit, session["github_base"])

    def publish_github(self, session, url):
        branch = "pitcrew/lap-" + session["id"]
        self.git(self.root, *GITHUB_OPTIONS, "push", "--", github_url(url),
                 f"{session['github_commit']}:refs/heads/{branch}")
        return branch

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
