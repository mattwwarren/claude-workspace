"""Client workspace configuration model.

Depends on ``cw.models.enums``, ``cw.models.tasks`` (for ``DEFAULT_LANE``), and
``cw.models.orchestrator_config``. See ``cw.models.__init__`` for the full DAG.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import ClassVar, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from cw.models.enums import SessionPurpose
from cw.models.orchestrator_config import (
    CODEX_BACKEND,
    OPENCODE_BACKEND,
    LaneConfig,
    StagePipelineConfig,
)
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

    ``plugin_id`` and ``settings_path`` apply to claude-native rows only.
    ``servers`` (#3055, RFC 0018 S3) maps a non-Claude backend (``codex`` or
    ``opencode``) to the MCP server name its own config declares for the
    tracker; ``servers[backend]`` applies to that backend's rows only, and in
    S3 only opencode PLAN/IMPL rows are gated on it. At least one of
    ``plugin_id`` or a non-empty ``servers`` is required. The server check is
    declaration-only: auth, env and headless load stay unverified.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    plugin_id: str | None = None
    settings_path: str = ".claude/settings.json"
    servers: dict[str, str] | None = None

    _SERVER_BACKENDS: ClassVar[frozenset[str]] = frozenset(
        {CODEX_BACKEND, OPENCODE_BACKEND}
    )

    @model_validator(mode="after")
    def _validate_gate_target(self) -> TrackerMcpGateConfig:
        if self.plugin_id is None and not self.servers:
            msg = "tracker_mcp_gate needs plugin_id or servers (a non-empty map)"
            raise ValueError(msg)
        for backend, server in (self.servers or {}).items():
            if backend not in self._SERVER_BACKENDS:
                msg = (
                    f"tracker_mcp_gate.servers key {backend!r} is not one of "
                    f"{sorted(self._SERVER_BACKENDS)}"
                )
                raise ValueError(msg)
            if not server.strip():
                msg = f"tracker_mcp_gate.servers[{backend!r}] is blank"
                raise ValueError(msg)
        return self


# Bounds of the #2917 mechanical repair/verify command timeouts. The post-lock
# drain runs repair jobs serially, so each declared command is capped.
MIN_REPAIR_COMMAND_TIMEOUT_SECONDS = 1
MAX_REPAIR_COMMAND_TIMEOUT_SECONDS = 600
DEFAULT_REPAIR_TIMEOUT_SECONDS = 300
DEFAULT_VERIFY_TIMEOUT_SECONDS = 120
_PATH_FORBIDDEN_SEGMENTS = frozenset({".", ".."})
_NUL = "\0"


def _validate_repo_glob(pattern: str, *, field: str = "paths") -> str:
    """One repo-relative glob: no leading or trailing ``/``, no ``.``/``..`` segment."""
    problem = None
    if not pattern:
        problem = "is empty"
    elif pattern.startswith("/"):
        problem = "is absolute"
    elif "\\" in pattern:
        problem = "contains a backslash"
    elif pattern.endswith("/"):
        problem = "ends with '/' (it never matches a file; use 'dir/**')"
    elif _PATH_FORBIDDEN_SEGMENTS.intersection(pattern.split("/")):
        problem = "has a '.' or '..' segment"
    if problem is not None:
        msg = f"{field} entry {pattern!r} {problem}; use a repo-relative glob"
        raise ValueError(msg)
    return pattern


def _validate_argv(argv: list[str]) -> list[str]:
    """A non-empty argv list of non-empty, NUL-free strings (no shell)."""
    if not argv:
        msg = "command must be a non-empty argv list"
        raise ValueError(msg)
    for arg in argv:
        if not arg or _NUL in arg:
            msg = f"command argument {arg!r} is empty or contains NUL"
            raise ValueError(msg)
    return argv


class MechanicalConflictRepairConfig(BaseModel):
    """Per-client opt-in for the #2917 ``repair_mechanical_conflict`` recipe.

    When a shipped PR conflicts with its default branch ONLY in files matching
    ``paths`` (anchored, repo-relative globs; ``**`` crosses directories, ``*``
    does not), the recipe merges ``origin/<default>`` into the branch in a
    detached scratch tree, runs ``repair_command`` there, verifies that
    nothing outside ``paths`` changed, runs ``verify_command``, and
    force-pushes with a lease. Presence of the entry is the opt-in; a lane or
    ticket ``review_recipes`` value of ``false`` vetoes it.

    Commands are argv lists run without a shell. A relative ``argv[0]`` that
    contains ``/`` resolves against the client workspace, never the branch
    under repair; arguments and the working directory are the scratch tree
    (branch content). Each command is bounded by its timeout (1-600 s).
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1)
    repair_command: list[str]
    verify_command: list[str]
    repair_timeout_seconds: int = Field(
        default=DEFAULT_REPAIR_TIMEOUT_SECONDS,
        ge=MIN_REPAIR_COMMAND_TIMEOUT_SECONDS,
        le=MAX_REPAIR_COMMAND_TIMEOUT_SECONDS,
    )
    verify_timeout_seconds: int = Field(
        default=DEFAULT_VERIFY_TIMEOUT_SECONDS,
        ge=MIN_REPAIR_COMMAND_TIMEOUT_SECONDS,
        le=MAX_REPAIR_COMMAND_TIMEOUT_SECONDS,
    )

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, value: list[str]) -> list[str]:
        return [_validate_repo_glob(pattern) for pattern in value]

    @field_validator("repair_command", "verify_command")
    @classmethod
    def _check_command(cls, value: list[str]) -> list[str]:
        return _validate_argv(value)


