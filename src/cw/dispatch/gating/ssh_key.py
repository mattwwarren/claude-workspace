"""SSH-agent-key preflight gate for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503): the per-tick
``ssh-add -l`` probe (#927), keyed on the client's push-remote transport
(#1495), with its operator error line, skip event and bypass event (#1437).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.dispatch.gating.context_json import _LOGGER_NAME
from cw.events import record_event
from cw.models import (
    DispatchSkipReason,
    OrchestratorEventType,
)
from cw.ssh import check_ssh_key_available, push_remote_scheme, remote_needs_ssh_probe

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        DevQueueStore,
    )
    from cw.ssh import RemoteScheme
from cw.dispatch.claim import _lane_occupants_for_client, _lane_stats_for_client

_log = logging.getLogger(_LOGGER_NAME)


# Sentinel key for the fleet-wide (not per-client) warned_ssh_key dedup set:
# the SSH-key preflight probe has no per-client dimension (a single local
# ssh-agent serves the whole fleet), so the operator error line is
# deduplicated on one shared sentinel rather than per-client.
_SSH_KEY_WARN_SENTINEL = "fleet"


def _resolve_ssh_key_once(ssh_key_available: bool | None) -> bool:
    """Return *ssh_key_available* unchanged, or resolve via ``check_ssh_key_available``.

    Per-tick memoization sibling of :func:`_resolve_availability_once`. No TTL
    cache, no persisted latch: ``check_ssh_key_available`` is instant and
    local (R1), so re-probing every tick this gate actually runs costs
    nothing -- the only reason to memoize at all is to avoid re-shelling out
    N times in the same tick for N clients, same rationale as the
    availability gate's per-tick memoization.
    """
    return check_ssh_key_available() if ssh_key_available is None else ssh_key_available


def _emit_ssh_key_skip(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    pending_count: int,
    running_count: int,
    cap: int,
    emit: Callable[[str], None] | None,
    warned_ssh_key: set[str] | None,
    remote_scheme: RemoteScheme,
) -> None:
    """Emit the operator error line (once per run) + dispatch.tick skip event.

    Mirrors :func:`_emit_availability_skip`'s dispatch.tick shape
    (``skip_reason=SSH_KEY_GATE``) but additionally prints the ticket's
    literal operator message exactly once per dispatch-loop run via ``emit``,
    deduplicated through ``warned_ssh_key`` (fleet-wide, not per-client --
    keyed on a single sentinel since this check has no per-client dimension).

    ``remote_scheme`` (#1495) records which push-remote transport engaged
    the probe (``ssh`` or ``unknown`` -- ``http``/``local`` clients never
    reach this helper), so a false gate is diagnosable from events alone.
    """
    if emit is not None and (
        warned_ssh_key is None or _SSH_KEY_WARN_SENTINEL not in warned_ssh_key
    ):
        emit(
            "Error: SSH key not available in agent."
            " Run 'ssh-add' to unlock before dispatching."
        )
        if warned_ssh_key is not None:
            warned_ssh_key.add(_SSH_KEY_WARN_SENTINEL)
    lane_occupants = _lane_occupants_for_client(client, queue_snapshot)
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client.name,
            "claimed": 0,
            "pending": pending_count,
            "running": running_count,
            "cap": cap,
            "skip_reason": DispatchSkipReason.SSH_KEY_GATE,
            "remote_scheme": remote_scheme,
            "lanes": _lane_stats_for_client(
                client, queue_snapshot, occupants=lane_occupants
            ),
            "lane_occupants": lane_occupants,
            "occupied": sum(len(v) for v in lane_occupants.values()),
        },
    )


def _emit_ssh_key_bypass(
    client: ClientConfig,
    *,
    probe_result: bool,
    gate_enabled: bool,
    remote_scheme: RemoteScheme,
) -> None:
    """Record the operator-forwarded bypass event (GitHub #1437).

    Sibling of :func:`_emit_ssh_key_skip`, NOT a reuse of it: called instead
    of that helper when the SSH-key probe reports unavailable but
    ``OrchestratorConfig.ssh_key_gate_enabled`` is False, so the would-be
    skip is suppressed and the client dispatches anyway. No operator stdout
    line here -- SSH_KEY_GATE_BYPASSED is in the default operator-channel
    forward-set (see ``_DEFAULT_OPERATOR_EVENT_TYPES``), so the forward-set
    already surfaces this to the operator channel without a duplicate
    stdout emit. No dispatch.tick skip event either: the client is not
    skipped.
    """
    record_event(
        OrchestratorEventType.SSH_KEY_GATE_BYPASSED,
        {
            "client": client.name,
            "probe_result": probe_result,
            "gate_enabled": gate_enabled,
            "remote_scheme": remote_scheme,
        },
    )


def _apply_ssh_key_gate(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    ssh_key_available: bool | None,
    pending_count: int,
    running_count: int,
    cap: int,
    emit: Callable[[str], None] | None,
    warned_ssh_key: set[str] | None,
    gate_enabled: bool,
) -> tuple[bool | None, bool]:
    """Run the SSH-agent-key gate; returns ``(resolved_probe, gated)``.

    Keys the gate on the transport the client's push actually uses (#1495):
    :func:`~cw.ssh.push_remote_scheme` resolves ``origin``'s effective push
    URL, and an ``http``/``local`` remote skips the probe entirely -- the
    memoized probe verdict is returned unresolved (still ``None``) and the
    client is never gated, since no SSH key is involved in its pushes. An
    ``ssh`` remote engages the pre-#1495 gate unchanged, and ``unknown``
    (resolution failed) deliberately does too, so a scheme lookup failure
    keeps the gate fail-closed instead of silently disabling it.

    The second element is True when *client* must be held PENDING (the
    dispatch.tick skip event has already been emitted). When the probe
    reports unavailable and *gate_enabled* is False (#1437), the would-be
    skip is suppressed: a bypass event is recorded and the client proceeds.

    Extracted from ``cw.dispatch.tick._run_preflight_gates`` for the same
    reason as :func:`_apply_disk_pressure_gate`: the remote-scheme branch
    would otherwise push that caller past the PLR0911/PLR0912 ceilings.
    """
    scheme = push_remote_scheme(client.repo_path or client.workspace_path)
    if not remote_needs_ssh_probe(scheme):
        _log.debug(
            "ssh_key_gate: skipping probe for %s (push remote scheme=%s)",
            client.name,
            scheme,
        )
        return ssh_key_available, False
    resolved = _resolve_ssh_key_once(ssh_key_available)
    if resolved:
        return resolved, False
    if not gate_enabled:
        _emit_ssh_key_bypass(
            client,
            probe_result=resolved,
            gate_enabled=gate_enabled,
            remote_scheme=scheme,
        )
        return resolved, False
    _emit_ssh_key_skip(
        client,
        queue_snapshot,
        pending_count=pending_count,
        running_count=running_count,
        cap=cap,
        emit=emit,
        warned_ssh_key=warned_ssh_key,
        remote_scheme=scheme,
    )
    return resolved, True
