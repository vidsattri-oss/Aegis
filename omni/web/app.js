"use strict";

const $ = (selector) => document.querySelector(selector);
const stages = [["research", "Research"], ["architecture", "Architecture"], ["design", "UX/UI design"],
  ["development", "Development"], ["qa", "QA / UAT"], ["deploy", "Deploy"]];
const names = Object.fromEntries([["lead", "Lead"], ["review", "Independent QA"], ...stages]);
const labels = { queued: "Queued", running: "Running", retrying: "Retried", completed: "Healthy",
  failed: "Failed", cancelled: "Cancelled", passed: "Passed", ready: "Ready", pending: "Pending" };
const state = { workspace: null, project: null, session: null, route: "", tab: "map",
  engine: null, csrf: "", refreshing: false, toast: null, scope: {}, telemetry: null, signature: "" };
const tabNames = [["map", "Team & Factory Line"], ["summary", "Summary"], ["output", "Live output"]];
const number = new Intl.NumberFormat();
const money = (value) => "$" + Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 5, minimumFractionDigits: 2 });
const timestamp = (value) => value ? new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
const val = (value) => JSON.stringify(value, null, 2);
const limit = (text, count = 92) => text.length > count ? text.slice(0, count - 1) + "…" : text;

function node(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else element.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

const button = (text, handler, style = "") => node("button", { type: "button", class: "button " + style, onclick: handler }, text);
const anchor = (text, href, style = "") => node("a", { href, class: "button " + style }, text);
const muted = (...items) => node("small", {}, items);
const actions = (...items) => node("div", { class: "actions" }, items);
const code = (text) => node("code", {}, text);
const sectionHeading = (name, action) => node("div", { class: "section-heading" }, node("h2", {}, name), action);
const list = (items) => node("ul", { class: "criteria" }, items.map((item) => node("li", {}, item)));
const facts = (pairs) => node("dl", { class: "facts" }, pairs.map(([name, value]) =>
  [node("dt", {}, name), node("dd", {}, value ?? "—")]));

function badge(value, text) {
  const normalized = String(value || "pending").toLowerCase();
  const type = ["success", "completed", "passed", "ready"].includes(normalized) ? "good"
    : normalized === "failed" ? "error" : ["retrying", "retried", "cancelled"].includes(normalized) ? "warn"
      : ["running", "queued"].includes(normalized) ? "active" : "";
  return node("span", { class: "badge " + type }, text || labels[normalized] || value || "Idle");
}

function duration(start, end) {
  if (!start || !end) return "—";
  const seconds = Math.max(0, (new Date(end) - new Date(start)) / 1000);
  return seconds < 60 ? seconds.toFixed(seconds < 10 ? 1 : 0) + " s" : Math.floor(seconds / 60) + "m " + Math.floor(seconds % 60) + "s";
}

function notify(text) {
  const toast = $("#toast");
  toast.textContent = text;
  toast.hidden = false;
  clearTimeout(state.toast);
  state.toast = setTimeout(() => { toast.hidden = true; }, 5300);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Omni-Token": state.csrf },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Workspace request failed");
  return result;
}

function route() {
  const fragments = location.hash.slice(1).split("/");
  return { project: fragments[0] === "project" && fragments[1], session: fragments[2] === "session" && fragments[3] };
}

const projectURL = (id) => "#project/" + id;
const sessionURL = (project, session) => projectURL(project) + "/session/" + session;

function heading(title, description, controls, className = "") {
  return node("div", { class: "page-heading " + className },
    node("div", {}, node("h1", {}, title), description && node("p", {}, description)), controls);
}

