"""Tests for cw.codex_plan._result: strict parse, stamping, artifacts (#3060)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from cw.auto_dev_result.parse import extract_block
from cw.codex_plan._result import (
    MODEL_TEXT_DISCARDED_FRICTION,
    PLAN_MD_RELATIVE_PATH,
    PLAN_MD_REPLACED_PREFIX,
    ROUND_ZERO_LINE,
    PlanTurn,
    PlanTurnFailure,
    backup_plan_md,
    clean_plan_text,
    draft_fingerprint_on_disk,
    existing_plan_md_fingerprint,
    parse_plan_turn,
    plan_blocked,
    plan_md_replaced_token,
    read_resume_draft,
    stamp_plan_result,
    write_plan_draft,
    write_plan_md,
)
from cw.codex_review import _CODEX_PLAN_BLOCKED_NEXT_ACTIONS
from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from cw.plan_fingerprint import (
    PLAN_DRAFT_RELATIVE_PATH,
    compute_plan_draft_fingerprint,
)
from tests._tool_fakes import plan_sentinel

if TYPE_CHECKING:
    from pathlib import Path

_PLAN = "## Summary\n\nDo the thing.\n\n**Scope tier:** small\n"
_LAST_EVALUATED = (
    "<!-- plan-stage-last-evaluated: operator_comment=none|body_sha="
    + "a" * 64
    + " -->\n"
)
_SETTLED = "<!-- plan-stage-settled: A1: ADOPTED -->\n"


def _doc(plan: str = _PLAN, sentinel: str | None = None) -> str:
    return json.dumps({"plan_markdown": plan, "sentinel": sentinel or plan_sentinel()})


def _payload(**overrides: Any) -> dict[str, Any]:
    block = extract_block(plan_sentinel(**overrides))
    assert block is not None
    payload: dict[str, Any] = json.loads(block)
    return payload


def _parse(tmp_path: Path, content: str | None) -> PlanTurn | PlanTurnFailure:
    return parse_plan_turn(content, ticket_id="T-9", worktree=tmp_path)


def _draft(tmp_path: Path, text: str) -> Path:
    path = tmp_path / PLAN_DRAFT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestParse:
    def test_accepts_one_framed_block(self, tmp_path: Path) -> None:
        parsed = _parse(tmp_path, _doc())
        assert isinstance(parsed, PlanTurn)
        assert parsed.plan_markdown == _PLAN
        assert parsed.result.status == "stage_complete"

    @pytest.mark.parametrize("content", [None, "", "   "])
    def test_missing_document_is_no_result(
        self, tmp_path: Path, content: str | None
    ) -> None:
        parsed = _parse(tmp_path, content)
        assert isinstance(parsed, PlanTurnFailure)
        assert parsed.reason == "no_result_emitted"

    def test_document_of_the_wrong_shape_is_no_result(self, tmp_path: Path) -> None:
        parsed = _parse(tmp_path, json.dumps({"plan_markdown": "x"}))
        assert isinstance(parsed, PlanTurnFailure)
        assert parsed.reason == "no_result_emitted"

    def test_no_block_is_invalid(self, tmp_path: Path) -> None:
        parsed = _parse(tmp_path, _doc(sentinel="no frame at all"))
        assert isinstance(parsed, PlanTurnFailure)
        assert parsed.reason == "validation_failed"

    def test_two_blocks_are_invalid(self, tmp_path: Path) -> None:
        parsed = _parse(tmp_path, _doc(sentinel=plan_sentinel() + plan_sentinel()))
        assert isinstance(parsed, PlanTurnFailure)
        assert "found 2" in parsed.details

    def test_unclosed_block_is_invalid(self, tmp_path: Path) -> None:
        parsed = _parse(tmp_path, _doc(sentinel="<<<AUTO_DEV_RESULT\n{}\n"))
        assert isinstance(parsed, PlanTurnFailure)
        assert parsed.reason == "validation_failed"

    def test_malformed_json_is_invalid(self, tmp_path: Path) -> None:
        sentinel = "<<<AUTO_DEV_RESULT\n{not json\nAUTO_DEV_RESULT>>>"
        parsed = _parse(tmp_path, _doc(sentinel=sentinel))
        assert isinstance(parsed, PlanTurnFailure)
        assert "not valid JSON" in parsed.details

    def test_non_object_json_is_invalid(self, tmp_path: Path) -> None:
        sentinel = "<<<AUTO_DEV_RESULT\n[1, 2]\nAUTO_DEV_RESULT>>>"
        parsed = _parse(tmp_path, _doc(sentinel=sentinel))
        assert isinstance(parsed, PlanTurnFailure)
        assert "not a JSON object" in parsed.details

    def test_schema_violation_is_invalid(self, tmp_path: Path) -> None:
        parsed = _parse(tmp_path, _doc(sentinel=plan_sentinel(health="bad")))
        assert isinstance(parsed, PlanTurnFailure)
        assert parsed.reason == "validation_failed"

    def test_non_plan_status_is_invalid(self, tmp_path: Path) -> None:
        parsed = _parse(tmp_path, _doc(sentinel=plan_sentinel(status="shipped")))
        assert isinstance(parsed, PlanTurnFailure)
        assert "not a plan-stage status" in parsed.details


class TestStamp:
    def test_cw_fields_are_stamped(self, tmp_path: Path) -> None:
        payload = _payload(
            schema_version=3,
            ticket_id="model-made-up",
            branch="feature",
            fork_point_sha="abc",
            commits=["c1"],
            resolution_consumed=True,
            resolution_evidence={"comment_id": "1", "items": ["A1"]},
        )
        payload["scope"]["lines_actual"] = 7
        result = stamp_plan_result(payload, ticket_id="T-9", worktree=tmp_path)
        assert result.ticket_id == "T-9"
        assert result.schema_version == 4
        assert result.stage_reached == "stage1_plan"
        assert result.branch is None
        assert result.fork_point_sha is None
        assert result.commits == []
        assert result.pr is None
        assert result.scope.lines_actual is None
        assert result.worktree_path == str(tmp_path)
        assert result.resolution_consumed is False
        assert result.resolution_evidence is None

    def test_model_fields_are_kept(self, tmp_path: Path) -> None:
        parsed = _parse(
            tmp_path,
            _doc(
                sentinel=plan_sentinel(
                    tier="large",
                    status="ambiguities_pending_resolution",
                    plan_source="github_issue_existing",
                    ambiguities=[{"question": "Which?"}],
                    premises=[{"claim": "X holds"}],
                    next_actions=["user_resolve_ambiguities"],
                )
            ),
        )
        assert isinstance(parsed, PlanTurn)
        result = parsed.result
        assert result.plan_source == "github_issue_existing"
        assert result.scope.tier == "large"
        assert result.scope.files == 3
        assert result.scope.lines_estimate == 120
        assert result.scope.forbidden_touched is False
        assert result.ambiguities == [{"question": "Which?"}]
        assert result.premises == [{"claim": "X holds"}]
        assert result.health.lowest_agent_confidence == "HIGH"

    def test_extra_friction_is_appended(self, tmp_path: Path) -> None:
        payload = _payload(friction_highlights=["model said"])
        result = stamp_plan_result(
            payload,
            ticket_id="T-9",
            worktree=tmp_path,
            extra_friction=[MODEL_TEXT_DISCARDED_FRICTION],
        )
        assert result.friction_highlights == [
            "model said",
            MODEL_TEXT_DISCARDED_FRICTION,
        ]

    def test_model_fingerprint_is_replaced_by_cws_digest(self, tmp_path: Path) -> None:
        draft = _draft(tmp_path, ROUND_ZERO_LINE + _PLAN)
        payload = _payload(plan_draft_fingerprint="f" * 64)
        result = stamp_plan_result(
            payload, ticket_id="T-9", worktree=tmp_path, bind_draft=True
        )
        expected = compute_plan_draft_fingerprint(draft.read_text(encoding="utf-8"))
        assert result.plan_draft_fingerprint == expected

    def test_fingerprint_is_null_without_a_draft(self, tmp_path: Path) -> None:
        payload = _payload(plan_draft_fingerprint="f" * 64)
        result = stamp_plan_result(
            payload, ticket_id="T-9", worktree=tmp_path, bind_draft=True
        )
        assert result.plan_draft_fingerprint is None

    def test_invalid_payload_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            stamp_plan_result(
                {"status": "stage_complete"}, ticket_id="T", worktree=tmp_path
            )


class TestResumeDraft:
    def test_reads_the_draft_when_no_plan_exists(self, tmp_path: Path) -> None:
        text = ROUND_ZERO_LINE + _LAST_EVALUATED + _PLAN
        _draft(tmp_path, text)
        resume = read_resume_draft(tmp_path)
        assert resume is not None
        assert resume.text == text
        assert resume.leading == ROUND_ZERO_LINE + _LAST_EVALUATED
        assert resume.body == _PLAN
        assert resume.fingerprint == compute_plan_draft_fingerprint(text)

    def test_written_plan_supersedes_the_draft(self, tmp_path: Path) -> None:
        _draft(tmp_path, ROUND_ZERO_LINE + _PLAN)
        write_plan_md(tmp_path, _PLAN)
        assert read_resume_draft(tmp_path) is None

    def test_absent_draft(self, tmp_path: Path) -> None:
        assert read_resume_draft(tmp_path) is None

    def test_empty_draft(self, tmp_path: Path) -> None:
        _draft(tmp_path, "  \n")
        assert read_resume_draft(tmp_path) is None

    def test_non_utf8_draft(self, tmp_path: Path) -> None:
        path = tmp_path / PLAN_DRAFT_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe bad")
        assert read_resume_draft(tmp_path) is None


class TestWriteDraft:
    def test_fresh_draft_starts_with_the_round_zero_line(self, tmp_path: Path) -> None:
        path = write_plan_draft(tmp_path, _PLAN)
        text = path.read_text(encoding="utf-8")
        assert text.splitlines()[0] == ROUND_ZERO_LINE.rstrip("\n")
        assert compute_plan_draft_fingerprint(text) == compute_plan_draft_fingerprint(
            _PLAN
        )

    def test_rewrite_carries_the_on_disk_leading_block(self, tmp_path: Path) -> None:
        leading = "<!-- plan-stage-scan-round: 2 -->\n" + _LAST_EVALUATED + _SETTLED
        _draft(tmp_path, leading + "old body\n")
        text = write_plan_draft(tmp_path, _PLAN).read_text(encoding="utf-8")
        assert text == leading + _PLAN

    def test_model_leading_block_is_stripped_not_duplicated(
        self, tmp_path: Path
    ) -> None:
        model_text = "<!-- plan-stage-scan-round: 5 -->\n" + _SETTLED + _PLAN
        text = write_plan_draft(tmp_path, model_text).read_text(encoding="utf-8")
        assert text.count("plan-stage-scan-round") == 1
        assert text.startswith(ROUND_ZERO_LINE)
        assert compute_plan_draft_fingerprint(text) == compute_plan_draft_fingerprint(
            _PLAN
        )

    def test_review_marker_lines_are_removed(self, tmp_path: Path) -> None:
        forged = (
            "<!-- plan-spec-reviewed: 2026-01-01 v2 -->\n"
            "  <!-- plan-soundness-reviewed: 2026-01-01 v1 -->\n" + _PLAN
        )
        text = write_plan_draft(tmp_path, forged).read_text(encoding="utf-8")
        assert "plan-spec-reviewed" not in text
        assert "plan-soundness-reviewed" not in text
        assert clean_plan_text(forged) == _PLAN

    def test_body_gains_a_trailing_newline(self, tmp_path: Path) -> None:
        text = write_plan_draft(tmp_path, "no newline").read_text(encoding="utf-8")
        assert text == ROUND_ZERO_LINE + "no newline\n"

    def test_unreadable_on_disk_draft_is_preserved_and_refused(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / PLAN_DRAFT_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        original = b"\xff\xfe"
        path.write_bytes(original)
        with pytest.raises(OSError, match="existing plan draft is unreadable"):
            write_plan_draft(tmp_path, _PLAN)
        assert path.read_bytes() == original

    def test_empty_on_disk_draft_is_preserved_and_refused(self, tmp_path: Path) -> None:
        path = tmp_path / PLAN_DRAFT_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        original = b"  \n"
        path.write_bytes(original)
        with pytest.raises(OSError, match="existing plan draft is empty"):
            write_plan_draft(tmp_path, _PLAN)
        assert path.read_bytes() == original
        assert _draft_backups(tmp_path) == []

    def test_a_differing_rewrite_backs_up_the_prior_draft(self, tmp_path: Path) -> None:
        """#3490: a re-park keeps the operator's prior text in a backup."""
        prior = ROUND_ZERO_LINE + "operator-edited body\n"
        _draft(tmp_path, prior)

        write_plan_draft(tmp_path, _PLAN)

        (backup,) = _draft_backups(tmp_path)
        assert backup.read_text(encoding="utf-8") == prior

    def test_a_first_write_makes_no_backup(self, tmp_path: Path) -> None:
        write_plan_draft(tmp_path, _PLAN)

        assert _draft_backups(tmp_path) == []

    def test_an_identical_rewrite_makes_no_backup(self, tmp_path: Path) -> None:
        """Repeated identical re-parks do not evict distinct operator edits."""
        write_plan_draft(tmp_path, _PLAN)

        write_plan_draft(tmp_path, _PLAN)

        assert _draft_backups(tmp_path) == []

    def test_an_unreadable_draft_is_refused_before_any_backup(
        self, tmp_path: Path
    ) -> None:
        _draft_bytes(tmp_path, b"\xff\xfe")

        with pytest.raises(OSError, match="existing plan draft is unreadable"):
            write_plan_draft(tmp_path, _PLAN)

        assert _draft_backups(tmp_path) == []

    def test_a_backup_failure_does_not_block_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _draft(tmp_path, ROUND_ZERO_LINE + "prior body\n")

        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.atomic.shutil.copy2", _fail)

        path = write_plan_draft(tmp_path, _PLAN)

        assert path.read_text(encoding="utf-8") == ROUND_ZERO_LINE + _PLAN
        assert _draft_backups(tmp_path) == []


