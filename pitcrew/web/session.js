"use strict";

const stations = [["lead", "Run plan"], ...stages];
const roles = (session) => ["lead", ...stages.map(([role]) => role), "review",
  ...(session.agents.some((agent) => agent.role === "specialist") ? ["specialist"] : [])];
const stageNames = Object.fromEntries(stations);
const attemptLabel = (count) => count + (count === 1 ? " attempt" : " attempts");
const portrait = (role) => node("span", { class: "portrait", "data-role": role, "aria-hidden": "true" });
const agentName = (agent) => agent.profile.persona_name || names[agent.role];
const scopeKey = ({ role, stage, attempt }) => [role || "", stage || "", attempt || ""].join("/");
const eventNames = {
  container: "Container", tool_call: "Tool call", tool_result: "Tool result", execution_log: "Execution log",
  exception: "Exception", gate_passed: "Gate passed", gate_failed: "Gate failed", review_committed: "Review committed",
  capacity_wait: "Waiting for Docker capacity", execution_plan: "Docker task sizes",
  released: "Released", preview_stopped: "Preview stopped", cancelled: "Cancelled", interrupted: "Interrupted",
  delegated: "Specialist assigned", github_base: "GitHub base", github_published: "GitHub branch published",
};
const toolNames = {
  "model.chat": "Model", "docker.execute": "Docker", "research.fetch": "Research fetch",
  "osv.querybatch": "Dependency scan", "preview.start": "Preview", "lead.chat": "Team Radio",
};

function scopeTitle({ role, stage, attempt }) {
  const agent = state.session.agents.find((item) => item.id === attempt);
  return agent ? agentName(agent) + " · attempt " + agent.number + " · " + stageNames[agent.stage]
    : [names[role], stageNames[stage]].filter(Boolean).join(" · ") || "All lap telemetry";
}

function matchingAgents(session, scope) {
  return session.agents.filter((agent) => (!scope.role || agent.role === scope.role)
    && (!scope.stage || agent.stage === scope.stage) && (!scope.attempt || agent.id === scope.attempt));
}

function scopeButton(label, scope, attributes, ...content) {
  return node("button", { type: "button", ...attributes, "aria-label": label, "aria-pressed": "false",
    "data-filter": scopeKey(scope),
    onclick: (event) => selectScope(scope, Boolean(event.currentTarget.closest(".map-stack"))) }, content);
}

function updateSelections() {
  const selected = scopeKey(state.scope);
  const agents = matchingAgents(state.session, state.scope);
  const filtered = Object.values(state.scope).some(Boolean);
  for (const control of $("#session-panel").querySelectorAll("[data-filter]")) {
    const exact = control.dataset.filter === selected;
    const [role, stage, attempt] = control.dataset.filter.split("/");
    const related = filtered && !exact && !attempt && Boolean(role || stage) && agents.some((agent) =>
      (!role || agent.role === role) && (!stage || agent.stage === stage));
    control.setAttribute("aria-pressed", String(exact));
    control.classList.toggle("is-related", related);
  }
}

function selectScope(scope, fromMap = false) {
  if (scopeKey(state.scope) !== scopeKey(scope)) {
    state.scope = scope;
    state.telemetry = null;
    updateSelections();
    renderLogPanel();
    loadTelemetry();
  }
  const heading = $("#log-heading");
  if (fromMap && heading && heading.getBoundingClientRect().top > window.innerHeight - 110) jumpToLogs();
}

function jumpToLogs() {
  const heading = $("#log-heading");
  if (heading) {
    heading.scrollIntoView({ block: "start" });
    heading.focus({ preventScroll: true });
  }
}

function jumpToMap() {
  const target = $(".map-stack [aria-pressed=true]") || $("#team-heading");
  if (target) {
    target.scrollIntoView({ block: "center" });
    target.focus({ preventScroll: true });
  }
}

