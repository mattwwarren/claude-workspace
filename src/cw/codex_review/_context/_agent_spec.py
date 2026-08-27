"""Per-role agent-spec resolution and the repo gate that governs its fallback.

The agent specification is the one reviewer input that is not read exclusively
from the worktree (#1773): a repo whose ``.claude/agents/<role>.md`` is missing
or blank falls back to the operator's own ``~/.claude/agents/<role>.md`` rather
than running the reviewer with an empty ``## Agent Specification`` section. The
fallback is gateable per-repo via
``[tool.cw.codex_review].agent_spec_global_fallback`` in ``pyproject.toml``, and
every outcome is reported on the returned :class:`AgentSpecStatus` rather than
swallowed.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_review._context._util import _load_optional_text
from cw.review_findings import AgentSpecStatus

if TYPE_CHECKING:
    from cw.review_findings import AgentSpecSource

_log = logging.getLogger(__name__)

# Reviewer role name -> authoritative agent-spec file under .claude/agents/.
# Role names match the `/review` Step 3 table and each agent file's `name:`.
_REVIEWER_ROLE_AGENT_FILES: dict[str, str] = {
    "Code Quality Reviewer": "code-reviewer.md",
    "SysAdmin Reviewer": "sysadmin-reviewer.md",
    "Data Safety Reviewer": "data-safety-reviewer.md",
    "Product Manager Reviewer": "product-manager-reviewer.md",
    "Architecture Reviewer": "architecture-reviewer.md",
    "Test Reviewer": "test-reviewer.md",
    "Performance Reviewer": "performance-reviewer.md",
    "API Contract Validator": "api-contract-validator.md",
    "Deployment Reviewer": "deployment-reviewer.md",
}

# The operator's own agent-spec directory, used as the fallback source when a
# worktree carries no usable ``.claude/agents/<role>.md`` (#1773). Bound at
# import time so tests can redirect it away from the real home (conftest's
# autouse ``_isolate_global_agents_dir``) — the fallback must never make a
# review pass depend on what happens to be installed on the host.
_GLOBAL_AGENTS_DIR: Path = Path.home() / ".claude" / "agents"


def _load_agent_spec_fallback_gate(worktree: Path) -> bool:
    """Read ``[tool.cw.codex_review].agent_spec_global_fallback`` (#1773).

    Same ``tomllib.load`` + fail-safe idiom as
    :func:`~cw.codex_review._context._repo_config._load_ruff_lint_config`, and
    the same reason for it: a repo that cannot be parsed must not silently
    change reviewer behavior. Defaults to ``True`` — a missing file, a missing
    table, a missing key, a non-boolean value, or malformed TOML all leave the
    fallback ENABLED. Only an explicit ``false`` turns it off, which is the
    opt-out for a repo that wants its reviewers grounded exclusively in its own
    tracked specs.
    """
    try:
        with (worktree / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, OSError):
        return True
    section = data.get("tool", {}).get("cw", {}).get("codex_review", {})
    value = section.get("agent_spec_global_fallback", True)
    return value if isinstance(value, bool) else True


class _AgentSpecResolution(NamedTuple):
    """One role's resolved agent-spec text plus the status describing it."""

    text: str
    status: AgentSpecStatus


def _resolve_agent_spec(
    worktree: Path, role: str, *, global_fallback_enabled: bool
) -> _AgentSpecResolution:
    """Resolve *role*'s agent spec repo-local-first, then global (#1773).

    Order: the worktree's ``.claude/agents/<role>.md`` wins whenever it exists
    and is non-blank. A missing OR blank repo copy falls through to
    ``_GLOBAL_AGENTS_DIR/<role>.md`` when *global_fallback_enabled*; with the
    gate off, the repo copy's state stands as the answer.

    Never raises and never returns ``None``: an unresolvable spec yields ``""``
    (the pre-#1773 fail-open behavior — a review pass still runs) but is now
    reported rather than silent. ``_log.warning`` fires only when the spec is
    genuinely absent everywhere consulted (``source == "none"``); a file that
    was found but blank is carried on the returned status for the verdict
    comment instead, because "present but empty" and "not there at all" are
    different facts about the repo.
    """
    filename = _REVIEWER_ROLE_AGENT_FILES[role]
    repo_path = worktree / ".claude" / "agents" / filename
    repo_text = _load_optional_text(repo_path)
    empty_repo_file = repo_text is not None and not repo_text.strip()

    def _resolution(text: str, source: AgentSpecSource) -> _AgentSpecResolution:
        usable = text if text.strip() else ""
        return _AgentSpecResolution(
            text=usable,
            status=AgentSpecStatus(
                role=role,
                source=source,
                empty=not usable,
                empty_repo_file=empty_repo_file,
            ),
        )

    if repo_text is not None and repo_text.strip():
        return _resolution(repo_text, "repo")
    if not global_fallback_enabled:
        if repo_text is None:
            _warn_agent_spec_absent(role, [repo_path])
            return _resolution("", "none")
        return _resolution("", "repo")
    global_path = _GLOBAL_AGENTS_DIR / filename
    global_text = _load_optional_text(global_path)
    if global_text is None:
        _warn_agent_spec_absent(role, [repo_path, global_path])
        return _resolution("", "none")
    return _resolution(global_text, "global")


def _warn_agent_spec_absent(role: str, paths: list[Path]) -> None:
    """Log the genuinely-absent-spec warning naming *role* and every path tried."""
    _log.warning(
        "agent_spec_absent: reviewer role %r has no agent specification — "
        "tried %s; this role's prompt will run with an empty "
        "`## Agent Specification` section",
        role,
        ", ".join(str(p) for p in paths),
    )
