const chatEl = document.getElementById("chat");
const emptyState = document.getElementById("emptyState");
const appEl = document.getElementById("app");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("sendBtn");
const uploadBtn = document.getElementById("uploadBtn");
const fileInput = document.getElementById("fileInput");
const statusPill = document.getElementById("statusPill");
const statusText = document.getElementById("statusText");
const skillList = document.getElementById("skillList");
const toolList = document.getElementById("toolList");
const modelLabel = document.getElementById("modelLabel");
const maxRoundsValue = document.getElementById("maxRoundsValue");
const sessionList = document.getElementById("sessionList");
const newSessionBtn = document.getElementById("newSessionBtn");
const newProjectBtn = document.getElementById("newProjectBtn");
const projectAssignSelect = document.getElementById("projectAssignSelect");
const sessionsPanel = document.getElementById("sessionsPanel");
const infoPanel = document.getElementById("infoPanel");
const toggleSessionsBtn = document.getElementById("toggleSessionsBtn");
const toggleInfoBtn = document.getElementById("toggleInfoBtn");
const closeSessionsBtn = document.getElementById("closeSessionsBtn");
const closeInfoBtn = document.getElementById("closeInfoBtn");
const settingsBtn = document.getElementById("settingsBtn");
const settingsModal = document.getElementById("settingsModal");
const settingsForm = document.getElementById("settingsForm");
const closeSettingsBtn = document.getElementById("closeSettingsBtn");
const settingsStatus = document.getElementById("settingsStatus");
const apiKeyInput = document.getElementById("apiKeyInput");
const baseUrlInput = document.getElementById("baseUrlInput");
const modelInput = document.getElementById("modelInput");
const maxRoundsInput = document.getElementById("maxRoundsInput");
const timeoutInput = document.getElementById("timeoutInput");
const mcpServerList = document.getElementById("mcpServerList");
const mcpNameInput = document.getElementById("mcpNameInput");
const mcpTransportInput = document.getElementById("mcpTransportInput");
const mcpCommandInput = document.getElementById("mcpCommandInput");
const mcpArgsInput = document.getElementById("mcpArgsInput");
const mcpUrlInput = document.getElementById("mcpUrlInput");
const addMcpBtn = document.getElementById("addMcpBtn");
const refreshMcpBtn = document.getElementById("refreshMcpBtn");
const mcpStatus = document.getElementById("mcpStatus");

const ACTIVE_SESSION_KEY = "countboter_active_session";
let sessions = [];
let projects = [];
let activeSessionId = null;
let currentSessionHasMessages = false;
let streaming = false;
let controller = null;
let typingEl = null;
let activeToolCard = null;


// 统一输出到浏览器控制台，方便观察 SSE 事件流
function log(message) {
  console.log("[agent-chat]", message);
}


function errorMessage(data, fallback) {
  return data && (data.message || data.error || data.detail) || fallback;
}


async function bootstrapIfNeeded() {
  const check = await fetch("/api/config");
  if (check.ok) return true;
  if (check.status !== 401) {
    statusText.textContent = "连接失败";
    return false;
  }

  const secret = window.prompt("输入服务终端显示的一次性本地认证码");
  if (!secret) {
    statusText.textContent = "需要本地认证";
    return false;
  }
  const response = await fetch("/api/auth/bootstrap", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ secret }),
  });
  statusText.textContent = response.ok ? "就绪" : "认证失败，请刷新重试";
  return response.ok;
}


function setPanelState(name, open) {
  appEl.classList.toggle(name + "-collapsed", !open);
  const button = name === "sessions" ? toggleSessionsBtn : toggleInfoBtn;
  if (button) button.classList.toggle("active", open);
  localStorage.setItem("countboter_panel_" + name, open ? "1" : "0");
}


function togglePanel(name) {
  const isCollapsed = appEl.classList.contains(name + "-collapsed");
  setPanelState(name, isCollapsed);
}


function scrollToBottom() {
  chatEl.scrollTop = chatEl.scrollHeight;
}


function nowTime() {
  return new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}


function setStatus(mode) {
  if (mode === "thinking") {
    statusPill.classList.add("thinking");
    statusText.textContent = "生成中";
  } else {
    statusPill.classList.remove("thinking");
    statusText.textContent = "就绪";
  }
}


