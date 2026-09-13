// The live-view dashboard's only script. No build step, no framework —
// this is a dev-facing observability tool, not a production client (see
// server/__init__.py for what's still not built: a full file tree with
// multi-file diffing, and a richer task board than the list here).
//
// Now auth-gated and multi-chat: a login/register screen gates everything
// else; once in, the left nav picks a project then a chat, and every
// dashboard call below is scoped to /api/chats/{currentChatId}/... instead
// of one implicit global session.

const $authScreen = document.getElementById("auth-screen");
const $appScreen = document.getElementById("app-screen");
const $authForm = document.getElementById("auth-form");
const $authUsername = document.getElementById("auth-username");
const $authPassword = document.getElementById("auth-password");
const $authError = document.getElementById("auth-error");
const $authSubmit = document.getElementById("auth-submit");
const $tabLogin = document.getElementById("tab-login");
const $tabRegister = document.getElementById("tab-register");
const $whoami = document.getElementById("whoami");
const $logoutBtn = document.getElementById("logout-btn");

const $projectList = document.getElementById("project-list");
const $chatList = document.getElementById("chat-list");
const $newProjectBtn = document.getElementById("new-project-btn");
const $newChatBtn = document.getElementById("new-chat-btn");
const $noChatNotice = document.getElementById("no-chat-notice");

const $log = document.getElementById("log");
const $planList = document.getElementById("plan-list");
const $costPanel = document.getElementById("cost-panel");
const $skillsList = document.getElementById("skills-list");
const $toolsPanel = document.getElementById("tools-panel");
const $tasksList = document.getElementById("tasks-list");
const $fileList = document.getElementById("file-list");
const $breadcrumb = document.getElementById("file-breadcrumb");
const $badgeLive = document.getElementById("badge-live");
const $badgeRow = document.getElementById("badge-row");
const $taskForm = document.getElementById("task-form");
const $taskInput = document.getElementById("task-input");
const $sendBtn = document.getElementById("send-btn");
const $cancelBtn = document.getElementById("cancel-btn");
const $taskError = document.getElementById("task-error");
const $modal = document.getElementById("file-modal");
const $modalPath = document.getElementById("modal-path");
const $modalContent = document.getElementById("modal-content");
const $modalClose = document.getElementById("modal-close");
const $approvalModal = document.getElementById("approval-modal");
const $approvalPayload = document.getElementById("approval-payload");
const $approvalApprove = document.getElementById("approval-approve");
const $approvalDeny = document.getElementById("approval-deny");

const $promptModal = document.getElementById("prompt-modal");
const $promptTitle = document.getElementById("prompt-title");
const $promptInput = document.getElementById("prompt-input");
const $promptOk = document.getElementById("prompt-ok");
const $promptCancel = document.getElementById("prompt-cancel");
const $confirmModal = document.getElementById("confirm-modal");
const $confirmMessage = document.getElementById("confirm-message");
const $confirmOk = document.getElementById("confirm-ok");
const $confirmCancel = document.getElementById("confirm-cancel");

let authMode = "login"; // "login" | "register"
let projects = [];
let chats = [];
let currentProjectId = null;
let currentChatId = null;
let currentDir = ".";
let openToolEntry = null; // the DOM node for the most recent unresolved tool_started
let streamingText = null; // the DOM node currently accumulating text_delta chunks
let approvalQueue = []; // {request_id, payload} — shown one at a time, oldest first
let eventSource = null; // the current chat's SSE connection; replaced on every chat switch

// --- text-input / confirm dialogs --------------------------------------
//
// Stand-ins for window.prompt/confirm — not every embedding this dashboard
// runs in (a native pywebview window, in particular) reliably supports
// those native dialogs, so these are real DOM modals instead. Only one of
// either kind is ever open at a time; each returns a Promise resolving to
// the entered text (or null if cancelled) / a boolean.

