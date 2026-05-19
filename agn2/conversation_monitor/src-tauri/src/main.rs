#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::env;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tauri::State;

#[derive(Debug, Deserialize, Default)]
struct MonitorFilters {
    search: Option<String>,
    participant: Option<String>,
    trace_id: Option<String>,
    task_id: Option<String>,
    topic: Option<String>,
    subsystem: Option<String>,
    from_ts: Option<String>,
    to_ts: Option<String>,
    conversation_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct MessageSection {
    label: String,
    value: String,
}

#[derive(Debug, Clone, Serialize)]
struct MonitorMessage {
    id: String,
    conversation_id: String,
    ts: String,
    sender: String,
    targets: Vec<String>,
    task_id: String,
    trace_id: String,
    topic_ids: Vec<String>,
    subsystem: String,
    kind: String,
    surface: String,
    attempt: u64,
    round: u64,
    in_reply_to_ref: String,
    in_reply_to_message_id: String,
    message_ref: String,
    artifact_path: String,
    source_event_path: String,
    preview: String,
    parse_status: String,
    issues: Vec<String>,
    raw_body: String,
    raw_envelope: Value,
    sections: Vec<MessageSection>,
}

#[derive(Debug, Clone, Serialize)]
struct ConversationSummary {
    id: String,
    trace_id: String,
    task_ids: Vec<String>,
    participants: Vec<String>,
    subsystem: String,
    topic_ids: Vec<String>,
    last_ts: String,
    message_count: usize,
    last_sender: String,
    last_preview: String,
    unresolved_count: usize,
}

#[derive(Debug, Clone, Serialize)]
struct ConversationDetail {
    id: String,
    trace_id: String,
    task_ids: Vec<String>,
    participants: Vec<String>,
    subsystem: String,
    topic_ids: Vec<String>,
    message_count: usize,
    source_refs: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
struct IndexIssue {
    kind: String,
    source: String,
    detail: String,
}

#[derive(Debug, Serialize)]
struct MonitorStatePayload {
    ok: bool,
    source_mode: String,
    summary: Value,
    controls: Value,
    conversations: Vec<ConversationSummary>,
    selected_conversation: Option<ConversationDetail>,
    transcript: Vec<MonitorMessage>,
    issues: Vec<IndexIssue>,
}

#[derive(Debug, Default)]
struct FileCursor {
    offset: u64,
    len: u64,
}

#[derive(Debug)]
struct MonitorCache {
    repo_root: PathBuf,
    file_cursors: HashMap<PathBuf, FileCursor>,
    manifest_cache: HashMap<PathBuf, HashMap<String, PathBuf>>,
    messages: BTreeMap<String, MonitorMessage>,
    ref_to_event: HashMap<String, String>,
    ref_to_actor: HashMap<String, String>,
    issues: Vec<IndexIssue>,
}

impl Default for MonitorCache {
    fn default() -> Self {
        Self {
            repo_root: repo_root(),
            file_cursors: HashMap::new(),
            manifest_cache: HashMap::new(),
            messages: BTreeMap::new(),
            ref_to_event: HashMap::new(),
            ref_to_actor: HashMap::new(),
            issues: Vec::new(),
        }
    }
}

impl MonitorCache {
    fn events_dir(&self) -> PathBuf {
        self.repo_root
            .join(".agn_workspace")
            .join("event_driven")
            .join("ssot")
            .join("events")
    }

    fn reset(&mut self) {
        self.file_cursors.clear();
        self.manifest_cache.clear();
        self.messages.clear();
        self.ref_to_event.clear();
        self.ref_to_actor.clear();
        self.issues.clear();
    }

    fn refresh(&mut self) {
        let events_dir = self.events_dir();
        let Ok(read_dir) = fs::read_dir(&events_dir) else {
            self.issues.push(IndexIssue {
                kind: "source_missing".to_string(),
                source: events_dir.display().to_string(),
                detail: "event source directory is missing".to_string(),
            });
            return;
        };

        let mut files: Vec<PathBuf> = read_dir
            .filter_map(|entry| entry.ok().map(|value| value.path()))
            .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("jsonl"))
            .collect();
        files.sort();

