export function buildSimpleControl(controlType) {
  return {
    control_type: String(controlType || "").toUpperCase(),
    payload: {},
  };
}

export function buildFallbackTopicControl(fallbackTopicId) {
  return {
    control_type: "FALLBACK_TOPIC",
    payload: {
      fallback_topic_id: String(fallbackTopicId || "").trim(),
    },
  };
}

export function buildModifyControl({ requestSummary, requestText, requestTextRef, needsContextRead, contextReadPath }) {
  const payload = {};
  if (requestSummary) payload.request_summary = requestSummary;
  if (requestText) payload.request_text = requestText;
  if (requestTextRef) payload.request_text_ref = requestTextRef;
  if (typeof needsContextRead === "boolean") payload.needs_context_read = needsContextRead;
  if (contextReadPath) payload.context_read_path = contextReadPath;

  return {
    control_type: "MODIFY",
    payload,
  };
}
