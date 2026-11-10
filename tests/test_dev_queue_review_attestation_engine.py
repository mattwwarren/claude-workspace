"""Engine-summary wiring of the attestation builder (#3108, RFC 0017 II1).

Every test drives ``attest_review_exit`` (not ``record_review_attestation``),
because the discovery of the engine round's summary lives there. Git and
subprocesses are wired to fail, so any shell-out in the builder fails the test.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue.review_attestation import (
    attest_review_exit,
    record_review_attestation,
)
from cw.models.review_attestation import FoundDuringReview
from cw.models.review_engine_summary import MAX_ENGINE_SUMMARY_BYTES
from cw.review_engine.constants import ENGINE_STATE_RELATIVE_DIR
from tests._review_attestation_helpers import (
    LOGGER,
    OTHER_SHA,
    WRITTEN_AT,
    attestation_for,
    clean_engine_summary,
    clean_sentinel,
    converged_worktree_ledger,
    engine_client,
    install_no_git_guard,
    review_row,
    with_review_backend,
    write_round_summary,
)
from tests._review_convergence_helpers import confirmed_ledger, make_review_client
from tests._review_engine_helpers import SHA, make_finding, make_summary, write_summary

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import ClientConfig, TicketTask
    from cw.models.review_attestation import ReviewAttestation

GOLDEN = Path(__file__).parent / "fixtures" / "review_attestation_engine_golden.json"
UNAVAILABLE = (
    "review attestation engine evidence unavailable: ticket=T-1 client=test-client"
)
ENGINE_DIR = ENGINE_STATE_RELATIVE_DIR


@pytest.fixture(autouse=True)
def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    install_no_git_guard(monkeypatch)


def _forbidden_scan(*_args: object, **_kwargs: object) -> object:
    msg = "the engine summary scan must not run"
    raise AssertionError(msg)


def _worktree(tmp_path: Path, *, ledger: bool = True) -> Path:
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    if ledger:
        converged_worktree_ledger(worktree, SHA, "T-1")
    return worktree


def _exit(
    worktree: Path,
    client: ClientConfig,
    *,
    last_result: dict[str, object] | None = None,
) -> tuple[TicketTask, ReviewAttestation | None]:
    task = review_row(worktree)
    result = clean_sentinel(SHA) if last_result is None else last_result
    return task, attest_review_exit(task, last_result=result, client=client)


def _records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def _round(worktree: Path, name: str, summary: dict[str, object], age: int) -> None:
    write_round_summary(worktree, name, summary)
    stamp = 2_000_000_000 - age
    os.utime(worktree / ENGINE_DIR / name, (stamp, stamp))


class TestEngineBacked:
    def test_summary_at_the_reviewed_head_backs_the_attestation(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        worktree = _worktree(tmp_path)
        write_round_summary(
            worktree,
            "round-1",
            make_summary(
                head_sha=SHA,
                risk="MEDIUM",
                acceptance=[
                    {"id": "AC-2", "status": "PASS", "evidence": None},
                    {"id": "AC-1", "status": "NOT_APPLICABLE", "evidence": None},
                ],
                gate_signals=[{"name": "tests", "status": "PASS", "detail": None}],
            ),
        )
        with caplog.at_level(logging.INFO, logger=LOGGER):
            task, att = _exit(worktree, engine_client(tmp_path))
        assert att is not None
        assert task.review_attestation == att
        assert att.engine == "code-review"
        assert att.risk == "MEDIUM"
        assert att.acceptance is not None
        assert [row.id for row in att.acceptance] == ["AC-1", "AC-2"]
        assert att.gate_signals is not None
        assert [(s.name, s.status) for s in att.gate_signals] == [("tests", "PASS")]
        assert att.counts.must_fix == 0
        assert att.found_during_review is None
        assert _records(caplog) == []


class TestRoundSelection:
    def test_the_matching_head_is_chosen_among_rounds(self, tmp_path: Path) -> None:
        worktree = _worktree(tmp_path)
        _round(worktree, "older", make_summary(head_sha=OTHER_SHA, risk="HIGH"), 0)
        _round(worktree, "match", make_summary(head_sha=SHA, risk="MEDIUM"), 10)
        _, att = _exit(worktree, engine_client(tmp_path))
        assert att is not None
        assert att.risk == "MEDIUM"

    def test_complete_pass_beats_a_failing_summary_at_the_same_head(
        self, tmp_path: Path
    ) -> None:
        worktree = _worktree(tmp_path)
        failing = make_summary([make_finding(severity="MUST_FIX")], head_sha=SHA)
        _round(worktree, "newer-fail", failing, 0)
        _round(worktree, "older-pass", make_summary(head_sha=SHA), 10)
        _, att = _exit(worktree, engine_client(tmp_path))
        assert att is not None
        assert att.engine == "code-review"

    def test_newest_mtime_wins_between_two_passes(self, tmp_path: Path) -> None:
        worktree = _worktree(tmp_path)
        _round(worktree, "old", make_summary(head_sha=SHA, risk="HIGH"), 10)
        _round(worktree, "new", make_summary(head_sha=SHA, risk="MEDIUM"), 0)
        _, att = _exit(worktree, engine_client(tmp_path))
        assert att is not None
        assert att.risk == "MEDIUM"


def _symlinked_round(worktree: Path) -> None:
    real = write_summary(worktree / "elsewhere", make_summary(head_sha=SHA)).parent
    engine = worktree / ENGINE_DIR
    engine.mkdir(parents=True)
    (engine / "linked").symlink_to(real, target_is_directory=True)


def _symlinked_summary(worktree: Path) -> None:
    real = write_summary(worktree / "elsewhere", make_summary(head_sha=SHA))
    directory = worktree / ENGINE_DIR / "round-1"
    directory.mkdir(parents=True)
    (directory / "summary.json").symlink_to(real)


def _no_engine_dir(_worktree: Path) -> None:
    return None


def _other_head(worktree: Path) -> None:
    write_round_summary(worktree, "r1", make_summary(head_sha=OTHER_SHA))


def _invalid_json(worktree: Path) -> None:
    write_round_summary(worktree, "r1", b"not json")


def _schema_invalid(worktree: Path) -> None:
    write_round_summary(worktree, "r1", make_summary(head_sha=SHA, risk="BOGUS"))


def _oversized(worktree: Path) -> None:
    write_round_summary(worktree, "r1", b" " * (MAX_ENGINE_SUMMARY_BYTES + 1))


def _bad_round_id(worktree: Path) -> None:
    write_round_summary(worktree, "-bad", make_summary(head_sha=SHA))


_UNAVAILABLE_SETUPS: dict[str, Callable[[Path], None]] = {
    "no_engine_dir": _no_engine_dir,
    "other_head": _other_head,
    "invalid_json": _invalid_json,
    "schema_invalid": _schema_invalid,
    "oversized": _oversized,
    "symlinked_round_dir": _symlinked_round,
    "symlinked_summary": _symlinked_summary,
    "bad_round_id": _bad_round_id,
}


class TestDowngrade:
    @pytest.mark.parametrize("setup", sorted(_UNAVAILABLE_SETUPS))
    def test_missing_evidence_writes_a_ledger_only_attestation(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, setup: str
    ) -> None:
        worktree = _worktree(tmp_path)
        _UNAVAILABLE_SETUPS[setup](worktree)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            task, att = _exit(worktree, engine_client(tmp_path))
        assert att is not None
        assert task.review_attestation == att
        assert att.engine is None
        assert att.acceptance is None
        assert att.risk is None
        assert att.gate_signals is None
        assert (att.counts.must_fix, att.counts.should_fix) == (0, 0)
        assert att.found_during_review == FoundDuringReview(must_fix=0, should_fix=0)
        assert att.reviewer_backend == "claude-native"
        assert _records(caplog) == [UNAVAILABLE]

    def test_no_worktree_skips_on_ledger_absent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No ledger is readable without a worktree, so the clean-mode client skips
        # on ledger_absent before any scan runs.
        task = review_row(None)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            att = attest_review_exit(
                task, last_result=clean_sentinel(SHA), client=engine_client(tmp_path)
            )
        assert att is None
        assert all("reason=ledger_absent" in line for line in _records(caplog))


class TestContradictingEvidence:
    @pytest.mark.parametrize(
        "overrides",
        [{"coverage": "PARTIAL"}, {"verdict": "FAIL"}],
        ids=["partial_coverage", "failing_verdict"],
    )
    def test_a_disagreeing_summary_withholds_the_attestation(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        overrides: dict[str, object],
    ) -> None:
        worktree = _worktree(tmp_path)
        summary = make_summary(
            [make_finding(severity="MUST_FIX")] if "verdict" in overrides else None,
            head_sha=SHA,
            **({} if "verdict" in overrides else overrides),
        )
        write_round_summary(worktree, "r1", summary)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            task, att = _exit(worktree, engine_client(tmp_path))
        assert att is None
        assert task.review_attestation is None
        (line,) = _records(caplog)
        assert "reason=engine_mismatch" in line
        assert UNAVAILABLE not in line


def _ledger_wrong_ticket(worktree: Path) -> None:
    converged_worktree_ledger(worktree, SHA, "T-OTHER")


def _ledger_wrong_sha(worktree: Path) -> None:
    converged_worktree_ledger(worktree, OTHER_SHA, "T-1")


_PRECEDENCE = {
    "not_success": (clean_sentinel(SHA, status="blocked"), True),
    "health_degraded": (
        clean_sentinel(SHA, recommendation="EXIT_FOR_HUMAN_REVIEW"),
        True,
    ),
    "missing_reviewed_sha": (clean_sentinel(None), True),
    "bad_reviewed_sha": (clean_sentinel("a" * 39), True),
    "ledger_absent": (clean_sentinel(SHA), False),
}


class TestPrecedence:
    @pytest.mark.parametrize("with_summary", [True, False])
    @pytest.mark.parametrize("token", sorted(_PRECEDENCE))
    def test_an_earlier_skip_wins_and_the_scan_never_runs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        token: str,
        with_summary: bool,
    ) -> None:
        sentinel, has_ledger = _PRECEDENCE[token]
        worktree = _worktree(tmp_path, ledger=has_ledger)
        if with_summary:
            write_round_summary(worktree, "r1", make_summary(head_sha=SHA))
        monkeypatch.setattr(
            "cw.dev_queue.review_attestation.find_round_summary_for_head",
            _forbidden_scan,
        )
        with caplog.at_level(logging.INFO, logger=LOGGER):
            task, att = _exit(worktree, engine_client(tmp_path), last_result=sentinel)
        assert att is None
        assert task.review_attestation is None
        (line,) = _records(caplog)
        assert f"reason={token} " in line

    @pytest.mark.parametrize(
        ("token", "stale"),
        [
            ("ledger_disagrees", _ledger_wrong_ticket),
            ("ledger_disagrees", _ledger_wrong_sha),
        ],
        ids=["wrong_ticket", "wrong_sha"],
    )
    def test_a_disagreeing_ledger_wins_over_the_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        token: str,
        stale: Callable[[Path], None],
    ) -> None:
        worktree = _worktree(tmp_path, ledger=False)
        stale(worktree)
        write_round_summary(worktree, "r1", make_summary(head_sha=SHA))
        monkeypatch.setattr(
            "cw.dev_queue.review_attestation.find_round_summary_for_head",
            _forbidden_scan,
        )
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _, att = _exit(worktree, engine_client(tmp_path))
        assert att is None
        (line,) = _records(caplog)
        assert f"reason={token} " in line

    def test_a_downgrade_never_replaces_a_stored_engine_backed_attestation(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        worktree = _worktree(tmp_path)
        task = review_row(worktree)
        stored = attestation_for(SHA, engine="code-review")
        task.review_attestation = stored
        with caplog.at_level(logging.INFO, logger=LOGGER):
            att = attest_review_exit(
                task, last_result=clean_sentinel(SHA), client=engine_client(tmp_path)
            )
        assert att is None
        assert task.review_attestation == stored
        assert any("reason=weaker_than_stored" in line for line in _records(caplog))


class TestNoScanLanes:
    def test_a_client_without_an_engine_never_scans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.dev_queue.review_attestation.find_round_summary_for_head",
            _forbidden_scan,
        )
        worktree = _worktree(tmp_path)
        write_round_summary(worktree, "r1", make_summary(head_sha=SHA))
        for convergence in ("clean", "blocking"):
            client = make_review_client(tmp_path, convergence=convergence)
            _, att = _exit(worktree, client)
            assert att is not None
            assert att.engine is None
            assert att.acceptance is None

    def test_an_opencode_lane_never_scans_and_has_no_engine_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """opencode never runs the engine, so it neither scans nor claims it."""
        monkeypatch.setattr(
            "cw.dev_queue.review_attestation.find_round_summary_for_head",
            _forbidden_scan,
        )
        worktree = _worktree(tmp_path)
        write_round_summary(worktree, "r1", make_summary(head_sha=SHA))
        client = with_review_backend(engine_client(tmp_path), "opencode")
        _, att = _exit(worktree, client)
        assert att is not None
        assert att.engine is None
        assert att.reviewer_backend == "opencode"
        assert att.acceptance is None
        assert att.risk is None
        assert att.gate_signals is None


class TestCodexLaneScan:
    """#3216: the scan gate is ``engine_supported``, so a codex lane scans."""

    def test_a_codex_lane_scans_the_round_summary_and_is_engine_backed(
        self, tmp_path: Path
    ) -> None:
        worktree = _worktree(tmp_path)
        write_round_summary(
            worktree,
            "round-1",
            make_summary(
                head_sha=SHA,
                risk="MEDIUM",
                acceptance=[{"id": "AC-1", "status": "PASS", "evidence": None}],
                gate_signals=[{"name": "tests", "status": "PASS", "detail": None}],
            ),
        )
        client = with_review_backend(engine_client(tmp_path), "codex")
        task, att = _exit(worktree, client)
        assert att is not None
        assert task.review_attestation == att
        assert att.engine == "code-review"
        assert att.reviewer_backend == "codex"
        assert att.risk == "MEDIUM"
        assert [row.id for row in att.acceptance or []] == ["AC-1"]
        assert [s.name for s in att.gate_signals or []] == ["tests"]

    def test_a_codex_row_with_no_summary_downgrades_to_no_engine_label(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Today's "keeps the configured skill" label is gone for codex."""
        worktree = _worktree(tmp_path)
        client = with_review_backend(engine_client(tmp_path), "codex")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _, att = _exit(worktree, client)
        assert att is not None
        assert att.engine is None
        assert att.reviewer_backend == "codex"
        assert att.risk is None
        assert _records(caplog) == [UNAVAILABLE]


class TestEngineLabel:
    def _record(
        self, client: ClientConfig, *, summary: bool
    ) -> ReviewAttestation | None:
        task = review_row(None)
        return record_review_attestation(
            task,
            last_result=clean_sentinel(SHA),
            engine_summary=clean_engine_summary() if summary else None,
            ledger=confirmed_ledger(sha=SHA, ticket_id=task.ticket_id),
            client=client,
            now=WRITTEN_AT,
        )

    def test_claude_native_without_a_summary_is_unlabelled(
        self, tmp_path: Path
    ) -> None:
        att = self._record(engine_client(tmp_path), summary=False)
        assert att is not None
        assert att.engine is None

    def test_claude_native_with_a_summary_is_labelled(self, tmp_path: Path) -> None:
        att = self._record(engine_client(tmp_path), summary=True)
        assert att is not None
        assert att.engine == "code-review"

    def test_codex_with_a_summary_is_labelled(self, tmp_path: Path) -> None:
        client = with_review_backend(engine_client(tmp_path), "codex")
        att = self._record(client, summary=True)
        assert att is not None
        assert att.engine == "code-review"

    @pytest.mark.parametrize("backend", ["codex", "opencode"])
    def test_other_backends_without_a_summary_are_unlabelled(
        self, tmp_path: Path, backend: str
    ) -> None:
        """#3216: every backend names the engine only when a summary backs it."""
        client = with_review_backend(engine_client(tmp_path), backend)
        att = self._record(client, summary=False)
        assert att is not None
        assert att.engine is None


class TestGolden:
    def test_engine_backed_attestation_matches_golden(self, tmp_path: Path) -> None:
        worktree = _worktree(tmp_path)
        write_round_summary(
            worktree,
            "round-1",
            make_summary(
                head_sha=SHA,
                risk="MEDIUM",
                acceptance=[
                    {"id": "AC-2", "status": "PASS", "evidence": None},
                    {"id": "AC-1", "status": "NOT_APPLICABLE", "evidence": None},
                ],
                gate_signals=[{"name": "tests", "status": "PASS", "detail": None}],
            ),
        )
        _, att = _exit(worktree, engine_client(tmp_path))
        assert att is not None
        produced = att.model_copy(update={"written_at": WRITTEN_AT}).model_dump(
            mode="json"
        )
        assert produced == json.loads(GOLDEN.read_text(encoding="utf-8"))