function showPrompt(title, defaultValue) {
  return new Promise((resolve) => {
    $promptTitle.textContent = title;
    $promptInput.value = defaultValue || "";
    $promptModal.classList.remove("hidden");
    $promptInput.focus();
    $promptInput.select();

    const cleanup = () => {
      $promptModal.classList.add("hidden");
      $promptOk.onclick = null;
      $promptCancel.onclick = null;
      $promptInput.onkeydown = null;
    };
    $promptOk.onclick = () => { const v = $promptInput.value.trim(); cleanup(); resolve(v || null); };
    $promptCancel.onclick = () => { cleanup(); resolve(null); };
    $promptInput.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); $promptOk.onclick(); }
      if (e.key === "Escape") { e.preventDefault(); $promptCancel.onclick(); }
    };
  });
}

function showConfirm(message) {
  return new Promise((resolve) => {
    $confirmMessage.textContent = message;
    $confirmModal.classList.remove("hidden");

    const cleanup = () => {
      $confirmModal.classList.add("hidden");
      $confirmOk.onclick = null;
      $confirmCancel.onclick = null;
    };
    $confirmOk.onclick = () => { cleanup(); resolve(true); };
    $confirmCancel.onclick = () => { cleanup(); resolve(false); };
  });
}

// --- auth ------------------------------------------------------------

function setAuthMode(mode) {
  authMode = mode;
  $tabLogin.classList.toggle("active", mode === "login");
  $tabRegister.classList.toggle("active", mode === "register");
  $authSubmit.textContent = mode === "login" ? "Log in" : "Register";
  $authPassword.autocomplete = mode === "login" ? "current-password" : "new-password";
  $authError.classList.add("hidden");
}

$tabLogin.onclick = () => setAuthMode("login");
$tabRegister.onclick = () => setAuthMode("register");

$authForm.onsubmit = async (e) => {
  e.preventDefault();
  $authError.classList.add("hidden");
  const username = $authUsername.value.trim();
  const password = $authPassword.value;
  const path = authMode === "login" ? "/api/auth/login" : "/api/auth/register";

  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    $authError.textContent = body.detail || "Something went wrong.";
    $authError.classList.remove("hidden");
    return;
  }
  $authPassword.value = "";
  await boot();
};

$logoutBtn.onclick = async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  closeChatConnection();
  projects = [];
  chats = [];
  currentProjectId = null;
  currentChatId = null;
  $appScreen.classList.add("hidden");
  $authScreen.classList.remove("hidden");
};

async function checkAuth() {
  const res = await fetch("/api/auth/me");
  if (!res.ok) return null;
  return res.json();
}

// --- projects & chats nav ---------------------------------------------

function renderProjectList() {
  $projectList.innerHTML = "";
  if (!projects.length) {
    $projectList.innerHTML = '<li class="muted">No projects yet.</li>';
    return;
  }
  for (const p of projects) {
    const li = document.createElement("li");
    li.className = "nav-item" + (p.id === currentProjectId ? " active" : "");
    li.innerHTML = `<span class="nav-item-label">${escapeHtml(p.name)}</span><button class="nav-item-delete" title="Delete project">×</button>`;
    li.querySelector(".nav-item-label").onclick = () => selectProject(p.id);
    li.querySelector(".nav-item-delete").onclick = async (e) => {
      e.stopPropagation();
      if (!(await showConfirm(`Delete project "${p.name}" and all its chats?`))) return;
      await fetch(`/api/projects/${p.id}`, { method: "DELETE" });
      if (p.id === currentProjectId) {
        currentProjectId = null;
        currentChatId = null;
      }
      await loadProjects();
    };
    $projectList.appendChild(li);
  }
}

