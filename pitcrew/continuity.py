"""Private workspace backups, restores, and optional Linux user service."""
import os
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, is_zipfile

from .contracts import require
from .repository import Repository
from .store import encode, uid

DATABASE = "workspace.sqlite3"
HISTORY = "monorepo.bundle"
BUILD = re.compile(r"builds/[0-9a-f]{16}\.zip\Z")
MARKER = "pitcrew-backup-v1"
LEGACY_MARKER = re.compile(r"([a-z][a-z0-9]{0,63})-backup-v1\Z")


def backup_workspace(directory, output):
    require((directory / DATABASE).is_file(), "The workspace database does not exist")
    require(not os.path.lexists(output), "Backup output already exists")
    with TemporaryDirectory(prefix=".pitcrew-backup-", dir=output.parent) as temporary:
        staging = Path(temporary)
        with (closing(sqlite3.connect((directory / DATABASE).as_uri() + "?mode=ro", uri=True)) as source,
              closing(sqlite3.connect(staging / DATABASE)) as snapshot):
            source.backup(snapshot)
        monorepo = directory / "monorepo"
        if (monorepo / ".git").is_dir():
            Repository.git(monorepo, "bundle", "create", str(staging / HISTORY), "--all")
        archive_path = staging / "workspace.zip"
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(MARKER, "Pit Crew Harness workspace backup\n")
            archive.write(staging / DATABASE, DATABASE)
            if (staging / HISTORY).is_file():
                archive.write(staging / HISTORY, HISTORY)
            for build in sorted((directory / "builds").glob("*.zip")):
                name = f"builds/{build.name}"
                if BUILD.fullmatch(name) and build.is_file() and not build.is_symlink():
                    archive.write(build, name)
        archive_path.chmod(0o600)
        os.link(archive_path, output)
    return output


def restore_workspace(backup, destination):
    require(not os.path.lexists(destination), "Choose a new workspace directory for restore")
    with TemporaryDirectory(prefix=".pitcrew-restore-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "workspace"
        staging.mkdir(mode=0o700)
        with ZipFile(backup) as archive:
            entries = archive.namelist()
            markers = {name: match[1] for name in entries if (match := LEGACY_MARKER.fullmatch(name))}
            require(len(entries) == len(set(entries)) and len(markers) == 1 and DATABASE in entries and all(
                name in markers or name in (DATABASE, HISTORY) or BUILD.fullmatch(name) for name in entries
            ), "Invalid Pit Crew Harness backup manifest")
            marker, application = next(iter(markers.items()))
            if marker != MARKER:
                # Older v1 backups recorded the same application name in both the marker and its contents.
                expected = f"{application} workspace backup\n".encode("ascii")
                require(archive.getinfo(marker).file_size == len(expected)
                        and archive.read(marker).lower() == expected, "Invalid Pit Crew Harness backup marker")
            for name in entries:
                if name == marker:
                    continue
                target = staging / name
                target.parent.mkdir(exist_ok=True)
                with archive.open(name) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
        (staging / DATABASE).chmod(0o600)
        with closing(sqlite3.connect(staging / DATABASE)) as db, db:
            require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "Backup database failed its integrity check")
            # A restored copy must have a separate workspace label and keyring slots.
            db.execute("INSERT OR REPLACE INTO settings VALUES ('workspace_id',?)", (encode(uid()),))
            db.execute("DELETE FROM settings WHERE key LIKE 'keyring-%'")
            db.execute("UPDATE sessions SET preview=NULL")
            for (sid,) in db.execute("SELECT id FROM sessions WHERE bundle IS NOT NULL").fetchall():
                name = f"builds/{sid}.zip"
                local = str(destination / name) if BUILD.fullmatch(name) and (staging / name).is_file() else None
                db.execute("UPDATE sessions SET bundle=? WHERE id=?", (local, sid))
        if (staging / HISTORY).is_file():
            tree = staging / "monorepo"
            tree.mkdir()
            Repository.git(staging, "clone", "--mirror", str(staging / HISTORY), str(tree / ".git"))
            Repository.git(tree, "config", "core.bare", "false")
            Repository.git(tree, "config", "core.hooksPath", "/dev/null")
            Repository.git(tree, "config", "--remove-section", "remote.origin")
            Repository.git(tree, "symbolic-ref", "HEAD", "refs/heads/main")
            Repository.git(tree, "reset", "--hard", "HEAD")
            (staging / HISTORY).unlink()
        require(not os.path.lexists(destination), "Restore directory was created by another process")
        staging.rename(destination)
    return destination


def unit_value(value):
    value = str(value)
    require(not any(ord(char) < 32 for char in value), "Invalid path or setting for a systemd user service")
    return value.replace("%", "%%")


def unit_arg(value, *, command=False):
    escaped = unit_value(value).replace("\\", "\\\\").replace('"', '\\"')
    if command:
        escaped = escaped.replace("$", "$$")
    return '"' + escaped + '"'


def user_service(app, directory, port, docker_context=None):
    require(1 <= port <= 65535, "Service port must be between 1 and 65535")
    app = Path(app).resolve()
    packaged = app.suffix == ".pyz" or is_zipfile(app)
    working = app.parent if packaged else Path(__file__).resolve().parents[1]
    entry = [app] if packaged else ["-m", "pitcrew"]
    args = [sys.executable, *entry, "serve", "--port", port, "--data", directory]
    command = " ".join(unit_arg(arg, command=True) for arg in args)
    context = f"Environment={unit_arg('DOCKER_CONTEXT=' + docker_context)}\n" if docker_context else ""
    return ("[Unit]\nDescription=Pit Crew Harness local workspace\n\n"
            "[Service]\nType=exec\n"
            f"WorkingDirectory={unit_value(working)}\n{context}"
            "EnvironmentFile=-%h/.config/pitcrew/service.env\n"
            f"ExecStart={command}\nRestart=on-failure\nRestartSec=3\nKillMode=mixed\nTimeoutStopSec=180\n\n"
            "[Install]\nWantedBy=default.target\n")
