export function connectTaskEvents({ onTaskUpdate, onStatus }) {
  const stream = new EventSource("/api/events");

  stream.addEventListener("open", () => {
    onStatus?.("SSE: connected", "ok");
  });

  stream.addEventListener("error", () => {
    onStatus?.("SSE: reconnecting...", "warn");
  });

  stream.addEventListener("task_update", (evt) => {
    try {
      const payload = JSON.parse(evt.data);
      onTaskUpdate?.(payload);
    } catch (_err) {
      onStatus?.("SSE: bad payload", "warn");
    }
  });

  stream.addEventListener("ping", () => {
    onStatus?.("SSE: alive", "ok");
  });

  return stream;
}