        let needs_reset = files.iter().any(|path| {
            let Ok(meta) = fs::metadata(path) else {
                return false;
            };
            let len = meta.len();
            self.file_cursors
                .get(path)
                .map(|cursor| len < cursor.offset || len < cursor.len)
                .unwrap_or(false)
        });

        if needs_reset {
            self.reset();
        }

        for path in files {
            self.refresh_event_file(&path);
        }
        self.rebuild_links();
    }

    fn refresh_event_file(&mut self, path: &Path) {
        let Ok(meta) = fs::metadata(path) else {
            self.issues.push(IndexIssue {
                kind: "source_missing".to_string(),
                source: path.display().to_string(),
                detail: "event file disappeared during refresh".to_string(),
            });
            return;
        };
        let len = meta.len();
        let current = self
            .file_cursors
            .get(path)
            .map(|cursor| cursor.len)
            .unwrap_or(0);
        if len == current {
            return;
        }

        let Ok(file) = File::open(path) else {
            self.issues.push(IndexIssue {
                kind: "source_missing".to_string(),
                source: path.display().to_string(),
                detail: "unable to open event file".to_string(),
            });
            return;
        };
        let mut reader = BufReader::new(file);
        let mut offset = self
            .file_cursors
            .get(path)
            .map(|cursor| cursor.offset)
            .unwrap_or(0);
        if offset > 0 {
            if reader.seek(SeekFrom::Start(offset)).is_err() {
                self.reset();
                self.refresh();
                return;
            }
        }

        let mut line = String::new();
        loop {
            line.clear();
            let Ok(bytes) = reader.read_line(&mut line) else {
                self.issues.push(IndexIssue {
                    kind: "source_read_error".to_string(),
                    source: path.display().to_string(),
                    detail: "failed while reading event file".to_string(),
                });
                break;
            };
            if bytes == 0 {
                break;
            }
            offset += bytes as u64;
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            self.process_event_line(path, trimmed);
        }
        self.file_cursors
            .insert(path.to_path_buf(), FileCursor { offset, len });
    }

    fn process_event_line(&mut self, source_path: &Path, line: &str) {
        let parsed: Value = match serde_json::from_str(line) {
            Ok(value) => value,
            Err(err) => {
                self.issues.push(IndexIssue {
                    kind: "malformed_payload".to_string(),
                    source: source_path.display().to_string(),
                    detail: format!("event json decode failed: {err}"),
                });
                return;
            }
        };
        if parsed.get("event_type").and_then(Value::as_str) != Some("RESEARCH_MESSAGE") {
            return;
        }

        let payload = parsed
            .get("payload")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let event_id = string_field(&parsed, "event_id");
        if event_id.is_empty() {
            self.issues.push(IndexIssue {
                kind: "malformed_payload".to_string(),
                source: source_path.display().to_string(),
                detail: "message event missing event_id".to_string(),
            });
            return;
        }

        let message_ref = payload
            .get("message_ref")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let task_id = string_field(&parsed, "task_id");
        let trace_id = string_field(&parsed, "trace_id");
        let sender = payload
            .get("actor")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string();
        let attempt = payload.get("attempt").and_then(Value::as_u64).unwrap_or(1);
        let raw_body_info = self.resolve_artifact(task_id.as_str(), attempt, message_ref.as_str());
        let parsed_body = serde_json::from_str::<Value>(&raw_body_info.raw_body).ok();
        let sections = display_sections(raw_body_info.raw_body.as_str(), parsed_body.as_ref());
        let topic_ids = topic_ids(parsed_body.as_ref());

        let mut issues = raw_body_info.issues;
        let parse_status = if message_ref.is_empty() {
            issues.push("missing message_ref".to_string());
            "malformed".to_string()
        } else if !issues.is_empty() {
            "unresolved".to_string()
        } else {
            "ok".to_string()
        };

        let message = MonitorMessage {
            id: event_id.clone(),
            conversation_id: format!("trace:{trace_id}"),
            ts: string_field(&parsed, "ts"),
            sender: sender.clone(),
            targets: Vec::new(),
            task_id: task_id.clone(),
            trace_id: trace_id.clone(),
            topic_ids,
            subsystem: "agn1_research_flow".to_string(),
            kind: payload
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            surface: payload
                .get("surface")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            attempt,
            round: payload.get("round").and_then(Value::as_u64).unwrap_or(0),
            in_reply_to_ref: payload
                .get("in_reply_to")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            in_reply_to_message_id: String::new(),
            message_ref: message_ref.clone(),
            artifact_path: raw_body_info.path.display().to_string(),
            source_event_path: source_path.display().to_string(),
            preview: payload
                .get("preview")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            parse_status,
            issues,
            raw_body: raw_body_info.raw_body,
            raw_envelope: parsed.clone(),
            sections,
        };
        self.ref_to_event.insert(message_ref.clone(), event_id.clone());
        self.ref_to_actor.insert(message_ref, sender);
        self.messages.insert(event_id, message);
    }