function renderChatList() {
  $chatList.innerHTML = "";
  $newChatBtn.disabled = !currentProjectId;
  if (!currentProjectId) {
    $chatList.innerHTML = '<li class="muted">Select a project.</li>';
    return;
  }
  if (!chats.length) {
    $chatList.innerHTML = '<li class="muted">No chats yet.</li>';
    return;
  }
  for (const c of chats) {
    const li = document.createElement("li");
    li.className = "nav-item" + (c.id === currentChatId ? " active" : "");
    li.innerHTML = `<span class="nav-item-label">${escapeHtml(c.title)}</span><button class="nav-item-delete" title="Delete chat">×</button>`;
    li.querySelector(".nav-item-label").onclick = () => selectChat(c.id);
    li.querySelector(".nav-item-label").ondblclick = async () => {
      const title = await showPrompt("Rename chat", c.title);
      if (!title || title === c.title) return;
      await fetch(`/api/chats/${c.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      await loadChats(currentProjectId);
    };
    li.querySelector(".nav-item-delete").onclick = async (e) => {
      e.stopPropagation();
      if (!(await showConfirm(`Delete chat "${c.title}"?`))) return;
      await fetch(`/api/chats/${c.id}`, { method: "DELETE" });
      if (c.id === currentChatId) currentChatId = null;
      await loadChats(currentProjectId);
    };
    $chatList.appendChild(li);
  }
}

async function loadProjects() {
  const res = await fetch("/api/projects");
  projects = await res.json();
  if (currentProjectId && !projects.some((p) => p.id === currentProjectId)) currentProjectId = null;
  if (!currentProjectId && projects.length) currentProjectId = projects[0].id;
  renderProjectList();
  await loadChats(currentProjectId);
}

async function loadChats(projectId) {
  currentProjectId = projectId;
  renderProjectList();
  if (!projectId) {
    chats = [];
    renderChatList();
    await selectChat(null);
    return;
  }
  const res = await fetch(`/api/projects/${projectId}/chats`);
  chats = await res.json();
  if (currentChatId && !chats.some((c) => c.id === currentChatId)) currentChatId = null;
  if (!currentChatId && chats.length) currentChatId = chats[0].id;
  renderChatList();
  await selectChat(currentChatId);
}

async function selectProject(projectId) {
  currentChatId = null;
  await loadChats(projectId);
}

async function selectChat(chatId) {
  currentChatId = chatId;
  renderChatList();
  closeChatConnection();
  resetDashboard();

  if (!chatId) {
    $noChatNotice.classList.remove("hidden");
    setRunning(false);
    $taskInput.disabled = true;
    $sendBtn.disabled = true;
    return;
  }
  $noChatNotice.classList.add("hidden");
  $taskInput.disabled = false;

  await refreshState();
  await refreshFiles(".");
  // No separate GET /log call here: connect()'s SSE stream replays this
  // chat's full history itself (_stream() in app.py yields the
  // broadcaster's history before any live event) — fetching it again here
  // would render every past event twice.
  connect();
}

$newProjectBtn.onclick = async () => {
  const name = await showPrompt("Project name", "");
  if (!name) return;
  const res = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const project = await res.json();
  await loadProjects();
  await selectProject(project.id);
};

$newChatBtn.onclick = async () => {
  if (!currentProjectId) return;
  const res = await fetch(`/api/projects/${currentProjectId}/chats`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "New chat" }),
  });
  const chat = await res.json();
  await loadChats(currentProjectId);
  await selectChat(chat.id);
};

function resetDashboard() {
  $log.innerHTML = "";
  streamingText = null;
  openToolEntry = null;
  approvalQueue = [];
  $approvalModal.classList.add("hidden");
  $planList.innerHTML = '<li class="muted">No plan yet.</li>';
  $costPanel.innerHTML = "";
  $skillsList.innerHTML = "";
  $toolsPanel.innerHTML = "";
  $tasksList.innerHTML = "";
  $fileList.innerHTML = "";
  $breadcrumb.innerHTML = "";
  currentDir = ".";
}

function closeChatConnection() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

// --- rendering -------------------------------------------------------

function planMark(status) {
  return { pending: "[ ]", in_progress: "[~]", done: "[x]", blocked: "[!]" }[status] || "[ ]";
}

function renderState(state) {
  $badgeLive.textContent = state.live ? "live" : "stub";
  $badgeLive.className = "badge " + (state.live ? "live" : "stub");

  $badgeRow.innerHTML = "";
  for (const text of [state.model.orchestrator, state.sandbox]) {
    const span = document.createElement("span");
    span.className = "badge";
    span.textContent = text;
    $badgeRow.appendChild(span);
  }

  setRunning(state.running);

  $planList.innerHTML = "";
  if (!state.plan.length) {
    $planList.innerHTML = '<li class="muted">No plan yet.</li>';
  } else {
    for (const item of state.plan) {
      const li = document.createElement("li");
      li.className = item.status;
      li.innerHTML = `<span class="plan-mark">${planMark(item.status)}</span><span>${escapeHtml(item.task)}</span>`;
      $planList.appendChild(li);
    }
  }

  const c = state.cost;
  const pct = c.cap_usd > 0 ? Math.min(100, (c.spent_usd / c.cap_usd) * 100) : 0;
  const costCls = pct > 90 ? "crit" : pct > 60 ? "warn" : "";
  let html = `
    <div>$${c.spent_usd.toFixed(4)} of $${c.cap_usd.toFixed(2)} (${c.calls} calls)
      <div class="meter"><div class="meter-fill ${costCls}" style="width:${pct}%"></div></div>
    </div>`;
  if (state.rate_limit) {
    const r = state.rate_limit;
    const rpct = r.per_day > 0 ? Math.min(100, (r.used_today / r.per_day) * 100) : 0;
    const rCls = rpct > 90 ? "crit" : rpct > 60 ? "warn" : "";
    html += `
      <div>${r.used_today} / ${r.per_day} free-tier requests today (${r.per_minute}/min cap)
        <div class="meter"><div class="meter-fill ${rCls}" style="width:${rpct}%"></div></div>
      </div>`;
  }
  $costPanel.innerHTML = html;

  $skillsList.innerHTML = state.skills.length
    ? state.skills.map(s => `<li><span class="skill-name">${escapeHtml(s.name)}</span><span class="skill-desc">${escapeHtml(s.description)}</span></li>`).join("")
    : '<li class="muted">None installed.</li>';

  const connectorsLine = state.connectors.length
    ? `Connectors: ${state.connectors.join(", ")}`
    : "Connectors: none configured";
  const latentLine = state.tools.latent.length
    ? `Latent (via search_tools): ${state.tools.latent.join(", ")}`
    : "No latent tools waiting to be activated.";
  $toolsPanel.innerHTML = `
    <div class="muted">${escapeHtml(connectorsLine)}</div>
    <div style="margin-top:6px">${escapeHtml(state.tools.active.join(", "))}</div>
    <div class="muted" style="margin-top:6px">${escapeHtml(latentLine)}</div>`;

  $tasksList.innerHTML = state.tasks.length
    ? state.tasks.map(t => {
        const body = t.status === "done" ? t.result : t.status === "failed" ? t.error : null;
        return `<li>
          <div><span class="task-status ${t.status}">${t.status}</span> ${escapeHtml(t.description)}</div>
          ${body ? `<div class="task-body">${escapeHtml(body)}</div>` : ""}
        </li>`;
      }).join("")
    : '<li class="muted">No background tasks yet.</li>';
}

function setRunning(running) {
  const hasChat = !!currentChatId;
  $sendBtn.disabled = running || !hasChat;
  $taskInput.disabled = running || !hasChat;
  $cancelBtn.disabled = !running || !hasChat;
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function renderDiff(diffText) {
  // A real unified diff, or one of loop.py's plain-text fallbacks ("(new
  // file)", "(no textual difference)", ...) — either way, just color the
  // +/- lines a unified diff would have; anything else renders plain.
  return diffText
    .split("\n")
    .map((line) => {
      const escaped = escapeHtml(line);
      if (line.startsWith("+") && !line.startsWith("+++")) return `<span class="diff-add">${escaped}</span>`;
      if (line.startsWith("-") && !line.startsWith("---")) return `<span class="diff-remove">${escaped}</span>`;
      if (line.startsWith("@@")) return `<span class="diff-hunk">${escaped}</span>`;
      return escaped;
    })
    .join("\n");
}

function appendLogNode(node) {
  const atBottom = $log.scrollHeight - $log.scrollTop - $log.clientHeight < 40;
  $log.appendChild(node);
  if (atBottom) $log.scrollTop = $log.scrollHeight;
}

function addUserMessage(text) {
  const div = document.createElement("div");
  div.className = "log-entry log-user";
  div.textContent = text;
  appendLogNode(div);
  streamingText = null;
  openToolEntry = null;
}

function handleEvent(ev) {
  switch (ev.type) {
    case "text_delta": {
      if (!streamingText) {
        streamingText = document.createElement("div");
        streamingText.className = "log-entry log-text";
        appendLogNode(streamingText);
      }
      streamingText.textContent += ev.text;
      $log.scrollTop = $log.scrollHeight;
      break;
    }
    case "tool_started": {
      const div = document.createElement("div");
      div.className = "log-entry log-tool";
      const args = JSON.stringify(ev.arguments);
      div.innerHTML = `<span class="tool-name">→ ${escapeHtml(ev.name)}</span> <span class="tool-args">${escapeHtml(args.length > 140 ? args.slice(0, 140) + "…" : args)}</span>`;
      appendLogNode(div);
      openToolEntry = div;
      streamingText = null;
      break;
    }
    case "tool_finished": {
      const target = openToolEntry;
      openToolEntry = null;
      if (!target) break;
      target.classList.add(ev.ok ? "ok" : "fail");
      const preview = document.createElement("span");
      preview.className = "tool-preview";
      preview.textContent = ev.preview;
      target.appendChild(preview);
      break;
    }
    case "file_changed": {
      const div = document.createElement("div");
      div.className = "log-diff";
      div.innerHTML = `<div class="diff-path">${escapeHtml(ev.path)}</div><pre class="diff-body">${renderDiff(ev.diff)}</pre>`;
      appendLogNode(div);
      break;
    }
    case "approval_needed": {
      approvalQueue.push(ev);
      if (approvalQueue.length === 1) showNextApproval();
      break;
    }
    case "finished": {
      const div = document.createElement("div");
      const blocked = ev.reason !== "done";
      div.className = "log-finished" + (blocked ? " blocked" : "");
      div.textContent = `${ev.reason} · ${ev.steps} steps · ~$${ev.cost_usd.toFixed(3)}`;
      appendLogNode(div);
      streamingText = null;
      openToolEntry = null;
      setRunning(false);
      refreshState();
      refreshFiles(currentDir);
      break;
    }
    case "state_changed": {
      refreshState();
      refreshFiles(currentDir);
      break;
    }
  }
}

// --- data fetching ----------------------------------------------------

async function refreshState() {
  if (!currentChatId) return;
  const res = await fetch(`/api/chats/${currentChatId}/state`);
  if (!res.ok) return;
  renderState(await res.json());
}

async function refreshFiles(path) {
  if (!currentChatId) return;
  currentDir = path;
  const res = await fetch(`/api/chats/${currentChatId}/files?path=` + encodeURIComponent(path));
  if (!res.ok) return;
  const data = await res.json();

  $breadcrumb.innerHTML = "";
  const parts = path === "." ? [] : path.split("/");
  const crumbs = [{ label: "workspace", path: "." }, ...parts.map((p, i) => ({ label: p, path: parts.slice(0, i + 1).join("/") }))];
  crumbs.forEach((c, i) => {
    const span = document.createElement("span");
    span.textContent = c.label + (i < crumbs.length - 1 ? " / " : "");
    span.onclick = () => refreshFiles(c.path);
    $breadcrumb.appendChild(span);
  });

  $fileList.innerHTML = "";
  for (const entry of data.entries) {
    const li = document.createElement("li");
    li.className = (entry.is_dir ? "dir " : "") + (entry.name.startsWith(".") ? "dotfile" : "");
    li.innerHTML = `<span>${escapeHtml(entry.name)}</span>` + (entry.size != null ? `<span class="file-size">${formatSize(entry.size)}</span>` : "");
    li.onclick = () => (entry.is_dir ? refreshFiles(entry.path) : openFile(entry.path));
    $fileList.appendChild(li);
  }
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + "b";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + "kb";
  return (bytes / (1024 * 1024)).toFixed(1) + "mb";
}

async function openFile(path) {
  if (!currentChatId) return;
  const res = await fetch(`/api/chats/${currentChatId}/file_content?path=` + encodeURIComponent(path));
  const data = await res.json();
  $modalPath.textContent = path + (data.truncated ? "  (truncated)" : "");
  $modalContent.textContent = data.binary ? "(binary file — not previewed)" : data.content;
  $modal.classList.remove("hidden");
}

$modalClose.onclick = () => $modal.classList.add("hidden");
$modal.onclick = (e) => { if (e.target === $modal) $modal.classList.add("hidden"); };

// --- approvals (§P5) ------------------------------------------------------
//
// An outward-facing tool (currently only `fetch`) blocks mid-turn waiting on
// POST /api/chats/{id}/approve — this modal is the only way to answer it
// from here. Approvals queue if more than one arrives; each is shown only
// after the previous one is resolved.

function showNextApproval() {
  const next = approvalQueue[0];
  if (!next) {
    $approvalModal.classList.add("hidden");
    return;
  }
  $approvalPayload.textContent = next.payload;
  $approvalModal.classList.remove("hidden");
}

async function resolveApproval(approved) {
  const current = approvalQueue.shift();
  if (!current || !currentChatId) return;
  await fetch(`/api/chats/${currentChatId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: current.request_id, approved }),
  });
  showNextApproval();
}

