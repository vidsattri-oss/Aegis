"""Run all six demo stages against real local Docker images."""
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni import demo
from omni.engine import Lead, TERMINAL
from omni.sandbox import Docker


def main():
    Docker().doctor()
    # Docker Desktop only shares configured host paths with its VM.
    with tempfile.TemporaryDirectory(prefix=".omni-smoke-", dir=Path.cwd()) as scratch:
        lead = Lead(Path(scratch))
        try:
            project = lead.store.project("Real Docker smoke test", "")
            sid = lead.launch(project["id"], "", sample=True)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                session = lead.get(sid)
                if session["state"] in TERMINAL:
                    break
                time.sleep(0.35)
            if session["state"] != "completed":
                raise AssertionError(f"{session['stage']} ended with {session['state']}: {session['error']}")
            assert len(lead.store.summary(sid)["gates"]) == 7
            with urlopen(session["preview"], timeout=15) as response:
                assert demo.TITLE.encode() in response.read()
            with urlopen(session["preview"] + "/panorama.css", timeout=15) as response:
                assert b"data:image/webp;base64," in response.read()
            with ZipFile(session["bundle"]) as archive:
                assert archive.testzip() is None and set(demo.FILES) <= set(archive.namelist())
            print("Lead plan, all six stage gates, live preview and build ZIP passed.")
        finally:
            lead.close()


if __name__ == "__main__":
    main()
