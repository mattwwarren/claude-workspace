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

A second, separate advisory (#3790) covers ``review.engine.requires_runtime_
capabilities``: a review lane whose REVIEW runner lacks a required capability
gets a ``review-capability/<client>/<lane>`` warning. It is advisory only,
never a failure, and it reuses the claim gate's gap function so the two cannot
drift. The ``review-engine/`` results are unchanged by it.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from cw.codex_review._engine_completion import checker_problem
from cw.dev_queue.dispositions import REVIEW_CAPABILITY_RELANE_GENERIC
from cw.dispatch.claim.review_capability import review_capability_gap
from cw.doctor._shared import CheckResult
from cw.exceptions import CwError
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.models import CODEX_BACKEND, Stage, TicketTask
from cw.review_convergence import engine_supported, resolve_default_branch_ref
from cw.review_engine.manifest_source import (
    ManifestRead,
    ManifestStatus,
    read_default_branch_manifest,
)
from cw.review_engine.skill import resolve_skill
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
_CAPABILITY_UNMET = (
    "review.engine requires runtime capabilities {required}, but lane '{lane}' runs"
    " REVIEW on backend={backend} declaring {declared} (missing {missing}); its REVIEW"
    " rows park as review_capability_unmet before claim. Declare the missing"
    " capabilities on this lane's pipeline.executors.review.runtime_capabilities in"
    " clients.yaml, then `cw dev-queue requeue <T> -c <C>`. Compatible lanes:"
    " {compatible}. " + REVIEW_CAPABILITY_RELANE_GENERIC
)
_CODEX_CHECKER_UNUSABLE = (
    "review.engine is set and lane '{lane}' runs REVIEW on codex, and the "
    "default-branch skill '{skill}' declares completion_check, but its checker "
    "is unusable (reason={reason}); a codex round would park as "
    "engine_completion_check_failed. Commit an executable checker inside the "
    "skill directory."
)
_CHECKER_UNRESOLVED_REASON = "checker_unverifiable"
#: Errors reading the default branch or the skill; the same set the manifest read maps.
_PROBE_ERRORS = (CwError, subprocess.SubprocessError, OSError, UnicodeError)
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
    """The client's default-branch manifest, read on first use (#3885).

    ``get`` is the one read of ``client.default_branch``; it keeps the branch it
    resolved so the completion probe never reads the client attribute again.
    """

    def __init__(self, client: ClientConfig, skill: str) -> None:
        self._client = client
        self._skill = skill
        self._read: ManifestRead | None = None
        self._branch: str | None = None
        self._completion: str | None = None
        self._completion_probed = False

    def get(self) -> ManifestRead:
        if self._read is None:
            branch = self._client.default_branch
            self._branch = branch
            self._read = read_default_branch_manifest(
                _git_dir(self._client), branch, self._skill
            )
        return self._read

    def completion_problem(self) -> str | None:
        """Why the declared checker is unusable at the default branch (#3885)."""
        if self._completion_probed:
            return self._completion
        self._completion_probed = True
        read = self.get()
        if read.status is not ManifestStatus.LOADED or read.loaded is None:
            return None
        check = read.loaded.manifest.completion_check
        if check is None or self._branch is None:
            return None
        self._completion = self._probe_checker(check.path, self._branch)
        return self._completion

    def _probe_checker(self, checker_path: str, branch: str) -> str | None:
        """The checker's problem at *branch*, or the unresolved reason if git fails.

        Mirrors the errors ``read_default_branch_manifest`` maps to an
        unresolvable read, so one repo problem warns instead of aborting the
        whole doctor run.
        """
        try:
            repo = _git_dir(self._client)
            ref = resolve_default_branch_ref(repo, branch)
            if ref is None:
                return _CHECKER_UNRESOLVED_REASON
            resolved = resolve_skill(repo, ref.commit, self._skill)
            return checker_problem(repo, ref.commit, resolved, checker_path)
        except _PROBE_ERRORS:
            return _CHECKER_UNRESOLVED_REASON


def _lane_detail(
    backend: str, lane: str, skill: str, manifest: _ClientManifest
) -> str | None:
    if not engine_supported(backend):
        return _UNSUPPORTED.format(lane=lane, backend=backend)
    if backend == CODEX_BACKEND:
        detail = _codex_detail(manifest.get(), lane, skill)
        if detail is not None:
            return detail
        problem = manifest.completion_problem()
        if problem is None:
            return None
        return _CODEX_CHECKER_UNUSABLE.format(lane=lane, skill=skill, reason=problem)
    return None  # claude-native runs the engine skill itself


def _capability_detail(name: str, client: ClientConfig, lane: str) -> str | None:
    """The runtime-capability advisory for *lane*; ``None`` when satisfied (#3790)."""
    # ticket_id="" is the documented "no associated ticket" sentinel.
    task = TicketTask(ticket_id="", client=name, lane=lane, stage=Stage.REVIEW)
    gap = review_capability_gap(task, client)
    if gap is None:
        return None
    compatible = ", ".join(gap.compatible_lanes) or "none"
    return _CAPABILITY_UNMET.format(
        lane=lane,
        backend=gap.backend,
        declared=list(gap.declared),
        required=list(gap.required),
        missing=list(gap.missing),
        compatible=compatible,
    )


def _check_review_engine(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Advisory warnings for each review lane of each client with an engine.

    Up to two per lane: ``review-engine/<client>/<lane>`` (the lane cannot run
    the engine) and ``review-capability/<client>/<lane>`` (the lane's REVIEW
    runner lacks a runtime capability the engine requires). Never a failure.
    """
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
            if detail is not None:
                results.append(
                    CheckResult(
                        name=f"review-engine/{name}/{lane.name}",
                        ok=True,
                        warn=True,
                        detail=detail,
                    )
                )
            capability = _capability_detail(name, client, lane.name)
            if capability is not None:
                results.append(
                    CheckResult(
                        name=f"review-capability/{name}/{lane.name}",
                        ok=True,
                        warn=True,
                        detail=capability,
                    )
                )
    return results
