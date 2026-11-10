"""``--override-must-fix`` releases a ``review_confirm_must_fix`` park (#3042).

The full release sequence over a ledger parked by the confirm round after a
repo review engine pass: the override reads the parked round's saved verdict
(never ``.claude/review-verdict.json``), converges the ledger through
``override_confirm_round``, stamps the row and records the audit events; a
``requeue --stage finalize`` follows. Every refusal writes nothing, and the
codex park keeps its behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.claude_review import ClaudeRunResult
from cw.codex_clean_confirm import apply_clean_confirm
from cw.codex_review import synthesize_codex_review_result
from cw.dev_queue import approve_must_fix_override_ticket, requeue_ticket
from cw.dev_queue.review_attestation import _overrides_from_row
from cw.dev_queue.storage import load_dev_queue, save_dev_queue
from cw.exceptions import ApproveGateError
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.models.client import ReviewConfig
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    evaluate_tip,
    load_ledger,
    load_round_verdict,
    round_verdict_path,
)
from cw.review_debt import fingerprint_v1, normalize_summary
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_findings import REVIEW_VERDICT_JSON_RELATIVE_PATH
from tests._claude_review_helpers import RoleAwareClaudeRunner, review_envelope
from tests._clients_yaml import write_clients_yaml
from tests._codex_review_helpers import _doc_json, _finding_payload
from tests._review_convergence_helpers import (
    engine_confirm_verdict,
    make_review_client,
    parked_confirm_ledger,
    write_ledger,
)
from tests._review_debt_helpers import _events_of
from tests.conftest import (
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
    _make_ticket_task,
    git_in,
    write_review_verdict_envelope,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger
    from cw.review_findings import Finding, ReviewVerdict

_TICKET = "T-3042"
_CLIENT = "acme"
_SHA = "c" * 40
_FILE = "src/thing.py"
_UNANCHORED = "Operator must rotate the deploy key"
_REGRESS = f"cw dev-queue requeue {_TICKET} -c {_CLIENT} --stage impl --regress"
_CONFIRM_PARK = "review_confirm_must_fix"


@pytest.fixture(autouse=True)
def _operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: "octocat")


def _client(tmp_path: Path, *, convergence: str = "clean") -> None:
    client = make_review_client(
        tmp_path / "workspace", convergence=convergence, name=_CLIENT
    )
    write_clients_yaml(client)


def _seed(
    tmp_path: Path, *, blocked_reason: str = _CONFIRM_PARK, clean: bool = True
) -> Path:
    _client(tmp_path, convergence="clean" if clean else "blocking")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    task = _make_ticket_task(
        ticket_id=_TICKET,
        client=_CLIENT,
        status=QueueItemStatus.BLOCKED_ON_USER,
        stage=Stage.REVIEW,
        blocked_reason=blocked_reason,
        worktree_path=worktree,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    return worktree


def _unanchored(verdict: ReviewVerdict, summary: str) -> ReviewVerdict:
    """*verdict* with the finding *summary* moved to ``file="N/A"``."""

    def _move(finding: Finding) -> Finding:
        if finding.summary != summary:
            return finding
        return finding.model_copy(update={"file": "N/A"})

    return verdict.model_copy(
        update={
            "accepted": [
                af.model_copy(update={"finding": _move(af.finding)})
                for af in verdict.accepted
            ],
            "must_fix": [_move(f) for f in verdict.must_fix],
        }
    )


def _park_verdict() -> ReviewVerdict:
    verdict = engine_confirm_verdict(
        must_fix=["Real bug", _UNANCHORED], sha=_SHA, file=_FILE
    )
    return _unanchored(verdict, _UNANCHORED)


def _row() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == _TICKET)


def _ledger(worktree: Path) -> ConvergenceLedger:
    ledger = load_ledger(worktree / LEDGER_RELATIVE_PATH)
    assert ledger is not None
    return ledger


class TestRelease:
    def test_the_full_release_sequence(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path)
        parked = parked_confirm_ledger(worktree, _park_verdict(), ticket_id=_TICKET)
        assert not (worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH).exists()

        result = approve_must_fix_override_ticket(_TICKET, _CLIENT, "accepted risk")

        assert result["reviewed_sha"] == _SHA
        assert result["ledger_overridden_round"] == parked.rounds[-1].round
        assert result["finding_ids"] == sorted(
            [
                (_FILE, normalize_summary("Real bug")),
                ("N/A", normalize_summary(_UNANCHORED)),
            ]
        )
        ledger = _ledger(worktree)
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == _SHA
        (override,) = ledger.confirm_overrides
        assert override.prior_stop_reason == "confirm_must_fix"
        row = _row()
        assert row.must_fix_override is not None
        assert row.must_fix_override.reviewed_sha == _SHA
        assert row.review_approver_kind == "operator"
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        (approved,) = _events_of(OrchestratorEventType.TICKET_APPROVED)
        assert "ledger_override" in approved
        assert len(_events_of(OrchestratorEventType.REVIEW_FINDING_SETTLED)) == 2
        assert not (worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH).exists()

        requeue_ticket(_TICKET, _CLIENT, Stage.FINALIZE.value)

        row = _row()
        assert row.stage == Stage.FINALIZE
        assert row.must_fix_override is not None
        tip = evaluate_tip(_ledger(worktree), head_sha=_SHA, origin_tip_sha=_SHA)
        assert tip.decision == "accepted"
        assert [o.kind for o in _overrides_from_row(row)] == ["must_fix_override"]

    def test_a_re_run_is_idempotent(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path)
        parked_confirm_ledger(worktree, _park_verdict(), ticket_id=_TICKET)
        approve_must_fix_override_ticket(_TICKET, _CLIENT, "first")
        again = approve_must_fix_override_ticket(_TICKET, _CLIENT, "second")
        assert "ledger_overridden_round" not in again
        assert len(_ledger(worktree).confirm_overrides) == 1


def _assert_refused(
    worktree: Path | None, match: str, *, names_regress: bool = False
) -> None:
    ledger_path = None if worktree is None else worktree / LEDGER_RELATIVE_PATH
    before = (
        ledger_path.read_bytes()
        if ledger_path is not None and ledger_path.exists()
        else None
    )
    with pytest.raises(ApproveGateError, match=match) as caught:
        approve_must_fix_override_ticket(_TICKET, _CLIENT, "why")
    if names_regress:
        assert _REGRESS in str(caught.value)
    assert _row().must_fix_override is None
    after = (
        ledger_path.read_bytes()
        if ledger_path is not None and ledger_path.exists()
        else None
    )
    assert after == before
    assert _events_of(OrchestratorEventType.TICKET_APPROVED) == []


class TestRefusals:
    def test_a_blank_reason(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path)
        parked_confirm_ledger(worktree, _park_verdict(), ticket_id=_TICKET)
        with pytest.raises(ApproveGateError, match="--reason is blank"):
            approve_must_fix_override_ticket(_TICKET, _CLIENT, "   ")
        assert _row().must_fix_override is None

    def test_a_row_parked_for_another_reason(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path, blocked_reason="plan_scope_drift")
        parked_confirm_ledger(worktree, _park_verdict(), ticket_id=_TICKET)
        _assert_refused(worktree, "review_confirm_must_fix")

    def test_a_non_clean_client(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path, clean=False)
        parked_confirm_ledger(worktree, _park_verdict(), ticket_id=_TICKET)
        _assert_refused(
            worktree, "not on `review.convergence: clean`", names_regress=True
        )

    def test_a_missing_ledger(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path)
        _assert_refused(worktree, "no convergence ledger exists")

    def test_a_foreign_ledger(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path)
        parked_confirm_ledger(worktree, _park_verdict(), ticket_id="T-other")
        _assert_refused(worktree, "belongs to ticket 'T-other'")

    def test_a_ledger_converged_at_another_commit(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path)
        ledger = parked_confirm_ledger(worktree, _park_verdict(), ticket_id=_TICKET)
        write_ledger(
            worktree,
            ledger.model_copy(
                update={"status": "converged", "last_clean_sha": "d" * 40}
            ),
        )
        _assert_refused(worktree, "converged at", names_regress=True)

    def test_a_non_stopped_ledger_the_override_cannot_converge(
        self, tmp_path: Path
    ) -> None:
        worktree = _seed(tmp_path)
        ledger = parked_confirm_ledger(worktree, _park_verdict(), ticket_id=_TICKET)
        write_ledger(worktree, ledger.model_copy(update={"status": "awaiting_confirm"}))
        _assert_refused(worktree, "not converged")

    def test_a_ledger_whose_last_round_is_not_a_confirm(self, tmp_path: Path) -> None:
        from tests._review_convergence_helpers import engine_pass_ledger, fresh_ledger

        worktree = _seed(tmp_path)
        write_ledger(
            worktree,
            engine_pass_ledger(fresh_ledger(confirm="codex", ticket_id=_TICKET)),
        )
        _assert_refused(worktree, "no confirm round last", names_regress=True)

    def test_a_missing_round_verdict(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path)
        ledger = parked_confirm_ledger(worktree, _park_verdict(), ticket_id=_TICKET)
        round_verdict_path(
            worktree / LEDGER_RELATIVE_PATH, ledger.rounds[-1].round
        ).unlink()
        _assert_refused(
            worktree, "could not read the saved verdict", names_regress=True
        )

    def test_a_park_with_no_must_fix(self, tmp_path: Path) -> None:
        worktree = _seed(tmp_path)
        verdict = engine_confirm_verdict(
            should_fix=[("Untested", "coverage.patch")], sha=_SHA, file=_FILE
        )
        parked_confirm_ledger(worktree, verdict, ticket_id=_TICKET)
        _assert_refused(worktree, "nothing to override", names_regress=True)

    @pytest.mark.parametrize("rule", ["coverage.patch", "acceptance.criteria"])
    def test_a_never_deferrable_must_fix(self, tmp_path: Path, rule: str) -> None:
        worktree = _seed(tmp_path)
        verdict = engine_confirm_verdict(
            must_fix=[("Untested", rule)], sha=_SHA, file=_FILE
        )
        parked_confirm_ledger(worktree, verdict, ticket_id=_TICKET)
        _assert_refused(worktree, "never-deferrable", names_regress=True)

    @pytest.mark.parametrize("rule", ["coverage.patch", "acceptance.criteria"])
    def test_a_deferrable_must_fix_beside_a_never_deferrable_should_fix(
        self, tmp_path: Path, rule: str
    ) -> None:
        worktree = _seed(tmp_path)
        verdict = engine_confirm_verdict(
            must_fix=["Real bug"], should_fix=[("Untested", rule)], sha=_SHA, file=_FILE
        )
        parked_confirm_ledger(worktree, verdict, ticket_id=_TICKET)
        _assert_refused(worktree, "never-deferrable", names_regress=True)


class TestCodexParkUnchanged:
    def _codex_park(self, tmp_path: Path, file: str) -> None:
        worktree = _seed(
            tmp_path, blocked_reason="codex_must_fix_findings", clean=False
        )
        write_review_verdict_envelope(
            worktree,
            ticket_id=_TICKET,
            reviewed_sha=_SHA,
            must_fix=[_make_finding(file=file, summary="Bug  here")],
        )

    def test_a_codex_park_still_binds_its_fingerprints(self, tmp_path: Path) -> None:
        self._codex_park(tmp_path, "src/a.py")
        result = approve_must_fix_override_ticket(_TICKET, _CLIENT, "ship it")
        assert result["finding_ids"] == [fingerprint_v1("src/a.py", "Bug  here")]
        assert "ledger_overridden_round" not in result

    def test_a_codex_park_still_fails_closed_on_an_unanchored_finding(
        self, tmp_path: Path
    ) -> None:
        self._codex_park(tmp_path, "N/A")
        _assert_refused(None, "has no diff anchor")


class TestCodexLaneEngineConfirmPark:
    """A codex-lane ``review_confirm_must_fix`` park (#3216, S6).

    The park is produced by the real codex gate (``apply_clean_confirm`` over an
    engine pass and a Claude confirm), never a hand-written ledger, so the
    saved round verdict is exactly what the codex lane persists: the decided,
    park-stamped verdict.
    """

    @pytest.fixture(autouse=True)
    def _claude_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: "/x/claude")

    @staticmethod
    def _park(
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        entries: Sequence[tuple[str, str, str | None]],
    ) -> tuple[Path, ReviewVerdict]:
        """Run the codex engine confirm over *entries*; return the repo and verdict.

        *entries* are ``(severity, summary, rule_id)``. The row is parked
        ``review_confirm_must_fix`` on that repo, as dispatch would leave it.
        """
        worktree, _origin = make_git_repo_with_origin("codex-engine-park")
        client = ClientConfig(
            name=_CLIENT,
            workspace_path=worktree,
            default_branch="main",
            review=ReviewConfig.model_validate({"convergence": "clean"}),
        )
        write_clients_yaml(client)
        task = _make_ticket_task(
            ticket_id=_TICKET,
            client=_CLIENT,
            status=QueueItemStatus.BLOCKED_ON_USER,
            stage=Stage.REVIEW,
            blocked_reason=_CONFIRM_PARK,
            worktree_path=worktree,
            scope_hint="small",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        head = git_in(worktree, "rev-parse", "HEAD")
        result, verdict = synthesize_codex_review_result(
            task=task,
            worktree=worktree,
            documents=[_make_reviewer_doc(reviewer_role=ENGINE_ROLE)],
            failures=[],
            diff=_make_diff("x = 1", files={"feature.py": [1]}),
            reviewed_sha=head,
            session_id="s-codex",
            default_branch="main",
            fix_loop_enabled=True,
        )
        assert verdict is not None

        def _confirm(role: str) -> ClaudeRunResult:
            findings = []
            for severity, summary, rule_id in entries:
                finding = _finding_payload(
                    severity=severity,
                    file="feature.py",
                    line_start=1,
                    line_end=1,
                    summary=summary,
                    evidence="x = 1",
                )
                if rule_id is not None:
                    finding["rule_id"] = rule_id
                findings.append(finding)
            return ClaudeRunResult(
                0, review_envelope(_doc_json(role=role, findings=findings)), ""
            )

        outcome = apply_clean_confirm(
            task=task,
            worktree=worktree,
            client=client,
            sid="sid-1",
            runner=RoleAwareClaudeRunner(_confirm),
            deadline=None,
            result=result,
            verdict=verdict,
        )
        assert outcome.result.blocker is not None
        assert outcome.result.blocker.reason == _CONFIRM_PARK
        saved = load_round_verdict(worktree / LEDGER_RELATIVE_PATH, 2)
        assert saved is not None
        return worktree, saved

    def test_a_plain_must_fix_park_is_released_by_the_override(
        self, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
    ) -> None:
        worktree, saved = self._park(
            make_git_repo_with_origin, [("MUST_FIX", "Real bug", None)]
        )
        assert {af.disposition for af in saved.accepted} == {"fixed"}
        assert _ledger(worktree).status == "stopped"

        result = approve_must_fix_override_ticket(_TICKET, _CLIENT, "accepted risk")

        head = git_in(worktree, "rev-parse", "HEAD")
        assert result["reviewed_sha"] == head
        assert result["ledger_overridden_round"] == 2
        assert result["finding_ids"] == [("feature.py", normalize_summary("Real bug"))]
        ledger = _ledger(worktree)
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == head
        (override,) = ledger.confirm_overrides
        assert override.prior_stop_reason == "confirm_must_fix"
        row = _row()
        assert row.must_fix_override is not None
        assert row.must_fix_override.reviewed_sha == head
        assert not (worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH).exists()

    @pytest.mark.parametrize("rule", ["coverage.patch", "acceptance.criteria"])
    def test_a_never_deferrable_should_fix_in_the_park_is_refused(
        self,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        rule: str,
    ) -> None:
        worktree, saved = self._park(
            make_git_repo_with_origin,
            [("MUST_FIX", "Real bug", None), ("SHOULD_FIX", "Untested", rule)],
        )
        assert "operator_actionable" in {af.disposition for af in saved.accepted}

        _assert_refused(worktree, "never-deferrable", names_regress=True)

    @pytest.mark.parametrize("rule", ["coverage.patch", "acceptance.criteria"])
    def test_a_never_deferrable_only_park_has_nothing_to_override(
        self,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        rule: str,
    ) -> None:
        worktree, _saved = self._park(
            make_git_repo_with_origin, [("SHOULD_FIX", "Untested", rule)]
        )

        _assert_refused(worktree, "nothing to override", names_regress=True)
