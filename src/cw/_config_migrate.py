"""Schema migration helpers for the persisted sessions.json payload.

Pure dict-transform helpers extracted from ``cw.config`` (#1322). These
normalise a raw sessions.json payload into a currently-valid shape without
touching the filesystem — file I/O (backup, read, write) stays in
``cw.config``. Depends only on ``cw.models``/``cw.native_daemon``, never on
``cw.config``, so there is no import cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from cw.models import CW_STATE_SCHEMA_VERSION, SessionOrigin
from cw.native_daemon import SHORT_SESSION_ID_RE

logger = logging.getLogger(__name__)


_VALID_SESSION_ORIGINS = frozenset(v.value for v in SessionOrigin)

# Schema version at which surface_ref became hex-only; legacy multiplexer
# surface_refs are cleared only during the upgrade pass from below this version.
_HEX_SURFACE_REF_SCHEMA_VERSION = 5

# Schema version at which local_liveness.start_time_ns switched reference
# points (boot-relative /proc -> epoch-relative psutil.create_time, #921).
# A handle written below this version is in the old format and will never
# compare equal to a freshly-read epoch-relative value for the same live
# process, so it is cleared only during the upgrade pass from below this
# version -- otherwise reconcile would misread a live aider process as dead
# and harvest it out from under an in-flight session.
_EPOCH_LIVENESS_SCHEMA_VERSION = 14


def migrate_cw_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw sessions.json payload into a currently-valid shape.

    The goal is to never brick the tool on a state file that was written by
    an older (or briefly-diverged) version of cw. Unknown or renamed fields
    are coerced; unknown enum values are reset to a safe default with a
    warning rather than raising a validation error.
    """
    sessions = raw.get("sessions")
    if "sessions" in raw and not isinstance(sessions, list):
        # Malformed payload — leave schema_version untouched so the
        # corruption surfaces downstream rather than getting a false
        # "fully migrated" stamp.
        return raw
    # Capture the on-disk version before we bump it so per-step guards
    # can condition on "is this an upgrade from version X?".
    on_disk_version = int(raw.get("schema_version") or 0)
    if isinstance(sessions, list):
        for session_raw in sessions:
            if not isinstance(session_raw, dict):
                continue
            _migrate_zellij_fields(session_raw)
            # Only clear legacy multiplexer surface_refs during the v4→v5
            # upgrade pass.  After migration the field may legally hold any
            # string set by the live daemon path; re-clearing it on every
            # load would wipe valid programmatic writes (e.g. test fixtures,
            # daemon-spawn short ids that happen to look like plain strings).
            if on_disk_version < _HEX_SURFACE_REF_SCHEMA_VERSION:
                _clear_non_hex_surface_refs(session_raw)
            if on_disk_version < _EPOCH_LIVENESS_SCHEMA_VERSION:
                _clear_stale_local_liveness(session_raw)
            _coerce_session_origin(session_raw)
            _fill_linkage_field_defaults(session_raw)
            _fill_last_result_default(session_raw)
            _fill_cost_fields_default(session_raw)
            _fill_session_lane_default(session_raw)
            _fill_session_stage_default(session_raw)
            _fill_session_consecutive_salvage_skips_default(session_raw)
            _fill_session_liveness_bucket_default(session_raw)
            _fill_session_consecutive_park_vetoes_default(session_raw)
            _fill_session_last_result_source_default(session_raw)
            _fill_session_consecutive_sentinel_mismatch_vetoes_default(session_raw)
    # Bump persisted schema_version to current after all migration steps.
    raw["schema_version"] = CW_STATE_SCHEMA_VERSION
    return raw


def _migrate_zellij_fields(session_raw: dict[str, Any]) -> None:
    """Rename the pre-0.4 zellij_pane field and drop zellij_tab.

    Migration armor — do not delete. Users in the wild still have
    `sessions.json` files from the Zellij era; the rename runs every load
    so upgrades stay transparent.
    """
    if "zellij_pane" in session_raw and "surface_ref" not in session_raw:
        session_raw["surface_ref"] = session_raw.pop("zellij_pane")
    else:
        session_raw.pop("zellij_pane", None)
    session_raw.pop("zellij_tab", None)


def _coerce_session_origin(session_raw: dict[str, Any]) -> None:
    """Reset unknown SessionOrigin values to 'user' with a warning.

    A stale sessions.json containing, for example, `origin: "delegate"`
    (a value that briefly existed in a branch but never landed) used to
    crash every cw command at Pydantic validation. Coerce instead, so
    users aren't locked out of their own state.
    """
    origin = session_raw.get("origin")
    if origin is not None and origin not in _VALID_SESSION_ORIGINS:
        logger.warning(
            "session %s has unknown origin %r; coercing to 'user'",
            session_raw.get("id", "<unknown>"),
            origin,
        )
        session_raw["origin"] = SessionOrigin.USER.value


