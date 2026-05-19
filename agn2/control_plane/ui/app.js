const tauriCore = window.__TAURI__?.core;

const SURFACE_LABELS = {
  lifecycle: "Lifecycle",
  control_plane: "Control Plane",
  conversation_monitor: "Conversation Monitor",
  desktop_control: "Desktop + Ghostty",
  vision_parser: "Vision Parser",
  worker_delegate: "Worker Delegate",
  flagship_review: "Flagship Review",
  dispatcher: "Dispatcher",
  memory_recorder: "Memory Recorder",
  message_bus: "Message Bus",
};

const SURFACE_CATEGORY_LABELS = {
  authority_control: "Authority Control",
  authority_state: "Authority State",
  observation: "Observation",
  execution: "Execution",
  review: "Review",
  memory: "Memory",
  runtime: "Runtime",
};

const TOOLCHAIN_LABELS = {
  control_plane_app: "Control Plane.app",
  conversation_monitor_app: "Conversation Monitor.app",
  cargo_tauri: "cargo-tauri",
  cargo: "cargo",
  ghostty: "Ghostty",
  gui_agent: "gui-agent",
  tesseract: "Tesseract",
  sips: "sips",
  python3: "python3",
};

function formatTimestamp(value) {
  if (!value) {
    return "unknown";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return `${date.toLocaleDateString()} ${date.toLocaleTimeString()}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 2600);
}

async function invoke(command, args = {}) {
  if (!tauriCore?.invoke) {
    throw new Error("Tauri API is unavailable. Run this UI inside the Tauri shell.");
  }
  return tauriCore.invoke(command, args);
}

function renderOverview(model, capability) {
  const panel = document.getElementById("overview-panel");
  const stop = model.system_mode?.emergency_stop_active ? "stop" : "";
  const counts = model.counts || {};
  const agn1 = model.agn1_subsystem;
  const escalations = model.recent_escalations || [];
  const capabilitySummary = [
    {
      value: Object.keys(capability?.surfaces || {}).length,
      label: "Execution Surfaces",
    },
    {
      value: Object.values(capability?.provider_capabilities?.executors || {}).filter((item) => item.available).length,
      label: "Executors Ready",
    },
    {
      value: Object.values(capability?.provider_capabilities?.reviewers || {}).filter((item) => item.available).length,
      label: "Reviewers Ready",
    },
    {
      value: capability?.skills?.installed_count || 0,
      label: "Skills Loaded",
    },
  ];

  let agn1Html = "";
  if (agn1) {
    const ts = formatTimestamp(agn1.last_event_ts);
    agn1Html = `
      <div class="stat"><span class="value">${agn1.event_count || 0}</span><span class="meta">AGN1.0 Events (Last: ${escapeHtml(ts)})</span></div>
    `;
  }

  let escalationsHtml = "";
  if (escalations.length) {
    escalationsHtml = `
      <div style="margin-top: 12px; border-top: 1px dotted var(--line); padding-top: 8px;">
        <h3 style="margin: 0 0 4px; font-size: 11px; text-transform: uppercase; color: var(--danger);">Recent Escalations</h3>
        <ul style="margin: 0; padding-left: 20px; font-size: 12px;">
          ${escalations.map(e => `<li><strong>${escapeHtml(e.task_id)}</strong>: ${escapeHtml(e.event_type)} (${escapeHtml(formatTimestamp(e.ts))})</li>`).join("")}
        </ul>
      </div>
    `;
  }

  const capabilitySummaryHtml = capabilitySummary
    .map(({ value, label }) => `<div class="stat compact"><span class="value">${value}</span><span class="meta">${escapeHtml(label)}</span></div>`)
    .join("");

  panel.innerHTML = `
    <div class="row-top">
      <h2>Overview</h2>
      <span class="pill ${stop}">Mode: ${model.system_mode?.mode || "unknown"}</span>
    </div>
    <div class="stats">
      <div class="stat"><span class="value">${counts.active_tasks || 0}</span><span class="meta">Active</span></div>
      <div class="stat"><span class="value">${counts.queued_tasks || 0}</span><span class="meta">Queued</span></div>
      <div class="stat"><span class="value">${counts.blocked_tasks || 0}</span><span class="meta">Blocked</span></div>
      <div class="stat"><span class="value">${counts.policy_gate_pending || 0}</span><span class="meta">Pending Gates</span></div>
      <div class="stat"><span class="value">${counts.dead_letters || 0}</span><span class="meta">Dead Letters</span></div>
      ${agn1Html}
    </div>
    <div class="stats secondary">${capabilitySummaryHtml}</div>
    ${escalationsHtml}
  `;
}

function renderCapabilitySnapshot(model) {
  const panel = document.getElementById("capability-panel");
  if (!model) {
    panel.innerHTML = `<div class="empty">Capability snapshot is unavailable.</div>`;
    return;
  }

  const surfaces = Object.entries(model.surfaces || {});
  const executors = Object.entries(model.provider_capabilities?.executors || {}).filter(([, item]) => item.available);
  const reviewers = Object.entries(model.provider_capabilities?.reviewers || {}).filter(([, item]) => item.available);
  const tools = Object.entries(model.toolchain || {});
  const agnSkills = model.skills?.agn_specific || [];
  const guidance = model.guidance || [];
  const defaultExecutor = model.provider_capabilities?.default_executor || "unknown";
  const defaultReviewer = model.provider_capabilities?.default_reviewer || "unknown";
  const taxonomy = Object.entries(model.surface_taxonomy || {});
  const providerRoles = Object.entries(model.provider_policy?.provider_roles || {});

  const surfaceCards = surfaces
    .map(([key, item]) => `
      <article class="capability-card">
        <div class="capability-card-top">
          <strong>${escapeHtml(SURFACE_LABELS[key] || key)}</strong>
          <span class="pill ${item.available ? "" : "stop"}">${item.available ? "ready" : "offline"}</span>
        </div>
        <p>${escapeHtml(item.why || "No description available.")}</p>
        <div class="mini">category: ${escapeHtml(SURFACE_CATEGORY_LABELS[item.category] || item.category || "unknown")}</div>
        <code>${escapeHtml(item.entry || "")}</code>
      </article>
    `)
    .join("");

  const executorPills = executors
    .map(([name, item]) => `<span class="chip">${escapeHtml(name)}<span class="chip-meta">${escapeHtml(item.kind || "unknown")}</span></span>`)
    .join("");
  const reviewerPills = reviewers
    .map(([name, item]) => `<span class="chip">${escapeHtml(name)}<span class="chip-meta">${escapeHtml(item.kind || "unknown")}</span></span>`)
    .join("");
  const toolPills = tools
    .map(([name, item]) => `<span class="chip ${item.available ? "" : "chip-off"}">${escapeHtml(TOOLCHAIN_LABELS[name] || name)}</span>`)
    .join("");
  const skillPills = agnSkills
    .map((name) => `<span class="chip">${escapeHtml(name)}</span>`)
    .join("");
  const guidanceList = guidance
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
  const taxonomyHtml = taxonomy
    .map(([category, members]) => `
      <div class="meta-block">
        <div class="meta-title">${escapeHtml(SURFACE_CATEGORY_LABELS[category] || category)}</div>
        <div class="chip-row">${(members || []).map((member) => `<span class="chip">${escapeHtml(SURFACE_LABELS[member] || member)}</span>`).join("") || '<span class="mini">None</span>'}</div>
      </div>
    `)
    .join("");
  const providerRoleHtml = providerRoles
    .map(([name, item]) => `
      <div class="meta-block">
        <div class="meta-title">${escapeHtml(name)}</div>
        <div class="chip-row">
          <span class="chip">${escapeHtml(item.grade || "unknown")}</span>
          <span class="chip ${item.available ? "" : "chip-off"}">${item.available ? "available" : "offline"}</span>
        </div>
        <div class="mini" style="margin-top:8px;">${escapeHtml(item.notes || "")}</div>
      </div>
    `)
    .join("");

  panel.innerHTML = `
    <div class="panel-header capability-header">
      <div>
        <h2>Capability Snapshot</h2>
        <div class="mini">Canonical runtime view from read models. Updated ${escapeHtml(formatTimestamp(model.generated_at))}</div>
      </div>
      <div class="capability-defaults">
        <span class="chip">default exec <span class="chip-meta">${escapeHtml(defaultExecutor)}</span></span>
        <span class="chip">default review <span class="chip-meta">${escapeHtml(defaultReviewer)}</span></span>
      </div>
    </div>
    <div class="capability-body">
      <section class="capability-section">
        <div class="section-header">
          <h3>Execution Surfaces</h3>
          <span class="mini">${surfaces.length} registered</span>
        </div>
        <div class="capability-grid">${surfaceCards}</div>
      </section>
      <section class="capability-section capability-meta-section">
        <div class="section-header">
          <h3>Models, Tools, Memory</h3>
          <span class="mini">${model.skills?.installed_count || 0} total skills</span>
        </div>
        <div class="capability-stack">
          <div class="meta-block">
            <div class="meta-title">Executors</div>
            <div class="chip-row">${executorPills || '<span class="mini">None</span>'}</div>
          </div>
          <div class="meta-block">
            <div class="meta-title">Reviewers</div>
            <div class="chip-row">${reviewerPills || '<span class="mini">None</span>'}</div>
          </div>
          <div class="meta-block">
            <div class="meta-title">Toolchain</div>
            <div class="chip-row">${toolPills || '<span class="mini">None</span>'}</div>
          </div>
          <div class="meta-block">
            <div class="meta-title">AGN Skills</div>
            <div class="chip-row">${skillPills || '<span class="mini">None</span>'}</div>
          </div>
          <div class="meta-block">
            <div class="meta-title">Operating Guidance</div>
            <ul class="guidance-list">${guidanceList || "<li>No guidance available.</li>"}</ul>
          </div>
          <div class="meta-block">
            <div class="meta-title">Review Policy</div>
            <div class="chip-row">
              ${(model.provider_policy?.reviewer_policy?.preferred_order || []).map((name) => `<span class="chip">${escapeHtml(name)}</span>`).join("") || '<span class="mini">None</span>'}
            </div>
          </div>
        </div>
      </section>
      <section class="capability-section capability-meta-section">
        <div class="section-header">
          <h3>Taxonomy</h3>
          <span class="mini">role and surface boundaries</span>
        </div>
        <div class="capability-stack">
          ${taxonomyHtml}
          ${providerRoleHtml}
        </div>
      </section>
    </div>
  `;
}

function renderExecutionDiscipline(model) {
  const panel = document.getElementById("discipline-panel");
  if (!model) {
    panel.innerHTML = `<div class="empty">Execution discipline is unavailable.</div>`;
    return;
  }

  const currentTask = model.current_task || {};
  const checks = model.execution_checks || [];
  const recommendedSurfaces = model.recommended_surfaces || [];
  const regressionSignals = model.regression_signals || [];
  const nextActions = model.next_actions || [];
  const workerState = model.worker_and_review_state || {};
  const reviewPolicy = model.provider_policy?.reviewer_policy || {};

  const checkRows = checks.length
    ? checks
        .map(
          (item) => `
            <div class="check-row">
              <div class="check-row-top">
                <strong>${escapeHtml(item.check || "unknown_check")}</strong>
                <span class="pill ${item.status === "blocked" ? "stop" : item.status === "attention" ? "attention" : ""}">${escapeHtml(item.status || "unknown")}</span>
              </div>
              <div class="check-detail">${escapeHtml(item.detail || "No detail available.")}</div>
            </div>
          `
        )
        .join("")
    : `<div class="empty">No preflight execution checks found.</div>`;

  const surfaceRows = recommendedSurfaces.length
    ? recommendedSurfaces
        .map(
          (item) => `
            <div class="surface-row">
              <div class="surface-row-top">
                <strong>${escapeHtml(SURFACE_LABELS[item.surface] || item.surface || "unknown_surface")}</strong>
                <span class="pill">${escapeHtml(item.surface || "")}</span>
              </div>
              <div class="surface-detail">${escapeHtml(item.reason || "No reason provided.")}</div>
              <code class="surface-entry">${escapeHtml(item.entry || "")}</code>
            </div>
          `
        )
        .join("")
    : `<div class="empty">No recommended surfaces in the latest preflight.</div>`;

  panel.innerHTML = `
    <div class="panel-header capability-header">
      <div>
        <h2>Execution Discipline</h2>
        <div class="mini">Task-start posture from the latest structured preflight.</div>
      </div>
      <div class="capability-defaults">
        <span class="chip">status <span class="chip-meta">${escapeHtml(model.status || "unknown")}</span></span>
        <span class="chip">preflight <span class="chip-meta">${escapeHtml(formatTimestamp(model.preflight_generated_at || model.generated_at || ""))}</span></span>
      </div>
    </div>
    <div class="discipline-body">
      <section class="discipline-stack">
        <div class="discipline-grid">
          <div class="discipline-card">
            <h3>Current Task</h3>
            <p><strong>${escapeHtml(currentTask.summary || "No current preflight task.")}</strong></p>
            <p>risk=${escapeHtml(currentTask.risk_level || "-")} trace=${escapeHtml(currentTask.trace_id || "-")} task=${escapeHtml(currentTask.task_id || "-")}</p>
          </div>
          <div class="discipline-card">
            <h3>Checks</h3>
            <p>ok=${escapeHtml(model.check_counts?.ok || 0)} attention=${escapeHtml(model.check_counts?.attention || 0)} blocked=${escapeHtml(model.check_counts?.blocked || 0)}</p>
            <p>worker lanes=${escapeHtml(Object.entries(workerState).filter(([, value]) => Boolean(value)).map(([name]) => name).join(", ") || "none")}</p>
          </div>
          <div class="discipline-card">
            <h3>Review Lane</h3>
            <p>${escapeHtml((reviewPolicy.preferred_order || []).join(" -> ") || "No review order configured.")}</p>
            <p>forbidden=${escapeHtml((reviewPolicy.forbidden_for_review || []).join(", ") || "none")}</p>
          </div>
        </div>
        <div class="section-header">
          <h3>Execution Checks</h3>
          <span class="mini">${checks.length} checks</span>
        </div>
        <div class="check-list">${checkRows}</div>
      </section>
      <section class="discipline-stack">
        <div class="section-header">
          <h3>Recommended Surfaces</h3>
          <span class="mini">${recommendedSurfaces.length} selected</span>
        </div>
        <div class="surface-list">${surfaceRows}</div>
        <div class="meta-block">
          <div class="meta-title">Regression Signals</div>
          <ul class="guidance-list">${regressionSignals.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>None.</li>"}</ul>
        </div>
        <div class="meta-block">
          <div class="meta-title">Next Actions</div>
          <ul class="guidance-list">${nextActions.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>None.</li>"}</ul>
        </div>
      </section>
    </div>
  `;
}

function taskActionButtons(task) {
  const isDone = ["COMPLETED", "FAILED", "CANCELLED", "DONE"].includes(task.state);
  return [
    { cmd: "PAUSE_TASK", label: "Pause", disabled: task.paused || isDone },
    { cmd: "RESUME_TASK", label: "Resume", disabled: !task.paused || isDone },
    { cmd: "CANCEL_TASK", label: "Cancel", disabled: isDone },
    { cmd: "REPRIORITIZE_TASK", label: "Priority++", disabled: isDone },
    { cmd: "RETRY_TASK", label: "Retry", disabled: false },
    { cmd: "FORCE_ESCALATE_TASK", label: "Escalate", disabled: isDone },
  ]
    .map(
      ({ cmd, label, disabled }) =>
        `<button class="mini-button" data-command="${cmd}" data-target-type="task" data-target-id="${task.task_id}" data-reason="${label.toLowerCase()} ${task.task_id}" ${disabled ? "disabled" : ""}>${label}</button>`
    )
    .join("");
}

function renderTaskBoard(model) {
  const node = document.getElementById("task-board");
  const items = model.items || [];
  if (!items.length) {
    node.innerHTML = `<div class="empty">No task state is available yet.</div>`;
    return;
  }
  node.innerHTML = `<div class="table-list">${items
    .map(
      (task) => {
        const timeStr = new Date(task.updated_at).toLocaleTimeString() || "unknown";
        return `
        <article class="row">
          <div class="row-top">
            <div>
              <strong>${task.task_id}</strong>
              <div class="mini" style="margin-bottom: 4px;">trace: ${task.trace_id || "-"}</div>
              <p><strong>Intent:</strong> <span style="color:#2a3a37;">${task.request_summary || "No summary."}</span></p>
            </div>
            <div style="text-align: right; display:flex; flex-direction:column; gap:4px; align-items:flex-end;">
              <span class="pill">${task.state}</span>
              ${task.paused ? `<span class="pill stop">paused</span>` : ""}
              ${task.admin_hold ? `<span class="pill stop">admin_hold</span>` : ""}
              <div class="mini" style="margin-top:2px;">${timeStr}</div>
            </div>
          </div>
          <div class="mini" style="margin-top:6px; color:#6b7975;">
            risk=<span style="color:var(--ink)">${task.risk_level}</span>
            pri=<span style="color:var(--ink)">${task.priority}</span>
            rev=<span style="color:var(--ink)">${task.review_requested}</span>
            [<span style="color:var(--ink)">${task.executor_provider || "-"}</span>:<span style="color:var(--ink)">${task.reviewer_provider || "-"}</span>]
          </div>
          <div class="row-actions">${taskActionButtons(task)}</div>
        </article>
      `
      }
    )
    .join("")}</div>`;
}

function gateButtons(item) {
  const disabled = item.effective_status !== "pending";
  return [
    { cmd: "APPROVE_GATE", label: "Approve", disabled },
    { cmd: "REJECT_GATE", label: "Reject", disabled },
    { cmd: "HOLD_GATE", label: "Hold", disabled },
    { cmd: "ESCALATE_COUNCIL", label: "Council", disabled: disabled || !item.council_required },
  ]
    .map(
      ({ cmd, label, disabled }) =>
        `<button class="mini-button" data-command="${cmd}" data-target-type="gate" data-target-id="${item.gate_id}" data-reason="${label.toLowerCase()} ${item.gate_id}" ${disabled ? "disabled" : ""}>${label}</button>`
    )
    .join("");
}

function renderApprovalGate(model) {
  const node = document.getElementById("approval-gate");
  const items = model.items || [];
  if (!items.length) {
    node.innerHTML = `<div class="empty">No blocked actions are waiting for review.</div>`;
    return;
  }
  node.innerHTML = `<div class="table-list">${items
    .map(
      (item) => {
        const timeStr = new Date(item.created_at).toLocaleTimeString() || "unknown";
        return `
        <article class="row">
          <div class="row-top">
            <div>
              <strong>${item.gate_id}</strong>
              <div class="mini" style="margin-bottom: 4px;">trace: ${item.trace_id || "-"} / task: ${item.task_id || "-"}</div>
              <p><strong>Summary:</strong> <span style="color:#2a3a37;">${item.summary}</span></p>
              <p><strong>Reason:</strong> <span style="color:#2a3a37;">${item.reason}</span></p>
            </div>
            <div style="text-align: right; display:flex; flex-direction:column; gap:4px; align-items:flex-end;">
              <span class="pill ${item.effective_status !== "pending" ? "stop" : ""}">${item.effective_status}</span>
              <div class="mini" style="margin-top:2px;">${timeStr}</div>
            </div>
          </div>
          <div class="mini" style="margin-top:6px; color:#6b7975;">
            rule=<span style="color:var(--ink)">${item.policy_rule_id}</span>
            risk=<span style="color:var(--ink)">${item.risk_level}</span>
            tgt=<span style="color:var(--ink)">${item.target_kind}</span>
            council=<span style="color:var(--ink)">${item.council_required}</span>
          </div>
          <div class="row-actions">${gateButtons(item)}</div>
        </article>
      `
      }
    )
    .join("")}</div>`;
}

let rawStreamItems = [];
let rawStreamConfig = {
  search: "",
  taskId: "",
  traceId: "",
  type: "",
  descending: true
};

function renderRawStreamData() {
  const node = document.getElementById("raw-stream");
  const typeSelect = document.getElementById("raw-type");

  if (!rawStreamItems.length) {
    node.textContent = "No data.";
    return;
  }

  // Populate types if empty
  if (typeSelect.options.length <= 1) {
    const types = new Set(rawStreamItems.map(i => i.kind || i.event_type || i.event || "unknown"));
    const currentVal = typeSelect.value;
    typeSelect.innerHTML = '<option value="">All Types</option>' +
      Array.from(types).sort().map(t => `<option value="${t}">${t}</option>`).join("");
    typeSelect.value = currentVal;
  }

  let filtered = rawStreamItems.filter(item => {
    const itemStr = JSON.stringify(item).toLowerCase();

    if (rawStreamConfig.search && !itemStr.includes(rawStreamConfig.search.toLowerCase())) return false;

    if (rawStreamConfig.taskId && item.task_id !== rawStreamConfig.taskId) return false;
    if (rawStreamConfig.traceId && item.trace_id !== rawStreamConfig.traceId) return false;

    const type = item.kind || item.event_type || item.event || "unknown";
    if (rawStreamConfig.type && type !== rawStreamConfig.type) return false;

    return true;
  });

  if (rawStreamConfig.descending) {
    filtered = filtered.reverse();
  }

  node.innerHTML = filtered.map(item => {
    const time = item.ts ? new Date(item.ts).toLocaleTimeString() : "";
    const type = item.kind || item.event_type || item.event || "unknown";
    const trace = item.trace_id || item.related_trace || "";
    // Omit heavy raw printing of some known huge keys to save horizontal space
    const strippedItem = { ...item };
    delete strippedItem.ts;
    delete strippedItem.kind;
    let jsonStr = JSON.stringify(strippedItem);
    if (jsonStr.length > 200) {
      jsonStr = jsonStr.substring(0, 200) + '...}';
    }

    return `<div class="log-entry"><span style="color:var(--muted)">[${time}]</span> <span style="color:#a87b28">[${type}]</span> <span style="color:#2f78e0">${trace ? `trace:${trace} ` : ''}</span>${jsonStr}</div>`;
  }).join("");
}

function renderRawStream(model) {
  rawStreamItems = model.items || [];
  renderRawStreamData();
}

document.getElementById("raw-search")?.addEventListener("input", (e) => {
  rawStreamConfig.search = e.target.value;
  renderRawStreamData();
});
document.getElementById("raw-task-id")?.addEventListener("input", (e) => {
  rawStreamConfig.taskId = e.target.value;
  renderRawStreamData();
});
document.getElementById("raw-trace-id")?.addEventListener("input", (e) => {
  rawStreamConfig.traceId = e.target.value;
  renderRawStreamData();
});
document.getElementById("raw-type")?.addEventListener("change", (e) => {
  rawStreamConfig.type = e.target.value;
  renderRawStreamData();
});
document.getElementById("raw-sort-btn")?.addEventListener("click", (e) => {
  rawStreamConfig.descending = !rawStreamConfig.descending;
  e.target.textContent = rawStreamConfig.descending ? "Sort: Newest First" : "Sort: Oldest First";
  renderRawStreamData();
});

async function submitCommand(command, targetType, targetId, reason, payload = {}) {
  const result = await invoke("submit_admin_command", {
    input: {
      issuer: "admin",
      command,
      target_type: targetType,
      target_id: targetId,
      reason,
      trace_id: "",
      payload,
      requires_ack: true,
      risk_override: "none",
      approval_context: {},
    },
  });
  showToast(`Queued ${result.command_id}`);
}

async function refreshAll() {
  await invoke("refresh_read_models");
  const [overview, capabilitySnapshot, executionDiscipline, taskBoard, approvalGate, rawStream] = await Promise.all([
    invoke("load_read_model", { name: "overview" }),
    invoke("load_read_model", { name: "capability_snapshot" }),
    invoke("load_read_model", { name: "execution_discipline" }),
    invoke("load_read_model", { name: "task_board" }),
    invoke("load_read_model", { name: "approval_gate" }),
    invoke("load_read_model", { name: "raw_stream" }),
  ]);
  renderOverview(overview, capabilitySnapshot);
  renderCapabilitySnapshot(capabilitySnapshot);
  renderExecutionDiscipline(executionDiscipline);
  renderTaskBoard(taskBoard);
  renderApprovalGate(approvalGate);
  renderRawStream(rawStream);
}

document.getElementById("refresh-button").addEventListener("click", async () => {
  try {
    await refreshAll();
    showToast("Read model refreshed");
  } catch (error) {
    showToast(String(error));
  }
});

document.getElementById("stop-button").addEventListener("click", async () => {
  try {
    await submitCommand("EMERGENCY_STOP", "system", "", "operator triggered emergency stop");
    await refreshAll();
  } catch (error) {
    showToast(String(error));
  }
});

document.getElementById("release-button").addEventListener("click", async () => {
  try {
    await submitCommand("RELEASE_STOP", "system", "", "operator released emergency stop");
    await refreshAll();
  } catch (error) {
    showToast(String(error));
  }
});

document.body.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-command]");
  if (!button) {
    return;
  }
  try {
    await submitCommand(
      button.dataset.command,
      button.dataset.targetType,
      button.dataset.targetId,
      button.dataset.reason
    );
    await refreshAll();
  } catch (error) {
    showToast(String(error));
  }
});

refreshAll().catch((error) => {
  showToast(String(error));
});
