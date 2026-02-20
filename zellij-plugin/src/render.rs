//! Status bar rendering for the Zellij plugin.

use crate::state::CwState;

/// Render a single-line status bar string.
///
/// Format: `cw: client1[impl:active review:bg] | client2[impl:active] | 2 queued`
pub fn render_status(state: &CwState, max_cols: usize) -> String {
    let clients = state.active_clients();
    if clients.is_empty() {
        return "cw: idle".to_string();
    }

    let mut parts: Vec<String> = Vec::new();

    for client in &clients {
        let mut session_parts: Vec<String> = Vec::new();
        for s in &state.sessions {
            if s.client != *client || s.status == "completed" {
                continue;
            }
            let status_short = match s.status.as_str() {
                "active" => "active",
                "backgrounded" => "bg",
                _ => &s.status,
            };
            session_parts.push(format!("{}:{status_short}", s.purpose));
        }
        if !session_parts.is_empty() {
            parts.push(format!("{client}[{}]", session_parts.join(" ")));
        }
    }

    let output = format!("cw: {}", parts.join(" | "));

    // Truncate if too long for the status bar
    if output.len() > max_cols {
        format!("{}...", &output[..max_cols.saturating_sub(3)])
    } else {
        output
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{CwState, Session};

    #[test]
    fn test_render_empty() {
        let state = CwState {
            sessions: Vec::new(),
        };
        assert_eq!(render_status(&state, 80), "cw: idle");
    }

    #[test]
    fn test_render_single_client() {
        let state = CwState {
            sessions: vec![
                Session {
                    name: "sigma/impl".into(),
                    client: "sigma".into(),
                    purpose: "impl".into(),
                    status: "active".into(),
                    branch: None,
                },
                Session {
                    name: "sigma/review".into(),
                    client: "sigma".into(),
                    purpose: "review".into(),
                    status: "backgrounded".into(),
                    branch: None,
                },
            ],
        };
        let output = render_status(&state, 80);
        assert!(output.contains("sigma["));
        assert!(output.contains("impl:active"));
        assert!(output.contains("review:bg"));
    }

    #[test]
    fn test_render_truncation() {
        let state = CwState {
            sessions: vec![Session {
                name: "verylongclientname/impl".into(),
                client: "verylongclientname".into(),
                purpose: "impl".into(),
                status: "active".into(),
                branch: None,
            }],
        };
        let output = render_status(&state, 20);
        assert!(output.len() <= 20);
        assert!(output.ends_with("..."));
    }
}
