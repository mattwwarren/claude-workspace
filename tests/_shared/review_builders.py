"""Review-finding builders: plans, escalations, findings, constraints, debt
records, reviewer documents, captured diffs and verdict envelopes. Moved
from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import pytest

from cw.models.operator_constraint import OperatorConstraint
from cw.review_findings import (
    CapturedDiff,
    Confidence,
    DebtRecord,
    EscalationMetadata,
    Finding,
    ReviewerFindingsDocument,
    Severity,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def plan_body(*, spec: bool = True, soundness: bool = True) -> str:
    """Build a plan-of-record body with optional signoff markers.

    Markers match the verbatim shape auto-dev-plan.md appends:
    ``<!-- plan-spec-reviewed: YYYY-MM-DD vN -->`` /
    ``<!-- plan-soundness-reviewed: YYYY-MM-DD vN -->``. Shared by
    test_reconcile_gate_recipes.py and test_dev_queue.py (#968) — both
    modules independently need a plan-of-record body shaped for the
    tracker-first/`.cw/plan.md`-fallback two-marker "plan reviewed" check.
    """
    lines = ["# Plan — some ticket", ""]
    if spec:
        lines.append("<!-- plan-spec-reviewed: 2026-07-08 v2 -->")
    if soundness:
        lines.append("<!-- plan-soundness-reviewed: 2026-07-08 v1 -->")
    lines.extend(["", "body text"])
    return "\n".join(lines)


def _plan_text(paths: list[str]) -> str:
    """Build a realistic plan document with a ``## Files Modified`` section.

    Shared by test_check_plan_scope_conformance.py and test_plan_files.py —
    both exercise the same ``## Files Modified`` parsing contract, one via
    the standalone .claude/scripts mirror, the other via src/cw.plan_files.
    """
    bullets = "\n".join(f"- {p} (~40 lines)" for p in paths)
    return (
        "# Implementation Plan: Something (#9999)\n\n"
        "## Patterns Found\n\n"
        "- Proposed: a thing.\n\n"
        "## Files Modified\n\n"
        f"{bullets}\n\n"
        "**Scope tier:** small\n\n"
        "## Ambiguities\n\n"
        "NO_AMBIGUITIES\n"
    )


def stub_fetch_plan(
    monkeypatch: pytest.MonkeyPatch,
    body: str | None,
    *,
    target: str = "cw.reconcile.gate_recipes.fetch_approved_plan_comment",
) -> None:
    """Patch ``fetch_approved_plan_comment`` at *target* to return *body*.

    Default target matches ``gate_recipes``' module-level import binding;
    pass ``target="cw.dev_queue.lifecycle.fetch_approved_plan_comment"`` to stub the
    binding ``_plan_is_reviewed`` reads instead (#968).
    """
    monkeypatch.setattr(target, lambda _ticket_id, **_k: body)


def _make_escalation(**overrides: object) -> EscalationMetadata:
    """Minimal-but-valid EscalationMetadata with keyword overrides (#1237)."""
    kwargs: dict[str, object] = {
        "target_reviewer": "Perf Reviewer",
        "evidence_quote": "def broken():",
    }
    kwargs.update(overrides)
    return EscalationMetadata.model_validate(kwargs)


class FindingKwargs(TypedDict):
    """Precisely-typed kwargs for a genuinely-valid Finding literal.

    Mirrors Finding's 10 non-defaulted-in-practice fields exactly (#1922) --
    the shape a real captured fixture needs to splat directly into
    Finding(**kwargs) and type-check under --strict. NOT for
    intentionally-invalid payloads; see _RawFindingKwargs for that.
    """

    severity: Severity
    file: str
    line_start: int | None
    line_end: int | None
    summary: str
    consequence: str
    suggested_fix: str
    evidence: str
    confidence: Confidence
    escalation: EscalationMetadata | None


class _RawFindingKwargs(TypedDict, total=False):
    """Loosely-typed kwargs bag for a possibly-invalid Finding payload (#1922).

    Every value is `object`, not the real field type: this shape exists
    solely to give Finding.model_construct(**...) splats a closed key set
    (excluding BaseModel.model_construct's `_fields_set` parameter) so mypy
    stops conservatively checking the splat against every keyword-reachable
    parameter. It intentionally does NOT constrain values -- callers
    deliberately construct invalid Findings here (bad severities, blank
    evidence) to bypass Pydantic validation and exercise defensive checks
    downstream.
    """

    severity: object
    file: object
    line_start: object
    line_end: object
    summary: object
    consequence: object
    suggested_fix: object
    evidence: object
    confidence: object
    escalation: object
    no_diff_anchor: object
    transitive_impact_evidence: object
    release_critical_exception: object
    contests_adjudication: object
    contests_decision: object


def _finding_kwargs(**overrides: object) -> _RawFindingKwargs:
    """Full kwargs for a valid Finding (#1237).

    Shared by :func:`_make_finding` and by tests that need the raw dict
    (e.g. ``Finding.model_construct(**_finding_kwargs(...))`` to bypass
    Pydantic validation) — a single source of truth so the two never drift.
    Defaults line up with ``_make_diff``: ``evidence`` appears in the diff
    text, ``file`` is a changed file, and the line range is a changed line.
    """
    kwargs: dict[str, object] = {
        "severity": "MUST_FIX",
        "file": "src/cw/foo.py",
        "line_start": 10,
        "line_end": 10,
        "summary": "Bug here",
        "consequence": "It breaks",
        "suggested_fix": "Fix it",
        "evidence": "def broken():",
        "confidence": "HIGH",
        "escalation": None,
    }
    kwargs.update(overrides)
    return cast("_RawFindingKwargs", kwargs)


def _without_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the required ``evidence`` field to build a schema-invalid payload
    (#2029).

    Omission rather than a blank value: the required-field failure is the
    shape a real reviewer produces when it forgets the field entirely, and it
    is what the ticket's own acceptance criteria name. Shared by
    ``test_review_findings.py``, ``test_codex_review_roles.py``, and
    ``test_cli_review_consolidate.py``, which each built a near-identical helper
    doing this same delete against their own base-payload builder — one
    source of truth so the mechanism can't silently drift between them.
    """
    payload = dict(payload)
    del payload["evidence"]
    return payload


def _make_finding(**overrides: object) -> Finding:
    """Minimal-but-valid Finding with keyword overrides (#1237)."""
    return Finding.model_validate(_finding_kwargs(**overrides))


_CONSTRAINT_RECORDED_AT = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _make_constraint(
    text: str, *, kind: str = "operator", round_: int = 1
) -> OperatorConstraint:
    """One row constraint entry with a fixed time and actor (#2984).

    *kind* is the provenance label: ``"operator"`` for a ``cw review
    constrain`` entry, or a plan-derived kind (``"adopted_assumption"``,
    ``"non_goal"``, ``"plan_decision"``).
    """
    return OperatorConstraint.model_validate(
        {
            "text": text,
            "recorded_at": _CONSTRAINT_RECORDED_AT,
            "actor": "octocat",
            "round": round_,
            "kind": kind,
        }
    )


def _make_debt_record(**overrides: object) -> DebtRecord:
    """Minimal-but-valid DebtRecord with keyword overrides (#1837).

    Lives here alongside :func:`_make_finding` so ``test_review_debt.py`` and
    ``test_codex_fix_loop_convergence.py`` share one builder instead of each
    keeping a local copy. Defaults line up with ``_finding_kwargs``.
    """
    kwargs: dict[str, object] = {
        "fingerprint": ("src/cw/foo.py", "bug here"),
        "file": "src/cw/foo.py",
        "evidence": "def broken():",
        "summary": "Bug here",
        "suggested_follow_up": "Fix it",
        "discovery_sha": "deadbee",
        "reviewer_role": "Code Quality Reviewer",
    }
    kwargs.update(overrides)
    return DebtRecord.model_validate(kwargs)


def _make_reviewer_doc(
    *findings: Finding, **overrides: object
) -> ReviewerFindingsDocument:
    """Minimal-but-valid ReviewerFindingsDocument wrapping *findings* (#1237)."""
    kwargs: dict[str, object] = {
        "reviewer_role": "Test Reviewer",
        "status": "ok",
        "detail": "reviewed; no issues found.",
        "findings": list(findings),
    }
    kwargs.update(overrides)
    return ReviewerFindingsDocument.model_validate(kwargs)


def _doc_payload(*findings: dict[str, Any], **overrides: object) -> dict[str, Any]:
    """A raw ``ReviewerFindingsDocument`` dict (bypasses Pydantic construction
    so invalid payloads — e.g. a bogus severity — can be sent through the CLI).
    """
    payload: dict[str, Any] = {
        "reviewer_role": "Code Quality Reviewer",
        "status": "ok",
        "detail": "",
        "findings": list(findings),
    }
    payload.update(overrides)
    return payload


def _make_diff(*added_lines: str, **overrides: object) -> CapturedDiff:
    """Minimal-but-valid CapturedDiff (#1237, restructured #1236).

    Positional args are added ("+"-prefixed) content lines. ``files`` maps a
    changed file path to its list of changed line numbers; ``extra_text`` is
    appended verbatim so context/removed lines can be exercised.

    Populates ``file_diffs`` (per-file hunk text, for prompt inlining and the
    file-level evidence fallback) and ``file_line_text`` (per-file
    ``{line_number: content}`` for the added lines) alongside the flat ``text``,
    so every call site keeps passing under the per-file/per-line
    ``_classify_finding``. Line numbers are paired with ``added_lines`` via a
    GLOBAL position counter shared across every file (not reset per file), so
    each file genuinely gets distinct content when multiple files are passed
    in ``files`` — MUST_FIX 3 (#1236): the previous per-file-reset ``enumerate``
    gave every file's first claimed line the same ``lines[0]`` text, so a
    "stolen from another file" R6 regression test could never actually prove
    file-scoping (the stolen evidence wasn't genuinely present in ANY file's
    structured map). The last content repeats if the combined line count
    across all files exceeds ``len(added_lines)``, keeping ``files[f] ==
    sorted(file_line_text[f])`` an invariant.

    ``file_window_text`` (#1738) is set equal to ``file_line_text`` — this
    helper never generates context lines (every body line is ``+``-prefixed),
    so it has no distinct content to contribute to the hunk-context superset;
    tests that need genuine context-line content use the real
    ``_parse_unified_diff`` parser against a real diff instead (see
    ``tests/test_review_findings.py``'s ``_pr1729_captured_diff``).
    """
    lines = added_lines or ("def broken():",)
    files = overrides.get("files", {"src/cw/foo.py": [10]})
    extra_text = str(overrides.get("extra_text", ""))
    assert isinstance(files, dict)
    header = "\n".join(f"+++ b/{path}" for path in files)
    body = "\n".join(f"+{line}" for line in lines)
    text = f"{header}\n{body}\n{extra_text}"
    file_diffs: dict[str, str] = {}
    file_line_text: dict[str, dict[int, str]] = {}
    pos = 0
    for path, line_nums in files.items():
        per_file: dict[int, str] = {}
        for ln in line_nums:
            per_file[ln] = lines[pos] if pos < len(lines) else lines[-1]
            pos += 1
        file_line_text[path] = per_file
        file_body = "\n".join(f"+{per_file[ln]}" for ln in line_nums)
        file_diffs[path] = f"+++ b/{path}\n{file_body}\n{extra_text}"
    return CapturedDiff(
        text=text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
        file_window_text=dict(file_line_text),
    )


def write_review_verdict_envelope(
    worktree: Path,
    *,
    ticket_id: str,
    reviewed_sha: str,
    must_fix: Sequence[Finding] = (),
    blocking: bool | None = None,
) -> Path:
    """Write a real ``ReviewVerdictEnvelope`` to the worktree's verdict JSON (#2205).

    Rendered by production's ``render_review_verdict_envelope`` from a
    ``consolidate_verdict`` result, so the file has the shape codex's
    background review persists. *blocking* defaults to "has must_fix".
    """
    from cw.review_findings import (
        REVIEW_VERDICT_JSON_RELATIVE_PATH,
        consolidate_verdict,
        render_review_verdict_envelope,
    )

    base = consolidate_verdict(
        [_make_reviewer_doc(_make_finding(severity="NIT"))],
        _make_diff(),
        reviewed_sha=reviewed_sha,
    )
    verdict = base.model_copy(
        update={
            "must_fix": list(must_fix),
            "blocking": bool(must_fix) if blocking is None else blocking,
        }
    )
    path = worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_review_verdict_envelope(verdict, ticket_id=ticket_id), encoding="utf-8"
    )
    return path
