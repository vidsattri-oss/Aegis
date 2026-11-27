"""Run all six demo stages against real local Docker images."""
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pitcrew import demo
from pitcrew.engine import Lead, TERMINAL
from pitcrew.sandbox import Docker


class RepairSmokeLead(Lead):
    def ask_model(self, session, role, data, agent, until, sample, *, instruction=None):
        draft = super().ask_model(session, role, data, agent, until, sample, instruction=instruction)
        if role == "development" and "repair" not in data:
            path = "index.html"
            draft["files"][path] = draft["files"][path].replace(demo.TITLE, "Wrong title for QA", 1)
        return draft


def main():
    Docker().doctor()
    # Docker Desktop only shares configured host paths with its VM.
    with tempfile.TemporaryDirectory(prefix=".pitcrew-smoke-", dir=Path.cwd()) as scratch:
        lead = RepairSmokeLead(Path(scratch))
        try:
            project = lead.store.project("Real Docker smoke test", "")
            demo.EXAMPLES["lead"]["specialists"] = [
                {"name": "Acceptance specialist", "persona": "Focus on observable user outcomes.",
                 "assignment": "Review the board and suggest acceptance checks.", "stage": "architecture",
                 "criterion": "AC1", "paths": [], "deliverable": "One testable board state invariant."},
                {"name": "Accessibility specialist", "persona": "Focus on keyboard navigation.",
                 "assignment": "Identify missing accessibility scenarios.", "stage": "architecture",
                 "criterion": "AC2", "paths": [], "deliverable": "One keyboard interaction to preserve."},
            ]
            demo.EXAMPLES["lead"]["sandbox_sizes"] = {"build": "compact", "specialist": "compact"}
            demo.EXAMPLES["lead"]["crew_notes"] = {"development": "Keep a stable heading for the QA journey."}
            sid = lead.launch(project["id"], "", sample=True)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                session = lead.get(sid)
                if session["state"] in TERMINAL:
                    break
                time.sleep(0.35)
            if session["state"] != "completed":
                raise AssertionError(f"{session['stage']} ended with {session['state']}: {session['error']}")
            summary = lead.store.summary(sid)
            assert len(summary["gates"]) == 7
            assert len([agent for agent in summary["agents"] if agent["role"] == "development"]) == 2
            assert lead.store.row("SELECT id FROM events WHERE session_id=? AND kind='repair_requested'", (sid,))
            specialists = [agent for agent in summary["agents"] if agent["role"] == "specialist"]
            assert len(specialists) == 2 and all(agent["status"] == "Success" for agent in specialists)
            assert all(agent["stage"] == "architecture" and agent["profile"]["network"] == "none" for agent in specialists)
            architecture = next(gate for gate in summary["gates"] if gate["stage"] == "architecture")
            assert [item["criterion"] for item in architecture["details"]["specialists"]] == ["AC1", "AC2"]
            release = summary["gates"][-1]["details"]
            assert release["evidence"]["security_review"]["isolation"]["network"] == "none"
            assert release["reviews"][0]["review"]["security"]["checks"]
            assert lead.dream()["playbooks_revised"] == 9
            with urlopen(session["preview"], timeout=15) as response:
                assert demo.TITLE.encode() in response.read()
                assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
            with urlopen(session["preview"] + "/panorama.css", timeout=15) as response:
                assert b"data:image/webp;base64," in response.read()
            with ZipFile(session["bundle"]) as archive:
                assert archive.testzip() is None and set(demo.FILES) <= set(archive.namelist())
            print("Lead plan, compact sandboxes, specialists, QA repair, release security, isolated preview, and build ZIP passed.")
        finally:
            lead.close()


if __name__ == "__main__":
    main()
