"use strict";

const STORAGE_KEY = "fieldwork-expedition-v1";
const STATES = [
  ["planned", "To plan"],
  ["active", "In progress"],
  ["ready", "Ready"],
];
const PEOPLE = {
  mae: ["Mae Rivers", "MR"],
  luca: ["Luca Vale", "LV"],
  noa: ["Noa Kim", "NK"],
  you: ["You", "Y"],
};

const initial = {
  tasks: [
    { id: "sunrise", title: "Scout sunrise compositions", notes: "Flag two setups with a clear eastern horizon.", status: "planned", category: "Photo", priority: "High", owner: "mae" },
    { id: "shuttle", title: "Confirm the trailhead shuttle", notes: "Check the early departure and return window.", status: "planned", category: "Logistics", priority: "Normal", owner: "luca" },
    { id: "route", title: "Map the ridge approach", notes: "Save the main track and a lower-altitude alternative.", status: "active", category: "Route", priority: "High", owner: "noa" },
    { id: "camera", title: "Assemble the camera kit", notes: "Pack for one body, two primes, and a long day.", status: "active", category: "Gear", priority: "Normal", owner: "mae" },
    { id: "refuge", title: "Book the mountain refuge", notes: "Three bunks confirmed for the ridge night.", status: "ready", category: "Logistics", priority: "Normal", owner: "luca" },
    { id: "map", title: "Download the offline maps", notes: "Trail network and huts saved on both phones.", status: "ready", category: "Route", priority: "Normal", owner: "noa" },
  ],
  gear: [
    { id: "boots", name: "Trail boots", packed: true },
    { id: "shell", name: "Waterproof shell", packed: true },
    { id: "headlamp", name: "Headlamp", packed: true },
    { id: "first-aid", name: "First aid kit", packed: true },
    { id: "batteries", name: "Spare batteries", packed: false },
    { id: "tripod", name: "Travel tripod", packed: false },
    { id: "cards", name: "Memory cards", packed: false },
    { id: "filter", name: "Water filter", packed: false },
  ],
  journal: [
    { id: "scout-note", title: "First light from the west ridge", body: "Look for a clean foreground before the sun catches the limestone. Bring the 35 mm and leave room for a frame of the valley in shadow.", stamp: "2027-09-15" },
    { id: "backup-note", title: "Keep a lower route handy", body: "If clouds settle over the three peaks, spend the morning below the ridge and look for layers of fog between the trees.", stamp: "2027-09-14" },
  ],
};

const $ = (selector) => document.querySelector(selector);
const board = $("#task-board");
const dialog = $("#task-dialog");
const taskForm = $("#task-form");
const search = $("#task-search");
const filter = $("#task-filter");
let canStore = true;
let editingId = null;
let draggingId = null;

function load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const saved = JSON.parse(raw);
      if (saved && ["tasks", "gear", "journal"].every((key) => Array.isArray(saved[key]))) {
        return saved;
      }
    }
  } catch (error) {
    canStore = error instanceof SyntaxError;
  }
  return structuredClone(initial);
}

const state = load();

function announce(message) {
  $("#announcement").textContent = message;
}

function storageStatus() {
  $("#storage").classList.toggle("unavailable", !canStore);
  $("#storage").lastChild.textContent = canStore ? " Saved on this device" : " Changes are temporary";
}

function save(message) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    canStore = true;
  } catch {
    canStore = false;
  }
  storageStatus();
  updateProgress();
  announce(canStore ? message : message + " Browser storage is unavailable.");
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function newId() {
  return Array.from(crypto.getRandomValues(new Uint32Array(2)), (value) => value.toString(36)).join("-");
}

function updateProgress() {
  const ready = state.tasks.filter((task) => task.status === "ready").length;
  const packed = state.gear.filter((item) => item.packed).length;
  const total = state.tasks.length + state.gear.length;
  const percent = total ? Math.round(100 * (ready + packed) / total) : 0;
  $("#ready-percent").textContent = percent + "%";
  $("#ready-progress").value = percent;
  $("#tasks-ready").textContent = ready + " / " + state.tasks.length;
  $("#gear-packed").textContent = packed + " / " + state.gear.length;
  $("#packing-count").textContent = packed + " of " + state.gear.length + " items packed";
  $("#packing-progress").max = Math.max(1, state.gear.length);
  $("#packing-progress").value = packed;
}

