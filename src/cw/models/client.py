"""Client workspace configuration model.

Depends on ``cw.models.enums``, ``cw.models.tasks`` (for ``DEFAULT_LANE``), and
``cw.models.orchestrator_config``. See ``cw.models.__init__`` for the full DAG.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cw.models.enums import SessionPurpose
from cw.models.orchestrator_config import LaneConfig, StagePipelineConfig
from cw.models.tasks import DEFAULT_LANE

DEFAULT_AUTO_PURPOSES: list[SessionPurpose] = [
    SessionPurpose.IDEA,
    SessionPurpose.IMPL,
    SessionPurpose.DEBT,
]


class TrackerMcpGateConfig(BaseModel):
    """Per-client opt-in for the #2442 pre-dispatch tracker-MCP gate.

    Before a PLAN/IMPL-stage PENDING ticket is claimed, dispatch reads
    ``settings_path`` from the ticket's own branch (``git show``, no checkout)
    and parks the ticket when that file's ``enabledPlugins`` verifiably lacks
    ``plugin_id`` -- a worker spawned there could not reach the tracker MCP.

    ``plugin_id`` is the exact ``enabledPlugins`` key the tracker's MCP plugin
    registers under (e.g. ``"linear@acme-marketplace"``); matching is exact,
    with no ``@``-suffix stripping and no case folding. Anything ambiguous --
    no branch yet, no settings file, malformed JSON, an unrecognized
    ``enabledPlugins`` shape -- fails open (the spawn proceeds).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    plugin_id: str
    settings_path: str = ".claude/settings.json"