function setSendButton() {
  sendBtn.textContent = streaming ? "停止" : "发送";
}


function showTyping() {
  if (typingEl) return;
  typingEl = document.createElement("div");
  typingEl.className = "typing";
  typingEl.innerHTML = "<i></i><i></i><i></i>";
  chatEl.appendChild(typingEl);
  scrollToBottom();
}


function hideTyping() {
  if (typingEl) {
    typingEl.remove();
    typingEl = null;
  }
}


function clearChat() {
  chatEl.querySelectorAll(".message, .tool-card, .system-line, .typing").forEach((el) => el.remove());
  if (emptyState) emptyState.style.display = "";
  resetToolStates();
}


function appendNote(text) {
  const line = document.createElement("div");
  line.className = "system-line";
  line.textContent = text;
  chatEl.appendChild(line);
  scrollToBottom();
}


function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}


// 轻量 Markdown：只处理代码块、内联代码、粗体、标题和列表
function inlineFormat(text) {
  return text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}


function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "";
  let inCode = false;
  let inList = false;

  for (const line of lines) {
    const trimmed = line.trim();

    if (trimmed.startsWith("```")) {
      if (inCode) {
        html += "</code></pre>";
        inCode = false;
      } else {
        if (inList) html += "</ul>";
        html += "<pre><code>";
        inList = false;
        inCode = true;
      }
      continue;
    }

    if (inCode) {
      html += line + "\n";
      continue;
    }

    if (/^###\s+/.test(trimmed)) {
      if (inList) html += "</ul>";
      html += "<h3>" + inlineFormat(trimmed.replace(/^###\s+/, "")) + "</h3>";
      inList = false;
      continue;
    }

    if (/^##\s+/.test(trimmed)) {
      if (inList) html += "</ul>";
      html += "<h2>" + inlineFormat(trimmed.replace(/^##\s+/, "")) + "</h2>";
      inList = false;
      continue;
    }

    if (/^#\s+/.test(trimmed)) {
      if (inList) html += "</ul>";
      html += "<h1>" + inlineFormat(trimmed.replace(/^#\s+/, "")) + "</h1>";
      inList = false;
      continue;
    }

    if (/^[-*]\s+/.test(trimmed)) {
      if (!inList) html += "<ul>";
      html += "<li>" + inlineFormat(trimmed.replace(/^[-*]\s+/, "")) + "</li>";
      inList = true;
      continue;
    }

    if (trimmed === "") {
      if (inList) html += "</ul>";
      inList = false;
      continue;
    }

    if (inList) html += "</ul>";
    html += "<p>" + inlineFormat(line) + "</p>";
    inList = false;
  }

  if (inList) html += "</ul>";
  if (inCode) html += "</code></pre>";
  return html || "<p></p>";
}


function appendBubble(role, text) {
  const row = document.createElement("div");
  row.className = "message " + role;

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "你" : "AI";

  const body = document.createElement("div");
  body.className = "message-body";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "assistant") {
    bubble.innerHTML = renderMarkdown(text);
  } else {
    bubble.textContent = text;
  }

  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = nowTime();

  if (role === "assistant") {
    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-btn";
    copyBtn.type = "button";
    copyBtn.textContent = "复制";
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = "已复制";
        setTimeout(() => {
          copyBtn.textContent = "复制";
        }, 1500);
      } catch (err) {
        log("复制失败: " + err.message);
      }
    });
    meta.appendChild(copyBtn);
  }

  body.appendChild(bubble);
  body.appendChild(meta);
  row.appendChild(avatar);
  row.appendChild(body);

  if (emptyState) emptyState.style.display = "none";
  chatEl.appendChild(row);
  scrollToBottom();

  return { row, bubble, meta };
}


function addToolCard(name, args) {
  const card = document.createElement("div");
  card.className = "tool-card running";

  const line = document.createElement("div");
  line.className = "tool-line";

  const nameEl = document.createElement("span");
  nameEl.className = "tool-name";
  nameEl.textContent = name;

  const stateEl = document.createElement("span");
  stateEl.className = "tool-status";
  stateEl.textContent = "运行中";

  const argsEl = document.createElement("div");
  argsEl.className = "tool-args";
  argsEl.textContent = "参数: " + (args || "{}");

  line.appendChild(nameEl);
  line.appendChild(stateEl);
  card.appendChild(line);
  card.appendChild(argsEl);

  chatEl.appendChild(card);
  scrollToBottom();
  return card;
}


function setToolCard(card, ok, note) {
  if (!card) return;
  card.classList.remove("running");
  card.classList.add(ok ? "ok" : "error");
  card.querySelector(".tool-status").textContent = ok ? "完成" : "失败";

  if (note) {
    const noteEl = document.createElement("div");
    noteEl.className = "tool-note";
    noteEl.textContent = note;
    card.appendChild(noteEl);
  }
}


function setSidebarToolState(name, state) {
  const items = toolList.querySelectorAll("li");
  for (const item of items) {
    const nameEl = item.querySelector(".tool-name");
    if (nameEl && nameEl.textContent === name) {
      item.className = state === "运行中" ? "running" : state === "完成" ? "ok" : state === "失败" ? "error" : "";
      item.querySelector(".tool-state").textContent = state;
    }
  }
}


function resetToolStates() {
  for (const item of toolList.querySelectorAll("li")) {
    item.className = "";
    const stateEl = item.querySelector(".tool-state");
    if (stateEl) stateEl.textContent = "就绪";
  }
}


function renderProjectSelect() {
  const current = projectAssignSelect.value;
  projectAssignSelect.innerHTML = '<option value="">请选择项目</option>';
  for (const project of projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.name;
    projectAssignSelect.appendChild(option);
  }
  projectAssignSelect.value = current;
}


async function loadProjects() {
  try {
    const response = await fetch("/api/projects");
    if (!response.ok) return;
    const data = await response.json();
    projects = data.projects || [];
    renderProjectSelect();
  } catch (err) {
    log("加载项目失败: " + err.message);
  }
}


async function ensureProjectSelected() {
  if (projectAssignSelect.value) return projectAssignSelect.value;
  if (projects.length > 0) {
    projectAssignSelect.value = projects[0].id;
    return projects[0].id;
  }
  await createProject();
  if (projects.length > 0) {
    projectAssignSelect.value = projects[0].id;
    return projects[0].id;
  }
  return null;
}


async function createProject() {
  const name = window.prompt("项目名称");
  if (!name || !name.trim()) return;
  try {
    const response = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (!response.ok) return;
    await loadProjects();
  } catch (err) {
    log("创建项目失败: " + err.message);
  }
}


async function deleteProject(projectId) {
  const project = projects.find((item) => item.id === projectId);
  if (!project) return;
  if (!window.confirm("删除项目“" + project.name + "”？会话会移入默认项目。")) return;
  try {
    const response = await fetch("/api/projects/" + projectId, { method: "DELETE" });
    if (!response.ok) return;
    await loadProjects();
    await refreshSessions();
  } catch (err) {
    log("删除项目失败: " + err.message);
  }
}


async function assignActiveSessionProject() {
  if (!activeSessionId) return;
  let projectId = projectAssignSelect.value || null;
  if (!projectId) {
    projectId = await ensureProjectSelected();
    if (!projectId) return;
  }
  try {
    const response = await fetch("/api/sessions/" + activeSessionId + "/project", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId }),
    });
    if (!response.ok) return;
    await refreshSessions();
    await loadProjects();
  } catch (err) {
    log("归入项目失败: " + err.message);
  }
}