function renderSidebar(current) {
  if (!state.workspace) return;
  const link = (label, icon, href, selected) => node("a",
    { href, class: "nav-link" + (selected ? " selected" : ""), "aria-current": selected ? "location" : null },
    node("span", { class: "nav-icon", "aria-hidden": "true" }, icon), label);
  $("#navigation").replaceChildren(link("Workspace", "⌂", "#workspace", !current.project),
    node("span", { class: "nav-title" }, "Projects"),
    ...state.workspace.projects.map((project) => link(project.name, "◇", projectURL(project.id), current.project === project.id)));
  const crumbs = [node("a", { href: "#workspace" }, "Workspace")];
  if (state.project) {
    crumbs.push(node("span", { class: "separator", "aria-hidden": "true" }, "/"));
    crumbs.push(current.session ? node("a", { href: projectURL(current.project) }, state.project.project.name)
      : node("span", { class: "breadcrumb-current", "aria-current": "page" }, state.project.project.name));
  }
  if (state.session) crumbs.push(node("span", { class: "separator", "aria-hidden": "true" }, "/"),
    node("span", { class: "breadcrumb-current", "aria-current": "page" }, "Session " + state.session.id.slice(0, 8)));
  $("#breadcrumbs").replaceChildren(...crumbs);
}

function metric(label, value, addition) {
  return node("div", { class: "metric" }, node("span", { class: "metric-label" }, label),
    node("strong", { class: "metric-value" }, value), addition && muted(addition));
}

function openDialog(title, content, wide = false) {
  const dialog = $("#dialog");
  dialog.className = wide ? "wide" : "";
  const close = node("button", { class: "close", type: "button", "aria-label": "Close dialog",
    onclick: () => dialog.close() }, "×");
  $("#dialog-content").replaceChildren(node("header", { class: "dialog-header" }, node("h2", { id: "dialog-title" }, title), close),
    node("div", { class: "dialog-body" }, content));
  if (!dialog.open) dialog.showModal();
  return dialog;
}

function field(label, element) {
  return node("label", {}, label, element);
}

function formDialog(title, fields, submitText, submit) {
  const error = node("p", { class: "error-text", role: "alert", hidden: true });
  const submitButton = node("button", { class: "button primary", type: "submit" }, submitText);
  const form = node("form", { class: "fields" }, fields, error, actions(submitButton,
    button("Cancel", () => $("#dialog").close(), "quiet")));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submitButton.disabled = true;
    error.hidden = true;
    try {
      await submit(Object.fromEntries(new FormData(form)));
      $("#dialog").close();
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      submitButton.disabled = false;
    }
  });
  openDialog(title, form);
  return form;
}

function newProject() {
  formDialog("New project", [
    field("Project name", node("input", { name: "name", placeholder: "Customer portal", required: true, maxlength: 100 })),
    field("Description", node("textarea", { name: "description", rows: 3, maxlength: 1000 })),
  ], "Create project", async (data) => {
    const project = await api("/api/projects", data);
    location.hash = projectURL(project.id);
  });
}

