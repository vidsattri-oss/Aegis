"""Environment settings and default workspace location."""
import os
from pathlib import Path


def environment(name, default=None):
    old_name = name.removeprefix("AEGIS_")
    return os.environ.get(name, os.environ.get("PITCREW_" + old_name, os.environ.get("OMNI_" + old_name, default)))


def default_workspace():
    configured = environment("AEGIS_DATA")
    if configured is not None:
        return Path(configured)
    data_home = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not data_home.is_absolute():
        data_home = Path.home() / ".local" / "share"
    workspace = data_home / "aegis"
    for existing in (workspace, data_home / "pitcrew", Path(".aegis-data"), Path(".pitcrew-data"), Path(".omni-data")):
        if existing.is_dir():
            return existing
    return workspace