function orgChart(session) {
  const card = (role) => {
    const attempts = matchingAgents(session, { role });
    const running = attempts.filter((agent) => agent.status === "Running").length;
    const retries = attempts.filter((agent) => agent.status === "Retried" || agent.status === "Failed").length;
    const status = running ? "Running" : attempts.at(-1)?.status;
    const title = names[role];
    const term = { lead: "race-engineer", review: "scrutineers" }[role];
    return scopeButton(title + ", " + attemptLabel(attempts.length), { role }, {
      ...(term && termHint(term)), class: "org-card", "data-role": role, "data-status": status || "Pending",
    }, portrait(role), node("span", { class: "org-card-text" }, node("strong", {}, title),
      muted(attemptLabel(attempts.length) + (retries > 0 ? " · " + retries + " failed" : ""))),
    node("span", { class: "org-indicator", "aria-hidden": "true", title: status || "Pending" }));
  };
  return node("section", { class: "map-section org-section", "aria-labelledby": "team-heading" },
    node("div", { class: "map-heading" }, node("div", { class: "term-label" },
      node("h2", { id: "team-heading", tabindex: "-1" }, "Pit Crew"), termHelp("crew")),
      muted(number.format(session.agents.filter((agent) => agent.status === "Running").length) + " running")),
    node("div", { class: "org-chart" },
      node("div", { class: "org-root" }, card("lead")),
      node("ul", { class: "org-children", role: "list", "aria-label": "Crew reporting to the race engineer" },
        roles(session).slice(1).map((role) => node("li", {}, card(role))))));
}

