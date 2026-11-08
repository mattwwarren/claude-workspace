"""Tests for ``cw review workspace record-document``'s engine refusal (#3039)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from cw.review_engine.constants import ENGINE_ROLE
from tests._review_engine_helpers import invoke
from tests.conftest import _doc_payload, _finding_kwargs

if TYPE_CHECKING:
    from pathlib import Path

BUILTIN_ROLE = "Code Quality Reviewer"


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    result = invoke(
        "review",
        "workspace",
        "init",
        "--ticket",
        "T-1",
        "--round-id",
        "r1",
        "--role",
        BUILTIN_ROLE,
        "--role",
        ENGINE_ROLE,
        "--worktree",
        str(path),
    )
    assert result.exit_code == 0, result.output
    return path


def _record(worktree: Path, role: str, payload: str) -> tuple[int, str]:
    result = invoke(
        "review",
        "workspace",
        "record-document",
        role,
        "-",
        "--worktree",
        str(worktree),
        input_text=payload,
    )
    return result.exit_code, result.output


def _finding(**overrides: Any) -> dict[str, Any]:
    return {**_finding_kwargs(), **overrides}


def test_record_document_rejects_engine_source_findings(worktree: Path) -> None:
    engine_finding = _finding(source="engine", fingerprint="fp-abcdef01")
    payload = json.dumps(_doc_payload(engine_finding, reviewer_role=ENGINE_ROLE))

    exit_code, output = _record(worktree, ENGINE_ROLE, payload)

    assert exit_code == 1
    assert "source: engine" in output
    assert "cw review engine record" in output
    assert not (worktree / ".cw/review-findings/repo-review-engine.json").exists()


def test_record_document_rejects_engine_finding_among_builtin_ones(
    worktree: Path,
) -> None:
    payload = json.dumps(
        _doc_payload(_finding(), _finding(source="engine", fingerprint="fp-abcdef01"))
    )

    exit_code, output = _record(worktree, BUILTIN_ROLE, payload)

    assert exit_code == 1
    assert "refuses engine-sourced findings" in output


def test_record_document_accepts_builtin_findings_unchanged(worktree: Path) -> None:
    payload = json.dumps(_doc_payload(_finding()))

    exit_code, output = _record(worktree, BUILTIN_ROLE, payload)

    assert exit_code == 0, output
    assert BUILTIN_ROLE in json.loads(output)["completed_roles"]
    stored = json.loads(
        (worktree / ".cw/review-findings/code-quality-reviewer.json").read_text()
    )
    assert "source" not in stored["findings"][0]


@pytest.mark.parametrize(
    "payload",
    ["{nope", "[1, 2]", json.dumps({"findings": "x"}), json.dumps({"findings": [1]})],
    ids=["malformed", "list", "findings-not-list", "finding-not-object"],
)
def test_record_document_malformed_json_unchanged_error(
    worktree: Path, payload: str
) -> None:
    exit_code, output = _record(worktree, BUILTIN_ROLE, payload)

    assert exit_code == 1
    assert "invalid reviewer findings document" in output
    assert "engine-sourced" not in output
