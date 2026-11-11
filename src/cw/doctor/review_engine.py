"""Doctor warnings for a repo review engine a lane cannot run (#3039, #3216).

A ``review.engine`` runs in the claude-native review stage and, through the
skill's ``cw-engine.json`` manifest, on a codex review lane (RFC 0018 A4). For
every client that sets one, each lane whose pipeline has a REVIEW stage gets at
most one ``review-engine/<client>/<lane>`` warning:

- claude-native: nothing;
- codex: nothing when the default-branch manifest loads; a warning when it is
  absent or invalid (rows park as ``review_engine_unsupported_backend``); a
  separate "cannot verify" warning when the repo, default branch or skill
  cannot be read, never a pass;
- any other backend: rows park as ``review_engine_unsupported_backend``.

The manifest is read once per client, and only when a codex lane needs it.
Output carries names and loader reasons only: no skill or manifest text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.models import CODEX_BACKEND, Stage, TicketTask
from cw.review_convergence import engine_supported
from cw.review_engine.manifest_source import (
    ManifestRead,
    ManifestStatus,
    read_default_branch_manifest,
)
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.models import ClientConfig

_UNSUPPORTED = (
    "review.engine is set, but lane '{lane}' runs REVIEW on backend={backend}, "
    "which cannot run a repo review engine; its REVIEW rows park as "
    "review_engine_unsupported_backend (cause=backend_unsupported). Move the "
    "lane to claude-native or codex, or remove review.engine."
)
_CODEX_REFUSED = (
    "review.engine is set and lane '{lane}' runs REVIEW on codex, but the "
    "default-branch skill '{skill}' has no usable cw-engine.json "
    "(cause={cause}{detail}); its REVIEW rows park as "
    "review_engine_unsupported_backend. Check it with "
    "`cw review engine validate --kind manifest`."
)
_CODEX_UNVERIFIED = (
    "review.engine is set and lane '{lane}' runs REVIEW on codex, but the "
    "engine manifest of skill '{skill}' cannot be verified (cause={cause}); "
    "this check does not pass it."
)


def _lane_backend(name: str, client: ClientConfig, lane: str) -> str | None:
    """The REVIEW backend of *lane*, or ``None`` when the lane has no REVIEW stage."""
    # ticket_id="" is the documented "no associated ticket" sentinel.
    task = TicketTask(ticket_id="", client=name, lane=lane, stage=Stage.REVIEW)
    if Stage.REVIEW not in resolve_pipeline_stages(task, client):
        return None
    return resolve_executor_config(Stage.REVIEW, task, client).backend


def _codex_detail(read: ManifestRead, lane: str, skill: str) -> str | None:
    """The codex-lane warning for *read*, or ``None`` when the manifest loaded."""
    if read.status is ManifestStatus.LOADED:
        return None
    if read.status is ManifestStatus.UNRESOLVABLE:
        return _CODEX_UNVERIFIED.format(lane=lane, skill=skill, cause=read.cause)
    detail = f"; {read.detail}" if read.detail else ""
    return _CODEX_REFUSED.format(
        lane=lane, skill=skill, cause=read.cause, detail=detail
    )


class _ClientManifest:
    """The client's default-branch manifest, read on first use."""

    def __init__(self, client: ClientConfig, skill: str) -> None:
        self._client = client
        self._skill = skill
        self._read: ManifestRead | None = None

    def get(self) -> ManifestRead:
        if self._read is None:
            self._read = read_default_branch_manifest(
                _git_dir(self._client), self._client.default_branch, self._skill
            )
        return self._read


def _lane_detail(
    backend: str, lane: str, skill: str, manifest: _ClientManifest
) -> str | None:
    if not engine_supported(backend):
        return _UNSUPPORTED.format(lane=lane, backend=backend)
    if backend == CODEX_BACKEND:
        return _codex_detail(manifest.get(), lane, skill)
    return None  # claude-native runs the engine skill itself


def _check_review_engine(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """At most one warning per review lane of each client with an engine."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        engine = client.review.engine
        if engine is None:
            continue
        manifest = _ClientManifest(client, engine.skill)
        for lane in client.effective_lanes:
            backend = _lane_backend(name, client, lane.name)
            if backend is None:
                continue
            detail = _lane_detail(backend, lane.name, engine.skill, manifest)
            if detail is None:
                continue
            results.append(
                CheckResult(
                    name=f"review-engine/{name}/{lane.name}",
                    ok=True,
                    warn=True,
                    detail=detail,
                )
            )
    return results
