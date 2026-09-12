// The live-view dashboard's only script. No build step, no framework —
// this is a dev-facing observability tool, not a production client (see
// server/__init__.py for what's deliberately not built yet: approvals,
// diff view, a task board).

const $log = document.getElementById("log");
const $planList = document.getElementById("plan-list");
const $costPanel = document.getElementById("cost-panel");
const $skillsList = document.getElementById("skills-list");
const $toolsPanel = document.getElementById("tools-panel");
const $fileList = document.getElementById("file-list");
const $breadcrumb = document.getElementById("file-breadcrumb");
const $badgeLive = document.getElementById("badge-live");
const $badgeRow = document.getElementById("badge-row");
const $taskForm = document.getElementById("task-form");
const $taskInput = document.getElementById("task-input");
const $sendBtn = document.getElementById("send-btn");
const $cancelBtn = document.getElementById("cancel-btn");
const $modal = document.getElementById("file-modal");
const $modalPath = document.getElementById("modal-path");
const $modalContent = document.getElementById("modal-content");
const $modalClose = document.getElementById("modal-close");

let currentDir = ".";
let openToolEntry = null; // the DOM node for the most recent unresolved tool_started
let streamingText = null; // the DOM node currently accumulating text_delta chunks

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
}

function setRunning(running) {
  $sendBtn.disabled = running;
  $taskInput.disabled = running;
  $cancelBtn.disabled = !running;
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
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
  const res = await fetch("/api/state");
  renderState(await res.json());
}

async function loadHistory() {
  const res = await fetch("/api/log");
  for (const ev of await res.json()) handleEvent(ev);
}

async function refreshFiles(path) {
  currentDir = path;
  const res = await fetch("/api/files?path=" + encodeURIComponent(path));
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
  const res = await fetch("/api/file_content?path=" + encodeURIComponent(path));
  const data = await res.json();
  $modalPath.textContent = path + (data.truncated ? "  (truncated)" : "");
  $modalContent.textContent = data.binary ? "(binary file — not previewed)" : data.content;
  $modal.classList.remove("hidden");
}

$modalClose.onclick = () => $modal.classList.add("hidden");
$modal.onclick = (e) => { if (e.target === $modal) $modal.classList.add("hidden"); };

// --- task form ----------------------------------------------------------

$taskForm.onsubmit = async (e) => {
  e.preventDefault();
  const message = $taskInput.value.trim();
  if (!message) return;

  const res = await fetch("/api/task", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (res.status === 409) {
    alert((await res.json()).detail);
    return;
  }
  addUserMessage(message);
  $taskInput.value = "";
  setRunning(true);
};

$cancelBtn.onclick = async () => {
  await fetch("/api/cancel", { method: "POST" });
};

// --- SSE ----------------------------------------------------------------

function connect() {
  const source = new EventSource("/api/events");
  source.onmessage = (msg) => {
    try {
      handleEvent(JSON.parse(msg.data));
    } catch {
      /* keep-alive comment lines never reach onmessage; ignore anything else malformed */
    }
  };
  source.onerror = () => {
    // EventSource retries on its own; nothing to do here.
  };
}

// --- boot -----------------------------------------------------------------

(async function init() {
  await refreshState();
  await loadHistory();
  await refreshFiles(".");
  connect();
})();