function makeCard(task) {
  const card = element("article", "task-card");
  card.draggable = true;
  card.dataset.taskId = task.id;
  const button = element("button", "task-open");
  button.id = "task-" + task.id;
  button.type = "button";
  button.setAttribute("aria-label", "Edit " + task.title);
  const top = element("span", "card-top");
  const tag = element("span", "tag", task.category);
  tag.dataset.category = task.category;
  top.append(tag, icon("grip"));
  button.append(top, element("span", "task-title", task.title));
  if (task.notes) button.append(element("span", "task-notes", task.notes));
  const footer = element("span", "card-footer");
  const [name, initials] = PEOPLE[task.owner] || PEOPLE.you;
  const owner = element("span", "card-owner");
  const avatar = element("span", "avatar", initials);
  avatar.dataset.owner = task.owner;
  avatar.setAttribute("aria-hidden", "true");
  owner.append(avatar, element("span", "", name.split(" ")[0]));
  const priority = element("span", "card-priority", task.priority);
  priority.dataset.priority = task.priority;
  footer.append(owner, priority);
  button.append(footer);
  button.addEventListener("click", () => openTask(task.status, task));
  card.append(button);
  return card;
}

function addTaskButton(status, name) {
  const button = element("button", "column-add");
  button.type = "button";
  button.setAttribute("aria-label", "Add task to " + name);
  button.append(icon("plus"));
  button.addEventListener("click", () => openTask(status));
  return button;
}

function renderBoard() {
  const query = search.value.trim().toLowerCase();
  const category = filter.value;
  const matches = state.tasks.filter((task) => (!category || task.category === category)
    && [task.title, task.notes, task.category].some((text) => text.toLowerCase().includes(query)));
  board.replaceChildren();

  for (const [status, name] of STATES) {
    const column = element("section", "column");
    column.dataset.status = status;
    column.setAttribute("aria-labelledby", "lane-" + status);
    const heading = element("div", "lane-heading");
    const title = element("h3", "", name);
    title.id = "lane-" + status;
    const tasks = matches.filter((task) => task.status === status);
    heading.append(element("span", "lane-dot"), title, element("span", "lane-count", tasks.length),
      addTaskButton(status, name));
    const cards = element("div", "lane-cards");
    for (const task of tasks) cards.append(makeCard(task));
    if (!tasks.length) {
      cards.append(element("p", "empty-lane", query || category ? "No matching tasks" : "No tasks yet"));
    }
    column.append(heading, cards);
    board.append(column);
  }
}

function openTask(status, task = null) {
  editingId = task?.id ?? null;
  taskForm.reset();
  const fields = taskForm.elements;
  fields.title.setCustomValidity("");
  fields.title.value = task?.title ?? "";
  fields.notes.value = task?.notes ?? "";
  fields.category.value = task?.category ?? "Route";
  fields.priority.value = task?.priority ?? "Normal";
  fields.owner.value = task?.owner ?? "you";
  fields.status.value = status;
  $("#editor-title").textContent = task ? "Edit task" : "New task";
  $("#delete-task").hidden = !task;
  dialog.showModal();
  fields.title.focus();
}

function finishTask(message, id) {
  dialog.close();
  save(message);
  renderBoard();
  (document.getElementById("task-" + id) || $("#new-task")).focus();
}

taskForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const fields = new FormData(taskForm);
  const title = fields.get("title").trim();
  if (!title) {
    taskForm.elements.title.setCustomValidity("Enter a task title.");
    taskForm.elements.title.reportValidity();
    return;
  }
  const task = {
    id: editingId || newId(),
    title,
    notes: fields.get("notes").trim(),
    category: fields.get("category"),
    priority: fields.get("priority"),
    owner: fields.get("owner"),
    status: fields.get("status"),
  };
  if (editingId) {
    const index = state.tasks.findIndex((item) => item.id === editingId);
    state.tasks[index] = task;
  } else {
    state.tasks.push(task);
    search.value = "";
    filter.value = "";
  }
  finishTask(editingId ? "Task updated." : "Task added.", task.id);
});

