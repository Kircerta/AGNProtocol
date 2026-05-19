const tauriCore = window.__TAURI__?.core;

const appState = {
  realtime: true,
  paused: false,
  selectedConversationId: "",
  selectedMessageId: "",
  lastPayload: null,
  pollHandle: null,
};

function encodeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 2200);
}

async function invoke(command, args = {}) {
  if (!tauriCore?.invoke) {
    throw new Error("Tauri API is unavailable. Run this UI inside the Tauri shell.");
  }
  return tauriCore.invoke(command, args);
}

function formatTs(ts) {
  if (!ts) return "unknown";
  const parsed = new Date(ts);
  if (Number.isNaN(parsed.getTime())) return ts;
  return parsed.toLocaleString();
}

function currentFilters() {
  const fromValue = document.getElementById("from-input").value;
  const toValue = document.getElementById("to-input").value;
  return {
    search: document.getElementById("search-input").value.trim(),
    participant: document.getElementById("participant-input").value.trim(),
    trace_id: document.getElementById("trace-input").value.trim(),
    task_id: document.getElementById("task-input").value.trim(),
    topic: document.getElementById("topic-input").value.trim(),
    subsystem: document.getElementById("subsystem-select").value,
    from_ts: fromValue ? new Date(fromValue).toISOString() : "",
    to_ts: toValue ? new Date(toValue).toISOString() : "",
    conversation_id: appState.selectedConversationId,
  };
}

async function refreshMonitor() {
  try {
    const payload = await invoke("load_monitor_state", { filters: currentFilters() });
    appState.lastPayload = payload;
    hydrateControls(payload.controls || {});
    renderSummary(payload);
    renderConversationList(payload.conversations || []);

    if (!appState.selectedConversationId && payload.selected_conversation?.id) {
      appState.selectedConversationId = payload.selected_conversation.id;
    }
    if (!payload.transcript?.some((item) => item.id === appState.selectedMessageId)) {
      appState.selectedMessageId = payload.transcript?.[0]?.id || "";
    }

    renderTranscript(payload.transcript || []);
    renderInspector(payload.selected_conversation || null, payload.transcript || [], payload.issues || []);
  } catch (error) {
    showToast(`Monitor refresh failed: ${error}`);
  }
}

function hydrateControls(controls) {
  const select = document.getElementById("subsystem-select");
  const previous = select.value;
  const subsystems = controls.subsystems || [];
  select.innerHTML = `<option value="">All Subsystems</option>${subsystems
    .map((item) => `<option value="${encodeHtml(item)}">${encodeHtml(item)}</option>`)
    .join("")}`;
  select.value = subsystems.includes(previous) ? previous : "";
}

function renderSummary(payload) {
  const summary = payload.summary || {};
  document.getElementById("summary-strip").innerHTML = [
    `Source: <strong>${encodeHtml(payload.source_mode || "unknown")}</strong>`,
    `Messages: <strong>${summary.message_count || 0}</strong>`,
    `Conversations: <strong>${summary.conversation_count || 0}</strong>`,
    `Event type: <strong>${encodeHtml(summary.supported_event_type || "unknown")}</strong>`,
    `Subsystem scope: <strong>${encodeHtml((summary.subsystem_scope || []).join(", ") || "-")}</strong>`,
    `<span class="mini">Realtime: ${appState.realtime ? "on" : "off"} / Stream: ${appState.paused ? "paused" : "running"}</span>`,
  ].join(" &nbsp;&nbsp; ");
}

function renderConversationList(conversations) {
  const list = document.getElementById("conversation-list");
  document.getElementById("conversation-count").textContent = `${conversations.length} visible`;
  if (!conversations.length) {
    list.innerHTML = `<div class="empty">No conversations matched the current filters.</div>`;
    return;
  }
  list.innerHTML = conversations
    .map((item) => {
      const active = item.id === appState.selectedConversationId ? "active" : "";
      const issues = item.unresolved_count ? `<span class="pill warn">${item.unresolved_count} unresolved</span>` : "";
      return `
        <article class="conversation-card ${active}" data-conversation-id="${encodeHtml(item.id)}">
          <div class="conversation-top">
            <div class="conversation-id">${encodeHtml(item.trace_id || item.id)}</div>
            ${issues}
          </div>
          <div class="participants">${encodeHtml((item.participants || []).join(" • ") || "participants unknown")}</div>
          <div class="conversation-meta">task: ${encodeHtml((item.task_ids || []).join(", ") || "-")}</div>
          <div class="conversation-meta">topic: ${encodeHtml((item.topic_ids || []).join(", ") || "-")}</div>
          <div class="conversation-meta">last: ${encodeHtml(item.last_sender || "-")} · ${encodeHtml(formatTs(item.last_ts))}</div>
          <div class="conversation-meta">messages: ${item.message_count || 0}</div>
        </article>
      `;
    })
    .join("");

  list.querySelectorAll("[data-conversation-id]").forEach((node) => {
    node.addEventListener("click", () => {
      appState.selectedConversationId = node.dataset.conversationId;
      refreshMonitor();
    });
  });
}

