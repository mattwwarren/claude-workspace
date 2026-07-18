"""Top-level persisted state model: CwState.

Depends on ``cw.models.enums`` and ``cw.models.session`` — the DAG leaf. See
``cw.models.__init__`` for the full ordering.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cw.models.enums import SessionStatus
from cw.models.session import Session

# Schema versions for persisted state. Bump when making a breaking change
# to the on-disk layout; add a migration in `cw.config.migrate_cw_state`
# or `cw.dev_queue.migrate_dev_queue` to handle older versions.
# v6: added Session.idle_observation_count (GitHub #545).
# v7: added Session.reap_reason (GitHub #380).
# v8: added Session.reap_proposed_at (GitHub #555).
# v9: added Session.lane (GitHub #594).
# v10: added Session.stage (GitHub #612).
# v11: added Session.local_liveness (GitHub #888).
# v12: added Session.consecutive_salvage_skips (#974).
# v13: added Session.liveness_bucket (GitHub #1001, RFC 0008 W2).
# v14: local_liveness.start_time_ns reference point changed from
#      boot-relative (/proc) to epoch-relative (psutil.create_time); stale
#      pre-v14 handles are cleared on migration so they don't false-positive
#      as "dead" against a live process re-read in the new format (GitHub #921).
CW_STATE_SCHEMA_VERSION = 14


class CwState(BaseModel):
    """Persisted state across all sessions."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    schema_version: int = CW_STATE_SCHEMA_VERSION
    sessions: list[Session] = Field(default_factory=list)

    def active_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.ACTIVE]

    def backgrounded_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.BACKGROUNDED]

    def idled_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.IDLE]

    def find_session(self, client: str, purpose: str) -> Session | None:
        """Find the most recent session for a client+purpose combo."""
        matches = [
            s
            for s in self.sessions
            if s.client == client
            and s.purpose == purpose
            and s.status != SessionStatus.COMPLETED
        ]
        if not matches:
            return None
        return max(matches, key=lambda s: s.started_at)

    def find_by_name_or_id(self, identifier: str) -> Session | None:
        """Find a session by name (client/purpose) or ID."""
        for s in reversed(self.sessions):
            if identifier in (s.name, s.id):
                return s
        return None