function settings() {
  if (!state.workspace) return;
  const provider = state.workspace.provider;
  const type = node("select", { name: "kind" },
    [["demo", "Built-in demo"], ["foundry", "Microsoft Foundry"], ["azure-gateway", "Azure AI Gateway for Foundry"],
      ["compatible", "Compatible Chat Completions"]].map(([id, name]) =>
      node("option", { value: id, selected: provider.kind === id }, name)));
  const endpoint = node("input", { name: "endpoint", type: "url", placeholder: "https://resource.services.ai.azure.com/openai/v1",
    value: provider.endpoint || "" });
  const model = node("input", { name: "model", placeholder: "Deployment or model", value: provider.model || "" });
  const credential = node("input", { name: "api_key", type: "password", autocomplete: "new-password" });
  const remember = node("input", { type: "checkbox", name: "remember_key", disabled: !provider.keyring_available });
  const credentialStatus = muted();
  const forget = node("input", { type: "checkbox", name: "forget_key" });
  const forgetLabel = node("label", { class: "checkbox" }, forget, " Forget saved credential");
  const clear = node("input", { type: "checkbox", name: "clear_key" });
  const clearLabel = node("label", { class: "checkbox" }, clear, " Clear session credential");
  const modelStatus = node("small", { role: "status", hidden: true });
  const choices = node("select", { onchange: () => { if (choices.value) model.value = choices.value; } });
  const choiceName = document.createTextNode("");
  const choicesLabel = node("label", { hidden: true }, choiceName, choices);
  const modelName = document.createTextNode("");
  const catalog = node("a", { href: "https://learn.microsoft.com/en-us/azure/foundry/concepts/foundry-models-overview",
    target: "_blank", rel: "noopener noreferrer", class: "button quiet" }, "Foundry model catalog");
  const loadModels = button("Load models", (event) => perform(async () => {
    if (!endpoint.reportValidity()) return;
    const kind = type.value;
    const url = endpoint.value;
    const foundry = kind === "foundry";
    modelStatus.textContent = foundry ? "Loading deployments…" : "Loading model IDs…";
    modelStatus.hidden = false;
    try {
      const { models } = await api("/api/provider/models", { kind, endpoint: url, api_key: credential.value });
      if (kind !== type.value || url !== endpoint.value) return;
      choices.replaceChildren(node("option", { value: "" }, foundry ? "Choose a deployment" : "Choose a listed ID"),
        ...models.map(({ id }) => node("option", { value: id }, id)));
      choices.value = models.some(({ id }) => id === model.value) ? model.value : "";
      choicesLabel.hidden = !models.length;
      const count = models.length;
      modelStatus.textContent = foundry ? (count === 1 ? "1 deployment found." : `${number.format(count)} deployments found.`)
        : (count ? `${number.format(count)} model IDs returned.` : "The endpoint returned no model IDs.");
    } catch (error) {
      if (kind !== type.value || url !== endpoint.value) return;
      choicesLabel.hidden = true;
      modelStatus.textContent = error.message;
    }
  }, event), "quiet");
  const connected = node("div", { class: "fields" }, field("API endpoint", endpoint),
    field("Provider credential", credential), node("label", { class: "checkbox" }, remember, " Remember in the system keyring"),
    credentialStatus, forgetLabel, clearLabel, actions(loadModels, catalog), modelStatus, choicesLabel,
    field(modelName, model));
  const configure = () => {
    const kind = type.value;
    const foundry = kind === "foundry";
    connected.hidden = kind === "demo";
    endpoint.required = model.required = kind !== "demo";
    endpoint.placeholder = foundry ? "https://resource.services.ai.azure.com/api/projects/project"
      : kind === "azure-gateway" ? "https://gateway.region.ai.gateway.azure.com/default/models/openai/v1/chat/completions"
        : "https://provider.example/v1";
    loadModels.textContent = foundry ? "Load deployments" : "Load models";
    choiceName.textContent = foundry ? "Project deployments" : "Model IDs from endpoint";
    modelName.textContent = foundry ? "Deployment name" : "Deployment or model alias";
    model.placeholder = kind === "azure-gateway" ? "Callable model alias from the gateway" : "";
    const url = endpoint.value.replace(/\/+$/, "");
    const normalized = type.value === "azure-gateway" ? url.replace(/\/chat\/completions$/, "") : url;
    const sameEndpoint = type.value === provider.kind && (normalized === provider.endpoint
      || (type.value === "foundry" && normalized + "/openai/v1" === provider.endpoint));
    const saved = sameEndpoint && provider.credential_saved;
    const loaded = sameEndpoint && provider.has_key;
    credential.placeholder = loaded ? "Leave blank to reuse the current credential" : "Enter a credential if required";
    forgetLabel.hidden = !saved;
    clearLabel.hidden = !loaded;
    if (!saved) forget.checked = false;
    if (!loaded) clear.checked = false;
    credentialStatus.textContent = saved ? "A credential is saved in your system keyring."
      : !provider.keyring_available ? "Install Linux Secret Service bindings to remember credentials."
        : "Credentials are held for this session unless you choose to remember them.";
    catalog.hidden = kind !== "azure-gateway";
    choicesLabel.hidden = modelStatus.hidden = true;
  };
  configure();
  type.addEventListener("change", configure);
  endpoint.addEventListener("input", configure);
  const rate = (name, label) => field(label, node("input",
    { name, type: "number", value: provider[name] || 0, min: 0, max: 10000, step: "any" }));
  formDialog("Provider settings", [field("Provider", type), connected,
    node("div", { class: "rates" }, rate("input_rate", "USD per 1M input tokens"),
      rate("output_rate", "USD per 1M output tokens"), rate("compute_rate", "USD per CPU hour")),
    button("Test saved provider", (event) => perform(async () => {
      const result = await api("/api/provider/test", {});
      notify("Model responded · " + result.model);
    }, event), "quiet"),
  ], "Save settings", async (data) => {
    for (const field of ["input_rate", "output_rate", "compute_rate"]) data[field] = Number(data[field]);
    for (const field of ["remember_key", "forget_key", "clear_key"]) data[field] = Boolean(data[field]);
    state.workspace.provider = await api("/api/provider", data);
    notify("Provider settings saved.");
    await refresh(true);
  });
}

