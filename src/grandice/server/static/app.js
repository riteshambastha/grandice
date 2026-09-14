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
const $planPanel = document.getElementById("plan-panel");
const $planList = document.getElementById("plan-list");
const $costPanel = document.getElementById("cost-panel");
const $skillsList = document.getElementById("skills-list");
const $skillsSummary = document.getElementById("skills-summary");
const $toolsPanel = document.getElementById("tools-panel");
const $toolsSummary = document.getElementById("tools-summary");
const $tasksPanel = document.getElementById("tasks-panel");
const $tasksList = document.getElementById("tasks-list");
const $fileList = document.getElementById("file-list");
const $breadcrumb = document.getElementById("file-breadcrumb");
const $badgeLive = document.getElementById("badge-live");
const $envPanel = document.getElementById("env-panel");
const $detailsToggle = document.getElementById("details-toggle");
const $detailsPanel = document.getElementById("details-panel");
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

const $modelSelect = document.getElementById("model-select");
const $fileInput = document.getElementById("file-input");
const $attachBtn = document.getElementById("attach-btn");
const $micBtn = document.getElementById("mic-btn");
const $attachmentChips = document.getElementById("attachment-chips");
const $dropOverlay = document.getElementById("drop-overlay");

let authMode = "login"; // "login" | "register"
let projects = [];
let chats = [];
let currentProjectId = null;
let currentChatId = null;
let currentDir = ".";
let openToolEntry = null; // the DOM node for the most recent unresolved tool_started
let streamingText = null; // the DOM node currently accumulating text_delta chunks
let streamingRawText = ""; // the raw markdown backing streamingText — rendered to HTML on every delta
let approvalQueue = []; // {request_id, payload} — shown one at a time, oldest first
let eventSource = null; // the current chat's SSE connection; replaced on every chat switch
let pendingAttachments = []; // [{path, name}] — uploaded, waiting to be sent with the next message
let recognizing = false; // whether the mic is actively listening
let lastState = null; // the most recent /state response, so the model selector can show the real default

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

