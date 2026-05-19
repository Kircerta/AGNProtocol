function escapeHtml(input) {
  return String(input)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function pill(text) {
  return `<span class="pill">${escapeHtml(text || "")}</span>`;
}

export function renderOverview(overview, container) {
  if (!overview) {
    container.innerHTML = '<span class="chip">No overview yet</span>';
    return;
  }
  const states = overview.task_counts_by_state || {};
  const queues = overview.queue_counts || {};
  const watchdog = overview.watchdog_summary || {};
  const chunks = [
    `<span class="chip">states: ${escapeHtml(JSON.stringify(states))}</span>`,
    `<span class="chip">queues: ${escapeHtml(JSON.stringify(queues))}</span>`,
    `<span class="chip">watchdog: running=${escapeHtml(watchdog.running_count || 0)} stale=${escapeHtml(watchdog.stale_running_count || 0)}</span>`,
    `<span class="chip">last tick: ${escapeHtml(overview.last_tick_utc || "n/a")}</span>`,
  ];
  container.innerHTML = chunks.join("");
}

export function renderTaskList(tasks, selectedTaskId, container) {
  if (!tasks.length) {
    container.innerHTML = '<div class="muted">No tasks found.</div>';
    return;
  }
  container.innerHTML = tasks
    .map((task) => {
      const active = task.id === selectedTaskId ? "active" : "";
      return `
        <article class="task-item ${active}" data-task-id="${escapeHtml(task.id)}">
          <div><strong>${escapeHtml(task.id)}</strong></div>
          <div class="task-id">trace: ${escapeHtml(task.trace_id || "")}</div>
          <div>${pill(task.checkpoint_state || "CREATED")} ${pill(task.status || "pending")}</div>
          <div class="muted small">source=${escapeHtml(task.source || "manual")} risk=${escapeHtml(task.risk_level || "low")} workflow=${escapeHtml(task.workflow_kind || "standard")}</div>
        </article>
      `;
    })
    .join("");
}

export function renderTaskHeader(task, checkpoint, container) {
  if (!task) {
    container.innerHTML = '<div class="muted">Select a task.</div>';
    return;
  }
  container.innerHTML = `
    <div class="kv"><span class="k">Task ID</span><span>${escapeHtml(task.id)}</span></div>
    <div class="kv"><span class="k">Trace ID</span><span>${escapeHtml(task.trace_id || "")}</span></div>
    <div class="kv"><span class="k">Status</span><span>${escapeHtml(task.status || "pending")}</span></div>
    <div class="kv"><span class="k">Checkpoint</span><span>${escapeHtml(task.checkpoint_state || checkpoint?.state || "CREATED")}</span></div>
    <div class="kv"><span class="k">Workflow</span><span>${escapeHtml(task.workflow_kind || "standard")}</span></div>
    <div class="kv"><span class="k">Research Phase</span><span>${escapeHtml(task.research_phase || checkpoint?.research_phase || "")}</span></div>
    <div class="kv"><span class="k">Source</span><span>${escapeHtml(task.source || "manual")}</span></div>
    <div class="kv"><span class="k">Updated</span><span>${escapeHtml(task.updated_at || "")}</span></div>
  `;
}

export function renderCheckpoint(checkpoint, container) {
  container.textContent = JSON.stringify(checkpoint || {}, null, 2);
}

function rowTemplate(title, subtitle, body = "") {
  return `
    <article class="row">
      <div class="row-head">
        <strong>${escapeHtml(title)}</strong>
        <span class="muted small">${escapeHtml(subtitle)}</span>
      </div>
      <div class="small">${body}</div>
    </article>
  `;
}

export function renderTimeline(events, container) {
  if (!events.length) {
    container.innerHTML = '<div class="muted">No timeline events.</div>';
    return;
  }
  container.innerHTML = events
    .map((event) => rowTemplate(
      `${event.event_type || "EVENT"}${event.action_id ? ` · ${event.action_id}` : ""}`,
      `${event.ts || ""}`,
      `<pre class="code">${escapeHtml(JSON.stringify(event.payload || {}, null, 2))}</pre>`,
    ))
    .join("");
}

export function renderPendingActions(actions, container) {
  if (!actions.length) {
    container.innerHTML = '<div class="muted">No pending actions.</div>';
    return;
  }
  container.innerHTML = actions
    .map((action) => rowTemplate(
      `${action.action_type || ""} · ${action.action_id || ""}`,
      `schema=${action.schema_valid ? "ok" : "bad"}`,
      `${pill(action.state_hint || "")}${action.created_at ? ` <span class="muted">${escapeHtml(action.created_at)}</span>` : ""}`,
    ))
    .join("");
}

export function renderTracePanel(task, checkpoint, timeline, messages, container) {
  if (!task || !checkpoint) {
    container.innerHTML = '<div class="muted">No trace summary yet.</div>';
    return;
  }
  const round = checkpoint.round || task.round || 0;
  const eventEntry = `/api/agn/v1/tasks/${encodeURIComponent(task.id)}/timeline`;
  const messageEntry = `/api/agn/v1/tasks/${encodeURIComponent(task.id)}/messages`;
  const refs = [
    ["experiment_ref", checkpoint.experiment_ref || ""],
    ["review_verdict_ref", checkpoint.review_verdict_ref || ""],
    ["paper_ref", checkpoint.paper_ref || ""],
    ["failure_note_ref", checkpoint.failure_note_ref || ""],
    ["archive_ref", checkpoint.archive_ref || task.archive_ref || ""],
  ].filter(([, value]) => String(value || "").trim().startsWith("agn://"));

  const refRows = refs.length
    ? refs
      .map(([label, value]) => (
        `<div class="small"><span class="muted">${escapeHtml(label)}</span> ` +
        `<button data-open-ref="${escapeHtml(String(value))}">Open</button> ` +
        `<span class="ref">${escapeHtml(String(value))}</span></div>`
      ))
      .join("")
    : '<div class="muted small">No artifact refs yet.</div>';

  container.innerHTML = [
    rowTemplate(
      "Research State",
      `${task.task_kind || task.workflow_kind || "task"} · ${task.checkpoint_state || checkpoint.state || "CREATED"}`,
      [
        `<div>${pill(`phase=${checkpoint.research_phase || task.research_phase || "n/a"}`)} ${pill(`round=${round}`)} ${pill(`proposal=${checkpoint.proposal_state || task.proposal_state || "n/a"}`)}</div>`,
        `<div class="small">rejected=${escapeHtml(Boolean(checkpoint.rejected))} third_round=${escapeHtml(Boolean(checkpoint.entered_third_round))} degraded=${escapeHtml(Boolean(checkpoint.degraded))}</div>`,
        `<div class="small">result=${escapeHtml(checkpoint.research_status || task.research_status || "n/a")} verdict=${escapeHtml((checkpoint.final_review && checkpoint.final_review.decision) || "n/a")}</div>`,
      ].join(""),
    ),
    rowTemplate(
      "Raw Trace Entry",
      `messages=${messages.length || 0} events=${timeline.length || 0}`,
      `<div class="small">messages: <span class="ref">${escapeHtml(messageEntry)}</span></div>
       <div class="small">timeline: <span class="ref">${escapeHtml(eventEntry)}</span></div>`,
    ),
    rowTemplate("Artifacts", "", refRows),
  ].join("");
}

export function renderMessages(messages, container) {
  if (!messages.length) {
    container.innerHTML = '<div class="muted">No raw messages indexed.</div>';
    return;
  }
  container.innerHTML = messages
    .map((message) => rowTemplate(
      `${message.role || message.actor || "actor"} @ ${message.surface || "surface"} · ${message.kind || "message"}`,
      `${message.ts || ""} · round=${message.round || 0} · attempt=${message.attempt || 0}`,
      `<div>${escapeHtml(message.preview || "")}</div>
       <div class="small muted">event=${escapeHtml(message.event_id || "")} chars=${escapeHtml(message.packet_chars || 0)} sha256=${escapeHtml((message.sha256 || "").slice(0, 12))}</div>
       <div class="small muted">corr=${escapeHtml(message.correlation_id || "")}</div>
       <div class="small"><button data-open-ref="${escapeHtml(message.message_ref || "")}">Open Raw</button></div>`,
    ))
    .join("");
}

export function renderTraceEvents(events, container) {
  if (!events.length) {
    container.innerHTML = '<div class="muted">No trace events.</div>';
    return;
  }
  container.innerHTML = events
    .map((event) => rowTemplate(
      `${event.event_type || ""}`,
      `${event.ts || ""}`,
      `<div>${escapeHtml(event.payload_preview || "")}</div>${Array.isArray(event.refs) && event.refs.length ? `<div class="small muted">refs: ${escapeHtml(event.refs.join(", "))}</div>` : ""}`,
    ))
    .join("");
}

export function renderCommandRequests(requests, container) {
  if (!requests.length) {
    container.innerHTML = '<div class="muted">No pending command requests.</div>';
    return;
  }
  container.innerHTML = requests
    .map((item) => rowTemplate(
      `${item.request_id || item.id || "request"}`,
      `${item.task_id || ""}`,
      `<div class="small">${escapeHtml(item.command || item.cmd || "")}</div>
       <div class="small">
         <button data-cr-action="approve" data-cr-id="${escapeHtml(item.request_id || item.id || "")}">Approve</button>
         <button data-cr-action="reject" data-cr-id="${escapeHtml(item.request_id || item.id || "")}">Reject</button>
       </div>`,
    ))
    .join("");
}

export function renderRefResult(payload, container) {
  if (!payload) {
    container.textContent = "No ref selected.";
    return;
  }
  container.textContent = JSON.stringify(payload, null, 2);
}

export function renderControlsList(rows, container) {
  if (!rows.length) {
    container.innerHTML = '<div class="muted">No control history.</div>';
    return;
  }
  container.innerHTML = rows
    .map((row) => rowTemplate(
      `${row.type || ""} · ${row.control_id || ""}`,
      `${row.status || "pending"}`,
      `${escapeHtml(row.created_at || "")}`,
    ))
    .join("");
}