function showExtensions() {
  const content = node("div", { class: "fields" }, muted("Loading extensions…"));
  openDialog("Extensions", content);
  const render = async (check = false) => {
    try {
      const config = check ? await api("/api/extensions/check", {}) : await api("/api/extensions");
      const skills = config.skills.map((skill) =>
        node("li", {}, node("strong", {}, skill.name), " · ", skill.roles.join(", ")));
      const servers = config.mcp.map((server) => node("li", {}, node("strong", {}, server.name), " · ", server.transport,
        server.ready === true ? " · Available" : "",
        list(Object.entries(server.tools).map(([tool, grants]) => tool + " · " + grants.join(", "))),
        server.error && node("p", { class: "error-text" }, server.error),
        server.unavailable_tools?.length > 0 && muted("Unavailable read-only tools · " + server.unavailable_tools.join(", "))));
      content.replaceChildren(code(config.config),
        node("h3", {}, "Skills"), skills.length ? node("ul", { class: "criteria" }, skills) : muted("No Skills configured."),
        node("h3", {}, "MCP servers"), servers.length ? node("ul", { class: "criteria" }, servers) : muted("No MCP servers configured."),
        ...(servers.length ? [button("Check approved tools", (event) => perform(() => render(true), event), "quiet")] : []));
    } catch (error) {
      content.replaceChildren(node("p", { class: "error-text", role: "alert" }, error.message));
    }
  };
  render();
}

function startSession(project, sample = false) {
  const create = async (data) => {
    const result = await api("/api/projects/" + project + "/sessions", data);
    location.hash = sessionURL(project, result.id);
  };
  if (sample) return perform(() => create({ sample: true }));
  if (state.workspace.provider.kind === "demo") {
    notify("Configure a model provider for your own request or run the built-in demo.");
    settings();
    return;
  }
  formDialog("New session", [
    field("Request for the Lead Agent", node("textarea", { rows: 7, name: "prompt", required: true, maxlength: 16000,
      placeholder: "Describe the software, constraints, and what should work when it’s ready." })),
  ], "Start session", create);
}

async function perform(operation, event) {
  const control = event?.currentTarget;
  if (control) control.disabled = true;
  try { await operation(); } catch (error) { notify(error.message); }
  finally { if (control) control.disabled = false; }
}