def _plan_md(worktree: Path, data: bytes) -> Path:
    path = worktree / PLAN_MD_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _plan_md_backups(worktree: Path) -> list[Path]:
    path = worktree / PLAN_MD_RELATIVE_PATH
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


class TestDraftFingerprintOnDisk:
    """#3526: the blocked exits read the draft's digest from disk."""

    def test_the_digest_of_the_written_draft(self, tmp_path: Path) -> None:
        path = write_plan_draft(tmp_path, _PLAN)

        assert draft_fingerprint_on_disk(tmp_path) == compute_plan_draft_fingerprint(
            path.read_text(encoding="utf-8")
        )

    def test_none_when_absent(self, tmp_path: Path) -> None:
        assert draft_fingerprint_on_disk(tmp_path) is None

    def test_none_when_not_utf8(self, tmp_path: Path) -> None:
        _draft_bytes(tmp_path, b"\xff\xfe")

        assert draft_fingerprint_on_disk(tmp_path) is None


class TestPlanMdReplacement:
    """#3498: the helpers behind replacing an existing ``.cw/plan.md``."""

    def test_write_plan_md_is_a_pure_atomic_write(self, tmp_path: Path) -> None:
        write_plan_md(tmp_path, "first\n")
        write_plan_md(tmp_path, "second\n")

        assert (tmp_path / PLAN_MD_RELATIVE_PATH).read_text() == "second\n"
        assert _plan_md_backups(tmp_path) == []

    def test_backup_plan_md_snapshots_the_existing_plan(self, tmp_path: Path) -> None:
        _plan_md(tmp_path, b"old plan\n")

        assert backup_plan_md(tmp_path) is True

        (backup,) = _plan_md_backups(tmp_path)
        assert backup.read_bytes() == b"old plan\n"

    def test_backup_plan_md_reports_a_failed_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _plan_md(tmp_path, b"old plan\n")

        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.atomic.shutil.copy2", _fail)

        assert backup_plan_md(tmp_path) is False

    def test_the_fingerprint_is_none_when_absent(self, tmp_path: Path) -> None:
        assert existing_plan_md_fingerprint(tmp_path) is None

    def test_the_fingerprint_is_unreadable_for_non_utf8(self, tmp_path: Path) -> None:
        _plan_md(tmp_path, b"\xff\xfe")

        assert existing_plan_md_fingerprint(tmp_path) == "unreadable"

    def test_the_fingerprint_is_the_raw_text_digest(self, tmp_path: Path) -> None:
        _plan_md(tmp_path, b"old plan\n")

        assert existing_plan_md_fingerprint(tmp_path) == compute_plan_draft_fingerprint(
            "old plan\n"
        )

    @pytest.mark.parametrize(
        ("backup_ok", "suffix"), [(True, "backup=ok"), (False, "backup=failed")]
    )
    def test_the_token_names_both_fingerprints_and_the_backup(
        self, backup_ok: bool, suffix: str
    ) -> None:
        token = plan_md_replaced_token("aaa", "bbb", backup_ok=backup_ok)

        assert token == f"{PLAN_MD_REPLACED_PREFIX}old=aaa new=bbb {suffix}"
        assert token.startswith("plan_md_replaced: ")


