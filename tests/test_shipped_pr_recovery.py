"""Tests for ``cw.shipped_pr_recovery.prepare_red_pr_recovery`` (GitHub #2945)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.config import get_client
from cw.dev_queue.red_pr_recovery import RedPrRecovery
from cw.exceptions import RequeueStateError
from cw.pr_feedback import FeedbackSynthesisError
from cw.shipped_pr_recovery import prepare_red_pr_recovery
from tests._adopt_pr_helpers import (
    ADOPT_PR_URL,
    make_repair_feedback,
)
from tests._changelog_repair_helpers import DISARM_NONE, DISARM_ZERO_STILL_ARMED
from tests._shipped_red_helpers import CHECK, HEAD_SHA, install_red_gh, make_red_row
from tests._ticket_view_helpers import (
    ACME,
    install_acme_client,
    write_lane_client,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ReviewFeedback


@pytest.fixture(autouse=True)
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)


def _stub_synth(
    monkeypatch: pytest.MonkeyPatch,
    feedback: ReviewFeedback | None = None,
    *,
    error: str | None = None,
) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    fb = feedback or make_repair_feedback(reviewed_sha=HEAD_SHA)

    def _synth(repo: str, pr_number: int, *, cwd: Path) -> ReviewFeedback:
        calls.append((repo, pr_number))
        if error is not None:
            raise FeedbackSynthesisError(error)
        return fb

    monkeypatch.setattr("cw.shipped_pr_recovery.synthesize_pr_feedback", _synth)
    return calls


def _prepare(**row_overrides: object) -> RedPrRecovery:
    return prepare_red_pr_recovery(make_red_row(**row_overrides), get_client(ACME))


class TestHappyPath:
    def test_bundle_carries_the_live_evidence_and_disarms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head, _req = install_red_gh(monkeypatch, armed=True)
        calls = _stub_synth(monkeypatch)

        bundle = _prepare()

        assert bundle.repo == "acme/widgets"
        assert bundle.pr_number == 7
        assert bundle.head_sha == HEAD_SHA
        assert bundle.checks == (CHECK,)
        assert bundle.feedback.reviewed_sha == HEAD_SHA
        assert bundle.was_armed is True
        assert calls == [("acme/widgets", 7)]
        assert head.disarm_calls == 1
        assert head.armed is False

    def test_unarmed_pr_is_not_touched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        head, _req = install_red_gh(monkeypatch, armed=False)
        _stub_synth(monkeypatch)

        bundle = _prepare()

        assert bundle.was_armed is False
        assert head.disarm_calls == 0


class TestRefusals:
    def test_green_required_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        head, _req = install_red_gh(monkeypatch, required=[])
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError) as exc:
            _prepare()

        assert str(exc.value) == (
            f"no required check is failing on {ADOPT_PR_URL}; nothing to recover"
        )
        assert head.disarm_calls == 0

    def test_required_checks_unreadable_is_not_treated_as_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head, req = install_red_gh(monkeypatch)
        req.result = None
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError, match="could not read the required"):
            _prepare()

        assert head.disarm_calls == 0

    def test_pr_not_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        head, _req = install_red_gh(monkeypatch)
        head.state = "MERGED"
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError, match="MERGED, not OPEN"):
            _prepare()

        assert head.disarm_calls == 0

    def test_head_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        head, _req = install_red_gh(monkeypatch)
        head.reads = {0: None}
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError, match="could not read the PR head"):
            _prepare()

    def test_synthesis_error_never_disarms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head, _req = install_red_gh(monkeypatch, armed=True)
        _stub_synth(monkeypatch, error="boom")

        with pytest.raises(RequeueStateError, match="boom"):
            _prepare()

        assert head.disarm_calls == 0
        assert head.armed is True

    def test_feedback_head_must_equal_the_live_head(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head, _req = install_red_gh(monkeypatch, armed=True)
        _stub_synth(monkeypatch, make_repair_feedback(reviewed_sha="c" * 40))

        with pytest.raises(RequeueStateError, match="head moved"):
            _prepare()

        assert head.disarm_calls == 0

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"status": "pending"}, "not COMPLETED"),
            ({"disposition": "merge_pending"}, "not 'shipped'"),
            ({"pr_url": "nope"}, "parseable"),
        ],
    )
    def test_row_refusals(
        self,
        monkeypatch: pytest.MonkeyPatch,
        overrides: dict[str, object],
        fragment: str,
    ) -> None:
        head, _req = install_red_gh(monkeypatch)
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError, match=fragment):
            _prepare(**overrides)

        assert head.log == []

    def test_not_completed_message_is_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_red_gh(monkeypatch)
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError) as exc:
            _prepare(status="pending")

        assert str(exc.value) == (
            "row is not COMPLETED (status: pending); recover-red-pr only applies"
            " to a shipped PR"
        )

    def test_review_stage_absent_from_the_lane_pipeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_lane_client(
            ACME, tmp_path / "ws", client_stages=["plan", "impl", "finalize"]
        )
        install_red_gh(monkeypatch)
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError, match="no review stage"):
            _prepare()

    def test_impl_absent_from_the_lane_pipeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_lane_client(ACME, tmp_path / "ws", client_stages=["plan", "review"])
        install_red_gh(monkeypatch)
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError, match="no impl stage"):
            _prepare()

    def test_non_claude_native_impl_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_red_gh(monkeypatch)
        _stub_synth(monkeypatch)
        monkeypatch.setattr(
            "cw.shipped_pr_recovery.resolve_executor_config",
            lambda *_a, **_k: type("C", (), {"backend": "opencode"})(),
        )

        with pytest.raises(RequeueStateError, match="#2983"):
            _prepare()


class TestDisarm:
    @pytest.mark.parametrize("mode", [DISARM_NONE, DISARM_ZERO_STILL_ARMED])
    def test_unverifiable_disarm_refuses_with_the_manual_command(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        install_red_gh(monkeypatch, armed=True, disarm=mode)
        _stub_synth(monkeypatch)

        with pytest.raises(RequeueStateError) as exc:
            _prepare()

        assert str(exc.value) == (
            f"could not verify that auto-merge is disarmed on {ADOPT_PR_URL}; the"
            " row is unchanged. Run gh pr merge 7 --disable-auto and retry"
        )

    def test_synthesis_runs_before_the_disarm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head, _req = install_red_gh(monkeypatch, armed=True)
        order: list[str] = []
        real = make_repair_feedback(reviewed_sha=HEAD_SHA)

        def _synth(repo: str, pr_number: int, *, cwd: Path) -> ReviewFeedback:
            order.append(f"synth after {head.disarm_calls} disarms")
            return real

        monkeypatch.setattr("cw.shipped_pr_recovery.synthesize_pr_feedback", _synth)

        _prepare()

        assert order == ["synth after 0 disarms"]
