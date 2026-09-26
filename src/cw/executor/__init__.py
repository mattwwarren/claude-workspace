"""RFC 0005 A2/E1/F3 — StageExecutor seam and its backends.

Split by concern into submodules; this package re-exports the flat surface the
former ``cw/executor.py`` module exposed so import sites stay stable:

- ``core``     — StageExecutor Protocol, ClaudeNativeExecutor, codex capability
                 probe, the shared executor-direct completion door, and the
                 shared fire-and-forget spawn skeleton.
- ``local``    — LocalExecutor (aider).
- ``opencode`` — OpencodeExecutor.
- ``codex``    — CodexExecutor.
- ``_resolve`` — backend/lane resolution (imports all four executors).

Patch a name where its bare-name lookup happens (e.g.
``cw.executor.local.aider_available``), not on this package.
"""

from cw.executor._resolve import (
    _lane_pipeline,
    resolve_executor,
    resolve_executor_config,
    resolve_pipeline_stages,
)
from cw.executor.codex import CODEX_REVIEW_ONLY, CodexExecutor
from cw.executor.core import (
    _DEFAULT_PROBE_TIMEOUT_SECONDS,
    CODEX_NOT_FOUND,
    CODEX_VERSION_UNKNOWN,
    ClaudeNativeExecutor,
    CodexCapabilityDiagnosis,
    StageExecutor,
    _complete_session_via_door,
    _persist_runtime_error_diagnostics,
    _PreflightOK,
    codex_capability_diagnosis,
)
from cw.executor.local import LocalExecutor, _local_preflight
from cw.executor.opencode import OpencodeExecutor, _opencode_preflight
from cw.local_runner import GithubIssuePlanFetcher

__all__ = [
    "CODEX_NOT_FOUND",
    "CODEX_REVIEW_ONLY",
    "CODEX_VERSION_UNKNOWN",
    "_DEFAULT_PROBE_TIMEOUT_SECONDS",
    "ClaudeNativeExecutor",
    "CodexCapabilityDiagnosis",
    "CodexExecutor",
    "GithubIssuePlanFetcher",
    "LocalExecutor",
    "OpencodeExecutor",
    "StageExecutor",
    "_PreflightOK",
    "_complete_session_via_door",
    "_lane_pipeline",
    "_local_preflight",
    "_opencode_preflight",
    "_persist_runtime_error_diagnostics",
    "codex_capability_diagnosis",
    "resolve_executor",
    "resolve_executor_config",
    "resolve_pipeline_stages",
]
