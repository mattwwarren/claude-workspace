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
    # RFC 0011 S1 D-S2b — override for the GitHub login used in counterparty
    # (self|external) and self-identity resolution (see
    # cw.operator_identity.resolve_operator_login). Opaque string — no
    # validation. Default None: the runtime-resolved `gh api user` login
    # (cw.gh.current_gh_login, process-lifetime cached) is authoritative.
    # Set this only for the rare multi-account case where the operator's
    # logged-in gh identity differs from the login this client should treat
    # as "self."
    operator_github_login: str | None = None
    # #1465 — master switch for the codex backend's autonomous MUST_FIX fix
    # loop (cw.codex_fix_loop.run_review_with_fix_loop's fix_loop_enabled
    # kwarg is the enforcement seam: CodexExecutor.spawn threads this field
    # straight through). Default False, mirroring gate_recipes_enabled's and
    # concierge_enabled's fail-safe defaults: enabling `review: {backend:
    # codex}` must not implicitly enable autonomous fix commits. When False,
    # a blocking cycle-0 codex review parks on CODEX_MUST_FIX_FINDINGS with
    # zero fix cycles attempted — the pre-#1392 park-on-MUST_FIX behavior.
    # Set True to let the bounded fix-cycle loop run and commit fixes
    # autonomously.
    codex_fix_loop_enabled: bool = False
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
