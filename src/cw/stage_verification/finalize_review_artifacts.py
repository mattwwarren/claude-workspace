"""FINALIZE Step 4d as Python: the review artifacts of the PR (#3543).

The claude-native finalize doc has an agent run the review-debt re-file, write
the ``## Review adjudication`` section, the operator override and the review
sign-off into the PR body, link the debt tickets and judge the UI evidence.
Codex FINALIZE has no agent for any of it, so cw runs it, calling the same CLIs
the doc names through :func:`~cw.stage_verification.finalize_gates.run_cw_cli`.

:func:`run_review_artifacts` runs on the create and the reuse path, after
mergeability and before the re-fence and the arm: UI files, the debt re-file,
one read of the PR body and head, the UI judgement, one PR-body write
(adjudication, override, sign-off), then the debt-ticket link. The UI-evidence
media check reads a copy of the body without cw's own sections
(:func:`~cw.stage_verification.finalize_pr.strip_cw_regions`), so finding text
is never mistaken for a screenshot.

A failed PR-body read blocks only when something needs it: UI files hold the
arm, an adjudication or override write blocks ``operator_unavailable``, and a
run with only the sign-off to write ships with friction. cw never writes a PR
body it did not read, and never disarms auto-merge.

No codex imports: any executor can call this.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.atomic import atomic_write_text
from cw.gh import edit_pr_body, fetch_pr_head, fetch_pr_view
from cw.review_adjudication import (
    DEFERRED_FINDINGS_RELATIVE_PATH,
    REVIEW_ADJUDICATION_HEADING,
    parse_deferred_findings_md,
    read_deferred_findings_md,
    render_deferred_findings_md,
)
from cw.review_adjudication._models import DEFERRED_OUTCOME
from cw.review_debt_filing import debt_entry, is_fixture_entry
from cw.review_signoff import friction_line
from cw.stage_verification.finalize_gates import run_cw_cli
from cw.stage_verification.finalize_pr import (
    apply_body_section,
    apply_override_section,
    heading_matches,
    strip_cw_regions,
)
from cw.stage_verification.finalize_result import (
    OPERATOR_UNAVAILABLE,
    REVIEW_DEBT_UNFILED,
    STAGE4_REVIEW_DEBT,
    FinalizeBlock,
)
from cw.stage_verification.finalize_ui_evidence import (
    check_ui_evidence,
    find_ui_files,
)
from cw.stage_verification.posted_text import redacted_tail

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from cw.review_adjudication import Adjudication

__all__ = [
    "ArtifactsOutcome",
    "PrTarget",
    "adjudication_section",
    "link_debt_tickets",
    "render_signoff",
    "run_debt_refile",
    "run_review_artifacts",
    "write_pr_body",
]

_log = logging.getLogger(__name__)

_GH_TIMEOUT: Final = 30
_FRICTION_PREFIX: Final = "friction: "
_REFUSED_FIXTURE_LINE: Final = "friction: debt_filing: refused_fixture_entry"
_FAILURE_KIND: Final = re.compile(r"debt filing failed \[([a-z_]+)\]")
_SIGNOFF_UNCHANGED: Final = 3
_ARTIFACT_ARG: Final = "--deferred-findings"
_ARTIFACT_HINT_PATH: Final = str(DEFERRED_FINDINGS_RELATIVE_PATH)
_BODY_CURRENT: Final = "pr-body.current.md"
_BODY_COMPOSED: Final = "pr-body.composed.md"
_BODY_FINAL: Final = "pr-body.final.md"
_SIGNOFF_WRITE_FAILED: Final = "review_signoff_write_failed"
_LINK_FAILED: Final = "review_debt_link: cli_failed"


@dataclass(frozen=True)
class PrTarget:
    """The PR Step 4d works on and the identity every CLI call carries."""

    worktree: Path
    scratch: Path
    ticket_id: str
    client_name: str
    pr_number: int
    pr_url: str
    base: str
    fork_point: str
    head_sha: str
    override_section: str | None


@dataclass(frozen=True)
class ArtifactsOutcome:
    """What a step decided: a *block*, its *friction* and an arm hold."""

    block: FinalizeBlock | None = None
    friction: tuple[str, ...] = ()
    hold_automerge: bool = False


def _friction_lines(done: subprocess.CompletedProcess[str] | None) -> tuple[str, ...]:
    """The ``friction: `` lines on a CLI's stderr, verbatim."""
    if done is None:
        return ()
    return tuple(
        line.strip()
        for line in done.stderr.splitlines()
        if line.startswith(_FRICTION_PREFIX)
    )


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