    fn resolve_artifact(&mut self, task_id: &str, attempt: u64, reference: &str) -> ArtifactBody {
        if reference.is_empty() {
            return ArtifactBody {
                path: PathBuf::new(),
                raw_body: String::new(),
                issues: vec!["message_ref is empty".to_string()],
            };
        }
        let attempt_dir = self
            .repo_root
            .join(".agn_workspace")
            .join("tasks")
            .join(task_id)
            .join(format!("attempt_{attempt}"));
        let manifest_path = attempt_dir.join("manifest.json");
        if !manifest_path.exists() {
            return ArtifactBody {
                path: PathBuf::new(),
                raw_body: String::new(),
                issues: vec![format!("manifest missing for task {task_id} attempt {attempt}")],
            };
        }
        if !self.manifest_cache.contains_key(&manifest_path) {
            self.manifest_cache
                .insert(manifest_path.clone(), load_manifest_map(&manifest_path, &self.repo_root));
        }
        let Some(map) = self.manifest_cache.get(&manifest_path) else {
            return ArtifactBody {
                path: PathBuf::new(),
                raw_body: String::new(),
                issues: vec!["manifest cache unavailable".to_string()],
            };
        };
        let Some(path) = map.get(reference) else {
            return ArtifactBody {
                path: PathBuf::new(),
                raw_body: String::new(),
                issues: vec![format!("unresolved message_ref: {reference}")],
            };
        };
        match fs::read_to_string(path) {
            Ok(raw_body) => ArtifactBody {
                path: path.clone(),
                raw_body,
                issues: Vec::new(),
            },
            Err(err) => ArtifactBody {
                path: path.clone(),
                raw_body: String::new(),
                issues: vec![format!("artifact read failed: {err}")],
            },
        }
    }

    fn rebuild_links(&mut self) {
        let ids: Vec<String> = self.messages.keys().cloned().collect();
        for id in ids {
            let Some(message) = self.messages.get(&id).cloned() else {
                continue;
            };
            let parsed_body = serde_json::from_str::<Value>(&message.raw_body).ok();
            let reply_id = self
                .ref_to_event
                .get(&message.in_reply_to_ref)
                .cloned()
                .unwrap_or_default();
            let targets = infer_targets(
                message.sender.as_str(),
                parsed_body.as_ref(),
                message.in_reply_to_ref.as_str(),
                &self.ref_to_actor,
            );
            if let Some(item) = self.messages.get_mut(&id) {
                item.in_reply_to_message_id = reply_id;
                item.targets = targets;
            }
        }
    }