// 从后端读取工具列表和运行配置，让侧边栏与后端保持一致
async function loadRuntimeInfo() {
  try {
    const skillsRes = await fetch("/api/skills");
    if (skillsRes.ok) {
      const skillsData = await skillsRes.json();
      skillList.innerHTML = "";
      for (const skill of skillsData.skills || []) {
        const li = document.createElement("li");
        li.className = "skill-item";

        const name = document.createElement("span");
        name.className = "skill-name";
        name.textContent = skill.name || "skill";

        const desc = document.createElement("span");
        desc.className = "skill-desc";
        desc.textContent = skill.description || "";

        li.appendChild(name);
        li.appendChild(desc);
        skillList.appendChild(li);
      }
    }

    const toolsRes = await fetch("/api/tools");
    if (toolsRes.ok) {
      const data = await toolsRes.json();
      toolList.innerHTML = "";
      for (const item of data.tools || []) {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.className = "tool-name";
        name.textContent = item.function && item.function.name ? item.function.name : "unknown";
        const state = document.createElement("span");
        state.className = "tool-state";
        state.textContent = "就绪";
        li.appendChild(name);
        li.appendChild(state);
        toolList.appendChild(li);
      }
    }

    const configRes = await fetch("/api/config");
    if (configRes.ok) {
      const cfg = await configRes.json();
      modelLabel.textContent = cfg.model || "DeepSeek Agent";
      maxRoundsValue.textContent = String(cfg.max_rounds ?? 5);
    }
  } catch (err) {
    log("加载运行信息失败: " + err.message);
  }
}