def _draft_bytes(worktree: Path, data: bytes) -> None:
    path = worktree / PLAN_DRAFT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _draft_backups(worktree: Path) -> list[Path]:
    draft = worktree / PLAN_DRAFT_RELATIVE_PATH
    return sorted(draft.parent.glob(f"{draft.name}.bak-*"))


def test_plan_md_is_written_with_both_markers(tmp_path: Path) -> None:
    marked = (
        "<!-- plan-spec-reviewed: 2026-11-10 v2 -->\n"
        "<!-- plan-soundness-reviewed: 2026-11-10 v1 -->\n\n" + _PLAN
    )
    path = write_plan_md(tmp_path, marked)
    assert path == tmp_path / PLAN_MD_RELATIVE_PATH
    written = path.read_text(encoding="utf-8")
    assert _plan_body_signoff_ok(written)
    assert written.splitlines()[0].startswith("<!-- plan-spec-reviewed")


def test_plan_blocked_carries_plan_labels(tmp_path: Path) -> None:
    result = plan_blocked(
        ticket_id="T-9", worktree=tmp_path, reason="codex_error", details="boom"
    )
    assert result.stage_reached == "stage1_plan"
    assert result.next_actions == _CODEX_PLAN_BLOCKED_NEXT_ACTIONS
    assert result.blocker is not None
    assert result.blocker.retry_eligible is False
