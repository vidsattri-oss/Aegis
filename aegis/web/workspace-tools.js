"use strict";

const extensionConfig = "/api/extensions/config";
const skillAPI = "/api/extensions/skill";

function showExtensions() {
  const content = node("div", { class: "fields" }, muted("Loading toolbox…"));
  openDialog("Skills & Tools", content, true);

  const render = async (check = false) => {
    const controls = actions(button("Edit configuration", editExtensionConfig, "primary"),
      button("Add Skill", () => editSkill(), "quiet"),
      button("Check approved tools", (event) => perform(() => render(true), event), "quiet"));
    try {
      const config = check ? await api("/api/extensions/check", {}) : await api("/api/extensions");
      const skills = config.skills.map((skill) => node("div", { class: "extension-row" },
        node("div", {}, node("strong", {}, skill.name),
          muted(skill.roles.map((role) => names[role]).join(", ") + " · " + (skill.resources.length + 1) + " files")),
        button("Edit files", () => editSkill(skill.name), "quiet")));
      const servers = config.mcp.map((server) => node("section", { class: "extension-item" },
        node("div", { class: "extension-row" }, node("div", {}, node("strong", {}, server.name),
          muted(server.isolation || (server.oauth ? "Browser OAuth" : server.transport))),
          server.oauth && badge(server.credentials_ready ? "ready" : "pending", server.credentials_ready ? "Connected" : "Sign-in needed")),
        list(Object.entries(server.tools).map(([tool, grants]) => tool + " · " + grants.map((role) => names[role]).join(", "))),
        server.oauth && actions(server.credentials_ready
          ? button("Disconnect", (event) => perform(async () => {
            await api("/api/extensions/oauth/disconnect", { name: server.name });
            await render();
          }, event), "quiet")
          : button("Connect in browser", (event) => perform(async () => {
            const result = await api("/api/extensions/oauth/start", { name: server.name });
            const url = new URL(result.url);
            if (url.protocol !== "https:") throw new Error("OAuth sign-in requires HTTPS.");
            location.assign(url.href);
          }, event), "primary")),
        server.ready === true && muted("Approved tools are available."),
        server.error && node("p", { class: "error-text" }, server.error),
        server.unavailable_tools?.length > 0 && muted("Unavailable read-only tools · " + server.unavailable_tools.join(", "))));
      content.replaceChildren(controls, node("h3", {}, "MCP servers"),
        ...(servers.length ? servers : [muted("No MCP servers configured.")]),
        node("h3", {}, "Skills"), ...(skills.length ? skills : [muted("Upload a SKILL.md with any text references it needs.")]));
    } catch (error) {
      content.replaceChildren(controls, node("p", { class: "error-text", role: "alert" }, error.message));
    }
  };
  render();
}

function editExtensionConfig(event) {
  return perform(async () => {
    const current = await api(extensionConfig);
    const editor = node("textarea", { class: "mono", rows: 13, readonly: !current.writable }, current.text);
    const example = `[[mcp]]\nname = "my-server"\nurl = "https://tools.example/mcp"\noauth = {}\n\n[mcp.tools]\nsearch = ["research"]\n`;
    const form = node("form", { class: "fields" }, field("extensions.toml", editor), muted(current.config),
      field("OAuth callback URL", node("input", { value: current.callback, readonly: true })),
      muted("Use oauth = {} for dynamic registration or set client_id and scopes in the OAuth entry."),
      actions(current.writable && node("button", { class: "button primary", type: "submit" }, "Save configuration"),
        current.writable && button("Insert OAuth example", () => {
          editor.value = editor.value.trimEnd() + (editor.value.trim() ? "\n\n" : "") + example;
          editor.focus();
        }, "quiet"), button("Back to Skills & Tools", showExtensions, "quiet")));
    if (!current.writable) form.append(muted("This workspace loads its configuration from AEGIS_EXTENSIONS."));
    form.addEventListener("submit", (change) => {
      change.preventDefault();
      perform(async () => {
        await api(extensionConfig, { text: editor.value, base: current.text });
        notify("Extension configuration saved.");
        showExtensions();
      }, { currentTarget: change.submitter });
    });
    openDialog("Edit extension configuration", form, true);
  }, event);
}