function factoryMap(session) {
  const gates = new Map(session.gates.map((gate) => [gate.stage, gate]));
  const counts = new Map();
  for (const agent of session.agents) {
    const key = scopeKey({ role: agent.role, stage: agent.stage });
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const stops = stations.map(([stage, title]) => {
    const status = gates.get(stage)?.state || "pending";
    const touches = matchingAgents(session, { stage }).length;
    return node("th", { scope: "col", class: "rail-stop", "data-state": status },
      scopeButton(title + ", " + (labels[status] || status) + ", " + attemptLabel(touches), { stage },
        { class: "rail-station", ...(stage === "deploy" && termHint("track-release")) },
        node("span", { class: "rail-marker", "aria-hidden": "true" }),
        node("strong", {}, title), node("span", { class: "rail-state" }, labels[status] || status),
        node("small", { class: "rail-total" }, attemptLabel(touches))));
  });
  const lines = roles(session).map((role) => {
    const touches = stations.map(([stage]) => counts.get(scopeKey({ role, stage })) || 0);
    const first = touches.findIndex(Boolean);
    const last = touches.findLastIndex(Boolean);
    return node("tr", { "data-role": role },
      node("th", { scope: "row", class: "rail-role-head" }, scopeButton(names[role], { role }, { class: "rail-role" },
        node("span", { class: "line-dot", "aria-hidden": "true" }), names[role])),
      stations.map(([stage, title], index) => {
        const count = touches[index];
        return node("td", { class: "rail-cell" + (first >= 0 && first < index && index <= last ? " has-left" : "")
          + (first >= 0 && first <= index && index < last ? " has-right" : "") + (index < first || index > last ? " off-route" : "") },
        count > 0 ? scopeButton(names[role] + " · " + title + " · " + attemptLabel(count), { role, stage },
          { class: "rail-count", title: attemptLabel(count) }, count)
          : node("span", { class: "rail-empty", "aria-label": "0 attempts" }, "–"));
      }));
  });
  return node("section", { class: "map-section factory-section", "aria-labelledby": "factory-heading" },
    node("div", { class: "map-heading" }, node("div", { class: "term-label" },
      node("h2", { id: "factory-heading" }, "Pit Lane"), termHelp("pit-lane")),
      muted("Attempts include retries")),
    node("div", { class: "rail-scroll", tabindex: "0", "aria-label": "Scrollable pit lane" },
      node("table", { class: "rail-map", "aria-label": "Crew attempts by stage" },
        node("thead", {}, node("tr", {}, node("th", { scope: "col", class: "rail-role-head rail-corner" }, "Crew"), stops)),
        node("tbody", {}, lines))));
}

function logInspector() {
  return node("aside", { id: "log-inspector", class: "log-inspector", "aria-labelledby": "log-heading" },
    node("div", { class: "map-heading" }, node("div", { class: "term-label" },
      node("h2", { id: "log-heading", tabindex: "-1" }, "Telemetry"), termHelp("telemetry")),
      actions(button("Pit Lane ↑", jumpToMap, "quiet log-back"),
        button("Reset filter", () => selectScope({}), "quiet log-reset"))),
    node("div", { id: "log-scope", class: "log-scope", role: "status" }),
    node("div", { id: "log-context" }),
    node("div", { id: "log-feed", class: "log-feed", tabindex: "0", role: "region", "aria-label": "Filtered telemetry" }));
}

function mapPanel(session) {
  return node("div", { class: "operations" },
    node("div", { class: "map-stack" },
      node("div", { class: "map-tools" }, node("span", { class: "session-signal", "data-state": session.state },
        node("i", { "aria-hidden": "true" }), ["queued", "running"].includes(session.state) ? "Updating live" : "Lap activity"),
        button("View telemetry ↓", jumpToLogs, "quiet log-jump")),
      orgChart(session), factoryMap(session)), logInspector());
}

function disclosure(label, body) {
  return node("details", { class: "log-disclosure", "data-disclosure": label }, node("summary", {}, label), body);
}

function logContext(session, telemetry) {
  const { role, stage, attempt } = state.scope;
  const gate = stage && session.gates.find((item) => item.stage === stage);
  const agents = matchingAgents(session, state.scope);
  const agent = attempt ? agents.find((item) => item.id === attempt) : null;
  const details = telemetry?.agent;
  const attempts = (!attempt && (role || stage) ? agents : []).map((item) => scopeButton(
    agentName(item) + " · " + stageNames[item.stage] + " · attempt " + item.number,
    { role: item.role, stage: item.stage, attempt: item.id }, { class: "attempt-card", "data-role": item.role },
    portrait(item.role), node("span", { class: "org-card-text" }, node("strong", {}, agentName(item)),
      muted(stageNames[item.stage] + " · attempt " + item.number)), badge(item.status)));
  return [
    gate && disclosure("Stage handoff and QA", node("div", { class: "disclosure-body" },
      facts([["Gate", badge(gate.state)], ["Cycle time", duration(gate.started, gate.ended)],
        ["Git commit", gate.commit_sha ? code(gate.commit_sha.slice(0, 12)) : "—"]]),
      gate.artifact && node("pre", {}, val(gate.artifact)),
      gate.details && node("pre", {}, val(gate.details)))),
    agent && node("div", { class: "agent-context" }, portrait(agent.role), node("div", {},
      node("strong", {}, agentName(agent)), muted(badge(agent.status), " ", agent.profile.cpu + " CPU / " + agent.profile.ram + " MiB"))),
    details && disclosure("Persona, input and resource details", node("div", { class: "disclosure-body" },
      facts([["Agent ID", code(details.id)], ["Spawned", timestamp(details.started)], ["Terminated", timestamp(details.ended)],
        ["Provider tokens", number.format(details.input_tokens + details.output_tokens)], ["Image", details.profile.image],
        ["CPU / Memory / Processes", `${details.profile.cpu} / ${details.profile.ram} MiB / ${details.profile.pids}`],
        ["Role tools", details.profile.tools?.join(", ") || "—"],
        ["MCP tools", details.profile.mcp_tools?.join(", ") || "—"],
        ["Skills", details.profile.skills?.join(", ") || "—"]]),
      node("h3", {}, "System persona"), node("pre", {}, details.persona),
      node("h3", {}, "Input prompt"), node("pre", {}, val(details.input)),
      details.trace && [node("h3", {}, "Exception trace"), node("pre", {}, val(details.trace))])),
    attempts.length > 0 && disclosure(attemptLabel(attempts.length),
      node("div", { class: "attempt-list" }, attempts)),
  ].filter(Boolean);
}

function eventCard(session, event, telemetry) {
  const title = [eventNames[event.kind] || event.kind, toolNames[event.tool] || event.tool].filter(Boolean).join(" · ");
  const details = node("details", { class: "log-event", "data-event": event.id },
    node("summary", {}, node("span", { class: "event-title" }, node("strong", {}, title),
      node("time", { datetime: event.time, title: timestamp(event.time) }, new Date(event.time).toLocaleTimeString())),
    node("span", { class: "event-context" }, [names[event.role], stageNames[event.stage], event.model].filter(Boolean).join(" · ")),
    event.excerpt && node("span", { class: "event-excerpt" }, event.excerpt)));
  const body = node("div", { class: "event-detail" });
  details.append(body);
  const show = (entry) => body.replaceChildren(node("pre", {}, event.kind === "execution_log"
    ? entry.payload.output || val(entry.payload) : val(entry.payload)));
  if (telemetry.details.has(event.id)) show(telemetry.details.get(event.id));
  details.addEventListener("toggle", async () => {
    if (!details.open || body.childElementCount > 0) return;
    const loading = node("p", { class: "muted" }, "Loading event…");
    body.append(loading);
    try {
      const entry = await api("/api/sessions/" + session.id + "/telemetry?event=" + event.id);
      telemetry.details.set(event.id, entry);
      show(entry);
    } catch (error) {
      loading.textContent = error.message;
      details.addEventListener("toggle", () => { if (!details.open) loading.remove(); }, { once: true });
    }
  });
  return details;
}

function renderLogPanel() {
  const target = $("#log-inspector");
  if (!target || state.tab !== "map") return;
  const telemetry = state.telemetry;
  const label = scopeTitle(state.scope);
  const scopeName = $("#log-scope");
  if (scopeName.textContent !== label) scopeName.textContent = label;
  target.querySelector(".log-reset").hidden = !Object.values(state.scope).some(Boolean);
  const context = $("#log-context");
  const opened = new Set([...context.querySelectorAll("[data-disclosure][open]")].map((item) => item.dataset.disclosure));
  const contextFocus = document.activeElement?.dataset.filter;
  context.replaceChildren(...logContext(state.session, telemetry));
  for (const item of context.querySelectorAll("[data-disclosure]")) item.open = opened.has(item.dataset.disclosure);
  if (contextFocus) context.querySelector('[data-filter="' + contextFocus + '"]')?.focus({ preventScroll: true });
  updateSelections();
  renderLogFeed(telemetry);
}

function renderLogFeed(telemetry) {
  const feed = $("#log-feed");
  const session = state.session;
  const filter = telemetry?.key || scopeKey(state.scope);
  const sameScope = feed.dataset.scope === filter;
  const existing = sameScope ? new Map([...feed.querySelectorAll(".log-event")].map((item) => [item.dataset.event, item])) : new Map();
  const focusedEvent = feed.contains(document.activeElement) ? document.activeElement.closest(".log-event") : null;
  const focusedMore = document.activeElement?.classList.contains("older-events");
  const position = sameScope ? feed.scrollTop : 0;
  feed.dataset.scope = filter;
  if (!telemetry || telemetry.loading && !telemetry.events.length) {
    feed.replaceChildren(node("p", { class: "muted log-empty" }, "Loading telemetry…"));
  } else if (telemetry.error) {
    feed.replaceChildren(node("p", { class: "error-text log-empty" }, telemetry.error),
      button("Retry loading telemetry", () => loadTelemetry(), "quiet"));
  } else if (!telemetry.events.length) {
    feed.replaceChildren(node("p", { class: "muted log-empty" }, "No events for this selection yet."));
  } else {
    feed.replaceChildren(...telemetry.events.map((item) => existing.get(String(item.id)) || eventCard(session, item, telemetry)),
      ...(telemetry.before ? [button("Load older events", () => loadTelemetry(true), "quiet older-events")] : []));
  }
  feed.scrollTop = position;
  focusedEvent?.querySelector("summary").focus({ preventScroll: true });
  if (focusedMore) feed.querySelector(".older-events")?.focus({ preventScroll: true });
}

async function loadTelemetry(older = false) {
  const session = state.session;
  if (!session || state.tab !== "map" || !$("#log-feed")) return;
  const key = scopeKey(state.scope);
  const telemetry = state.telemetry ||= { key, events: [], details: new Map(), before: null };
  const selected = session.agents.find((agent) => agent.id === state.scope.attempt);
  const agentVersion = selected ? JSON.stringify(selected) : null;
  if (telemetry.loading || (older ? !telemetry.before : !telemetry.error && telemetry.revision === session.event_revision
      && telemetry.agentVersion === agentVersion)) return;
  telemetry.loading = true;
  const query = new URLSearchParams(state.scope);
  if (older) query.set("before", telemetry.before);
  const base = "/api/sessions/" + session.id;
  try {
    const [page, metadata] = await Promise.all([
      api(base + "/telemetry?" + query),
      selected && agentVersion !== telemetry.agentVersion ? api(base + "/agents/" + selected.id + "?metadata=1") : null,
    ]);
    if (telemetry !== state.telemetry || session.id !== state.session?.id) return;
    if (metadata) telemetry.agent = metadata.agent;
    telemetry.agentVersion = agentVersion;
    const overlap = page.events.some((item) => telemetry.events.some((loaded) => loaded.id === item.id));
    const keepHistory = older || overlap;
    telemetry.events = [...new Map([...(keepHistory ? telemetry.events : []), ...page.events]
      .map((item) => [item.id, item])).values()].sort((a, b) => b.id - a.id);
    if (older || !overlap) telemetry.before = page.before;
    if (!older) telemetry.revision = session.event_revision;
    telemetry.error = null;
  } catch (error) {
    if (telemetry === state.telemetry) telemetry.error = error.message;
  } finally {
    telemetry.loading = false;
    if (telemetry === state.telemetry) renderLogPanel();
  }
}

function sessionPage(session) {
  const panels = { map: mapPanel, summary: summaryPanel, output: outputPanel };
  const tabTerms = { map: "pit-lane", summary: "debrief", output: "parc-ferme" };
  const changeTab = (id, focus = false) => {
    state.tab = id;
    for (const tab of $("#session-tabs").querySelectorAll("[role=tab]")) {
      const selected = tab.id === "tab-" + id;
      tab.setAttribute("aria-selected", String(selected));
      tab.classList.toggle("active", selected);
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    }
    const panel = $("#session-panel");
    panel.classList.toggle("map-panel", id === "map");
    panel.setAttribute("aria-labelledby", "tab-" + id);
    panel.replaceChildren(...[panels[id](state.session)].flat(Infinity).filter(Boolean));
    if (id === "map") { renderLogPanel(); loadTelemetry(); }
  };
  const tabs = node("div", { class: "tabs", role: "tablist", id: "session-tabs", "aria-label": "Lap views",
    onkeydown: (event) => {
      const current = tabNames.findIndex(([id]) => id === state.tab);
      const last = tabNames.length - 1;
      const target = { ArrowLeft: current === 0 ? last : current - 1,
        ArrowRight: current === last ? 0 : current + 1, Home: 0, End: last }[event.key];
      if (target !== undefined) { event.preventDefault(); changeTab(tabNames[target][0], true); }
    } }, tabNames.map(([id, title]) => node("button", { ...termHint(tabTerms[id]),
      type: "button", class: "tab" + (state.tab === id ? " active" : ""),
      id: "tab-" + id, role: "tab", "aria-controls": "session-panel", "aria-selected": String(state.tab === id),
      tabindex: state.tab === id ? 0 : -1, onclick: () => changeTab(id) }, title)));
  const control = (path) => (event) => perform(async () => {
    await api("/api/sessions/" + session.id + "/" + path, {});
    await refresh(true);
  }, event);
  const active = ["queued", "running"].includes(session.state);
  return [heading(limit(session.plan?.summary || session.prompt, 95), "Lap " + session.id.slice(0, 8),
    actions(badge(session.state), active ? button("Cancel", control("cancel"), "quiet danger")
      : ["failed", "cancelled"].includes(session.state) && button("Retry", control("retry"), "quiet"),
      !active && button("New lap", () => startSession(session.project_id), "quiet")), "session-heading"),
    node("section", { class: "inspection", "aria-label": "Lap views" }, tabs,
      node("div", { id: "session-panel", class: "tab-content" + (state.tab === "map" ? " map-panel" : ""),
        role: "tabpanel", "aria-labelledby": "tab-" + state.tab }, panels[state.tab](session)))];
}

function syncLeadChat(session) {
  const root = $("#lead-chat-root");
  if (!session) { root.replaceChildren(); delete root.dataset.session; return; }
  if (root.dataset.session !== session.id) {
    root.dataset.session = session.id;
    root.dataset.lastRead = session.messages.at(-1)?.id || 0;
    delete root.dataset.lastRendered;
    const launcher = node("button", { ...termHint("team-radio"), type: "button", class: "chat-launcher", "aria-expanded": "false",
      "aria-controls": "lead-drawer" }, portrait("lead"), "Team Radio", node("span", { class: "chat-unread", hidden: true }));
    const messages = node("div", { class: "messages", role: "log", "aria-label": "Team radio conversation" });
    const input = node("input", { name: "message", "aria-label": "Message the race engineer",
      placeholder: "Ask the race engineer…", required: true, maxlength: 4000 });
    const send = node("button", { type: "submit", class: "button primary" }, "Send");
    const draft = node("form", { class: "chat-draft", onsubmit: async (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message || send.disabled) return;
      send.disabled = true;
      try {
        await api("/api/sessions/" + session.id + "/messages", { text: message });
        input.value = "";
        await refresh(true);
      } catch (error) { notify(error.message); }
      finally { send.disabled = false; }
    } }, input, send);
    const drawer = node("section", { id: "lead-drawer", class: "lead-drawer", "aria-labelledby": "lead-title", hidden: true },
      node("header", { class: "chat-header" }, portrait("lead"), node("h2", { id: "lead-title" }, "Team Radio"),
        node("button", { type: "button", class: "close", "aria-label": "Minimize team radio",
          onclick: () => toggle(false) }, "×")), messages, draft);
    const toggle = (open) => {
      drawer.hidden = !open;
      launcher.hidden = open;
      launcher.setAttribute("aria-expanded", String(open));
      if (open) {
        root.dataset.lastRead = state.session.messages.at(-1)?.id || 0;
        launcher.querySelector(".chat-unread").hidden = true;
        messages.scrollTop = messages.scrollHeight;
        input.focus({ preventScroll: true });
      } else launcher.focus({ preventScroll: true });
    };
    drawer.addEventListener("keydown", (event) => { if (event.key === "Escape") toggle(false); });
    launcher.addEventListener("click", () => toggle(true));
    root.replaceChildren(launcher, drawer);
  }
  const last = session.messages.at(-1)?.id || 0;
  if (String(last) === root.dataset.lastRendered) return;
  const messages = root.querySelector(".messages");
  const unread = root.querySelector(".chat-unread");
  const isOpen = !$("#lead-drawer").hidden;
  const follow = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 22;
  const position = messages.scrollTop;
  const appended = session.messages.filter((message) => message.id > Number(root.dataset.lastRendered || 0));
  messages.append(...appended.map((message) => node("div", { class: "message " + message.speaker },
    muted((message.speaker === "lead" ? "Race Engineer" : "You") + " · " + timestamp(message.created)), node("p", {}, message.text))));
  messages.scrollTop = follow ? messages.scrollHeight : position;
  root.dataset.lastRendered = last;
  if (isOpen) root.dataset.lastRead = last;
  unread.hidden = !session.messages.some((message) => message.speaker === "lead" && message.id > Number(root.dataset.lastRead));
  unread.setAttribute("aria-label", "Unread message");
}