function workspacePage() {
  const { projects, memory } = state.workspace;
  const sum = (key) => projects.reduce((total, project) => total + project[key], 0);
  const projectCards = projects.map((project) => node("a", { class: "project-card", href: projectURL(project.id) },
    node("div", { class: "card-heading" }, node("strong", {}, project.name), badge(project.health, project.health ? null : "No sessions")),
    project.description && node("p", { class: "muted" }, limit(project.description)),
    node("div", { class: "row-meta" }, muted(number.format(project.session_count) + " sessions"), muted(money(project.model_cost + project.compute_cost)))));
  return [heading("Workspace", "Manage projects and follow each build from request through verification.",
    actions(button("+ New project", newProject, "primary"))),
    node("div", { class: "metric-grid" }, metric("Projects", number.format(projects.length)),
      metric("Sessions", number.format(sum("session_count"))), metric("Active agents", number.format(sum("active_agents")),
        number.format(sum("failed_agents")) + " failed attempts"), metric("Estimated spend", money(sum("model_cost") + sum("compute_cost")),
        number.format(sum("tokens")) + " provider tokens")),
    sectionHeading("Projects"),
    projects.length ? node("div", { class: "project-grid" }, projectCards)
      : node("div", { class: "empty" }, node("h3", {}, "Create your first project"),
        node("p", { class: "muted" }, "Give the Lead Agent a request, or try the built-in workshop task board."),
        button("Create project", newProject)),
    node("section", { class: "panel" }, sectionHeading("Hive Memory", button("Run dreaming", async (event) => perform(async () => {
      const result = await api("/api/memory/dream", {});
      notify("Dreaming finished. " + result.pruned + " pruned, " + result.synthesized + " patterns synthesized.");
      await refresh(true);
    }, event), "quiet")),
    node("div", { class: "hive" }, [[memory.nodes, "Knowledge nodes"], [memory.links, "Graph links"],
      [memory.applications, "Successful applications"]].map(([value, text]) =>
      node("div", {}, node("strong", {}, number.format(value)), muted(text)))),
    muted(memory.last_dream ? "Last consolidated " + timestamp(memory.last_dream.at) : "Dreaming runs between active sessions."))];
}

function projectPage() {
  const { project, sessions } = state.project;
  const vitals = state.workspace.projects.find((item) => item.id === project.id);
  return [heading(project.name, project.description, actions(button("Run demo", () => startSession(project.id, true), "quiet"),
    button("+ New session", () => startSession(project.id), "primary"))),
    state.engine && !state.engine.ready && node("p", { class: "notice" }, state.engine.detail),
    sectionHeading("Project vitals"),
    node("div", { class: "metric-grid" },
      metric("Sessions", number.format(vitals.session_count)),
      metric("Estimated spend", money(vitals.model_cost + vitals.compute_cost),
        money(vitals.model_cost) + " model · " + money(vitals.compute_cost) + " compute"),
      metric("Active agents", number.format(vitals.active_agents), number.format(vitals.failed_agents) + " failed attempts"),
      metric("Build health", labels[vitals.health] || "Idle", number.format(vitals.tokens) + " total tokens")),
    sectionHeading("Sessions"),
    sessions.length ? node("div", { class: "session-list" }, sessions.map((session) =>
      node("a", { class: "session-row", href: sessionURL(project.id, session.id) },
        node("div", {}, node("strong", {}, limit(session.prompt)), muted(timestamp(session.created) + " · " + (names[session.stage] || session.stage))),
        badge(session.state))))
      : node("div", { class: "empty" }, node("h3", {}, "No sessions yet"),
        node("p", { class: "muted" }, "Start a session to give the Lead Agent your requirements."))];
}

function summaryPanel(session) {
  const decisions = session.gates.flatMap((gate) => (gate.artifact?.decisions || []).map((item) => names[gate.stage] + " · " + item));
  return [facts([["Branch", code(session.branch)], ["Status", badge(session.state)],
    ["Estimated cost", money(session.cost)], ["Provider tokens", number.format(session.tokens)]]),
    session.error && node("p", { class: "error-text" }, session.error),
    node("h3", {}, "Original request"), node("p", {}, session.prompt),
    session.plan && [node("h3", {}, "Lead plan"), node("p", { class: "muted" }, session.plan.summary),
      list(stages.map(([id, name]) => name + " · " + session.plan.stages[id])),
      node("h3", {}, "Acceptance criteria"), list(session.plan.criteria.map((item) => item.id + " · " + item.description))],
    decisions.length > 0 && [node("h3", {}, "Decisions"), list(decisions)]];
}

