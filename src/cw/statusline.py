"""The ``cw statusline render`` work-summary segment (#1644).

Renders one short line describing what a session is working on — e.g.
``client-a/impl 2▶ 1⧗ !1`` or ``client-a/impl PAUSED 0▶ 1⧗`` — from the local
JSON stores only. No ``gh``, no ``git``, no network, no subprocess (R1): the
inputs are ``focus.json``, ``dev_queue.json``, ``concurrency_overrides.json``,
and ``clients.yaml`` (a plain local ``yaml.safe_load`` via ``cw.config``, the
one non-JSON reader — disclosed in the plan, and timed at ~9ms against a real
17-client config, well inside the 300ms statusline debounce).

Resolution is a strict three-step ladder with no fourth fallback (R2):

1. The session's focused client/lane from ``focus.json``.
2. Otherwise, the client whose workspace/repo/worktree tree contains *cwd*,
   aggregated across all its lanes.
3. Otherwise, the empty string.

A focus entry naming a client or lane that ``clients.yaml`` no longer declares
falls through to step 2 — config drift is a designed-in eventuality under R6's
no-pruning policy, and is treated exactly like an unknown session.

:func:`render_work_segment` must never raise (R3): it is invoked on every
assistant message, so it carries an outer ``except Exception`` that degrades to
the empty string, mirroring ``cw guard-cwd``'s silent must-never-crash idiom.
"""

from __future__ import annotations

from pathlib import Path

from cw.config import (
    _load_concurrency_overrides,
    load_clients,
)
from cw.dev_queue import load_dev_queue, task_attention_state
from cw.focus import get_focus
from cw.models import ClientConfig, QueueItemStatus, TicketTask
from cw.worktree import effective_worktree_bases

# R5 pins the bare word, deliberately distinct from the bracketed
# ``_PAUSED_LANE_MARKER`` that ``cw dev-queue status`` renders.
_PAUSED_MARKER = "PAUSED"
_RUNNING_GLYPH = "▶"
_PENDING_GLYPH = "⧗"
_ATTENTION_GLYPH = "!"


def _client_roots(client: ClientConfig) -> set[Path]:
    """Every directory tree that means "you are working in *client*".

    The declared workspace, the backing repo for a worktree-mode client, and
    each base ``cw`` may have provisioned worktrees under. Pure path arithmetic
    — ``effective_worktree_bases`` shells out to nothing (R1).
    """
    roots = {client.workspace_path, *effective_worktree_bases(client)}
    if client.repo_path is not None:
        roots.add(client.repo_path)
    return {Path(root).resolve() for root in roots}


def resolve_client_for_cwd(cwd: Path) -> str | None:
    """Return the configured client whose tree contains *cwd*, else None.

    When several clients match (e.g. a client whose workspace nests inside
    another's), the longest — most specific — root wins, so the answer does not
    depend on ``clients.yaml`` ordering.
    """
    resolved = Path(cwd).resolve()
    best_name: str | None = None
    best_depth = -1
    for name, client in load_clients().items():
        for root in _client_roots(client):
            if resolved == root or root in resolved.parents:
                depth = len(root.parts)
                if depth > best_depth:
                    best_name, best_depth = name, depth
    return best_name


def _load_tasks() -> list[TicketTask]:
    """Every queued task, or ``[]`` when ``dev_queue.json`` is unreadable.

    ``load_dev_queue`` deliberately raises on a corrupt payload — other callers
    (dispatch, reconcile) want that loud failure. The statusline is not one of
    them, so the tolerance is applied here rather than in the shared loader.
    """
    try:
        return load_dev_queue().tasks
    except (ValueError, OSError):
        return []


def _lane_is_paused(client: str, lane: str) -> bool:
    """True when ``concurrency_overrides.json`` marks ``<client>/<lane>`` paused."""
    override = _load_concurrency_overrides().lanes.get(f"{client}/{lane}")
    return override is not None and bool(override.paused)


def _format_segment(label: str, tasks: list[TicketTask], *, paused: bool) -> str:
    """Render ``<label>[ PAUSED] <n>▶ <n>⧗[ !<n>]`` for *tasks*.

    The ``!N`` suffix is suppressed at zero (R5), so a healthy lane stays terse.
    """
    running = sum(1 for t in tasks if t.status == QueueItemStatus.RUNNING)
    pending = sum(1 for t in tasks if t.status == QueueItemStatus.PENDING)
    attention = sum(1 for t in tasks if task_attention_state(t) is not None)
    paused_part = f" {_PAUSED_MARKER}" if paused else ""
    attention_part = f" {_ATTENTION_GLYPH}{attention}" if attention else ""
    return (
        f"{label}{paused_part}"
        f" {running}{_RUNNING_GLYPH} {pending}{_PENDING_GLYPH}"
        f"{attention_part}"
    )


def _render_focused(client: str, lane: str | None) -> str:
    """Step 1's segment: one lane's counts, or the client's aggregate."""
    tasks = [
        t
        for t in _load_tasks()
        if t.client == client and (lane is None or t.lane == lane)
    ]
    if lane is None:
        # No lane detail in the aggregate view, pause state included: an
        # operator who wants to see a paused lane can focus that lane.
        return _format_segment(client, tasks, paused=False)
    return _format_segment(
        f"{client}/{lane}", tasks, paused=_lane_is_paused(client, lane)
    )


def _focus_is_declared(
    client: str, lane: str | None, clients: dict[str, ClientConfig]
) -> bool:
    """True when *client* (and *lane*, if set) still exist in ``clients.yaml``."""
    client_cfg = clients.get(client)
    if client_cfg is None:
        return False
    return lane is None or lane in {ln.name for ln in client_cfg.effective_lanes}


def _resolve_segment(session_id: str | None, cwd: Path) -> str:
    """The R2 three-step ladder. Kept separate from the never-raise guard."""
    clients = load_clients()

    # Step 1 — the session's own focus, when it still names live config.
    entry = get_focus(session_id) if session_id else None
    if entry is not None and _focus_is_declared(entry.client, entry.lane, clients):
        return _render_focused(entry.client, entry.lane)

    # Step 2 — the client whose tree contains cwd, aggregated across lanes.
    client = resolve_client_for_cwd(cwd)
    if client is not None:
        return _format_segment(
            client,
            [t for t in _load_tasks() if t.client == client],
            paused=False,
        )

    # Step 3 — nothing to say.
    return ""


def render_work_segment(session_id: str | None, cwd: Path) -> str:
    """Return the statusline work segment, or ``""`` when there is nothing to show.

    Never raises (R3). Any unexpected failure degrades to the empty string —
    a statusline that crashed on every assistant message would be strictly
    worse than one that occasionally says nothing.
    """
    try:
        return _resolve_segment(session_id, cwd)
    except Exception:  # noqa: BLE001 — machine-invoked; must never crash.
        return ""