// A short, then-absolute-once-old timestamp — same shape as most chat UIs
// (a few minutes/hours as "Nm/Nh ago", then a plain date once it's not
// today's activity anymore). The exact moment is still one hover away via
// the element's own `title` attribute, set alongside this in renderChatList.
function formatChatTimestamp(unixSeconds) {
  const date = new Date(unixSeconds * 1000);
  const diffMin = (Date.now() - date.getTime()) / 60000;
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${Math.floor(diffMin)}m ago`;
  if (diffMin < 24 * 60) return `${Math.floor(diffMin / 60)}h ago`;
  if (diffMin < 7 * 24 * 60) return `${Math.floor(diffMin / (24 * 60))}d ago`;
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: sameYear ? undefined : "numeric" });
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
    li.className = "nav-item chat-item" + (c.id === currentChatId ? " active" : "");
    li.innerHTML = `
      <div class="nav-item-main" title="Double-click to rename">
        <span class="nav-item-label">${escapeHtml(c.title)}</span>
        <span class="nav-item-meta" title="${escapeHtml(new Date(c.updated_at * 1000).toLocaleString())}">${formatChatTimestamp(c.updated_at)}</span>
      </div>
      <button class="nav-item-delete" title="Delete chat">×</button>`;
    li.querySelector(".nav-item-main").onclick = () => selectChat(c.id);
    li.querySelector(".nav-item-main").ondblclick = async () => {
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

// Re-fetches just the chat list's own data (a title from auto-titling, a
// bumped last-activity timestamp) without touching the selection or
// disturbing the live connection the way loadChats' selectChat(...) call
// would — this runs on every `state_changed` event, i.e. potentially
// mid-stream on the very SSE connection that's delivering it.
async function refreshChatList() {
  if (!currentProjectId) return;
  const res = await fetch(`/api/projects/${currentProjectId}/chats`);
  chats = await res.json();
  renderChatList();
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
  stopListening();

  if (!chatId) {
    $noChatNotice.classList.remove("hidden");
    setRunning(false);
    $taskInput.disabled = true;
    $sendBtn.disabled = true;
    $modelSelect.disabled = true;
    return;
  }
  $noChatNotice.classList.add("hidden");
  $taskInput.disabled = false;

  await refreshState();
  await refreshFiles(".");
  await loadModels();
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
  streamingRawText = "";
  openToolEntry = null;
  approvalQueue = [];
  $approvalModal.classList.add("hidden");
  $planList.innerHTML = "";
  $planPanel.classList.add("hidden");
  $costPanel.innerHTML = "";
  $skillsList.innerHTML = "";
  $skillsSummary.textContent = "";
  $toolsPanel.innerHTML = "";
  $toolsSummary.textContent = "";
  $tasksList.innerHTML = "";
  $tasksPanel.classList.add("hidden");
  $fileList.innerHTML = "";
  $breadcrumb.innerHTML = "";
  currentDir = ".";
  pendingAttachments = [];
  renderAttachmentChips();
  $modelSelect.innerHTML = '<option value="">model…</option>';
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
  lastState = state;
  $badgeLive.textContent = state.live ? "live" : "stub";
  $badgeLive.className = "badge " + (state.live ? "live" : "stub");

  $envPanel.innerHTML = "";
  for (const text of [state.model.orchestrator, state.sandbox]) {
    const span = document.createElement("span");
    span.className = "badge";
    span.textContent = text;
    $envPanel.appendChild(span);
  }

  setRunning(state.running);

  $planPanel.classList.toggle("hidden", !state.plan.length);
  $planList.innerHTML = "";
  for (const item of state.plan) {
    const li = document.createElement("li");
    li.className = item.status;
    li.innerHTML = `<span class="plan-mark">${planMark(item.status)}</span><span>${escapeHtml(item.task)}</span>`;
    $planList.appendChild(li);
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

  $skillsSummary.textContent = state.skills.length ? `${state.skills.length} installed` : "none installed";
  $skillsList.innerHTML = state.skills.length
    ? state.skills.map(s => `<li><span class="skill-name">${escapeHtml(s.name)}</span><span class="skill-desc">${escapeHtml(s.description)}</span></li>`).join("")
    : '<li class="muted">None installed.</li>';

  const connectorsLine = state.connectors.length
    ? `Connectors: ${state.connectors.join(", ")}`
    : "Connectors: none configured";
  const latentLine = state.tools.latent.length
    ? `Latent (via search_tools): ${state.tools.latent.join(", ")}`
    : "No latent tools waiting to be activated.";
  $toolsSummary.textContent = `${state.tools.active.length} active`;
  $toolsPanel.innerHTML = `
    <div class="muted">${escapeHtml(connectorsLine)}</div>
    <div style="margin-top:6px">${escapeHtml(state.tools.active.join(", "))}</div>
    <div class="muted" style="margin-top:6px">${escapeHtml(latentLine)}</div>`;

  $tasksPanel.classList.toggle("hidden", !state.tasks.length);
  $tasksList.innerHTML = state.tasks.map(t => {
    const body = t.status === "done" ? t.result : t.status === "failed" ? t.error : null;
    return `<li>
      <div><span class="task-status ${t.status}">${t.status}</span> ${escapeHtml(t.description)}</div>
      ${body ? `<div class="task-body">${escapeHtml(body)}</div>` : ""}
    </li>`;
  }).join("");
}

function setRunning(running) {
  const hasChat = !!currentChatId;
  $sendBtn.disabled = running || !hasChat;
  $taskInput.disabled = running || !hasChat;
  $cancelBtn.disabled = !running || !hasChat;
  $attachBtn.disabled = running || !hasChat;
  $micBtn.disabled = running || !hasChat || !speechRecognitionSupported();
  $modelSelect.disabled = !hasChat;
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function renderMarkdownInto(el, rawText) {
  // marked.parse() is re-run on the whole accumulated text on every delta —
  // simplest correct approach for a streaming markdown parse (a partial
  // fence or list mid-token briefly renders oddly, then corrects itself
  // once the rest arrives, same as Claude's own chat). DOMPurify sanitizes
  // the result first: this text ultimately comes from a model, and a
  // self-hosted one is a real, more direct exception to the input-is-data
  // rule than a hosted provider's is — never trust it to emit only safe
  // HTML on its own.
  if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
    el.textContent = rawText; // vendor scripts failed to load — degrade to plain text, not a blank pane
    return;
  }
  el.innerHTML = DOMPurify.sanitize(marked.parse(rawText));
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

function addUserMessage(text, attachments) {
  const div = document.createElement("div");
  div.className = "log-entry log-user";
  div.textContent = text;
  if (attachments && attachments.length) {
    const chips = document.createElement("div");
    chips.className = "user-message-attachments";
    chips.textContent = "📎 " + attachments.map((a) => a.name).join(", ");
    div.appendChild(chips);
  }
  appendLogNode(div);
  streamingText = null;
  streamingRawText = "";
  openToolEntry = null;
}

function handleEvent(ev) {
  switch (ev.type) {
    case "text_delta": {
      if (!streamingText) {
        streamingText = document.createElement("div");
        streamingText.className = "log-entry log-text markdown-body";
        appendLogNode(streamingText);
        streamingRawText = "";
      }
      streamingRawText += ev.text;
      renderMarkdownInto(streamingText, streamingRawText);
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
      streamingRawText = "";
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
      streamingRawText = "";
      openToolEntry = null;
      setRunning(false);
      refreshState();
      refreshFiles(currentDir);
      break;
    }
    case "state_changed": {
      refreshState();
      refreshFiles(currentDir);
      refreshChatList();
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

// --- model selector ------------------------------------------------------
//
// GET /api/chats/{id}/models asks the *provider itself* what it exposes
// (falling back to the configured tiers if that fails or isn't live) —
// so this reflects whatever a given gateway actually offers, not a
// hardcoded guess. Selecting one persists as this chat's own default via
// PATCH; it stays in effect (including after a reload) until changed again.

async function loadModels() {
  if (!currentChatId) return;
  const res = await fetch(`/api/chats/${currentChatId}/models`);
  if (!res.ok) return;
  const data = await res.json();

  $modelSelect.innerHTML = "";
  for (const id of data.models) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = id;
    $modelSelect.appendChild(opt);
  }
  // No explicit override yet: show what's actually in effect (the
  // orchestrator tier's own default), not just whichever option happens to
  // come first — the dropdown should never silently misrepresent reality.
  const shown = data.current || lastState?.model?.orchestrator;
  if (shown && data.models.includes(shown)) {
    $modelSelect.value = shown;
  }
  $modelSelect.disabled = false;
}

$modelSelect.onchange = async () => {
  if (!currentChatId) return;
  await fetch(`/api/chats/${currentChatId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: $modelSelect.value }),
  });
};

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