# -- the deferred-findings artifact ------------------------------------------


def _section_from(text: str) -> str:
    """*text* from its adjudication heading line on; ``""`` without one."""
    match = next(heading_matches(text, REVIEW_ADJUDICATION_HEADING), None)
    return text[match.start() :] if match is not None else ""


def adjudication_section(artifact: Path) -> str:
    """The ``## Review adjudication`` section of the artifact at *artifact*.

    Copied from the heading line on, so the file title and provenance line are
    dropped and the ``DEFERRED-REVIEW-FINDINGS`` sentinels and ``ticket:``
    lines stay intact. ``""`` for an absent, blank, unreadable or undecodable
    file and for one without the heading: ``cw review debt file`` still blocks
    on an unreadable artifact, so nothing is silently dropped. A test-fixture
    deferral never reaches a body: the section is rendered without it.
    """
    try:
        text = artifact.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        _log.warning("finalize artifacts: the deferred-findings artifact is unreadable")
        return ""
    if not text.strip():
        return ""
    try:
        entries = parse_deferred_findings_md(text)
    except ValueError:
        return _section_from(text)
    kept = [e for e in entries if not _is_fixture_defer(e)]
    if len(kept) == len(entries):
        return _section_from(text)
    return _section_from(render_deferred_findings_md(kept))


def _is_fixture_defer(entry: Adjudication) -> bool:
    return entry.outcome == DEFERRED_OUTCOME and is_fixture_entry(debt_entry(entry))


def _drop_fixture_entries(artifact: Path) -> None:
    """Remove refused test-fixture deferrals from the artifact (best effort).

    The artifact file is deleted when nothing remains. A read or write failure
    is logged: the re-file already succeeded, so the run does not stop.
    """
    try:
        entries = read_deferred_findings_md(artifact)
        rendered = render_deferred_findings_md(
            [e for e in entries if not _is_fixture_defer(e)]
        )
        if rendered:
            atomic_write_text(artifact, rendered)
        else:
            artifact.unlink()
    except (OSError, ValueError):
        _log.warning("finalize artifacts: could not drop the fixture deferrals")


# -- the review-debt re-file ---------------------------------------------------


def _mcp_hint(target: PrTarget) -> str:
    who = f"-c {target.client_name} --ticket {target.ticket_id}"
    return (
        "a rerun stays mcp_pending until the ids are recorded; from the ticket "
        f"worktree list the payloads with `cw review debt emit {who} "
        f"{_ARTIFACT_ARG} {_ARTIFACT_HINT_PATH}`, file one ticket per listed "
        "payload, run `cw review debt record --ids <file> "
        f"{who} {_ARTIFACT_ARG} {_ARTIFACT_HINT_PATH}`, then `cw dev-queue "
        f"requeue {target.ticket_id} -c {target.client_name} --stage finalize`"
    )


def _debt_json(done: subprocess.CompletedProcess[str]) -> dict[str, object] | None:
    try:
        data = json.loads(done.stdout)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _debt_details(
    target: PrTarget, done: subprocess.CompletedProcess[str] | None
) -> str:
    """The block's one-line details, with the right release hint."""
    rerun = "rerun cw review debt file, then requeue finalize"
    if done is None:
        return f"cw review debt file did not finish; {rerun}"
    data = _debt_json(done)
    status = data.get("status") if data is not None else None
    if data is None:
        tail = redacted_tail(done.stderr).replace("\n", " | ")
        return f"cw review debt file exited {done.returncode}: {tail}; {rerun}"
    kinds = sorted(set(_FAILURE_KIND.findall(done.stderr))) or ["none reported"]
    hint = _mcp_hint(target) if status == "mcp_pending" else rerun
    return (
        f"cw review debt file exited {done.returncode} (status {status}): "
        f"{data.get('unticketed')} deferral(s) still unticketed; failure kinds: "
        f"{', '.join(kinds)}; {hint}"
    )


