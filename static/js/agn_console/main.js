import { agnApi, commandRequestApi } from "./api.js";
import { buildFallbackTopicControl, buildModifyControl, buildSimpleControl } from "./controls.js";
import {
  renderCheckpoint,
  renderCommandRequests,
  renderMessages,
  renderOverview,
  renderPendingActions,
  renderRefResult,
  renderTracePanel,
  renderTaskHeader,
  renderTaskList,
  renderTimeline,
  renderTraceEvents,
} from "./render.js";
import { connectTaskEvents } from "./sse.js";
import { ensureSelectedTask, setToken, state } from "./state.js";

const el = {
  overview: document.getElementById("overview"),
  status: document.getElementById("connection-status"),
  jwtInput: document.getElementById("jwt-input"),
  taskSearch: document.getElementById("task-search"),
  taskStateFilter: document.getElementById("task-state-filter"),
  tasksRefresh: document.getElementById("tasks-refresh"),
  taskList: document.getElementById("task-list"),
  taskHeader: document.getElementById("task-header"),
  checkpointJson: document.getElementById("checkpoint-json"),
  timelineList: document.getElementById("timeline-list"),
  pendingActions: document.getElementById("pending-actions"),
  tracePanel: document.getElementById("trace-panel"),
  traceEvents: document.getElementById("trace-events"),
  messages: document.getElementById("message-list"),
  refInput: document.getElementById("ref-input"),
  readRefBtn: document.getElementById("read-ref-btn"),
  refResult: document.getElementById("ref-result"),
  commandRequests: document.getElementById("command-requests"),
  modifySummary: document.getElementById("modify-summary"),
  modifyText: document.getElementById("modify-text"),
  modifyTextRef: document.getElementById("modify-text-ref"),
  modifyContextPath: document.getElementById("modify-context-path"),
  modifyNeedsContext: document.getElementById("modify-needs-context"),
  submitModify: document.getElementById("submit-modify"),
  fallbackTopic: document.getElementById("fallback-topic-id"),
  submitFallback: document.getElementById("submit-fallback"),
};
let searchDebounceTimer = null;

function setConnectionStatus(text, className) {
  el.status.textContent = text;
  el.status.classList.remove("ok", "warn");
  if (className) el.status.classList.add(className);
}

function notifyError(err) {
  const message = err instanceof Error ? err.message : String(err);
  setConnectionStatus(`Error: ${message}`, "warn");
  console.error(err);
}

function render() {
  renderOverview(state.overview, el.overview);
  renderTaskList(state.tasks, state.selectedTaskId, el.taskList);
  renderTaskHeader(state.selectedTask, state.checkpoint, el.taskHeader);
  renderCheckpoint(state.checkpoint, el.checkpointJson);
  renderTimeline(state.timeline, el.timelineList);
  renderPendingActions(state.pendingActions, el.pendingActions);
  renderTracePanel(state.selectedTask, state.checkpoint, state.timeline, state.messages, el.tracePanel);
  renderMessages(state.messages, el.messages);
  renderTraceEvents(state.traceEvents, el.traceEvents);
  renderRefResult(state.refResult, el.refResult);
  renderCommandRequests(state.commandRequests, el.commandRequests);
}

async function refreshOverview() {
  state.overview = await agnApi.overview();
}

async function refreshTasks() {
  const payload = await agnApi.tasks({
    state: state.filters.state,
    search: state.filters.search,
    limit: 200,
  });
  state.tasks = payload.tasks || [];
  ensureSelectedTask();
}

async function refreshCommandRequests() {
  const payload = await commandRequestApi.listPending();
  state.commandRequests = payload.requests || [];
}

async function refreshSelectedTask() {
  if (!state.selectedTaskId) {
    state.selectedTask = null;
    state.checkpoint = null;
    state.timeline = [];
    state.pendingActions = [];
    state.messages = [];
    state.traceEvents = [];
    return;
  }

  state.selectedTask = await agnApi.task(state.selectedTaskId);
  const checkpoint = await agnApi.checkpoint(state.selectedTaskId);
  state.checkpoint = checkpoint;

  const [timelineResp, pendingResp, messagesResp, controlsResp, traceResp] = await Promise.all([
    agnApi.timeline(state.selectedTaskId, 200),
    agnApi.pendingActions(state.selectedTaskId),
    agnApi.messages(state.selectedTaskId, 200),
    agnApi.controls(state.selectedTaskId, "pending", 100),
    agnApi.traceEvents(state.selectedTask.trace_id, 200),
  ]);
  state.timeline = timelineResp.events || [];
  state.pendingActions = pendingResp.actions || [];
  state.messages = messagesResp.messages || [];
  const controls = controlsResp.controls || [];
  if (controls.length) {
    state.timeline = [...state.timeline, ...controls.map((item) => ({
      event_id: item.control_id,
      event_type: `CONTROL_${item.type}`,
      ts: item.created_at,
      payload: item,
    }))];
  }
  state.traceEvents = traceResp.events || [];
}