function editSkill(original = null) {
  return perform(async () => {
    const [config, saved] = await Promise.all([api(extensionConfig), original ? api(skillAPI + "?" + new URLSearchParams({ name: original })) : null]);
    const files = new Map(Object.entries(saved?.files || {}));
    const upload = node("input", { type: "file", multiple: true });
    const folder = node("input", { type: "file", webkitdirectory: true, multiple: true });
    const picker = node("select", { "aria-label": "Skill file" });
    const editor = node("textarea", { class: "mono", rows: 12 });
    const renderFiles = (preferred) => {
      picker.replaceChildren(...Array.from(files.keys()).sort((a, b) => a === "SKILL.md" ? -1 : b === "SKILL.md" ? 1 : a.localeCompare(b))
        .map((path) => node("option", { value: path }, path)));
      if (files.has(preferred)) picker.value = preferred;
      editor.disabled = !picker.value;
      editor.value = files.get(picker.value) || "";
    };
    const addFiles = async (input) => {
      const next = new Map(files);
      for (const file of input.files) {
        const path = file.webkitRelativePath ? file.webkitRelativePath.split("/").slice(1).join("/") : file.name;
        if (file.size > (path === "SKILL.md" ? 22000 : 9000)) throw new Error(path + " exceeds its size limit.");
        next.set(path, new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()));
      }
      if (next.size > 9) throw new Error("A Skill can contain SKILL.md and up to eight reference files.");
      next.forEach((text, path) => files.set(path, text));
      renderFiles(files.has("SKILL.md") ? "SKILL.md" : picker.value);
    };
    for (const input of [upload, folder]) input.addEventListener("change", () => perform(async () => {
      try { await addFiles(input); } finally { input.value = ""; }
    }));
    picker.addEventListener("change", () => renderFiles(picker.value));
    editor.addEventListener("input", () => files.set(picker.value, editor.value));
    renderFiles("SKILL.md");

    const grants = config.roles.map((role) => {
      const input = node("input", { type: "checkbox", value: role, checked: saved?.roles.includes(role) });
      return { input, field: node("label", { class: "checkbox" }, input, names[role] || role) };
    });
    const form = node("form", { class: "fields" },
      field("Upload text files", upload), field("Upload a Skill folder", folder),
      field("File to edit", picker), field("File contents", editor),
      node("fieldset", { class: "skill-grants" }, node("legend", {}, "Agent roles"), grants.map(({ field }) => field)),
      actions(node("button", { class: "button primary", type: "submit" }, "Save Skill"),
        button("Back to Skills & Tools", showExtensions, "quiet")));
    form.addEventListener("submit", (change) => {
      change.preventDefault();
      perform(async () => {
        await api(skillAPI, { original, base: saved, files: Object.fromEntries(files),
          roles: grants.filter(({ input }) => input.checked).map(({ input }) => input.value) });
        notify("Skill saved for new runs.");
        showExtensions();
      }, { currentTarget: change.submitter });
    });
    openDialog(original ? "Edit Skill · " + original : "Add Skill", form, true);
  });
}

function showDreaming() {
  const history = node("div", { class: "fields" });
  const records = node("div", { class: "fields" });
  const playbooks = node("div", { class: "fields" });
  const search = node("input", { type: "search", maxlength: 200, placeholder: "Search memory" });
  let historyPage = 0;
  let recordPage = 0;
  let query = "";
  const loadHistory = async () => {
    const { history: runs, more } = await api("/api/memory/history?page=" + historyPage);
    history.replaceChildren(...runs.map((run) => node("div", { class: "extension-row" },
      node("strong", {}, timestamp(run.at)), muted(`${run.synthesized} patterns · ${run.pruned} pruned`))));
    if (!runs.length) history.append(muted("No consolidation runs yet."));
    history.append(actions(historyPage > 0 && button("Previous runs", (event) => perform(async () => { historyPage--; await loadHistory(); }, event)),
      more && button("Older runs", (event) => perform(async () => { historyPage++; await loadHistory(); }, event))));
  };
  const loadRecords = async () => {
    const result = await api("/api/memory/records?" + new URLSearchParams({ q: query, page: recordPage }));
    records.replaceChildren(...result.records.map((item) => node("details", { class: "memory-record" },
      node("summary", {}, item.label, " · ", muted(item.kind)),
      muted((item.projects || "Workspace") + " · " + item.uses + " applied"), node("p", {}, item.body),
      item.links.length > 0 && list(item.links.map((link) => link.kind.replaceAll("_", " ") + " · " + link.label)))),
    !result.records.length ? muted("No matching memory records.") : node("div", { class: "actions" },
      recordPage > 0 && button("Previous records", (event) => perform(async () => { recordPage--; await loadRecords(); }, event)),
      result.more && button("More records", (event) => perform(async () => { recordPage++; await loadRecords(); }, event))));
  };
  const loadPlaybooks = async () => {
    if (state.workspace.memory.agent_playbooks === undefined) return;
    const project = state.project?.project.id;
    const result = await api("/api/memory/playbooks" + (project ? "?project=" + project : ""));
    const entries = result.playbooks.map((item) => node("details", { class: "memory-record" },
      node("summary", {}, (names[item.role] || item.role) + " · " + item.name, " · ", muted("Revision " + item.revision)),
      list(item.lessons.map((lesson) => node("div", {}, lesson.lesson, node("p", { class: "muted" }, lesson.evidence))))));
    playbooks.replaceChildren(...entries.length ? entries :
      [muted("Playbooks appear after the agents has approved work and consolidated its notes.")]);
  };
  const searchForm = node("form", { class: "fields" }, field("Search session notes", search),
    actions(node("button", { type: "submit", class: "button quiet" }, "Search")));
  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    query = search.value.trim();
    recordPage = 0;
    perform(loadRecords, { currentTarget: event.submitter });
  });
  openDialog("Memory", node("div", { class: "fields" },
    sectionHeading("Consolidation history", button("Consolidate notes", (event) => perform(async () => {
      await api("/api/memory/dream", {});
      historyPage = recordPage = 0;
      await Promise.all([loadHistory(), loadRecords(), loadPlaybooks(), refresh(true)]);
    }, event), "primary")),
    muted("Aegis consolidates decisions and fixes between sessions."), history,
    node("h3", {}, "Agent playbooks"), playbooks,
    node("h3", {}, "Session notes"), searchForm, records), true);
  perform(() => Promise.all([loadHistory(), loadRecords(), loadPlaybooks()]));
}