DEFAULT_BLOCKING_MAX_CYCLES = 5
DEFAULT_CLEAN_MAX_CYCLES = 8
# The top-level ``client`` key of ``.claude/cw-context.json`` (the client name),
# spelled by its producer (``_write_hook_context``) through this constant.
CLIENT_CONTEXT_KEY = "client"

_log = logging.getLogger(__name__)

#: Retired ``review`` keys already warned about in this process (#2979), so a
#: ``cw`` invocation that loads several clients, or reloads one, logs the
#: removal once rather than once per validation.
_WARNED_RETIRED_REVIEW_KEYS: set[str] = set()

#: The ``review`` key retired with the delta confirm round (#2918, #2979).
_RETIRED_DELTA_CONFIRM_KEY: Final = "default_merge_delta_confirm"


#: A bare skill name: the repo provides the skill files, so there is no
#: ``plugin:skill`` form and no path. Matched with ``re.fullmatch`` (a bare ``$``
#: would admit a trailing newline); ``..`` is rejected separately.
ENGINE_SKILL_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
MAX_ENGINE_GUARDED_PATHS = 32


class ReviewEngineConfig(BaseModel):
    """Per-client opt-in for a repo review engine (#3036, RFC 0017 S1).

    Presence of the block is the opt-in; there is no ``enabled`` flag and no
    schema-version pin. ``skill`` names the repo's own review skill, which
    becomes the round engine. The repo provides the skill files and cw probes
    for nothing: a misnamed skill surfaces when a round runs, not at load.

    There is no load-time lane cross-check. Lanes that do not run the
    Claude-native review ignore the engine. The ``cw doctor`` warning names
    such a lane (I1, #3039), and the engine reaches ``.claude/cw-context.json``
    only for the Claude-native review stage.

    ``guarded_paths`` are repo-relative globs for files outside the skill
    directory that the engine depends on, such as validator scripts; I1's
    D-13 check treats a diff touching them like a diff touching the skill.
    Each entry follows the ``MechanicalConflictRepairConfig.paths`` rules, and
    at most ``MAX_ENGINE_GUARDED_PATHS`` entries are allowed.

    The engine requires ``review.convergence: clean``; ``ReviewConfig``
    enforces that rule, since it spans both keys.
    """

    model_config = ConfigDict(extra="forbid")

    skill: str
    guarded_paths: list[str] = Field(default_factory=list)

    @field_validator("skill")
    @classmethod
    def _check_skill(cls, value: str) -> str:
        if re.fullmatch(ENGINE_SKILL_NAME_RE, value) is None or ".." in value:
            msg = (
                f"review.engine.skill {value!r} is not a bare skill name; "
                "use letters, digits, '_', '.' and '-' (no '/', ':', '..' or "
                "whitespace, and no leading '.', '_' or '-')"
            )
            raise ValueError(msg)
        return value

    @field_validator("guarded_paths")
    @classmethod
    def _check_guarded_paths(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_ENGINE_GUARDED_PATHS:
            msg = (
                f"guarded_paths has {len(value)} entries; the cap is "
                f"{MAX_ENGINE_GUARDED_PATHS} (first excess entry: index "
                f"{MAX_ENGINE_GUARDED_PATHS}, "
                f"{value[MAX_ENGINE_GUARDED_PATHS]!r})"
            )
            raise ValueError(msg)
        return [_validate_repo_glob(entry, field="guarded_paths") for entry in value]


class RenumberPattern(BaseModel):
    """One migration-renumber rule of the attestation verifier (#3040).

    A commit that renames a file matching ``path_glob`` and edits only lines
    matching ``line_regex`` (``re.search``) is a migration renumber, which
    ``cw review attestation check`` tolerates after the attested head.
    ``path_glob`` follows the ``MechanicalConflictRepairConfig.paths`` rules.
    """

    model_config = ConfigDict(extra="forbid")

    path_glob: str
    line_regex: str

    @field_validator("path_glob")
    @classmethod
    def _check_path_glob(cls, value: str) -> str:
        return _validate_repo_glob(value, field="renumber_patterns path_glob")

    @field_validator("line_regex")
    @classmethod
    def _check_line_regex(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"renumber_patterns line_regex {value!r} does not compile: {exc}"
            raise ValueError(msg) from exc
        return value


class ReviewAttestationConfig(BaseModel):
    """Per-client settings of the review sign-off attestation (#3040, RFC 0017).

    ``renumber_patterns`` extend, never replace, the verifier's default
    migration-renumber rules (``cw.review_attestation_tolerance``). Valid in
    either review mode: classes (b) to (d) apply to every client.

    ``pr_section`` (#3043, RFC 0017 II2) opts the client into the finalize-written
    ``## Review sign-off`` PR-body section. It covers engine-backed and
    engine-less attestations alike, and is read from the live config by
    ``cw review signoff render``; it is never stamped into the context.
    """

    model_config = ConfigDict(extra="forbid")

    renumber_patterns: list[RenumberPattern] = Field(default_factory=list)
    pr_section: StrictBool = False


class ReviewDebtAutoEnqueueConfig(BaseModel):
    """Per-client toggle for review-debt auto-enqueue (#3045, RFC 0017 D-8).

    Presence of the block is the toggle; absence means off. There is no
    ``enabled`` flag. ``lane`` names the debt lane a merged PR's debt tickets
    enqueue into; it must be one of the client's explicitly declared ``lanes:``
    (checked by ``ClientConfig``). The setting is operator-only: no worker or
    ``cw lane set`` path writes it, it is not stamped into the cw context, and
    it is not retroactive (historical merges are not back-filled).
    """

    model_config = ConfigDict(extra="forbid")

    lane: str = Field(min_length=1)


class ReviewConfig(BaseModel):
    """Per-client Stage 3 review settings (#2787 converge-to-clean mode).

    ``convergence="blocking"`` (the default) keeps the historical loop that
    stops once no MUST_FIX survives. ``"clean"`` keeps cycling until a full
    round is clean at the reviewed sha and an independent confirm round agrees.

    ``max_cycles`` caps clean-mode rounds only; ``None`` resolves to
    ``DEFAULT_CLEAN_MAX_CYCLES`` there, and the configured value is inert in
    blocking mode. ``confirm_backend`` is the raw configured choice (``None``
    when unset); the resolved backend is picked at convergence time.

    ``default_merge_tolerance`` applies in clean mode only and is inert in
    blocking mode. It is on by default: finalize's tip check then accepts a
    merge of ``origin/<default>`` made after the last clean round when git
    itself would have written it (no conflict resolution, no hand edit),
    whatever files it touches (#2979). ``converge init``, ``converge status``
    and the dispatch REVIEW staleness gate apply the same rule (#2861).
    ``cw review attestation check`` applies the same rule to a merge after the
    attested head (#3040): tolerated only in clean mode with the flag on.
    ``False`` restores the strict final-sha rule for every merge, on every one
    of those paths.

    The retired ``default_merge_delta_confirm`` key (#2918, retired by #2979)
    is stripped with a once-per-process warning so an existing
    ``clients.yaml`` keeps loading; delete it from the config.

    ``confirm_reentry`` applies in clean mode only and is inert in blocking
    mode. It also needs the lane's codex fix loop enabled. It is on by
    default: a dirty Claude confirm round on a codex lane re-enters the codex
    fix loop with the confirm's MUST_FIX findings as seeds (#2807). ``False``
    restores the park on a dirty confirm.

    ``engine`` (#3036) names the repo's review skill as the round engine; see
    ``ReviewEngineConfig``. It requires ``convergence="clean"``, checked on the
    effective value, so the ``blocking`` default rejects it too: a re-raised
    deferred fingerprint staying deferred (D-6) needs the clean-mode ledger. The
    rule is a model validator, so key order never matters and a dump then
    re-validate round trip behaves like the original load. It is not stamped
    into ``.claude/cw-context.json`` for the Claude-native review stage (I1,
    #3039); the engine round in that stage runs the named skill inline.
    ``attestation`` (#3040) holds the sign-off attestation verifier's settings;
    see ``ReviewAttestationConfig``. It is read from the live config by
    ``cw review attestation check`` and is not stamped into the context.
    """

    model_config = ConfigDict(extra="forbid")

    convergence: Literal["blocking", "clean"] = "blocking"
    max_cycles: int | None = Field(default=None, ge=1)
    confirm_backend: Literal["codex", "claude", "none"] | None = None
    default_merge_tolerance: bool = True
    confirm_reentry: bool = True
    engine: ReviewEngineConfig | None = None
    attestation: ReviewAttestationConfig | None = None
    # #3045: presence is the toggle; the lane is validated by ClientConfig.
    # Not stamped into the context (to_context() does not read it).
    debt_autoenqueue: ReviewDebtAutoEnqueueConfig | None = None

    _RETIRED_KEYS: ClassVar[frozenset[str]] = frozenset({_RETIRED_DELTA_CONFIRM_KEY})

    @model_validator(mode="before")
    @classmethod
    def _strip_retired_keys(cls, data: object) -> object:
        """Drop retired ``review`` keys, warning once per process (#2979).

        A one-time config-load tolerance, not a behavioural shim: no reader of
        the key survives, but it lives in the operator-owned ``clients.yaml``
        and ``extra="forbid"`` would otherwise refuse every client that still
        sets it. Drop this validator once operator configs no longer carry it.
        """
        if not isinstance(data, dict):
            return data
        present = sorted(cls._RETIRED_KEYS & set(data))
        if not present:
            return data
        kept = {key: value for key, value in data.items() if key not in present}
        fresh = [key for key in present if key not in _WARNED_RETIRED_REVIEW_KEYS]
        if fresh:
            _WARNED_RETIRED_REVIEW_KEYS.update(fresh)
            _log.warning(
                "review: ignoring retired setting(s) %s -- the delta confirm "
                "round was removed (#2979); delete them from clients.yaml",
                fresh,
            )
        return kept

    @model_validator(mode="after")
    def _engine_requires_clean_convergence(self) -> ReviewConfig:
        if self.engine is not None and not self.is_clean:
            msg = (
                "review.engine requires review.convergence: clean "
                f"(got {self.convergence})"
            )
            raise ValueError(msg)
        return self

    @property
    def is_clean(self) -> bool:
        """True in converge-to-clean mode; the one spelling of that check."""
        return self.convergence == "clean"

    @property
    def effective_max_cycles(self) -> int:
        """Cycle ceiling: the configured value, else the mode's default."""
        if self.max_cycles is not None:
            return self.max_cycles
        if self.is_clean:
            return DEFAULT_CLEAN_MAX_CYCLES
        return DEFAULT_BLOCKING_MAX_CYCLES

    def to_context(self) -> dict[str, object]:
        """The ``review`` object stamped into ``.claude/cw-context.json``."""
        clean = self.is_clean
        return {
            "convergence": self.convergence,
            "max_cycles": self.effective_max_cycles if clean else None,
            "confirm_backend": self.confirm_backend,
            "default_merge_tolerance": self.default_merge_tolerance,
            "engine": None
            if self.engine is None
            else {
                "skill": self.engine.skill,
                "guarded_paths": list(self.engine.guarded_paths),
            },
        }


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
    # ticket-from-session-name parsing. Never read directly: every consumer
    # goes through cw.models.resolve_feature_branch, where a row's recorded
    # TicketTask.branch takes precedence over this default (#2948).
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
    #   1. project-config.yaml `quality_gates:` — the original key was removed
    #      by #1616; it is live again as `quality_gates.timeouts` (#2758,
    #      per-gate /prep-pr ceilings, read by .claude/scripts/prep_pr_state.py)
    #      and `quality_gates.autofix` (#3412, per-gate codex fix-loop autofix,
    #      read by cw.codex_fix_loop.gate_autofix).
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
    # It is the merge gate's alone: the clean-mode tip rule (#2979) reads no
    # path list, because it tolerates only a merge git itself would write.
    # Delivered to headless workers via `.claude/cw-context.json` (schema v11,
    # see cw.spawn.CW_CONTEXT_SCHEMA_VERSION), never read from clients.yaml
    # inside an agent's own bash.
    merge_gate_ignore_paths: list[str] = Field(default_factory=list)
    # Per-client switch for the whole Stage 4a merge gate (#2851). False makes
    # auto-dev-finalize.md skip the open-PR inventory, overlap filter and
    # `git merge-tree` probe and proceed to Step 4b, recording
    # "merge gate disabled by client config" in friction_highlights. Strict bool:
    # a non-bool value (`"no"`, `0`) fails loud at config load rather than being
    # coerced. Operator-only (ADR-0020): `cw lane set` refuses it and a dispatch
    # worker never writes it. Delivered to headless workers via
    # `.claude/cw-context.json` (schema v16); a context without the key means
    # enabled. `cw doctor` lists clients that turned it off.
    merge_gate_enabled: StrictBool = True
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
    # Per-client rollout for the #2717 gate-block retry backoff. True keeps
    # the fixed 60-second-to-900-second backoff enabled; False disables new
    # gate stamps and releases an existing gate-owned wait.
    gate_block_backoff_enabled: bool = True
    # Per-client opt-in for the #2442 pre-dispatch tracker-MCP gate. None (the
    # default) means the gate never runs for this client -- it never even
    # reads the branch's settings file. There is deliberately no fleet-wide
    # OrchestratorConfig companion toggle: per-client opt-in IS the rollout
    # control. See TrackerMcpGateConfig above.
    tracker_mcp_gate: TrackerMcpGateConfig | None = None
    # Per-client opt-in for the #2917 repair_mechanical_conflict recipe. None
    # (the default) means the recipe never fires for this client; the entry
    # also carries the globs and commands. See MechanicalConflictRepairConfig.
    mechanical_conflict_repair: MechanicalConflictRepairConfig | None = None
    # #2787 converge-to-clean review mode; the default block is blocking mode,
    # byte-identical to pre-#2787 behavior. Delivered to headless workers via
    # `.claude/cw-context.json` (schema v15), never read from clients.yaml
    # inside an agent's own bash. Its `engine` (#3036) rides the same context
    # object (I1, #3039) for the Claude-native review stage.
    review: ReviewConfig = Field(default_factory=ReviewConfig)
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

    def tracker_mcp_server(self, backend: str) -> str | None:
        """The MCP server name tracker_mcp_gate.servers names for *backend*, or None."""
        gate = self.tracker_mcp_gate
        if gate is None or gate.servers is None:
            return None
        return gate.servers.get(backend)

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

    @model_validator(mode="after")
    def _debt_autoenqueue_lane_declared(self) -> ClientConfig:
        autoenqueue = self.review.debt_autoenqueue
        if autoenqueue is None:
            return self
        # Explicit lanes only, NOT lane_names: a client with no lanes: block
        # must not accept the synthesized default lane (#3045 R2).
        declared = [lane.name for lane in self.lanes]
        if autoenqueue.lane not in declared:
            msg = (
                f"review.debt_autoenqueue.lane {autoenqueue.lane!r} is not a lane"
                f" declared for client {self.name!r}; declared lanes:"
                f" {', '.join(declared) or '(none)'}. Declare it under lanes: or"
                " remove review.debt_autoenqueue"
            )
            raise ValueError(msg)
        return self

    @property
    def is_worktree_client(self) -> bool:
        """True when this client uses repo_path + branch (worktree mode)."""
        return self.repo_path is not None and self.branch is not None
