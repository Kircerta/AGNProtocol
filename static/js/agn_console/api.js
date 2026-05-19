const JSON_HEADERS = {
  "Content-Type": "application/json",
};

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: {
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  return response.json();
}

export const agnApi = {
  overview() {
    return requestJson("/api/agn/v1/overview");
  },
  tasks({ state = "", search = "", limit = 120, offset = 0 } = {}) {
    const params = new URLSearchParams();
    if (state) params.set("state", state);
    if (search) params.set("search", search);
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    return requestJson(`/api/agn/v1/tasks?${params.toString()}`);
  },
  task(taskId) {
    return requestJson(`/api/agn/v1/tasks/${encodeURIComponent(taskId)}`);
  },
  checkpoint(taskId) {
    return requestJson(`/api/agn/v1/tasks/${encodeURIComponent(taskId)}/checkpoint`);
  },
  timeline(taskId, limit = 200) {
    return requestJson(`/api/agn/v1/tasks/${encodeURIComponent(taskId)}/timeline?limit=${encodeURIComponent(limit)}`);
  },
  pendingActions(taskId) {
    return requestJson(`/api/agn/v1/tasks/${encodeURIComponent(taskId)}/pending-actions`);
  },
  messages(taskId, limit = 200) {
    return requestJson(`/api/agn/v1/tasks/${encodeURIComponent(taskId)}/messages?limit=${encodeURIComponent(limit)}`);
  },
  controls(taskId, status = "pending", limit = 100) {
    const params = new URLSearchParams({ status, limit: String(limit) });
    return requestJson(`/api/agn/v1/tasks/${encodeURIComponent(taskId)}/controls?${params.toString()}`);
  },
  enqueueControl(taskId, payload, token) {
    const headers = { ...JSON_HEADERS };
    if (token) headers.Authorization = `Bearer ${token}`;
    return requestJson(`/api/agn/v1/tasks/${encodeURIComponent(taskId)}/controls`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
  },
  traceEvents(traceId, limit = 200) {
    return requestJson(`/api/agn/v1/traces/${encodeURIComponent(traceId)}/events?limit=${encodeURIComponent(limit)}`);
  },
  readRef({ ref, mode = "tail", tailLines = 120, maxBytes = 16384, taskId = "" }) {
    const params = new URLSearchParams({
      ref,
      mode,
      tail_lines: String(tailLines),
      max_bytes: String(maxBytes),
    });
    if (taskId) params.set("task_id", taskId);
    return requestJson(`/api/agn/v1/refs/read?${params.toString()}`);
  },
};

export const commandRequestApi = {
  listPending() {
    return requestJson("/api/command-requests");
  },
  approve(requestId, token) {
    return requestJson(`/api/command-requests/${encodeURIComponent(requestId)}/approve`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
  },
  reject(requestId, token) {
    return requestJson(`/api/command-requests/${encodeURIComponent(requestId)}/reject`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
  },
};
