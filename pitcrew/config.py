"""Environment settings and default workspace location."""
import os
from pathlib import Path


def environment(name, default=None):
    return os.environ.get(name, os.environ.get(name.replace("PITCREW_", "OMNI_", 1), default))


def default_workspace():
    configured = environment("PITCREW_DATA")
    if configured is not None:
        return Path(configured)
    workspace = Path(".pitcrew-data")
    legacy = Path(".omni-data")
    if workspace.exists():
        return workspace
    if legacy.exists():
        return legacy
    if Path("Makefile").is_file() and Path("pitcrew/build.py").is_file():
        return workspace

    data_home = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not data_home.is_absolute():
        data_home = Path.home() / ".local" / "share"
    return data_home / "pitcrew"