function renderSessions() {
  sessionList.innerHTML = "";

  const groups = {
    "": { id: "", name: "未分组", sessions: [] },
  };
  for (const project of projects) {
    groups[project.id] = { id: project.id, name: project.name, sessions: [] };
  }
  for (const session of sessions) {
    const key = session.project_id || "";
    if (!groups[key]) {
      groups[key] = { id: key, name: "未分组", sessions: [] };
    }
    groups[key].sessions.push(session);
  }

  const groupKeys = ["", ...projects.map((project) => project.id)];
  for (const key of groupKeys) {
    const group = groups[key];
    if (!group) continue;

    const groupEl = document.createElement("div");
    groupEl.className = "project-group";

    const header = document.createElement("div");
    header.className = "project-header";

    const nameEl = document.createElement("span");
    nameEl.className = "project-name";
    nameEl.textContent = group.name + (group.id ? " (" + group.sessions.length + ")" : "");
    nameEl.title = group.name;
    if (group.id) {
      nameEl.addEventListener("dblclick", async () => {
        const newName = window.prompt("项目名称", group.name);
        if (newName && newName.trim()) {
          try {
            await fetch("/api/projects/" + group.id, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name: newName.trim() }),
            });
            await loadProjects();
            await refreshSessions();
          } catch (err) {
            log("重命名项目失败: " + err.message);
          }
        }
      });
    }
    header.appendChild(nameEl);

    if (group.id) {
      const deleteBtn = document.createElement("button");
      deleteBtn.className = "project-delete";
      deleteBtn.type = "button";
      deleteBtn.textContent = "删除";
      deleteBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteProject(group.id);
      });
      header.appendChild(deleteBtn);
    }

    groupEl.appendChild(header);

    for (const session of group.sessions) {
      const li = document.createElement("li");
      li.className = "session-item" + (session.id === activeSessionId ? " active" : "");

      const title = document.createElement("span");
      title.className = "session-title";
      title.textContent = session.title || "新会话";
      title.title = session.title || "新会话";
      title.addEventListener("click", () => openSession(session.id));
      title.addEventListener("dblclick", async () => {
        const newTitle = window.prompt("会话标题", session.title || "新会话");
        if (newTitle && newTitle.trim()) {
          await renameSession(session.id, newTitle.trim());
        }
      });

      const removeBtn = document.createElement("button");
      removeBtn.className = "session-delete";
      removeBtn.type = "button";
      removeBtn.textContent = "删除";
      removeBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteSession(session.id);
      });

      li.appendChild(title);
      li.appendChild(removeBtn);
      groupEl.appendChild(li);
    }

    sessionList.appendChild(groupEl);
  }
}


async function loadSessions() {
  try {
    const response = await fetch("/api/sessions");
    if (!response.ok) return;
    const data = await response.json();
    sessions = data.sessions || [];
    await loadProjects();
    renderSessions();

    const savedId = localStorage.getItem(ACTIVE_SESSION_KEY);
    if (savedId && sessions.some((session) => session.id === savedId)) {
      openSession(savedId);
    }
  } catch (err) {
    log("加载会话失败: " + err.message);
  }
}


async function refreshSessions() {
  try {
    const response = await fetch("/api/sessions");
    if (!response.ok) return;
    const data = await response.json();
    sessions = data.sessions || [];
    await loadProjects();
    renderSessions();
  } catch (err) {
    log("刷新会话失败: " + err.message);
  }
}