def _fill_linkage_field_defaults(session_raw: dict[str, Any]) -> None:
    """Fill parent_session_id and worker_session_ids introduced in schema v2.

    Runs unconditionally and is idempotent: if the fields are already present
    they are left untouched, so a v2 file round-trips without modification.
    The canonical source of truth for these defaults is the Session Pydantic
    model; this helper exists only to ensure the on-disk file gets the keys
    explicitly so re-saves don't lose them.
    """
    if "parent_session_id" not in session_raw:
        session_raw["parent_session_id"] = None
    if "worker_session_ids" not in session_raw:
        session_raw["worker_session_ids"] = []


def _fill_last_result_default(session_raw: dict[str, Any]) -> None:
    """Fill last_result introduced in schema v3.

    Idempotent like the linkage defaults helper. Sessions that pre-date the
    headless auto-dev parser have no last_result on disk; setting None
    explicitly keeps the on-disk shape stable across re-saves.
    """
    if "last_result" not in session_raw:
        session_raw["last_result"] = None


def _fill_cost_fields_default(session_raw: dict[str, Any]) -> None:
    """Fill cost_usd and cost_breakdown introduced in schema v4.

    Idempotent: existing values are preserved.
    """
    if "cost_usd" not in session_raw:
        session_raw["cost_usd"] = None
    if "cost_breakdown" not in session_raw:
        session_raw["cost_breakdown"] = None


def _fill_session_lane_default(session_raw: dict[str, Any]) -> None:
    """Fill Session.lane introduced in schema v9 (GitHub #594). Idempotent."""
    if "lane" not in session_raw:
        session_raw["lane"] = None


def _fill_session_stage_default(session_raw: dict[str, Any]) -> None:
    """Fill Session.stage introduced in schema v10 (GitHub #612). Idempotent."""
    if "stage" not in session_raw:
        session_raw["stage"] = None


def _fill_session_consecutive_salvage_skips_default(
    session_raw: dict[str, Any],
) -> None:
    """Fill Session.consecutive_salvage_skips introduced in schema v12 (#974).

    Idempotent.
    """
    if "consecutive_salvage_skips" not in session_raw:
        session_raw["consecutive_salvage_skips"] = 0


def _fill_session_liveness_bucket_default(session_raw: dict[str, Any]) -> None:
    """Fill Session.liveness_bucket introduced in schema v13 (GitHub #1001).

    Idempotent.
    """
    if "liveness_bucket" not in session_raw:
        session_raw["liveness_bucket"] = "live"


def _fill_session_consecutive_park_vetoes_default(
    session_raw: dict[str, Any],
) -> None:
    """Fill Session.consecutive_park_vetoes introduced in schema v15 (#1445).

    Idempotent.
    """
    if "consecutive_park_vetoes" not in session_raw:
        session_raw["consecutive_park_vetoes"] = 0


def _fill_session_last_result_source_default(session_raw: dict[str, Any]) -> None:
    """Fill Session.last_result_source introduced in schema v16 (#1456).

    Idempotent.
    """
    if "last_result_source" not in session_raw:
        session_raw["last_result_source"] = None


def _fill_session_consecutive_sentinel_mismatch_vetoes_default(
    session_raw: dict[str, Any],
) -> None:
    """Fill Session.consecutive_sentinel_mismatch_vetoes (schema v17, #1449).

    Idempotent.
    """
    if "consecutive_sentinel_mismatch_vetoes" not in session_raw:
        session_raw["consecutive_sentinel_mismatch_vetoes"] = 0


def _clear_non_hex_surface_refs(session_raw: dict[str, Any]) -> None:
    """Clear non-native surface_ref values (legacy cmux/tmux pane IDs).

    Native daemon workers store an 8-char hex short-id as surface_ref.
    Legacy cmux/tmux backends stored pane references like "ws:0.1" or
    "tmux-pane-3". Clear any value that doesn't match the native hex
    pattern so stale references don't confuse reconcile.
    """
    surface_ref = session_raw.get("surface_ref")
    if surface_ref is None:
        return
    if not SHORT_SESSION_ID_RE.fullmatch(surface_ref):
        session_raw["surface_ref"] = None


def _clear_stale_local_liveness(session_raw: dict[str, Any]) -> None:
    """Clear a pre-v14 local_liveness handle (boot-relative start_time_ns).

    A handle captured before the #921 psutil switch stores start_time_ns in
    the old boot-relative /proc format, which will never equal a freshly-read
    epoch-relative value for the same still-live process -- so leaving it in
    place would cause reconcile to misclassify a live aider session as dead
    on the first pass after upgrade. Clearing it drops the fast process-exit
    harvest path for that session; recovery falls back to stalled.py's
    headless wall-clock sweep (gated on _is_headless, not surface_ref/
    local_liveness -- LOCAL sessions never carry a surface_ref, so idle.py
    and the phantom detector in _shared.py both skip them) once
    resolve_headless_budget() elapses, or a fresh LocalExecutor spawn
    re-establishes an epoch-relative handle.
    """
    if session_raw.get("local_liveness") is not None:
        logger.info(
            "session %s: clearing pre-v%d local_liveness handle "
            "(boot-relative start_time_ns incompatible with epoch-relative "
            "format, GitHub #921)",
            session_raw.get("id", "<unknown>"),
            _EPOCH_LIVENESS_SCHEMA_VERSION,
        )
        session_raw["local_liveness"] = None