def _debt_block(
    target: PrTarget, done: subprocess.CompletedProcess[str] | None
) -> FinalizeBlock:
    """``review_debt_unfiled`` with the PR as it is: never disarmed."""
    pr_info: dict[str, object] = {
        "number": target.pr_number,
        "url": target.pr_url,
        "base": target.base,
    }
    head = fetch_pr_head(target.pr_url, cwd=target.worktree)
    if head is not None:
        pr_info["auto_merge"] = head.auto_merge_armed
    _log.warning(
        "finalize artifacts: review debt unfiled ticket=%s pr=%s",
        target.ticket_id,
        target.pr_number,
    )
    return FinalizeBlock(
        REVIEW_DEBT_UNFILED,
        _debt_details(target, done),
        retry_eligible=True,
        blocker_stage=STAGE4_REVIEW_DEBT,
        pr_info=pr_info,
    )


def run_debt_refile(target: PrTarget) -> ArtifactsOutcome:
    """``cw review debt file``: the finalize re-file gate (before the arm).

    Any non-zero exit means deferrals are still unticketed, so the run blocks
    ``review_debt_unfiled``. On success a refused test-fixture entry is removed
    from the artifact, so it reaches no PR body.
    """
    artifact = target.worktree / DEFERRED_FINDINGS_RELATIVE_PATH
    done = run_cw_cli(
        target.worktree,
        [
            "review",
            "debt",
            "file",
            "--ticket",
            target.ticket_id,
            "-c",
            target.client_name,
            _ARTIFACT_ARG,
            str(artifact),
        ],
    )
    friction = _friction_lines(done)
    if done is None or done.returncode != 0:
        return ArtifactsOutcome(block=_debt_block(target, done), friction=friction)
    if _REFUSED_FIXTURE_LINE in friction:
        _drop_fixture_entries(artifact)
    return ArtifactsOutcome(friction=friction)


# -- the PR body ------------------------------------------------------------------


def render_signoff(
    target: PrTarget, *, composed: Path, current: Path, pr_head: str
) -> tuple[str | None, tuple[str, ...]]:
    """``cw review signoff render``: ``(new body or None for no edit, friction)``.

    Opt-in and attestation validity stay inside the CLI. An empty *pr_head* or
    a CLI that did not finish returns the composed body untouched with a
    friction token; this never blocks.
    """
    text = composed.read_text(encoding="utf-8")
    if not pr_head:
        return text, (friction_line("skipped_pr_head_unreadable"),)
    done = run_cw_cli(
        target.worktree,
        [
            "review",
            "signoff",
            "render",
            "--body-file",
            str(composed),
            "--current-body-file",
            str(current),
            "--pr-head",
            pr_head,
            "--ticket",
            target.ticket_id,
            "-c",
            target.client_name,
        ],
    )
    friction = _friction_lines(done)
    if done is not None and done.returncode == _SIGNOFF_UNCHANGED:
        return None, friction
    if done is None or done.returncode != 0 or not done.stdout.strip():
        _log.warning(
            "finalize artifacts: review signoff render failed ticket=%s pr=%s",
            target.ticket_id,
            target.pr_number,
        )
        return text, (*friction, friction_line("skipped_cli_failed"))
    return done.stdout, friction


def _unread_block(target: PrTarget, section: str) -> FinalizeBlock | None:
    """The block for a failed PR-body read, or ``None`` when nothing needs it."""
    needed = [
        name
        for name, present in (
            ("review adjudication", bool(section)),
            ("operator override", bool(target.override_section)),
        )
        if present
    ]
    if not needed:
        return None
    return FinalizeBlock(
        OPERATOR_UNAVAILABLE,
        "could not read the PR body, so cw did not write the "
        f"{' and '.join(needed)} section",
        retry_eligible=True,
    )