    fn payload(&mut self, filters: MonitorFilters) -> MonitorStatePayload {
        self.refresh();
        let mut message_refs: Vec<&MonitorMessage> = self.messages.values().collect();
        message_refs.sort_by(|a, b| a.ts.cmp(&b.ts));

        let filtered: Vec<&MonitorMessage> = message_refs
            .into_iter()
            .filter(|message| matches_filters(message, &filters))
            .collect();

        let conversations = build_conversation_summaries(&filtered);
        let selected_id = filters
            .conversation_id
            .clone()
            .filter(|value| conversations.iter().any(|item| item.id == *value))
            .or_else(|| conversations.first().map(|item| item.id.clone()));
        let transcript: Vec<MonitorMessage> = filtered
            .iter()
            .filter(|message| selected_id.as_ref().map(|id| &message.conversation_id == id).unwrap_or(false))
            .map(|message| (*message).clone())
            .collect();
        let selected_conversation = selected_id
            .as_ref()
            .map(|id| build_conversation_detail(id, &transcript));

        let participants: BTreeSet<String> = self
            .messages
            .values()
            .flat_map(|message| {
                let mut values = vec![message.sender.clone()];
                values.extend(message.targets.clone());
                values
            })
            .filter(|value| !value.is_empty())
            .collect();
        let topics: BTreeSet<String> = self
            .messages
            .values()
            .flat_map(|message| message.topic_ids.clone())
            .filter(|value| !value.is_empty())
            .collect();
        let subsystems: BTreeSet<String> = self
            .messages
            .values()
            .map(|message| message.subsystem.clone())
            .collect();

        MonitorStatePayload {
            ok: true,
            source_mode: "read_only_event_ledger_plus_message_ref".to_string(),
            summary: json!({
                "message_count": self.messages.len(),
                "conversation_count": conversations.len(),
                "supported_event_type": "RESEARCH_MESSAGE",
                "subsystem_scope": ["agn1_research_flow"]
            }),
            controls: json!({
                "participants": participants.into_iter().collect::<Vec<_>>(),
                "topics": topics.into_iter().collect::<Vec<_>>(),
                "subsystems": subsystems.into_iter().collect::<Vec<_>>(),
            }),
            conversations,
            selected_conversation,
            transcript,
            issues: self.issues.clone(),
        }
    }
}

#[derive(Default)]
struct MonitorAppState {
    cache: Mutex<MonitorCache>,
}

#[derive(Debug)]
struct ArtifactBody {
    path: PathBuf,
    raw_body: String,
    issues: Vec<String>,
}

fn repo_root() -> PathBuf {
    if let Ok(raw) = env::var("AGN_REPO_ROOT") {
        let path = PathBuf::from(raw);
        if path.exists() {
            return path;
        }
    }
    let mut current = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    loop {
        if current.join(".git").exists() {
            return current;
        }
        if !current.pop() {
            break;
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn load_manifest_map(path: &Path, repo_root: &Path) -> HashMap<String, PathBuf> {
    let mut map = HashMap::new();
    let Ok(raw) = fs::read_to_string(path) else {
        return map;
    };
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return map;
    };
    let Some(artifacts) = value.get("artifacts").and_then(Value::as_object) else {
        return map;
    };
    for artifact in artifacts.values() {
        let Some(obj) = artifact.as_object() else {
            continue;
        };
        let relative = obj.get("path").and_then(Value::as_str).unwrap_or("");
        if relative.is_empty() {
            continue;
        }
        let resolved = repo_root.join(relative);
        if let Some(reference) = obj.get("ref").and_then(Value::as_str) {
            map.insert(reference.to_string(), resolved.clone());
        }
        if let Some(reference) = obj.get("legacy_ref").and_then(Value::as_str) {
            map.insert(reference.to_string(), resolved.clone());
        }
    }
    map
}

fn string_field(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn display_sections(raw_body: &str, parsed: Option<&Value>) -> Vec<MessageSection> {
    let mut sections = Vec::new();
    if let Some(Value::Object(obj)) = parsed {
        let keys = [
            "message",
            "reason",
            "problem",
            "risk",
            "minimal_change",
            "error",
            "goal",
            "current_action_required",
            "question",
            "hypothesis",
            "baseline",
            "single_change",
            "title",
            "topic_id",
            "decision",
            "verdict",
            "status",
            "ack",
            "strategy",
        ];
        for key in keys {
            if let Some(value) = obj.get(key) {
                if let Some(rendered) = render_value(value) {
                    sections.push(MessageSection {
                        label: key.to_string(),
                        value: rendered,
                    });
                }
            }
        }
        if let Some(Value::Array(notes)) = obj.get("notes") {
            let values: Vec<String> = notes
                .iter()
                .filter_map(Value::as_str)
                .map(ToString::to_string)
                .collect();
            if !values.is_empty() {
                sections.push(MessageSection {
                    label: "notes".to_string(),
                    value: values.join("\n"),
                });
            }
        }
        if let Some(Value::Object(proposal)) = obj.get("current_proposal") {
            for key in ["title", "topic_id", "question", "hypothesis", "baseline", "single_change"] {
                if let Some(value) = proposal.get(key) {
                    if let Some(rendered) = render_value(value) {
                        sections.push(MessageSection {
                            label: format!("current_proposal.{key}"),
                            value: rendered,
                        });
                    }
                }
            }
        }
    }
    if sections.is_empty() {
        sections.push(MessageSection {
            label: "body".to_string(),
            value: raw_body.to_string(),
        });
    }
    sections
}

fn render_value(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::Bool(v) => Some(v.to_string()),
        Value::Number(v) => Some(v.to_string()),
        Value::String(v) => {
            let trimmed = v.trim();
            if trimmed.is_empty() {
                None
            } else {
                Some(trimmed.to_string())
            }
        }
        Value::Array(items) => {
            let rendered: Vec<String> = items.iter().filter_map(render_value).collect();
            if rendered.is_empty() {
                None
            } else {
                Some(rendered.join("\n"))
            }
        }
        Value::Object(_) => Some(serde_json::to_string_pretty(value).unwrap_or_else(|_| value.to_string())),
    }
}

fn topic_ids(parsed: Option<&Value>) -> Vec<String> {
    let mut topics = BTreeSet::new();
    if let Some(Value::Object(obj)) = parsed {
        if let Some(topic) = obj.get("topic_id").and_then(Value::as_str) {
            if !topic.trim().is_empty() {
                topics.insert(topic.trim().to_string());
            }
        }
        if let Some(Value::Object(proposal)) = obj.get("current_proposal") {
            if let Some(topic) = proposal.get("topic_id").and_then(Value::as_str) {
                if !topic.trim().is_empty() {
                    topics.insert(topic.trim().to_string());
                }
            }
        }
    }
    topics.into_iter().collect()
}

fn infer_targets(
    sender: &str,
    parsed: Option<&Value>,
    in_reply_to_ref: &str,
    ref_to_actor: &HashMap<String, String>,
) -> Vec<String> {
    let mut targets = BTreeSet::new();
    if let Some(Value::Object(obj)) = parsed {
        if let Some(role) = obj.get("role").and_then(Value::as_str) {
            if role != sender {
                targets.insert(role.to_string());
            }
        }
        if let Some(Value::Object(schema)) = obj.get("confirmation_schema") {
            if let Some(role) = schema.get("role").and_then(Value::as_str) {
                if role != sender {
                    targets.insert(role.to_string());
                }
            }
        }
    }
    if targets.is_empty() && !in_reply_to_ref.is_empty() {
        if let Some(actor) = ref_to_actor.get(in_reply_to_ref) {
            if actor != sender {
                targets.insert(actor.clone());
            }
        }
    }
    if targets.is_empty() && matches!(sender, "executor" | "reviewer") {
        targets.insert("coordinator".to_string());
    }
    targets.into_iter().collect()
}

fn contains_ci(haystack: &str, needle: &str) -> bool {
    haystack.to_lowercase().contains(&needle.to_lowercase())
}

fn matches_filters(message: &MonitorMessage, filters: &MonitorFilters) -> bool {
    if let Some(value) = filters.search.as_deref() {
        let needle = value.trim();
        if !needle.is_empty() {
            let joined = format!(
                "{}\n{}\n{}\n{}\n{}\n{}",
                message.sender,
                message.targets.join(" "),
                message.task_id,
                message.trace_id,
                message.raw_body,
                serde_json::to_string(&message.raw_envelope).unwrap_or_default()
            );
            if !contains_ci(&joined, needle) {
                return false;
            }
        }
    }
    if let Some(value) = filters.participant.as_deref() {
        let needle = value.trim();
        if !needle.is_empty() {
            let mut participants = vec![message.sender.clone()];
            participants.extend(message.targets.clone());
            if !participants.iter().any(|item| contains_ci(item, needle)) {
                return false;
            }
        }
    }
    if let Some(value) = filters.trace_id.as_deref() {
        let needle = value.trim();
        if !needle.is_empty() && !contains_ci(&message.trace_id, needle) {
            return false;
        }
    }
    if let Some(value) = filters.task_id.as_deref() {
        let needle = value.trim();
        if !needle.is_empty() && !contains_ci(&message.task_id, needle) {
            return false;
        }
    }
    if let Some(value) = filters.topic.as_deref() {
        let needle = value.trim();
        if !needle.is_empty() && !message.topic_ids.iter().any(|item| contains_ci(item, needle)) {
            return false;
        }
    }
    if let Some(value) = filters.subsystem.as_deref() {
        let needle = value.trim();
        if !needle.is_empty() && !contains_ci(&message.subsystem, needle) {
            return false;
        }
    }
    if let Some(value) = filters.from_ts.as_deref() {
        let needle = value.trim();
        if !needle.is_empty() && message.ts.as_str() < needle {
            return false;
        }
    }
    if let Some(value) = filters.to_ts.as_deref() {
        let needle = value.trim();
        if !needle.is_empty() && message.ts.as_str() > needle {
            return false;
        }
    }
    true
}

fn build_conversation_summaries(messages: &[&MonitorMessage]) -> Vec<ConversationSummary> {
    let mut grouped: BTreeMap<String, Vec<&MonitorMessage>> = BTreeMap::new();
    for message in messages {
        grouped
            .entry(message.conversation_id.clone())
            .or_default()
            .push(*message);
    }
    let mut summaries: Vec<ConversationSummary> = grouped
        .into_iter()
        .map(|(id, items)| {
            let mut participants = BTreeSet::new();
            let mut task_ids = BTreeSet::new();
            let mut topics = BTreeSet::new();
            let mut unresolved_count = 0usize;
            for item in &items {
                participants.insert(item.sender.clone());
                for target in &item.targets {
                    participants.insert(target.clone());
                }
                task_ids.insert(item.task_id.clone());
                for topic in &item.topic_ids {
                    topics.insert(topic.clone());
                }
                if item.parse_status != "ok" {
                    unresolved_count += 1;
                }
            }
            let last = items.last().cloned();
            ConversationSummary {
                id,
                trace_id: last.map(|item| item.trace_id.clone()).unwrap_or_default(),
                task_ids: task_ids.into_iter().collect(),
                participants: participants.into_iter().collect(),
                subsystem: last
                    .map(|item| item.subsystem.clone())
                    .unwrap_or_else(|| "agn1_research_flow".to_string()),
                topic_ids: topics.into_iter().collect(),
                last_ts: last.map(|item| item.ts.clone()).unwrap_or_default(),
                message_count: items.len(),
                last_sender: last.map(|item| item.sender.clone()).unwrap_or_default(),
                last_preview: last.map(|item| item.preview.clone()).unwrap_or_default(),
                unresolved_count,
            }
        })
        .collect();
    summaries.sort_by(|a, b| b.last_ts.cmp(&a.last_ts));
    summaries
}

fn build_conversation_detail(id: &str, messages: &[MonitorMessage]) -> ConversationDetail {
    let mut participants = BTreeSet::new();
    let mut task_ids = BTreeSet::new();
    let mut topics = BTreeSet::new();
    let mut refs = BTreeSet::new();
    let mut trace_id = String::new();
    let mut subsystem = "agn1_research_flow".to_string();
    for item in messages {
        if item.conversation_id != id {
            continue;
        }
        trace_id = item.trace_id.clone();
        subsystem = item.subsystem.clone();
        task_ids.insert(item.task_id.clone());
        participants.insert(item.sender.clone());
        for target in &item.targets {
            participants.insert(target.clone());
        }
        for topic in &item.topic_ids {
            topics.insert(topic.clone());
        }
        refs.insert(item.message_ref.clone());
        refs.insert(item.artifact_path.clone());
        refs.insert(item.source_event_path.clone());
    }
    ConversationDetail {
        id: id.to_string(),
        trace_id,
        task_ids: task_ids.into_iter().collect(),
        participants: participants.into_iter().collect(),
        subsystem,
        topic_ids: topics.into_iter().collect(),
        message_count: messages.len(),
        source_refs: refs.into_iter().filter(|item| !item.is_empty()).collect(),
    }
}

#[tauri::command]
fn load_monitor_state(filters: Option<MonitorFilters>, state: State<MonitorAppState>) -> Result<Value, String> {
    let mut cache = state.cache.lock().map_err(|_| "monitor_state_lock_failed".to_string())?;
    serde_json::to_value(cache.payload(filters.unwrap_or_default())).map_err(|err| format!("json_encode_failed:{err}"))
}

fn main() {
    tauri::Builder::default()
        .manage(MonitorAppState::default())
        .invoke_handler(tauri::generate_handler![load_monitor_state])
        .run(tauri::generate_context!())
        .expect("error while running AGN Conversation Monitor");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_sections_prefers_original_language_fields() {
        let payload = json!({
            "message": "Proposal rejected due to mismatch.",
            "problem": "Title and topic drift apart.",
            "risk": "The wrong experiment could run."
        });
        let sections = display_sections(&payload.to_string(), Some(&payload));
        assert_eq!(sections[0].label, "message");
        assert!(sections.iter().any(|item| item.label == "problem"));
        assert!(sections.iter().any(|item| item.label == "risk"));
    }

    #[test]
    fn infer_targets_prefers_role_and_reply_context() {
        let payload = json!({ "role": "reviewer" });
        let mut refs = HashMap::new();
        refs.insert("agn://artifact/x".to_string(), "coordinator".to_string());
        let direct = infer_targets("coordinator", Some(&payload), "", &refs);
        assert_eq!(direct, vec!["reviewer".to_string()]);
        let reply = infer_targets("reviewer", None, "agn://artifact/x", &refs);
        assert_eq!(reply, vec!["coordinator".to_string()]);
    }

    #[test]
    fn filter_matches_keyword_and_topic() {
        let message = MonitorMessage {
            id: "evt-1".to_string(),
            conversation_id: "trace:t1".to_string(),
            ts: "2026-03-14T02:00:00Z".to_string(),
            sender: "reviewer".to_string(),
            targets: vec!["coordinator".to_string()],
            task_id: "task-1".to_string(),
            trace_id: "t1".to_string(),
            topic_ids: vec!["ml_robustness".to_string()],
            subsystem: "agn1_research_flow".to_string(),
            kind: "topic_vote".to_string(),
            surface: "cli".to_string(),
            attempt: 1,
            round: 1,
            in_reply_to_ref: String::new(),
            in_reply_to_message_id: String::new(),
            message_ref: "agn://artifact/a".to_string(),
            artifact_path: "/tmp/a.txt".to_string(),
            source_event_path: "/tmp/e.jsonl".to_string(),
            preview: "Proposal rejected".to_string(),
            parse_status: "ok".to_string(),
            issues: Vec::new(),
            raw_body: "{\"message\":\"Proposal rejected\"}".to_string(),
            raw_envelope: json!({}),
            sections: vec![],
        };
        let filters = MonitorFilters {
            search: Some("rejected".to_string()),
            topic: Some("ml_rob".to_string()),
            ..Default::default()
        };
        assert!(matches_filters(&message, &filters));
    }
}