$approvalApprove.onclick = () => resolveApproval(true);
$approvalDeny.onclick = () => resolveApproval(false);

// --- task form ----------------------------------------------------------

$taskForm.onsubmit = async (e) => {
  e.preventDefault();
  if (!currentChatId) return;
  const message = $taskInput.value.trim();
  if (!message) return;

  const res = await fetch(`/api/chats/${currentChatId}/task`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (res.status === 409) {
    $taskError.textContent = (await res.json()).detail;
    $taskError.classList.remove("hidden");
    setTimeout(() => $taskError.classList.add("hidden"), 4000);
    return;
  }
  addUserMessage(message);
  $taskInput.value = "";
  setRunning(true);
};

$cancelBtn.onclick = async () => {
  if (!currentChatId) return;
  await fetch(`/api/chats/${currentChatId}/cancel`, { method: "POST" });
};

// --- SSE ----------------------------------------------------------------

function connect() {
  if (!currentChatId) return;
  const chatId = currentChatId;
  const source = new EventSource(`/api/chats/${chatId}/events`);
  source.onmessage = (msg) => {
    if (chatId !== currentChatId) return; // a stale connection from a chat we've since left
    try {
      handleEvent(JSON.parse(msg.data));
    } catch {
      /* keep-alive comment lines never reach onmessage; ignore anything else malformed */
    }
  };
  source.onerror = () => {
    // EventSource retries on its own; nothing to do here.
  };
  eventSource = source;
}

// --- boot -----------------------------------------------------------------

async function boot() {
  const user = await checkAuth();
  if (!user) {
    $appScreen.classList.add("hidden");
    $authScreen.classList.remove("hidden");
    return;
  }
  $whoami.textContent = user.username;
  $authScreen.classList.add("hidden");
  $appScreen.classList.remove("hidden");
  await loadProjects();
}

boot();
