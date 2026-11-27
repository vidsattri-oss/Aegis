"""Package the control plane as a Python executable and ship the Docker build context."""
import shutil
import tarfile
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_file(path):
    return "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo")


def main():
    target = ROOT / "dist"
    target.mkdir(exist_ok=True)
    binary = target / "aegis"
    with tempfile.TemporaryDirectory(prefix="aegis-build-") as scratch:
        packed = Path(scratch)
        shutil.copytree(ROOT / "aegis", packed / "aegis", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        temporary = target / "aegis.tmp"
        zipapp.create_archive(packed, temporary, main="aegis.cli:main", interpreter="/usr/bin/env python3",
                              compressed=True)
        temporary.replace(binary)
    app = target / "aegis-src.tar.gz"
    temporary = target / "aegis-src.tmp"
    with tarfile.open(temporary, "w:gz") as archive:
        for name in ("README.md", "Makefile", "requirements-dev.txt", ".coveragerc", ".gitignore", ".dockerignore",
                     "containers", "aegis", "pitcrew", "omni", "tests", "examples", "docs/screenshots"):
            path = ROOT / name
            if path.is_file():
                archive.add(path, arcname=name)
            else:
                for item in sorted(path.rglob("*")):
                    if item.is_file() and source_file(item):
                        archive.add(item, arcname=item.relative_to(ROOT))
    temporary.replace(app)
    print(binary)
    print(app)


if __name__ == "__main__":
    main()
