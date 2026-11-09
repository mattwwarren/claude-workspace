"""Tests for ``cw.review_engine.record`` and ``cw review engine record`` (R9)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.models.review_engine_summary import MAX_ENGINE_SUMMARY_BYTES
from cw.review_engine.constants import ENGINE_ROLE, EngineRoundFailure
from cw.review_engine.materialize import engine_paths
from cw.review_engine.record import RecordResult, record_engine_round
from cw.review_engine_summary_io import EngineSummaryRejection
from tests._review_engine_helpers import (
    EngineRepo,
    StubEngine,
    engine_finding,
    invoke,
    make_engine_repo,
    make_finding,
    make_summary,
    pad_to,
    summary_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Callable

ROUND = "r1"
MARKER = ".cw/review-findings-round.json"
DOCUMENT = ".cw/review-findings/repo-review-engine.json"
EXIT_FAILED = 3


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo)


def _init_round(repo: EngineRepo) -> None:
    result = invoke(
        "review",
        "workspace",
        "init",
        "--ticket",
        repo.ticket,
        "--round-id",
        ROUND,
        "--role",
        ENGINE_ROLE,
        "--worktree",
        str(repo.worktree),
    )
    assert result.exit_code == 0, result.output


def _record(
    repo: EngineRepo, *, expect_head: str | None = None, summary: Path | None = None
) -> tuple[int, dict[str, object]]:
    args = [
        "review",
        "engine",
        "record",
        "--round-id",
        ROUND,
        "--expect-head",
        expect_head or repo.head,
        "--worktree",
        str(repo.worktree),
    ]
    if summary is not None:
        args += ["--summary", str(summary)]
    result = invoke(*args)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = {}
    return result.exit_code, body


def _marker(repo: EngineRepo) -> dict[str, Any]:
    marker: dict[str, Any] = json.loads(
        (repo.worktree / MARKER).read_text(encoding="utf-8")
    )
    return marker


def _assert_failed_round(
    repo: EngineRepo,
    exit_code: int,
    body: dict[str, object],
    reason: EngineRoundFailure,
) -> None:
    assert exit_code == EXIT_FAILED
    assert body["result"] == "failed"
    assert body["reason"] == reason.value
    assert body["findings_recorded"] == 0
    assert isinstance(body["friction"], str)
    assert str(body["friction"]).startswith(reason.value)
    marker = _marker(repo)
    roles = {entry["role"]: entry["status"] for entry in marker["SPAWNED_ROLES"]}
    assert roles[ENGINE_ROLE] == "failed"
    failed = marker["failed_reviewers"]
    assert [(f["role"], f["reason"]) for f in failed] == [(ENGINE_ROLE, reason.value)]
    assert (
        f"reviewer_failed: {ENGINE_ROLE} ({reason.value})"
        in marker["friction_highlights"]
    )


def test_pass_summary_recorded_as_document(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary([engine_finding("NIT")]))

    exit_code, body = _record(repo)

    assert exit_code == 0
    assert body == {
        "result": "recorded",
        "reason": None,
        "friction": None,
        "findings_recorded": 1,
    }
    document = json.loads((repo.worktree / DOCUMENT).read_text(encoding="utf-8"))
    assert document["reviewer_role"] == ENGINE_ROLE
    assert document["status"] == "ok"
    assert document["detail"].strip()
    assert document["findings"][0]["source"] == "engine"
    assert _marker(repo)["completed_roles"] == [ENGINE_ROLE]


def test_fail_summary_records_all_findings(repo: EngineRepo) -> None:
    _init_round(repo)
    findings = [
        engine_finding("MUST_FIX"),
        engine_finding("SHOULD_FIX", rule_id="style.other"),
        engine_finding("DEBT", rule_id="style.debt"),
    ]
    StubEngine(repo).run(ROUND, make_summary(findings))

    exit_code, body = _record(repo)

    assert exit_code == 0
    assert body["findings_recorded"] == 3
    document = json.loads((repo.worktree / DOCUMENT).read_text(encoding="utf-8"))
    assert [f["severity"] for f in document["findings"]] == [
        "MUST_FIX",
        "SHOULD_FIX",
        "DEBT",
    ]


def test_zero_findings_pass_records_ok_document_with_detail(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary())

    exit_code, body = _record(repo)

    assert exit_code == 0
    assert body["findings_recorded"] == 0
    document = json.loads((repo.worktree / DOCUMENT).read_text(encoding="utf-8"))
    assert document["status"] == "ok"
    assert document["findings"] == []
    assert "verdict=PASS" in document["detail"]


def test_engine_no_summary_missing_file(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, skip=True)

    exit_code, body = _record(repo)

    _assert_failed_round(repo, exit_code, body, EngineRoundFailure.ENGINE_NO_SUMMARY)
    assert str(engine_paths(repo.worktree, ROUND).summary_path) in str(body["friction"])


def _unreadable(path: Path) -> None:
    path.write_bytes(summary_bytes(make_summary()))
    path.chmod(0)


@pytest.mark.parametrize(
    "rejection",
    list(EngineSummaryRejection),
    ids=[r.value for r in EngineSummaryRejection],
)
def test_engine_summary_invalid_for_every_loader_rejection(
    repo: EngineRepo, rejection: EngineSummaryRejection
) -> None:
    if rejection is EngineSummaryRejection.UNREADABLE and os.geteuid() == 0:
        pytest.skip("root reads mode-0 files")
    _init_round(repo)
    evidence = "SENTINEL-EVIDENCE-41aa"
    finding = make_finding(evidence=evidence)
    bodies: dict[EngineSummaryRejection, bytes] = {
        EngineSummaryRejection.OVERSIZED: pad_to(
            summary_bytes(make_summary()), MAX_ENGINE_SUMMARY_BYTES + 1
        ),
        EngineSummaryRejection.NOT_UTF8: b"\xff\xfe{}",
        EngineSummaryRejection.NOT_JSON: b"{nope",
        EngineSummaryRejection.NOT_OBJECT: b"[1]",
        EngineSummaryRejection.UNSUPPORTED_SCHEMA_VERSION: summary_bytes(
            make_summary(schema_version=2)
        ),
        EngineSummaryRejection.SCHEMA_INVALID: summary_bytes(
            make_summary([make_finding(evidence=evidence, file="N/A")])
        ),
        EngineSummaryRejection.INCONSISTENT: summary_bytes(
            make_summary([finding, finding])
        ),
    }
    path = engine_paths(repo.worktree, ROUND).summary_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if rejection is EngineSummaryRejection.UNREADABLE:
        _unreadable(path)
    else:
        path.write_bytes(bodies[rejection])
    try:
        exit_code, body = _record(repo)
    finally:
        path.chmod(0o644)

    _assert_failed_round(
        repo, exit_code, body, EngineRoundFailure.ENGINE_SUMMARY_INVALID
    )
    assert str(body["friction"]).startswith(
        f"engine_summary_invalid: {rejection.value}"
    )
    assert evidence not in str(body["friction"])


def test_unreadable_non_enoent_is_invalid_not_no_summary(repo: EngineRepo) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads mode-0 files")
    _init_round(repo)
    path = engine_paths(repo.worktree, ROUND).summary_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _unreadable(path)
    try:
        exit_code, body = _record(repo)
    finally:
        path.chmod(0o644)

    assert exit_code == EXIT_FAILED
    assert body["reason"] == "engine_summary_invalid"


@pytest.mark.parametrize("coverage", ["PARTIAL", "FAILED"])
def test_engine_coverage_incomplete(repo: EngineRepo, coverage: str) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary(coverage=coverage))

    exit_code, body = _record(repo)

    _assert_failed_round(
        repo, exit_code, body, EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE
    )
    assert body["friction"] == f"engine_coverage_incomplete: coverage={coverage}"


def test_engine_head_mismatch_summary_sha_differs(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary(head_sha="b" * 40))

    exit_code, body = _record(repo)

    _assert_failed_round(repo, exit_code, body, EngineRoundFailure.ENGINE_HEAD_MISMATCH)
    assert "summary=" + "b" * 12 in str(body["friction"])


def test_engine_head_mismatch_head_moved(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary())

    exit_code, body = _record(repo, expect_head="c" * 40)

    _assert_failed_round(repo, exit_code, body, EngineRoundFailure.ENGINE_HEAD_MISMATCH)
    assert "summary=-" in str(body["friction"])


def test_engine_worktree_mutated_tracked_edit(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary(), mutate=True)

    exit_code, body = _record(repo)

    _assert_failed_round(
        repo, exit_code, body, EngineRoundFailure.ENGINE_WORKTREE_MUTATED
    )


def test_engine_worktree_mutated_untracked_file(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary())
    (repo.worktree / "stray.txt").write_text("x", encoding="utf-8")

    exit_code, body = _record(repo)

    _assert_failed_round(
        repo, exit_code, body, EngineRoundFailure.ENGINE_WORKTREE_MUTATED
    )


def test_verdict_fail_without_gating_finding_is_invalid(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary([engine_finding("NIT")], verdict="FAIL"))

    exit_code, body = _record(repo)

    _assert_failed_round(
        repo, exit_code, body, EngineRoundFailure.ENGINE_SUMMARY_INVALID
    )
    assert "fail_without_gating_finding" in str(body["friction"])


def test_record_without_active_workspace_exit_1(repo: EngineRepo) -> None:
    StubEngine(repo).run(ROUND, make_summary())

    exit_code, _body = _record(repo)

    assert exit_code == 1


def test_record_twice_exit_1(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary())
    assert _record(repo)[0] == 0

    exit_code, _body = _record(repo)

    assert exit_code == 1


def test_failure_while_record_failure_itself_errors_propagates_exit_1(
    repo: EngineRepo,
) -> None:
    """A failed round with no workspace to record into is a fault, not exit 3."""
    StubEngine(repo).run(ROUND, make_summary(coverage="PARTIAL"))

    exit_code, _body = _record(repo)

    assert exit_code == 1


def test_derived_summary_path_default_matches_materialize(repo: EngineRepo) -> None:
    _init_round(repo)
    materialized = invoke(
        "review",
        "engine",
        "materialize",
        "--round-id",
        ROUND,
        "--expect-head",
        repo.head,
        "--worktree",
        str(repo.worktree),
    )
    assert materialized.exit_code == 0, materialized.output
    summary_path = Path(json.loads(materialized.output)["summary_path"])
    assert summary_path == engine_paths(repo.worktree, ROUND).summary_path.resolve()
    StubEngine(repo).run(ROUND, make_summary([engine_finding("NIT")]))
    assert summary_path.is_file()

    exit_code, body = _record(repo)

    assert exit_code == 0
    assert body["findings_recorded"] == 1


def test_explicit_summary_path_is_used(repo: EngineRepo, tmp_path: Path) -> None:
    _init_round(repo)
    custom = tmp_path / "elsewhere.json"
    custom.write_bytes(summary_bytes(make_summary(head_sha=repo.head)))

    exit_code, body = _record(repo, summary=custom)

    assert exit_code == 0
    assert body["result"] == "recorded"


def test_json_result_shape_recorded_and_failed(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, make_summary())
    _exit, recorded = _record(repo)
    assert set(recorded) == {"result", "reason", "friction", "findings_recorded"}

    failed = RecordResult("failed", EngineRoundFailure.ENGINE_NO_SUMMARY, "x", 0)
    assert failed.findings_recorded == 0


def test_library_returns_failed_result_and_records_marker(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(ROUND, skip=True)

    result = record_engine_round(
        repo.worktree,
        round_id=ROUND,
        expect_head=repo.head,
        summary_path=None,
        skill="repo-review",
    )

    assert result.result == "failed"
    assert result.reason is EngineRoundFailure.ENGINE_NO_SUMMARY