class ClientConfig(BaseModel):
    """Configuration for a client workspace.

    Two modes:
    - **Legacy**: ``workspace_path`` points to an existing clone.
    - **Worktree**: ``repo_path`` + ``branch`` are set.  ``workspace_path``
      is auto-set to ``repo_path`` as a sentinel; the real worktree path is
      resolved at session start time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    # Typed as Path but defaults to None; the model validator below guarantees
    # it is always set after construction (from either the user or repo_path).
    workspace_path: Path = Field(default=None)  # type: ignore[assignment]
    repo_path: Path | None = None
    branch: str | None = None
    default_branch: str = "main"
    # Prefix for the per-ticket feature branch the staged pipeline provisions
    # and the auto-dev skills push to: ``<feature_branch_prefix>/<ticket_id>``
    # (e.g. ``dev/662``). Single source of truth shared by cw's worktree
    # provisioning (dispatch) and the skill's branch_pattern, so cw and the
    # worker agree on one branch — no mid-pipeline rename that would trip the
    # worktree-reuse guard (#712). Distinct from the session-name prefix
    # ``auto-dev/`` (AUTO_DEV_LABEL_PREFIX), which stays for reconcile's
    # ticket-from-session-name parsing.
    feature_branch_prefix: str = "dev"
    worktree_base: Path | None = None
    auto_purposes: list[SessionPurpose] = Field(
        default_factory=lambda: list(DEFAULT_AUTO_PURPOSES),
    )
    purpose_prompts: dict[str, str] = Field(default_factory=dict)
    # When set, ``cw`` passes ``--model <worker_model>`` to ``claude --bg``
    # for DAEMON-origin spawns (auto-dev workers, including resume re-spawns
    # in :func:`cw.session.resume_session`). Opaque string — no validation;
    # user is responsible for matching Anthropic's published model ids.
    # Default ``None`` inherits the user's logged-in default model.
    # See issue #248.
    worker_model: str | None = None
    # Per-client override for the gate command(s) named in the impl/debt session
    # prompts (default: "ruff check, mypy, pytest"). Opaque, unvalidated string,
    # consumed only by get_purpose_prompt(). None = inherit the default triad;
    # "" = omit the gate sentence entirely.
    #
    # There are three unrelated "quality gates" concepts in this codebase —
    # if you are the fourth, read this first:
    #   1. project-config.yaml `quality_gates:` — dead, deleted by #1616,
    #      read by nothing.
    #   2. clients.yaml `quality_gate_commands` (this field) — feeds the
    #      worker session prompt.
    #   3. CLAUDE.md "## Quality Gates" section — #1744, feeds codex-review
    #      lint grounding.
    quality_gate_commands: str | None = None
    # Per-client ignore list for Step 4a's merge-gate overlap check (#2431).
    # Repo-relative, root-anchored, exact-match paths (no globs) excluded from
    # the branch/PR file intersection before it escalates to a `git
    # merge-tree` probe -- e.g. a checked-in mypy-baseline.txt or lock file
    # that nearly every PR touches. It does not excuse a genuine textual
    # conflict in a listed path; the gate simply never looks at that path.
    # Delivered to headless workers via `.claude/cw-context.json` (schema v11,
    # see cw.spawn.CW_CONTEXT_SCHEMA_VERSION), never read from clients.yaml
    # inside an agent's own bash.
    merge_gate_ignore_paths: list[str] = Field(default_factory=list)
    # RFC 0011 S1 D-S2b — override for the GitHub login used in counterparty
    # (self|external) and self-identity resolution (see
    # cw.operator_identity.resolve_operator_login). Opaque string — no
    # validation. Default None: the runtime-resolved `gh api user` login
    # (cw.gh.current_gh_login, process-lifetime cached) is authoritative.
    # Set this only for the rare multi-account case where the operator's
    # logged-in gh identity differs from the login this client should treat
    # as "self."
    operator_github_login: str | None = None
    # Per-client rollout override for the #2077 worktree-occupancy screen.
    # None inherits OrchestratorConfig.occupancy_gate_enabled; False skips
    # the pre-claim probe for this client only. The global switch remains the
    # fleet-wide emergency control.
    occupancy_gate_enabled: bool | None = None
    # Per-client opt-in for the #2401 deterministic-parse and #2405
    # unrecognized-reason catch-all requeue policies. False preserves terminal
    # handling for this client. Enable explicitly after reconciling any
    # already-requeued rows/events for the client.
    blocked_result_requeue_enabled: bool = False
    # Per-client opt-in for the #2405 cap-only phantom stage-mismatch veto.
    # False retains the pre-#2405 transcript-liveness fallback. Keep this
    # setting readable so clients.yaml written during the staged rollout
    # remains loadable after the policy becomes the default implementation.
    sentinel_mismatch_veto_enabled: bool = False
    # Per-client opt-in for the #2442 pre-dispatch tracker-MCP gate. None (the
    # default) means the gate never runs for this client -- it never even
    # reads the branch's settings file. There is deliberately no fleet-wide
    # OrchestratorConfig companion toggle: per-client opt-in IS the rollout
    # control. See TrackerMcpGateConfig above.
    tracker_mcp_gate: TrackerMcpGateConfig | None = None
    auto_background_threshold: int | None = None
    notifications: bool = False
    lanes: list[LaneConfig] = Field(default_factory=list)
    # RFC 0005 A1 — dormant pipeline config; no dispatch wiring yet (#612).
    pipeline: StagePipelineConfig = Field(default_factory=StagePipelineConfig)

    @property
    def effective_lanes(self) -> list[LaneConfig]:
        """Return declared lanes; synthesize a default lane when none are declared."""
        if self.lanes:
            return list(self.lanes)
        return [LaneConfig(name=DEFAULT_LANE)]

    @property
    def lane_names(self) -> set[str]:
        """Names of this client's effective lanes, for membership checks."""
        return {lane.name for lane in self.effective_lanes}

    @model_validator(mode="after")
    def _validate_path_config(self) -> ClientConfig:
        has_workspace = self.workspace_path is not None
        has_repo = self.repo_path is not None and self.branch is not None

        if not has_workspace and not has_repo:
            msg = "Either workspace_path or both repo_path + branch must be set"
            raise ValueError(msg)

        if self.repo_path is not None and not has_workspace:
            # Sentinel: real path resolved at start time via create_worktree
            self.workspace_path = self.repo_path

        return self

    @property
    def is_worktree_client(self) -> bool:
        """True when this client uses repo_path + branch (worktree mode)."""
        return self.repo_path is not None and self.branch is not None
