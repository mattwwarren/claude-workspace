"""Tests for ``cw review workspace`` record commands: engine refusal (#3039),
the one-shot frame-missing re-prompt (#3096), and the documented clean frame."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from cw.codex_review._context._agent_spec import _REVIEWER_ROLE_AGENT_FILES
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_park_findings import (
    REVIEW_PARK_FINDINGS_RELATIVE_PATH,
    write_park_findings,
)
from tests._review_engine_helpers import invoke
from tests._shared.review_builders import _doc_payload, _finding_kwargs

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from click.testing import Result

BUILTIN_ROLE = "Code Quality Reviewer"


def _invoke_init(
    path: Path,
    roles: Sequence[str],
    *,
    ticket: str = "T-1",
    round_id: str = "r1",
) -> Result:
    """Run ``workspace init`` without asserting on the exit code."""
    args: list[str] = []
    for role in roles:
        args.extend(["--role", role])
    return invoke(
        "review",
        "workspace",
        "init",
        "--ticket",
        ticket,
        "--round-id",
        round_id,
        *args,
        "--worktree",
        str(path),
    )


def _init_round(path: Path, roles: Sequence[str]) -> Path:
    path.mkdir()
    result = _invoke_init(path, roles)
    assert result.exit_code == 0, result.output
    return path


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    return _init_round(tmp_path / "wt", [BUILTIN_ROLE, ENGINE_ROLE])


def _reprompt(worktree: Path, role: str) -> tuple[int, str]:
    result = invoke(
        "review",
        "workspace",
        "record-reprompt",
        role,
        "--worktree",
        str(worktree),
    )
    return result.exit_code, result.output


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


def test_record_reprompt_grants_once_then_refuses(worktree: Path) -> None:
    exit_code, output = _reprompt(worktree, BUILTIN_ROLE)

    assert exit_code == 0, output
    assert json.loads(output)["reprompt"] == "granted"

    exit_code, output = _reprompt(worktree, BUILTIN_ROLE)

    assert exit_code == 3, output
    assert json.loads(output)["reprompt"] == "refused"


def test_record_reprompt_rejects_role_outside_roster(worktree: Path) -> None:
    exit_code, output = _reprompt(worktree, "Not A Reviewer")

    assert exit_code == 1
    assert "outside the persisted roster" in output


def test_explicit_clean_empty_frame_completes_round(tmp_path: Path) -> None:
    roles = [BUILTIN_ROLE]
    worktree = _init_round(tmp_path / "wt", roles)
    clean = json.dumps(
        _doc_payload(reviewer_role=BUILTIN_ROLE, detail="checked X; no issues found")
    )

    exit_code, output = _record(worktree, BUILTIN_ROLE, clean)
    assert exit_code == 0, output

    finish = invoke(
        "review",
        "workspace",
        "finish",
        "--status",
        "completed",
        "--worktree",
        str(worktree),
    )
    assert finish.exit_code == 0, finish.output


def test_blank_detail_empty_frame_is_rejected(worktree: Path) -> None:
    blank = json.dumps(_doc_payload(reviewer_role=BUILTIN_ROLE))

    exit_code, output = _record(worktree, BUILTIN_ROLE, blank)

    assert exit_code == 1
    assert "invalid reviewer findings document" in output


def test_every_reviewer_role_records_clean_frame_and_finishes(tmp_path: Path) -> None:
    roles = list(_REVIEWER_ROLE_AGENT_FILES)
    worktree = _init_round(tmp_path / "wt", roles)

    for role in roles:
        payload = json.dumps(
            _doc_payload(
                reviewer_role=role,
                detail="checked the assigned scope; no issues found",
            )
        )
        exit_code, output = _record(worktree, role, payload)
        assert exit_code == 0, (role, output)

    finish = invoke(
        "review",
        "workspace",
        "finish",
        "--status",
        "completed",
        "--worktree",
        str(worktree),
    )
    assert finish.exit_code == 0, finish.output


def _seed_park(worktree: Path) -> Path:
    assert write_park_findings(
        worktree, ticket_id="T-1", reviewed_sha="c" * 40, text="parked finding"
    )
    return worktree / REVIEW_PARK_FINDINGS_RELATIVE_PATH


class TestInitClearsParkFindings:
    """Every successful ``workspace init`` drops the saved park findings (#3762)."""

    def test_started_init_clears_the_artifact(self, tmp_path: Path) -> None:
        path = tmp_path / "wt"
        path.mkdir()
        artifact = _seed_park(path)
        result = _invoke_init(path, [BUILTIN_ROLE])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["action"] == "started"
        assert not artifact.exists()

    def test_resumed_init_clears_the_artifact(self, worktree: Path) -> None:
        artifact = _seed_park(worktree)
        result = _invoke_init(worktree, [BUILTIN_ROLE, ENGINE_ROLE])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["action"] == "resumed"
        assert not artifact.exists()

    def test_recovered_result_clears_the_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.cli.review.workspace.initialize_workspace", lambda *_a, **_k: None
        )
        path = tmp_path / "wt"
        path.mkdir()
        artifact = _seed_park(path)
        result = _invoke_init(path, [BUILTIN_ROLE])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"recovered": True}
        assert not artifact.exists()

    def test_a_missing_artifact_is_fine(self, tmp_path: Path) -> None:
        path = tmp_path / "wt"
        path.mkdir()
        result = _invoke_init(path, [BUILTIN_ROLE])
        assert result.exit_code == 0, result.output
        assert not (path / REVIEW_PARK_FINDINGS_RELATIVE_PATH).exists()

    def test_a_failed_clear_does_not_fail_the_command(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "wt"
        (path / REVIEW_PARK_FINDINGS_RELATIVE_PATH).mkdir(parents=True)
        with caplog.at_level(logging.WARNING, logger="cw.review_park_findings"):
            result = _invoke_init(path, [BUILTIN_ROLE])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["action"] == "started"
        assert [r.getMessage().split()[0] for r in caplog.records] == [
            "review_park_findings_clear_failed"
        ]

    def test_no_clear_when_initialize_workspace_raises(self, worktree: Path) -> None:
        artifact = _seed_park(worktree)
        result = _invoke_init(worktree, [BUILTIN_ROLE], ticket="T-2", round_id="r2")
        assert result.exit_code != 0
        assert "belongs to ticket 'T-1'" in result.output
        assert artifact.exists()
