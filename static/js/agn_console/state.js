const storedToken = localStorage.getItem("agn_console_jwt") || "";

export const state = {
  overview: null,
  tasks: [],
  selectedTaskId: "",
  selectedTask: null,
  checkpoint: null,
  timeline: [],
  pendingActions: [],
  messages: [],
  controls: [],
  traceEvents: [],
  commandRequests: [],
  refResult: null,
  filters: {
    state: "",
    search: "",
  },
  jwtToken: storedToken,
};

export function setToken(token) {
  state.jwtToken = String(token || "").trim();
  localStorage.setItem("agn_console_jwt", state.jwtToken);
}

export function ensureSelectedTask() {
  if (!state.selectedTaskId && state.tasks.length > 0) {
    state.selectedTaskId = state.tasks[0].id;
  }
}
