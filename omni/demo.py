"""Repeatable six-stage sample. The sample runs the same gates as live providers."""
import json

PROMPT = "Build a workshop task board where I can add tasks, mark them done, and keep them after a reload."
TITLE = "Workshop task board"
FILES = {
    "harness.json": json.dumps({"runtime": "static", "mode": "web"}),
    "index.html": """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Workshop task board</title>
<link rel="stylesheet" href="styles.css"><script src="app.js" defer></script></head>
<body><main><p>WORKSHOP</p><h1>Workshop task board</h1><form>
<label for="task">New task</label><div><input id="task" required maxlength="120"><button>Add task</button></div>
</form><ul id="tasks" aria-label="Task list"></ul><p id="count" aria-live="polite"></p></main></body></html>""",
    "styles.css": """*{box-sizing:border-box}body{margin:0;background:#e9edf4;color:#192840;font:1rem system-ui}
main{max-width:46rem;margin:8vh auto;background:#fff;padding:clamp(1rem,5vw,3rem);border-radius:1.2rem}
h1{margin-top:0;font-size:clamp(1.8rem,5vw,3rem)}label{display:block;margin:.7rem 0}
form div{display:flex;gap:.6rem}input,button{font:inherit;border-radius:.55rem;padding:.65rem}
input{border:1px solid #73819a;min-width:0;flex:1}button{background:#243c70;color:#fff;border:0;cursor:pointer}
ul{list-style:none;padding:0}li{display:flex;align-items:center;justify-content:space-between;gap:1rem;
padding:.6rem 0;border-bottom:1px solid #ccd5e5}li[data-done=true] span{text-decoration:line-through}""",
    "app.js": """const list = document.querySelector('#tasks');
const count = document.querySelector('#count');
const input = document.querySelector('#task');
let tasks;
try { tasks = JSON.parse(localStorage.getItem('workshop-tasks') || '[]'); } catch { tasks = []; }
if (!Array.isArray(tasks)) tasks = [];
function render() {
  list.replaceChildren();
  for (const task of tasks) {
    const item = document.createElement('li');
    item.dataset.done = task.done;
    const name = document.createElement('span');
    name.textContent = task.name;
    const button = document.createElement('button');
    button.textContent = task.done ? 'Reopen' : 'Mark done';
    button.addEventListener('click', () => { task.done = !task.done; save(); });
    item.append(name, button);
    list.append(item);
  }
  count.textContent = tasks.filter(task => task.done).length + ' completed';
}
function save() { localStorage.setItem('workshop-tasks', JSON.stringify(tasks)); render(); }
document.querySelector('form').addEventListener('submit', (event) => {
  event.preventDefault();
  const name = input.value.trim();
  if (!name) return;
  tasks.push({name, done: false});
  save();
  input.value = '';
  input.focus();
});
render();
""",
}
SOURCE = "https://developer.mozilla.org/en-US/docs/Web/API/Window/localStorage"
EXAMPLES = {
    "lead": {"summary": "Build and verify a local workshop task board.",
             "stages": {"research": "Evaluate browser persistence.", "architecture": "Design a static app.",
                        "design": "Specify the task interaction.", "development": "Implement the task board.",
                        "qa": "Exercise task creation, completion and persistence.",
                        "deploy": "Package the build and start a local preview."},
             "criteria": [{"id": "AC1", "description": "Add a task from a labeled input."},
                          {"id": "AC2", "description": "Mark a task complete and see the updated count."},
                          {"id": "AC3", "description": "Retain the task and completion after reload."}]},
    "research": {"summary": "Use native browser APIs for a dependency-free task board.",
                 "findings": ["localStorage persists per origin and requires no backend.",
                              "Render task names with textContent to keep input as data."],
                 "sources": [SOURCE], "decisions": ["Use version-independent browser APIs with no third-party packages."]},
    "architecture": {"summary": "Serve a dependency-free static application.", "runtime": "static", "mode": "web",
                     "components": ["Task form", "Task list", "Browser persistence"],
                     "dataModel": "An array of task objects containing name and done.",
                     "api": "GET / serves the application; no remote data API.",
                     "boundaries": "Tasks are stored under the isolated preview origin.",
                     "decisions": ["Keep rendering and state in the browser."]},
    "design": {"summary": "Provide one keyboard-accessible task workflow.",
               "flows": ["Enter a task, submit the form, then mark it complete."],
               "wireframes": ["Page heading, labeled task input, action button, task list and progress count."],
               "components": ["Task form", "Task row", "Progress announcement"],
               "accessibility": ["Use native buttons, a visible input label and a live completion count."],
               "decisions": ["Use a single responsive page."]},
    "development": {"summary": "Implemented task entry, completion and local persistence.", "files": FILES},
    "qa": {"summary": "Verify the complete task journey in Chromium.",
           "checks": [{"criterion": "AC1", "evidence": "Submit the form and assert the task appears."},
                      {"criterion": "AC2", "evidence": "Click Mark done and assert the completed count."},
                      {"criterion": "AC3", "evidence": "Navigate to the root again and assert persisted data."}],
           "journeys": [{"name": "Add, complete and restore a task", "steps": [
               {"action": "goto", "path": "/"}, {"action": "fill", "selector": "#task", "value": "Cut frame"},
               {"action": "click", "selector": "form button"}, {"action": "text", "selector": "li", "value": "Cut frame"},
               {"action": "click", "selector": "li button"}, {"action": "text", "selector": "#count", "value": "1 completed"},
               {"action": "goto", "path": "/"}, {"action": "text", "selector": "li", "value": "Cut frame"},
               {"action": "text", "selector": "#count", "value": "1 completed"},
           ]}]},
    "deploy": {"summary": "Package the tested task board and serve it over a loopback preview.",
               "instructions": ["Open the preview to enter and complete tasks.",
                                "Download the source build to serve index.html using a local HTTP server."],
               "decisions": ["Serve the same files that passed browser verification."]},
}