function renderTranscript(messages) {
  const view = document.getElementById("transcript-view");
  const meta = document.getElementById("transcript-meta");
  meta.textContent = `${messages.length} message${messages.length === 1 ? "" : "s"}`;
  if (!messages.length) {
    view.innerHTML = `<div class="empty">Select a conversation to inspect its transcript.</div>`;
    return;
  }
  view.innerHTML = messages
    .map((message) => {
      const active = message.id === appState.selectedMessageId ? "active" : "";
      const targets = (message.targets || []).length ? `→ ${(message.targets || []).join(", ")}` : "→ unresolved";
      const issues = message.parse_status !== "ok" ? `<span class="pill warn">${encodeHtml(message.parse_status)}</span>` : "";
      const sections = (message.sections || [])
        .map(
          (section) => `
            <section class="message-section">
              <span class="message-section-label">${encodeHtml(section.label)}</span>
              <div class="message-section-value">${encodeHtml(section.value)}</div>
            </section>
          `
        )
        .join("");
      return `
        <article class="message-card ${active}" data-message-id="${encodeHtml(message.id)}">
          <div class="message-top">
            <div>
              <div class="sender-line">
                <span class="sender-badge">${encodeHtml(message.sender)}</span>
                <span class="mini">${encodeHtml(targets)}</span>
                <span class="mini">${encodeHtml(message.kind || "unknown")}</span>
                ${issues}
              </div>
              <div class="mini">task: ${encodeHtml(message.task_id)} · trace: ${encodeHtml(message.trace_id)}</div>
            </div>
            <div class="message-meta">
              <div>${encodeHtml(formatTs(message.ts))}</div>
              <div>round ${message.round || 0} · ${encodeHtml(message.surface || "unknown")}</div>
            </div>
          </div>
          <div class="message-sections">${sections}</div>
        </article>
      `;
    })
    .join("");

  view.querySelectorAll("[data-message-id]").forEach((node) => {
    node.addEventListener("click", () => {
      appState.selectedMessageId = node.dataset.messageId;
      renderInspector(appState.lastPayload?.selected_conversation || null, messages, appState.lastPayload?.issues || []);
      renderTranscript(messages);
    });
  });
}

function renderInspector(conversation, messages, issues) {
  const conversationNode = document.getElementById("conversation-inspector");
  const envelopeNode = document.getElementById("message-envelope");
  const bodyNode = document.getElementById("message-body");
  const issuesNode = document.getElementById("issues-list");
  const selected = messages.find((item) => item.id === appState.selectedMessageId) || messages[0] || null;

  if (!conversation) {
    conversationNode.innerHTML = `<div class="empty">No conversation selected.</div>`;
  } else {
    conversationNode.innerHTML = `
      <dl class="metadata-grid">
        <dt>Conversation</dt><dd>${encodeHtml(conversation.id || "-")}</dd>
        <dt>Trace</dt><dd>${encodeHtml(conversation.trace_id || "-")}</dd>
        <dt>Tasks</dt><dd>${encodeHtml((conversation.task_ids || []).join("\n") || "-")}</dd>
        <dt>Participants</dt><dd>${encodeHtml((conversation.participants || []).join("\n") || "-")}</dd>
        <dt>Subsystem</dt><dd>${encodeHtml(conversation.subsystem || "-")}</dd>
        <dt>Topics</dt><dd>${encodeHtml((conversation.topic_ids || []).join("\n") || "-")}</dd>
        <dt>Sources</dt><dd>${encodeHtml((conversation.source_refs || []).join("\n") || "-")}</dd>
      </dl>
    `;
  }

  if (!selected) {
    envelopeNode.textContent = "No message selected.";
    bodyNode.textContent = "";
  } else {
    envelopeNode.textContent = JSON.stringify(selected.raw_envelope || {}, null, 2);
    bodyNode.textContent = selected.raw_body || "";
  }

  if (!issues.length) {
    issuesNode.innerHTML = `<div class="empty">No source issues are currently recorded.</div>`;
    return;
  }
  issuesNode.innerHTML = issues
    .map(
      (issue) => `
        <article class="issue-card">
          <div><strong>${encodeHtml(issue.kind || "issue")}</strong></div>
          <div>${encodeHtml(issue.source || "")}</div>
          <div>${encodeHtml(issue.detail || "")}</div>
        </article>
      `
    )
    .join("");
}

function syncPolling() {
  if (appState.pollHandle) {
    window.clearInterval(appState.pollHandle);
    appState.pollHandle = null;
  }
  if (!appState.realtime || appState.paused) {
    return;
  }
  appState.pollHandle = window.setInterval(() => {
    refreshMonitor();
  }, 2500);
}

document.getElementById("refresh-button").addEventListener("click", () => {
  refreshMonitor();
});

document.getElementById("realtime-button").addEventListener("click", (event) => {
  appState.realtime = !appState.realtime;
  event.target.textContent = appState.realtime ? "Realtime On" : "Realtime Off";
  event.target.classList.toggle("active", appState.realtime);
  syncPolling();
  refreshMonitor();
});

document.getElementById("pause-button").addEventListener("click", (event) => {
  appState.paused = !appState.paused;
  event.target.textContent = appState.paused ? "Resume Stream" : "Pause Stream";
  syncPolling();
});

[
  "search-input",
  "participant-input",
  "trace-input",
  "task-input",
  "topic-input",
  "subsystem-select",
  "from-input",
  "to-input",
].forEach((id) => {
  const node = document.getElementById(id);
  const eventName = node.tagName === "SELECT" ? "change" : "input";
  node.addEventListener(eventName, () => {
    appState.selectedConversationId = "";
    appState.selectedMessageId = "";
    refreshMonitor();
  });
});

refreshMonitor();
syncPolling();