async function openSession(sessionId) {
  if (streaming && controller) controller.abort();
  try {
    const response = await fetch("/api/sessions/" + sessionId);
    if (!response.ok) return;
    const data = await response.json();
    activeSessionId = sessionId;
    localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    clearChat();
    currentSessionHasMessages = (data.messages || []).some(
      (message) =>
        (message.role === "user" && message.content) ||
        (message.role === "assistant" && message.content)
    );
    if (data.session) {
      projectAssignSelect.value = data.session.project_id || "";
    }
    for (const message of data.messages || []) {
      if (message.role === "user" && message.content) {
        appendBubble("user", message.content);
      } else if (message.role === "assistant" && message.content) {
        appendBubble("assistant", message.content);
      }
    }
    renderSessions();
  } catch (err) {
    log("打开会话失败: " + err.message);
  }
}


async function createNewSession() {
  if (streaming && controller) controller.abort();
  if (activeSessionId && !currentSessionHasMessages) {
    appendNote("当前会话为空，继续使用当前会话");
    return;
  }
  const projectId = await ensureProjectSelected();
  if (!projectId) {
    appendNote("请先创建项目");
    return;
  }
  try {
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "新会话", project_id: projectId }),
    });
    if (!response.ok) return;
    const session = await response.json();
    sessions.unshift(session);
    activeSessionId = session.id;
    localStorage.setItem(ACTIVE_SESSION_KEY, session.id);
    currentSessionHasMessages = false;
    projectAssignSelect.value = session.project_id || "";
    clearChat();
    renderSessions();
  } catch (err) {
    log("创建会话失败: " + err.message);
  }
}


async function renameSession(sessionId, title) {
  try {
    const response = await fetch("/api/sessions/" + sessionId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (!response.ok) return;
    await loadSessions();
  } catch (err) {
    log("重命名会话失败: " + err.message);
  }
}


async function deleteSession(sessionId) {
  if (!window.confirm("删除这个会话？")) return;
  try {
    const response = await fetch("/api/sessions/" + sessionId, { method: "DELETE" });
    if (!response.ok) return;
    sessions = sessions.filter((session) => session.id !== sessionId);
    if (activeSessionId === sessionId) {
      activeSessionId = null;
      currentSessionHasMessages = false;
      localStorage.removeItem(ACTIVE_SESSION_KEY);
      clearChat();
    }
    renderSessions();
  } catch (err) {
    log("删除会话失败: " + err.message);
  }
}


async function ensureSession(title) {
  if (activeSessionId) return activeSessionId;
  const projectId = await ensureProjectSelected();
  if (!projectId) throw new Error("请先创建项目");
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "新会话", project_id: projectId }),
  });
  if (!response.ok) throw new Error("创建会话失败");
  const session = await response.json();
  sessions.unshift(session);
  activeSessionId = session.id;
  localStorage.setItem(ACTIVE_SESSION_KEY, session.id);
  renderSessions();
  return session.id;
}


function renderMcpServers(data) {
  mcpServerList.innerHTML = "";
  const statusById = {};
  for (const item of data.status || []) {
    statusById[item.id] = item;
  }
  for (const server of data.servers || []) {
    const li = document.createElement("li");
    li.className = "mcp-server-item";

    const info = document.createElement("span");
    info.className = "mcp-server-info";
    const toolCount = statusById[server.id] ? statusById[server.id].tool_count : 0;
    info.textContent = server.name + " · " + server.transport + " · " + toolCount + " tools";

    const removeBtn = document.createElement("button");
    removeBtn.className = "session-delete";
    removeBtn.type = "button";
    removeBtn.textContent = "删除";
    removeBtn.addEventListener("click", () => deleteMcpServer(server.id));

    li.appendChild(info);
    li.appendChild(removeBtn);
    mcpServerList.appendChild(li);
  }
}


async function loadMcpServers() {
  try {
    const response = await fetch("/api/mcp/servers");
    if (!response.ok) return;
    const data = await response.json();
    renderMcpServers(data);
  } catch (err) {
    log("加载 MCP 失败: " + err.message);
  }
}


async function refreshMcp() {
  mcpStatus.textContent = "刷新中";
  try {
    const response = await fetch("/api/mcp/refresh", { method: "POST" });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(errorMessage(data, "刷新失败"));
    }
    const data = await response.json();
    mcpStatus.textContent = "工具 " + data.tool_count + " 个";
    await loadMcpServers();
    await loadRuntimeInfo();
  } catch (err) {
    mcpStatus.textContent = "刷新失败";
    log("刷新 MCP 失败: " + err.message);
  }
}