// --- attachments -----------------------------------------------------------
//
// A file is uploaded immediately on selection (not deferred to send-time) —
// it lands in the chat's own workspace under uploads/, exactly like any
// other workspace file, so `read`/`glob` can reach it the same way. Only
// its path is kept client-side (in pendingAttachments) until the message
// is actually sent.

$attachBtn.onclick = () => $fileInput.click();

$fileInput.onchange = async () => {
  const files = [...$fileInput.files];
  $fileInput.value = ""; // so picking the same file again still fires onchange
  for (const file of files) await uploadFile(file);
};

async function uploadFile(file) {
  if (!currentChatId) return;
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`/api/chats/${currentChatId}/upload`, { method: "POST", body: formData });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    $taskError.textContent = body.detail || `Could not upload ${file.name}.`;
    $taskError.classList.remove("hidden");
    setTimeout(() => $taskError.classList.add("hidden"), 4000);
    return;
  }
  const uploaded = await res.json();
  pendingAttachments.push(uploaded);
  renderAttachmentChips();
}

function renderAttachmentChips() {
  $attachmentChips.classList.toggle("hidden", pendingAttachments.length === 0);
  $attachmentChips.innerHTML = pendingAttachments
    .map(
      (a, i) => `<span class="attachment-chip">📎 ${escapeHtml(a.name)}<button type="button" data-i="${i}" title="Remove">×</button></span>`
    )
    .join("");
  for (const btn of $attachmentChips.querySelectorAll("button")) {
    btn.onclick = () => {
      pendingAttachments.splice(Number(btn.dataset.i), 1);
      renderAttachmentChips();
    };
  }
}

// Drag-and-drop onto the whole log panel, not just the input — matches
// the common "drop it anywhere in the chat" pattern rather than a small
// fixed target.
const $logPanel = $log.closest(".log-panel");
let dragDepth = 0; // dragenter/dragleave fire on every child crossed, not just the container

