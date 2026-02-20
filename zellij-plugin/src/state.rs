//! Parse cw's sessions.json state file.

use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct CwState {
    #[serde(default)]
    pub sessions: Vec<Session>,
}

#[derive(Debug, Deserialize)]
pub struct Session {
    pub name: String,
    pub client: String,
    pub purpose: String,
    pub status: String,
    #[serde(default)]
    pub branch: Option<String>,
}

impl CwState {
    /// Count sessions by status for a given client.
    pub fn client_summary(&self, client: &str) -> ClientSummary {
        let mut summary = ClientSummary::default();
        for s in &self.sessions {
            if s.client != client {
                continue;
            }
            match s.status.as_str() {
                "active" => summary.active += 1,
                "backgrounded" => summary.backgrounded += 1,
                "completed" => summary.completed += 1,
                _ => {}
            }
        }
        summary
    }

    /// Get unique client names with non-completed sessions.
    pub fn active_clients(&self) -> Vec<String> {
        let mut clients: Vec<String> = self
            .sessions
            .iter()
            .filter(|s| s.status != "completed")
            .map(|s| s.client.clone())
            .collect();
        clients.sort();
        clients.dedup();
        clients
    }
}

#[derive(Debug, Default)]
pub struct ClientSummary {
    pub active: usize,
    pub backgrounded: usize,
    pub completed: usize,
}

/// Load and parse the state file, returning None on any error.
pub fn load_state(path: &str) -> Option<CwState> {
    let content = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&content).ok()
}