def write_pr_body(
    target: PrTarget, *, current: str | None, pr_head: str, section: str
) -> ArtifactsOutcome:
    """One PR-body write: adjudication, override, then the sign-off.

    *current* is the body as read (``None`` for a failed read) and *section*
    the adjudication text to write (``""`` keeps whatever the body has). A
    failed write blocks when an adjudication or override section was meant to
    land; one that carried only the sign-off is friction.
    """
    if current is None:
        _log.warning(
            "finalize artifacts: PR body unreadable ticket=%s pr=%s",
            target.ticket_id,
            target.pr_number,
        )
        block = _unread_block(target, section)
        if block is not None:
            return ArtifactsOutcome(block=block)
        return ArtifactsOutcome(friction=(friction_line("skipped_pr_head_unreadable"),))
    composed = current
    if section:
        composed = apply_body_section(
            composed, section, heading=REVIEW_ADJUDICATION_HEADING
        )
    if target.override_section:
        composed = apply_override_section(composed, target.override_section)
    sections_changed = _normalized(composed) != _normalized(current)
    current_file = target.scratch / _BODY_CURRENT
    composed_file = target.scratch / _BODY_COMPOSED
    current_file.write_text(current, encoding="utf-8")
    composed_file.write_text(composed, encoding="utf-8")
    new, friction = render_signoff(
        target, composed=composed_file, current=current_file, pr_head=pr_head
    )
    if new is None or _normalized(new) == _normalized(current):
        return ArtifactsOutcome(friction=friction)
    final_file = target.scratch / _BODY_FINAL
    final_file.write_text(new, encoding="utf-8")
    done = edit_pr_body(target.pr_url, body_file=final_file, cwd=target.worktree)
    if done is not None and done.returncode == 0:
        return ArtifactsOutcome(friction=friction)
    if sections_changed:
        return ArtifactsOutcome(
            block=FinalizeBlock(
                OPERATOR_UNAVAILABLE,
                "could not write the PR body review sections",
                retry_eligible=True,
            ),
            friction=friction,
        )
    return ArtifactsOutcome(friction=(*friction, _SIGNOFF_WRITE_FAILED))


def link_debt_tickets(target: PrTarget) -> tuple[str, ...]:
    """``cw review debt link-pr``: friction only, never a block."""
    done = run_cw_cli(
        target.worktree,
        [
            "review",
            "debt",
            "link-pr",
            "--pr",
            str(target.pr_number),
            "--ticket",
            target.ticket_id,
            "-c",
            target.client_name,
        ],
    )
    friction = _friction_lines(done)
    if done is None or done.returncode != 0:
        _log.warning(
            "finalize artifacts: debt ticket link failed ticket=%s pr=%s",
            target.ticket_id,
            target.pr_number,
        )
        return (*friction, _LINK_FAILED)
    return friction


def _read_pr(target: PrTarget) -> tuple[str | None, str]:
    """``(body, head)`` from one ``gh pr view``; ``(None, "")`` when it failed."""
    view = fetch_pr_view(
        target.pr_url,
        timeout=_GH_TIMEOUT,
        fields="body,headRefOid",
        cwd=target.worktree,
    )
    body = view.get("body") if view is not None else None
    if view is None or not isinstance(body, str):
        return None, ""
    head = view.get("headRefOid")
    return body, head if isinstance(head, str) else ""


def run_review_artifacts(target: PrTarget) -> ArtifactsOutcome:
    """Step 4d, in order (see the module docstring)."""
    ui_files = find_ui_files(
        target.worktree, fork_point=target.fork_point, head_sha=target.head_sha
    )
    debt = run_debt_refile(target)
    if debt.block is not None:
        return ArtifactsOutcome(
            block=debt.block, friction=(*ui_files.friction, *debt.friction)
        )
    section = adjudication_section(target.worktree / DEFERRED_FINDINGS_RELATIVE_PATH)
    current, pr_head = _read_pr(target)
    ui = check_ui_evidence(
        ui_files, body=None if current is None else strip_cw_regions(current)
    )
    if ui.hold:
        _log.info(
            "finalize artifacts: UI evidence holds the arm ticket=%s pr=%s",
            target.ticket_id,
            target.pr_number,
        )
    written = write_pr_body(target, current=current, pr_head=pr_head, section=section)
    friction = (*debt.friction, *ui.friction, *written.friction)
    if written.block is not None:
        return ArtifactsOutcome(block=written.block, friction=friction)
    return ArtifactsOutcome(
        friction=(*friction, *link_debt_tickets(target)), hold_automerge=ui.hold
    )