$("#delete-task").addEventListener("click", () => {
  const task = state.tasks.find((item) => item.id === editingId);
  if (task && window.confirm("Delete “" + task.title + "”?")) {
    state.tasks = state.tasks.filter((item) => item.id !== editingId);
    finishTask("Task deleted.", editingId);
  }
});
$("#task-title").addEventListener("input", (event) => event.target.setCustomValidity(""));
$("#new-task").addEventListener("click", () => openTask("planned"));
$("#close-task").addEventListener("click", () => dialog.close());
search.addEventListener("input", renderBoard);
filter.addEventListener("change", renderBoard);

function clearDropTarget() {
  board.querySelector(".drop-target")?.classList.remove("drop-target");
}

board.addEventListener("dragstart", (event) => {
  const card = event.target.closest("[data-task-id]");
  if (!card) return;
  draggingId = card.dataset.taskId;
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", draggingId);
});
board.addEventListener("dragover", (event) => {
  const column = event.target.closest(".column");
  if (!draggingId || !column) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  clearDropTarget();
  column.classList.add("drop-target");
});
board.addEventListener("dragleave", (event) => {
  if (!board.contains(event.relatedTarget)) clearDropTarget();
});
board.addEventListener("dragend", () => {
  draggingId = null;
  clearDropTarget();
});
board.addEventListener("drop", (event) => {
  const column = event.target.closest(".column");
  const task = state.tasks.find((item) => item.id === draggingId);
  if (!column || !task) return;
  event.preventDefault();
  draggingId = null;
  clearDropTarget();
  if (task.status !== column.dataset.status) {
    task.status = column.dataset.status;
    save("Moved " + task.title + " to " + STATES.find(([status]) => status === task.status)[1] + ".");
    renderBoard();
  }
});

function renderGear() {
  const list = $("#gear-list");
  list.replaceChildren();
  for (const item of state.gear) {
    const row = element("li", "gear-row");
    const label = element("label", "gear-check");
    const box = element("input");
    box.type = "checkbox";
    box.checked = item.packed;
    box.addEventListener("change", () => {
      item.packed = box.checked;
      save(item.packed ? "Item packed." : "Item unpacked.");
    });
    label.append(box, element("span", "", item.name));
    const remove = element("button", "icon-button");
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove " + item.name);
    remove.append(icon("close"));
    remove.addEventListener("click", () => {
      state.gear = state.gear.filter((entry) => entry.id !== item.id);
      save("Item removed.");
      renderGear();
      $("#gear-name").focus();
    });
    row.append(label, remove);
    list.append(row);
  }
}

$("#gear-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("#gear-name");
  const name = input.value.trim();
  if (!name) return;
  state.gear.push({ id: newId(), name, packed: false });
  save("Gear item added.");
  renderGear();
  input.value = "";
  input.focus();
});

function renderJournal() {
  const list = $("#journal-entries");
  list.replaceChildren();
  for (const entry of state.journal) {
    const article = element("article", "paper journal-entry");
    const date = new Date(entry.stamp + "T12:00:00");
    const stamp = element("time", "entry-date", date.toLocaleDateString("en", { dateStyle: "medium" }));
    stamp.dateTime = entry.stamp;
    article.append(stamp, element("h2", "", entry.title), element("p", "", entry.body));
    list.append(article);
  }
}

$("#journal-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const title = form.elements.title.value.trim();
  const body = form.elements.body.value.trim();
  if (!title || !body) return;
  const date = new Date();
  const stamp = [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0")].join("-");
  state.journal.unshift({ id: newId(), title, body, stamp });
  save("Journal entry saved.");
  renderJournal();
  form.reset();
});

const panels = [...document.querySelectorAll("[data-panel]")];
const links = [...document.querySelectorAll("[data-view]")];

function showView(focusHeading = false) {
  const fragment = location.hash.slice(1);
  if (fragment === "content") {
    $("#content").focus();
    return;
  }
  const panel = panels.find((item) => item.dataset.panel === fragment) || panels[0];
  for (const item of panels) item.hidden = item !== panel;
  for (const link of links) {
    if (link.dataset.view === panel.dataset.panel) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("#new-task").hidden = panel !== panels[0];
  if (focusHeading) {
    window.scrollTo(0, 0);
    const heading = panel.querySelector("h1");
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
  }
}

window.addEventListener("hashchange", () => showView(true));
storageStatus();
updateProgress();
renderBoard();
renderGear();
renderJournal();
showView();
