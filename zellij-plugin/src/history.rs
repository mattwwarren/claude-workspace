//! Parse cw's JSONL history files.

use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct HistoryEvent {
    pub event_type: String,
    pub client: String,
    #[serde(default)]
    pub session_name: Option<String>,
    #[serde(default)]
    pub detail: Option<String>,
}

/// Load the last N events from a client's history JSONL file.
pub fn load_recent_events(history_dir: &str, client: &str, limit: usize) -> Vec<HistoryEvent> {
    let path = format!("{history_dir}/{client}.jsonl");
    let content = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };

    let mut events: Vec<HistoryEvent> = content
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();

    // Return last N events (most recent)
    if events.len() > limit {
        events.drain(..events.len() - limit);
    }
    events
}
