"""Per-client reviewer agent-spec resolution drift check for cw doctor (#1776).

Setup-time visibility into #1773's per-role agent-spec resolution: for every
configured client and every reviewer role in ``_REVIEWER_ROLE_AGENT_FILES``,
resolves the spec via ``cw.codex_review``'s ``_resolve_agent_spec`` (repo-local
first, then ``~/.claude/agents/<role>.md`` when the per-repo
``agent_spec_global_fallback`` gate allows it) and reports which roles would
run unspecified. No resolution logic lives here — every decision (repo-vs-
global order, the blank-tracked-file distinction, the fallback gate) is read
straight through #1773's implementation in ``cw.codex_review._context``. One
``CheckResult`` per client, always ``ok=True`` (advisory, mirroring
``skills-commands-drift``); ``warn=True`` when any reviewer role has no usable
spec. Detection only — no writes. Leaf module — no cross-``doctor``
dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_review import (
    _REVIEWER_ROLE_AGENT_FILES,
    _load_agent_spec_fallback_gate,
    _resolve_agent_spec,
)
from cw.doctor._shared import CheckResult

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.review_findings import AgentSpecStatus

# Check name for the per-client agent-spec-drift check ("<name>/<client>").
_CHECK_NAME = "agent-spec-drift"


def _role_satisfied(status: AgentSpecStatus) -> bool:
    """True when *status* resolved to a usable (non-blank) spec."""
    return not status.empty


def _absent_role_note(status: AgentSpecStatus) -> str:
    """Describe one unspecified role, distinguishing blank from absent files."""
    filename = _REVIEWER_ROLE_AGENT_FILES[status.role]
    if status.empty_repo_file:
        return f"{status.role} ({filename}, blank tracked file)"
    return f"{status.role} ({filename}, not found)"


def _build_client_detail(
    root: Path, statuses: list[AgentSpecStatus], *, fallback_enabled: bool
) -> str:
    """Build the per-client detail string summarizing role resolution."""
    total = len(statuses)
    absent = [status for status in statuses if not _role_satisfied(status)]
    repo_count = sum(
        1 for status in statuses if status.source == "repo" and _role_satisfied(status)
    )
    global_count = sum(
        1
        for status in statuses
        if status.source == "global" and _role_satisfied(status)
    )

    if not absent:
        return (
            f"{total}/{total} roles resolved ({repo_count} repo, {global_count} global)"
        )

    resolved = total - len(absent)
    agents_dir = root / ".claude" / "agents"
    if resolved == 0 and not agents_dir.exists():
        missing = ", ".join(_absent_role_note(status) for status in absent)
        return (
            f"no {agents_dir} directory; install reviewer specs there"
            f" — missing: {missing}"
        )

    notes = ", ".join(_absent_role_note(status) for status in absent)
    detail = (
        f"{len(absent)}/{total} roles unspecified: {notes}"
        f" ({repo_count} repo, {global_count} global resolve)"
    )
    if not fallback_enabled:
        detail += " (global fallback disabled for this repo)"
    return detail


def _check_agent_spec_drift_for_client(name: str, client: ClientConfig) -> CheckResult:
    """Resolve every reviewer role's agent spec for one client and report drift."""
    root = client.repo_path or client.workspace_path
    fallback_enabled = _load_agent_spec_fallback_gate(root)
    statuses = [
        _resolve_agent_spec(root, role, global_fallback_enabled=fallback_enabled).status
        for role in _REVIEWER_ROLE_AGENT_FILES
    ]
    return CheckResult(
        f"{_CHECK_NAME}/{name}",
        ok=True,
        warn=any(not _role_satisfied(status) for status in statuses),
        detail=_build_client_detail(root, statuses, fallback_enabled=fallback_enabled),
    )


def _check_agent_spec_drift(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Run the agent-spec-drift check for every configured client."""
    return [
        _check_agent_spec_drift_for_client(name, client)
        for name, client in clients.items()
    ]
