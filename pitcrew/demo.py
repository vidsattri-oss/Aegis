"""Repeatable six-stage sample. The sample runs the same gates as live providers."""
import base64
import json
from importlib.resources import files

PROMPT = (
    "Build Fieldwork, an expedition planner for a three-day photography trip in the Dolomites. "
    "Include a task board, an expandable itinerary, a packing checklist and a field journal. "
    "Keep edits after reload and make the application work offline in the preview."
)
TITLE = "Fieldwork — Expedition planner"
ASSETS = files("pitcrew").joinpath("sample")
# The build contract accepts text files. Embedding WebP in CSS keeps the preview offline.
PANORAMA = base64.b64encode(ASSETS.joinpath("dolomites.webp").read_bytes()).decode("ascii")
FILES = {
    "harness.json": json.dumps({"runtime": "static", "mode": "web"}),
    **{name: ASSETS.joinpath(name).read_text(encoding="utf-8") for name in ("index.html", "styles.css", "app.js")},
    "panorama.css": f':root{{--panorama:url("data:image/webp;base64,{PANORAMA}");}}\n',
}

SOURCE = "https://developer.mozilla.org/en-US/docs/Web/API/Window/localStorage"
EXAMPLES = {
    "lead": {
        "summary": "Build and verify an offline alpine expedition planner.",
        "stages": {
            "research": "Evaluate native browser persistence and controls.",
            "architecture": "Design the offline app and its data model.",
            "design": "Specify the four expedition views and their interactions.",
            "development": "Implement a responsive, editable expedition workspace.",
            "qa": "Exercise each view and verify that edits survive reload.",
            "deploy": "Package the verified build and start the sandbox preview.",
        },
        "criteria": [
            {"id": "AC1", "description": "Create and move a task and retain the change after reload."},
            {"id": "AC2", "description": "Open the itinerary and expand a day's schedule."},
            {"id": "AC3", "description": "Add and pack a gear item and retain the packing count after reload."},
            {"id": "AC4", "description": "Write a journal entry and retain it after reload."},
        ],
    },
    "research": {
        "summary": "Use native browser APIs with no runtime dependencies.",
        "findings": [
            "localStorage persists per origin and requires no backend.",
            "Native dialog, details and form controls support keyboard interactions.",
            "Render user content as text and bundle the panorama with the application.",
        ],
        "sources": [SOURCE],
        "decisions": ["Use browser primitives and keep all assets available offline."],
    },
    "architecture": {
        "summary": "Serve one static application with four navigable views.",
        "runtime": "static",
        "mode": "web",
        "components": ["Task board", "Itinerary", "Gear checklist", "Field journal", "Browser persistence"],
        "dataModel": "One versioned browser store holds tasks, packing items and journal entries.",
        "api": "GET / serves the planner. Views use URL fragments and all writes are local.",
        "boundaries": "Edits belong to the isolated preview origin.",
        "decisions": ["Ship HTML, CSS and JavaScript without a network dependency."],
    },
    "design": {
        "summary": "Give users four responsive views with visible progress.",
        "flows": [
            "Create or open a task, choose a status, and save it to the appropriate column.",
            "Browse the itinerary, check off the packing list, and write a journal entry.",
        ],
        "wireframes": [
            "Expedition navigation, panoramic hero, progress strip and three board columns.",
            "Day-by-day itinerary, gear checklist with packing progress and a journal with entry form.",
        ],
        "components": ["Navigation", "Task editor", "Progress indicators", "Day schedules", "Journal entries"],
        "accessibility": [
            "Use labeled fields, native dialog, keyboard-accessible status controls and live feedback.",
            "Use text labels alongside the status colors and stack the board at narrow widths.",
        ],
        "decisions": ["Use a warm paper palette, landscape photography and compact board cards."],
    },
    "development": {"summary": "Implemented Fieldwork and bundled its offline assets.", "files": FILES},
    "qa": {
        "summary": "Verify the board, itinerary, packing and journal journeys in Chromium.",
        "checks": [
            {"criterion": "AC1", "evidence": "Create a task, edit its status, reload, and find it in Ready."},
            {"criterion": "AC2", "evidence": "Open the itinerary and expand Day 2 to inspect its sunrise schedule."},
            {"criterion": "AC3", "evidence": "Add and pack an item and assert the packing count after reload."},
            {"criterion": "AC4", "evidence": "Save a note and assert that its title and body survive reload."},
        ],
        "journeys": [
            {"name": "Create, move and restore a task", "steps": [
                {"action": "goto", "path": "/"},
                {"action": "title", "value": TITLE},
                {"action": "click", "selector": "#new-task"},
                {"action": "fill", "selector": "#task-title", "value": "Prepare shot list"},
                {"action": "click", "selector": "#save-task"},
                {"action": "visible", "selector": '[data-status="planned"] [aria-label="Edit Prepare shot list"]'},
                {"action": "click", "selector": '[aria-label="Edit Prepare shot list"]'},
                {"action": "click", "selector": '#task-form input[value="ready"]'},
                {"action": "click", "selector": "#save-task"},
                {"action": "goto", "path": "/?check=board#board"},
                {"action": "visible", "selector": '[data-status="ready"] [aria-label="Edit Prepare shot list"]'},
                {"action": "text", "selector": "#tasks-ready", "value": "3 / 7"},
                {"action": "fill", "selector": "#task-search", "value": "shot list"},
                {"action": "text", "selector": '[data-status="planned"] .empty-lane', "value": "No matching tasks"},
            ]},
            {"name": "Open a day's itinerary", "steps": [
                {"action": "goto", "path": "/"},
                {"action": "click", "selector": '.hero a[href="#itinerary"]'},
                {"action": "visible", "selector": "#itinerary-title"},
                {"action": "click", "selector": ".route-day:nth-child(2) summary"},
                {"action": "visible", "selector": ".route-day:nth-child(2) li:first-child"},
                {"action": "text", "selector": ".route-day:nth-child(2) li:first-child", "value": "Set up for sunrise"},
            ]},
            {"name": "Pack an item and restore the checklist", "steps": [
                {"action": "goto", "path": "/?journey=gear#gear"},
                {"action": "fill", "selector": "#gear-name", "value": "Lens cloth"},
                {"action": "click", "selector": "#gear-form button"},
                {"action": "click", "selector": "#gear-list li:last-child label"},
                {"action": "goto", "path": "/?check=gear#gear"},
                {"action": "text", "selector": "#packing-count", "value": "5 of 9 items packed"},
                {"action": "visible", "selector": "#gear-list li:last-child input:checked"},
                {"action": "text", "selector": "#gear-list li:last-child", "value": "Lens cloth"},
            ]},
            {"name": "Save and restore a journal entry", "steps": [
                {"action": "goto", "path": "/?journey=journal#journal"},
                {"action": "fill", "selector": '#journal-form input[name="title"]', "value": "Blue hour"},
                {"action": "fill", "selector": "#journal-form textarea", "value": "Scout the quieter side of the ridge."},
                {"action": "click", "selector": "#journal-form button"},
                {"action": "goto", "path": "/?check=journal#journal"},
                {"action": "text", "selector": "#journal-entries article:first-child h2", "value": "Blue hour"},
                {"action": "text", "selector": "#journal-entries article:first-child p", "value": "Scout the quieter side of the ridge."},
            ]},
        ],
    },
    "deploy": {
        "summary": "Package the verified expedition planner and start its sandbox preview.",
        "instructions": [
            "Open the preview to explore the board, itinerary, gear and journal.",
            "Download the build to serve it with any local static HTTP server.",
        ],
        "decisions": ["Serve the same files that passed browser verification."],
    },
}