async function addMcpServer() {
  let args = [];
  if (mcpArgsInput.value.trim()) {
    try {
      args = JSON.parse(mcpArgsInput.value.trim());
      if (!Array.isArray(args)) throw new Error("args must be array");
    } catch (err) {
      mcpStatus.textContent = "参数必须是 JSON 数组";
      return;
    }
  }

  const payload = {
    name: mcpNameInput.value.trim(),
    transport: mcpTransportInput.value,
    command: mcpCommandInput.value.trim(),
    args,
    url: mcpUrlInput.value.trim(),
    enabled: true,
  };
  if (!payload.name) {
    mcpStatus.textContent = "请输入服务器名称";
    return;
  }

  try {
    const response = await fetch("/api/mcp/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(errorMessage(data, "添加失败"));
    }
    mcpNameInput.value = "";
    mcpCommandInput.value = "";
    mcpArgsInput.value = "";
    mcpUrlInput.value = "";
    await refreshMcp();
  } catch (err) {
    mcpStatus.textContent = "添加失败";
    log("添加 MCP 失败: " + err.message);
  }
}


async function deleteMcpServer(serverId) {
  if (!window.confirm("删除这个 MCP Server？")) return;
  try {
    await fetch("/api/mcp/servers/" + serverId, { method: "DELETE" });
    await refreshMcp();
  } catch (err) {
    log("删除 MCP 失败: " + err.message);
  }
}


async function openSettings() {
  settingsModal.hidden = false;
  settingsStatus.textContent = "读取中";
  try {
    const response = await fetch("/api/settings");
    if (response.ok) {
      const settings = await response.json();
      baseUrlInput.value = settings.base_url || "";
      modelInput.value = settings.model || "";
      maxRoundsInput.value = settings.max_rounds ?? 5;
      timeoutInput.value = settings.tool_timeout ?? 10;
      apiKeyInput.value = "";
      settingsStatus.textContent = settings.api_key_configured ? "已配置" : "未配置";
    }
    await loadMcpServers();
  } catch (err) {
    settingsStatus.textContent = "读取失败";
    log("读取设置失败: " + err.message);
  }
}


function closeSettings() {
  settingsModal.hidden = true;
}


async function saveSettings(event) {
  event.preventDefault();
  settingsStatus.textContent = "保存中";
  const payload = {
    api_key: apiKeyInput.value.trim(),
    base_url: baseUrlInput.value.trim(),
    model: modelInput.value.trim(),
    max_rounds: Number(maxRoundsInput.value),
    tool_timeout: Number(timeoutInput.value),
  };
  try {
    const response = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(errorMessage(data, "保存失败"));
    }
    apiKeyInput.value = "";
    settingsStatus.textContent = "已保存并加密";
    await loadRuntimeInfo();
    setTimeout(closeSettings, 1200);
  } catch (err) {
    settingsStatus.textContent = "保存失败";
    log("保存设置失败: " + err.message);
  }
}


function autoResize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 140) + "px";
}


async function uploadFile(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  appendNote("正在上传 " + file.name + "...");
  const formData = new FormData();
  formData.append("file", file);

  try {
    const response = await fetch("/api/upload", {
      method: "POST",
      body: formData,
    });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(errorMessage(data, "HTTP " + response.status));
    }
    const data = await response.json();
    const parsedType = data.parsed && data.parsed.type ? data.parsed.type : "unsupported";
    appendNote(
      "上传成功: " + data.filename +
      " (" + data.size + " bytes, file_id=" + data.file_id + ")"
    );
    appendNote("解析类型: " + parsedType + "，可让 Agent 使用 file_id 处理");
  } catch (err) {
    appendNote("上传失败: " + err.message);
  } finally {
    fileInput.value = "";
  }
}


