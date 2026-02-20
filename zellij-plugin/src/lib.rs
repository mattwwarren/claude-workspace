//! Zellij status bar plugin for claude-workspace.
//!
//! Reads cw's state files and displays a compact summary
//! of sessions and queue status in the Zellij status bar.

mod history;
mod render;
mod state;

use zellij_tile::prelude::*;

use crate::render::render_status;
use crate::state::CwState;

/// Plugin configuration keys.
const STATE_FILE_KEY: &str = "state_file";
const HISTORY_DIR_KEY: &str = "history_dir";

/// Default paths (relative to $HOME).
const DEFAULT_STATE_REL: &str = ".local/share/cw/sessions.json";
const DEFAULT_HISTORY_REL: &str = ".local/share/cw/history";

/// Refresh interval in seconds.
const REFRESH_INTERVAL_S: f64 = 5.0;

#[derive(Default)]
struct CwStatusPlugin {
    state_file: String,
    history_dir: String,
    cw_state: Option<CwState>,
    cols: usize,
}

impl ZellijPlugin for CwStatusPlugin {
    fn load(&mut self, configuration: BTreeMap<String, String>) {
        // Read config or use defaults
        let home = std::env::var("HOME").unwrap_or_default();

        self.state_file = configuration
            .get(STATE_FILE_KEY)
            .cloned()
            .unwrap_or_else(|| format!("{home}/{DEFAULT_STATE_REL}"));

        self.history_dir = configuration
            .get(HISTORY_DIR_KEY)
            .cloned()
            .unwrap_or_else(|| format!("{home}/{DEFAULT_HISTORY_REL}"));

        // Subscribe to timer events for periodic refresh
        subscribe(&[EventType::Timer]);
        set_timeout(REFRESH_INTERVAL_S);

        // Initial load
        self.refresh();
    }

    fn update(&mut self, event: Event) -> bool {
        if let Event::Timer(_) = event {
            self.refresh();
            set_timeout(REFRESH_INTERVAL_S);
            return true; // request re-render
        }
        false
    }

    fn render(&mut self, rows: usize, cols: usize) {
        let _ = rows;
        self.cols = cols;
        let output = match &self.cw_state {
            Some(state) => render_status(state, cols),
            None => "cw: no state".to_string(),
        };
        print!("{output}");
    }
}

impl CwStatusPlugin {
    fn refresh(&mut self) {
        self.cw_state = state::load_state(&self.state_file);
    }
}

register_plugin!(CwStatusPlugin);