async function refreshAll() {
  try {
    await refreshOverview();
    await refreshTasks();
    await refreshSelectedTask();
    await refreshCommandRequests();
    render();
  } catch (err) {
    notifyError(err);
  }
}

async function submitControl(payload) {
  if (!state.selectedTaskId) return;
  try {
    await agnApi.enqueueControl(state.selectedTaskId, payload, state.jwtToken);
    await refreshSelectedTask();
    await refreshOverview();
    render();
  } catch (err) {
    notifyError(err);
  }
}

function bindHandlers() {
  el.jwtInput.value = state.jwtToken;
  el.jwtInput.addEventListener("change", () => {
    setToken(el.jwtInput.value);
  });

  el.taskSearch.addEventListener("input", () => {
    state.filters.search = el.taskSearch.value.trim();
    if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(() => {
      refreshAll();
    }, 280);
  });

  el.taskStateFilter.addEventListener("change", () => {
    state.filters.state = el.taskStateFilter.value;
    refreshAll();
  });

  el.tasksRefresh.addEventListener("click", () => {
    state.filters.search = el.taskSearch.value.trim();
    refreshAll();
  });

  el.taskList.addEventListener("click", async (event) => {
    const target = event.target.closest("[data-task-id]");
    if (!target) return;
    state.selectedTaskId = target.getAttribute("data-task-id") || "";
    await refreshSelectedTask();
    render();
  });

  document.querySelectorAll("button[data-control]").forEach((button) => {
    button.addEventListener("click", async () => {
      const controlType = button.getAttribute("data-control") || "";
      await submitControl(buildSimpleControl(controlType));
    });
  });

  el.submitModify.addEventListener("click", async () => {
    const payload = buildModifyControl({
      requestSummary: el.modifySummary.value.trim(),
      requestText: el.modifyText.value.trim(),
      requestTextRef: el.modifyTextRef.value.trim(),
      needsContextRead: el.modifyNeedsContext.checked,
      contextReadPath: el.modifyContextPath.value.trim(),
    });
    await submitControl(payload);
  });

  el.submitFallback.addEventListener("click", async () => {
    const topicId = el.fallbackTopic.value.trim();
    if (!topicId) return;
    await submitControl(buildFallbackTopicControl(topicId));
  });

  el.readRefBtn.addEventListener("click", async () => {
    const ref = el.refInput.value.trim();
    if (!ref) return;
    try {
      state.refResult = await agnApi.readRef({
        ref,
        mode: "tail",
        tailLines: 160,
        maxBytes: 16384,
        taskId: state.selectedTaskId || "",
      });
      render();
    } catch (err) {
      notifyError(err);
    }
  });

  el.commandRequests.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-cr-action]");
    if (!button) return;
    const requestId = button.getAttribute("data-cr-id");
    const action = button.getAttribute("data-cr-action");
    if (!requestId || !action) return;
    try {
      if (action === "approve") {
        await commandRequestApi.approve(requestId, state.jwtToken);
      } else {
        await commandRequestApi.reject(requestId, state.jwtToken);
      }
      await refreshCommandRequests();
      render();
    } catch (err) {
      notifyError(err);
    }
  });

  async function openRef(ref) {
    state.refResult = await agnApi.readRef({
      ref,
      mode: "tail",
      tailLines: 200,
      maxBytes: 32768,
      taskId: state.selectedTaskId || "",
    });
    render();
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-open-ref]");
    if (!button) return;
    const ref = button.getAttribute("data-open-ref") || "";
    if (!ref) return;
    el.refInput.value = ref;
    try {
      await openRef(ref);
    } catch (err) {
      notifyError(err);
    }
  });
}

function startEventStream() {
  connectTaskEvents({
    onTaskUpdate: async (payload) => {
      try {
        if (!payload || !payload.task_id) {
          await refreshOverview();
          await refreshTasks();
        } else {
          if (payload.task_id === state.selectedTaskId) {
            await refreshSelectedTask();
          }
          await refreshOverview();
          await refreshTasks();
        }
        render();
      } catch (err) {
        notifyError(err);
      }
    },
    onStatus: setConnectionStatus,
  });
}

async function boot() {
  bindHandlers();
  await refreshAll();
  setConnectionStatus("SSE: connecting...", "warn");
  startEventStream();
}

boot().catch(notifyError);