function renderWorkspace() {
  const main = $("#content");
  const active = document.activeElement;
  const railScroll = $(".rail-scroll")?.scrollLeft || 0;
  const oldLogs = $("#log-feed");
  const oldContext = $("#log-context");
  renderSidebar(route());
  main.replaceChildren(...(state.session ? sessionPage(state.session) : state.project ? projectPage() : workspacePage()).filter(Boolean));
  syncLeadChat(state.session);
  if (state.session && state.tab === "map") {
    if (oldLogs) $("#log-feed").replaceWith(oldLogs);
    if (oldContext) $("#log-context").replaceWith(oldContext);
    $(".rail-scroll").scrollLeft = railScroll;
    renderLogPanel();
    loadTelemetry();
  }
  if (active?.id?.startsWith("tab-") || active?.id === "log-heading") document.getElementById(active.id)?.focus({ preventScroll: true });
  if (active?.classList.contains("term-help") && !active.isConnected) {
    main.querySelector('.term-help[data-f1-term="' + active.dataset.f1Term + '"]')?.focus({ preventScroll: true });
  }
  if (active?.dataset.filter !== undefined) {
    const candidates = [...main.querySelectorAll('[data-filter="' + active.dataset.filter + '"]')];
    candidates.find((item) => item.className === active.className)?.focus({ preventScroll: true });
  }
}