async function sendMessage() {
  const text = input.value.trim();
  if (!text || streaming) return;

  input.value = "";
  autoResize();
  appendBubble("user", text);
  currentSessionHasMessages = true;
  log("发送用户消息: " + text);

  streaming = true;
  controller = new AbortController();
  setStatus("thinking");
  setSendButton();
  showTyping();

  let assistantText = "";
  let assistantMessage = null;
  let failed = false;

  try {
    const sessionId = await ensureSession(text);
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text }),
      signal: controller.signal,
    });
    log("SSE 连接已建立，HTTP 状态: " + response.status);

    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(errorMessage(data, "HTTP " + response.status));
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE 事件以空行分隔，这里逐个取出 data: 行
      let separator = buffer.indexOf("\n\n");
      while (separator !== -1) {
        const rawEvent = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);

        const dataLine = rawEvent.split("\n").find((line) => line.startsWith("data:"));
        if (dataLine) {
          const event = JSON.parse(dataLine.slice(5).trim());
          switch (event.type) {
            case "round":
              hideTyping();
              log("第 " + event.round + " 轮");
              appendNote("第 " + event.round + " 轮");
              break;
            case "text":
              hideTyping();
              if (!assistantMessage) {
                assistantMessage = appendBubble("assistant", "");
              }
              assistantText += event.content;
              assistantMessage.bubble.innerHTML = renderMarkdown(assistantText);
              break;
            case "tool_call":
              hideTyping();
              log("工具调用: " + event.name + " " + event.arguments);
              activeToolCard = addToolCard(event.name, event.arguments);
              setSidebarToolState(event.name, "运行中");
              break;
            case "tool_result":
              log("工具结果: " + event.name + " ok=" + event.ok);
              setToolCard(activeToolCard, event.ok, event.message);
              activeToolCard = null;
              setSidebarToolState(event.name, event.ok ? "完成" : "失败");
              break;
            case "done":
              hideTyping();
              log("本轮完成: " + event.rounds + " 轮，耗时 " + event.total_ms + " ms");
              appendNote("完成（第 " + event.rounds + " 轮，耗时 " + event.total_ms + " ms）");
              break;
            case "error":
              failed = true;
              hideTyping();
              log("后端返回错误: " + event.message);
              appendNote("错误: " + event.message);
              break;
          }
        }

        separator = buffer.indexOf("\n\n");
      }
    }
  } catch (err) {
    failed = true;
    hideTyping();
    if (err.name === "AbortError") {
      log("用户停止生成");
      appendNote("已停止");
    } else {
      log("请求异常: " + err.message);
      appendNote("错误: " + err.message);
    }
  } finally {
    streaming = false;
    controller = null;
    setStatus("ready");
    setSendButton();
    if (!failed && activeSessionId) refreshSessions();
  }
}


form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (streaming) {
    if (controller) controller.abort();
    return;
  }
  sendMessage();
});


input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});


input.addEventListener("input", autoResize);
uploadBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", uploadFile);
newSessionBtn.addEventListener("click", createNewSession);
newProjectBtn.addEventListener("click", createProject);
projectAssignSelect.addEventListener("change", assignActiveSessionProject);
settingsBtn.addEventListener("click", openSettings);
closeSettingsBtn.addEventListener("click", closeSettings);
settingsForm.addEventListener("submit", saveSettings);
addMcpBtn.addEventListener("click", addMcpServer);
refreshMcpBtn.addEventListener("click", refreshMcp);
settingsModal.addEventListener("click", (event) => {
  if (event.target === settingsModal) closeSettings();
});

toggleSessionsBtn.addEventListener("click", () => togglePanel("sessions"));
toggleInfoBtn.addEventListener("click", () => togglePanel("info"));
closeSessionsBtn.addEventListener("click", () => setPanelState("sessions", false));
closeInfoBtn.addEventListener("click", () => setPanelState("info", false));

// 右侧信息面板的每个分区都可以点击展开/收起
document.querySelectorAll(".info-section-toggle").forEach((button) => {
  button.addEventListener("click", () => {
    const section = button.closest(".info-section");
    if (section) section.classList.toggle("open");
  });
});

// 恢复上次的抽屉展开状态
setPanelState("sessions", localStorage.getItem("countboter_panel_sessions") !== "0");
setPanelState("info", localStorage.getItem("countboter_panel_info") !== "0");

bootstrapIfNeeded().then((ready) => {
  if (ready) {
    loadRuntimeInfo();
    loadSessions();
  }
}).catch(() => { statusText.textContent = "连接失败"; });