$logPanel.addEventListener("dragenter", (e) => {
  e.preventDefault();
  if (!currentChatId) return;
  dragDepth++;
  $dropOverlay.classList.remove("hidden");
});
$logPanel.addEventListener("dragover", (e) => e.preventDefault());
$logPanel.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) $dropOverlay.classList.add("hidden");
});
$logPanel.addEventListener("drop", async (e) => {
  e.preventDefault();
  dragDepth = 0;
  $dropOverlay.classList.add("hidden");
  if (!currentChatId) return;
  for (const file of [...e.dataTransfer.files]) await uploadFile(file);
});

// --- voice input -----------------------------------------------------------
//
// The browser's own SpeechRecognition API — no server round-trip, no model
// call. Chrome/Edge support it (as the vendor-prefixed webkitSpeechRecognition);
// Firefox and Safari currently don't, so the mic button just disables itself
// with an explanatory title rather than pretending to work.

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
let speechRecognition = null;

function speechRecognitionSupported() {
  return !!SpeechRecognitionImpl;
}

if (!speechRecognitionSupported()) {
  $micBtn.title = "Voice input isn't supported in this browser (try Chrome or Edge).";
}

function stopListening() {
  if (speechRecognition) speechRecognition.stop();
}

$micBtn.onclick = () => {
  if (!speechRecognitionSupported() || !currentChatId) return;
  if (recognizing) {
    stopListening();
    return;
  }
  speechRecognition = new SpeechRecognitionImpl();
  speechRecognition.continuous = true;
  speechRecognition.interimResults = true;
  speechRecognition.lang = navigator.language || "en-US";

  let finalText = $taskInput.value ? $taskInput.value + " " : "";

  speechRecognition.onstart = () => {
    recognizing = true;
    $micBtn.classList.add("recording");
  };
  speechRecognition.onresult = (event) => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) finalText += transcript + " ";
      else interim += transcript;
    }
    $taskInput.value = finalText + interim;
  };
  speechRecognition.onerror = () => stopListening();
  speechRecognition.onend = () => {
    recognizing = false;
    $micBtn.classList.remove("recording");
  };
  speechRecognition.start();
};

// --- task form ----------------------------------------------------------

$taskForm.onsubmit = async (e) => {
  e.preventDefault();
  if (!currentChatId) return;
  const message = $taskInput.value.trim();
  if (!message) return;

  stopListening();
  const attachments = pendingAttachments;

  const res = await fetch(`/api/chats/${currentChatId}/task`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, attachments: attachments.map((a) => a.path) }),
  });
  if (res.status === 409) {
    $taskError.textContent = (await res.json()).detail;
    $taskError.classList.remove("hidden");
    setTimeout(() => $taskError.classList.add("hidden"), 4000);
    return;
  }
  addUserMessage(message, attachments);
  $taskInput.value = "";
  pendingAttachments = [];
  renderAttachmentChips();
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

// --- details panel (dev-facing detail, opt-in via the topbar toggle) ------

// A plain end user never needs to see cost/skills/tools telemetry; this
// stays out of the way until asked for, and remembers the choice per
// browser so it doesn't have to be re-opened on every reload.
const DETAILS_OPEN_KEY = "grandice.detailsOpen";

function setDetailsOpen(open) {
  $detailsPanel.classList.toggle("hidden", !open);
  $detailsToggle.classList.toggle("active", open);
  try {
    localStorage.setItem(DETAILS_OPEN_KEY, open ? "1" : "0");
  } catch {
    /* private browsing / blocked storage — the toggle still works this session */
  }
}

$detailsToggle.onclick = () => setDetailsOpen($detailsPanel.classList.contains("hidden"));

let detailsOpenDefault = false;
try {
  detailsOpenDefault = localStorage.getItem(DETAILS_OPEN_KEY) === "1";
} catch {
  /* ignore — default closed */
}
setDetailsOpen(detailsOpenDefault);

for (const header of document.querySelectorAll(".collapsible-header")) {
  const target = document.getElementById(header.dataset.target);
  header.onclick = () => {
    target.classList.toggle("hidden");
    header.classList.toggle("expanded", !target.classList.contains("hidden"));
  };
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