function outputPanel(session) {
  if (session.state !== "completed") return node("div", { class: "empty" }, node("h3", {}, "Build pending"),
    node("p", { class: "muted" }, "The build and preview become available when all factory gates pass."));
  const isWeb = session.gates.find((gate) => gate.stage === "architecture")?.artifact?.mode === "web";
  const adjust = (start) => (event) => perform(async () => {
    await api("/api/sessions/" + session.id + "/preview", { start });
    await refresh(true);
  }, event);
  const preview = session.preview && new URL(session.preview);
  return [node("h3", {}, "Verified build"),
    node("div", { class: "actions output-actions" },
      preview && preview.protocol === "http:" && preview.hostname === "127.0.0.1" &&
        node("a", { class: "button primary", href: preview.href, target: "_blank", rel: "noopener noreferrer" }, "Open local preview ↗"),
      isWeb && button(session.preview ? "Stop preview" : "Start preview", adjust(!session.preview), "quiet"),
      session.bundle && anchor("Download build", "/api/sessions/" + session.id + "/build")),
    session.bundle && node("code", { class: "output-path" }, session.bundle),
    preview && node("code", { class: "output-path" }, preview.href),
    list(session.gates.find((gate) => gate.stage === "deploy")?.artifact?.instructions || [])];
}

async function refresh(force = false) {
  if (state.refreshing) return;
  state.refreshing = true;
  const path = location.hash;
  const current = route();
  try {
    const [workspace, project, session] = await Promise.all([
      api("/api/workspace"), current.project ? api("/api/projects/" + current.project + "/sessions") : null,
      current.session ? api("/api/sessions/" + current.session) : null,
    ]);
    if (path !== location.hash) return;
    state.csrf = workspace.csrf;
    if (state.route !== path) {
      state.route = path;
      state.tab = "map";
      state.scope = {};
      state.telemetry = null;
    }
    if (session && session.project_id !== current.project) {
      location.hash = sessionURL(session.project_id, session.id);
      return;
    }
    const signature = JSON.stringify([path, workspace, project, session, state.engine]);
    if (force || signature !== state.signature) {
      state.workspace = workspace;
      state.project = project;
      state.session = session;
      state.signature = signature;
      renderWorkspace();
    }
  } catch (error) {
    if (!state.workspace) $("#content").replaceChildren(node("p", { class: "error-text", role: "alert" }, error.message));
    else if (force) notify(error.message);
  } finally {
    state.refreshing = false;
    if (path !== location.hash) queueMicrotask(() => refresh(true));
  }
}

async function engineStatus() {
  try { state.engine = await api("/api/doctor"); }
  catch (error) { state.engine = { ready: false, detail: error.message }; }
  const engine = $("#engine");
  engine.className = "engine " + (state.engine.ready ? "ready" : "issue");
  engine.title = state.engine.detail || "All local container images are ready";
  engine.querySelector("span").textContent = state.engine.ready ? "Docker ready" : "Docker unavailable";
  refresh();
}

document.addEventListener("DOMContentLoaded", () => {
  $(".skip").addEventListener("click", (event) => {
    event.preventDefault();
    $("#content").focus();
  });
  $("#settings-button").addEventListener("click", settings);
  $("#extensions-button").addEventListener("click", showExtensions);
  window.addEventListener("hashchange", () => refresh(true));
  refresh(true);
  engineStatus();
  setInterval(() => { if (!document.hidden) refresh(); }, 1800);
  setInterval(() => { if (!document.hidden) engineStatus(); }, 20000);
});
