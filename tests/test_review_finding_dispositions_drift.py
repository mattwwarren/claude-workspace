"""Tests for ``cw.review_finding_dispositions.drift`` (GitHub #2232, #2498).

``disposition_drifted`` and the drift surfacing it drives through
``suppress_adjudicated_findings``. Relocated verbatim from the flat
``tests/test_review_finding_dispositions.py`` when the module became a package
(#2498), 1:1 with the ``drift`` submodule per the CLAUDE.md Testing convention.
Reuses ``tests/conftest.py``'s ``_make_finding`` fixture rather than
re-declaring an equivalent; ``_accepted``/``_verdict`` are declared file-local
here for the same reason ``tests/test_review_adjudication.py`` declares its own
— they are thin, module-specific construction helpers, not generically
reusable builders.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result import Review
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
    disposition_drifted,
    disposition_event_payload,
    suppress_adjudicated_findings,
)
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

from .conftest import _make_finding, commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_LOGGER = "cw.review_finding_dispositions"
_TICKET = "T-1838"
#: The gh login ``cw review settle`` would record as having settled a finding.
_OPERATOR = "mattwwarren"


def _accepted(finding: Finding, **overrides: object) -> AcceptedFinding:
    """An AcceptedFinding at its post-consolidate default disposition."""
    kwargs: dict[str, object] = {"finding": finding, "reviewers": ["Test Reviewer"]}
    kwargs.update(overrides)
    return AcceptedFinding.model_validate(kwargs)


def _verdict(*accepted: AcceptedFinding, **overrides: object) -> ReviewVerdict:
    """A ReviewVerdict shaped the way ``consolidate_verdict`` builds one."""
    must_fix = [af.finding for af in accepted if af.finding.severity == "MUST_FIX"]
    review = Review(
        must_fix_initial=len(must_fix),
        should_fix=sum(1 for af in accepted if af.finding.severity == "SHOULD_FIX"),
        fix_cycles_used=0,
        deferred=0,
        agents_run=len(accepted) or 1,
    )
    kwargs: dict[str, object] = {
        "blocking": bool(must_fix),
        "must_fix": must_fix,
        "reviewed_sha": "abc1234",
        "accepted": list(accepted),
        "review": review,
    }
    kwargs.update(overrides)
    return ReviewVerdict.model_validate(kwargs)


def _entry(**overrides: object) -> FindingDisposition:
    """A FindingDisposition shaped the way ``cw review settle`` mints one.

    Carries the full provenance set by default (#2210 round 2): the reader
    applies a record only when it can say which finding, who settled it, when,
    against what code, and why — so a fixture missing any of those would
    silently stop testing suppression at all.
    """
    kwargs: dict[str, object] = {
        "outcome": "REJECTED",
        "rationale": "intentional tradeoff, settled in round 1",
        "recorded_at": "2026-08-16T00:00:00Z",
        "actor": _OPERATOR,
        "reviewed_sha": "abc1234",
        "summary": "Bug here",
    }
    kwargs.update(overrides)
    return FindingDisposition.model_validate(kwargs)


def _ledger_for(
    file: str, summary: str, /, **overrides: object
) -> dict[str, FindingDisposition]:
    """A single-entry ledger keyed on ``(file, summary)`` (#2210).

    Module-scope so the claim/contest/gate classes added by #2210 can build
    multi-entry ledgers as dict unions of it rather than each growing a
    near-identical class-local builder.

    Both parameters are positional-only so ``**overrides`` can still carry a
    ``summary=`` of its own — the entry's VERBATIM summary is a provenance
    field a test may want to blank independently of the key it is filed under.
    """
    key = _disposition_key(file, summary)
    assert key is not None
    overrides.setdefault("summary", summary)
    return {key: _entry(**overrides)}


def _ledger(finding: Finding, **overrides: object) -> dict[str, FindingDisposition]:
    """A single-entry ledger keyed on *finding*'s own identity."""
    return _ledger_for(finding.file, finding.summary, **overrides)


# ---------------------------------------------------------------------------
# disposition_drifted / drift surfacing (#2232)
# ---------------------------------------------------------------------------


class TestDispositionDrifted:
    """#2232: has the code a settle was granted against moved since?

    The predicate the ledger's "surface, don't silently suppress" behaviour
    rests on. It fails toward SURFACING — an unanswerable question reads as
    drift — matching this module's own "a contest fails toward blocking"
    posture: the safe direction is a finding that keeps blocking and can be
    re-settled, never a suppression nobody can account for.
    """

    def test_unchanged_file_across_two_commits_is_not_drift(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = make_git_repo("wt-2232-same")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        first = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/other.py", "b = 2\n")
        second = git_in(worktree, "rev-parse", "HEAD")
        assert first != second
        assert disposition_drifted(worktree, first, second, "src/cw/foo.py") is False

    def test_changed_file_across_two_commits_is_drift(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = make_git_repo("wt-2232-changed")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        first = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 2  # reworked\n")
        second = git_in(worktree, "rev-parse", "HEAD")
        assert disposition_drifted(worktree, first, second, "src/cw/foo.py") is True

    def test_identical_shas_short_circuit_without_running_git(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = make_git_repo("wt-2232-samesha")

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "equal shas must not shell out to git"
            raise AssertionError(msg)

        monkeypatch.setattr("cw._git.run_git", _boom)
        assert disposition_drifted(worktree, "abc1234", "abc1234", "f.py") is False

    @pytest.mark.parametrize(
        ("entry_sha", "current_sha"),
        [("", "abc1234"), ("abc1234", ""), ("", "")],
    )
    def test_a_blank_sha_is_not_drift(
        self,
        make_git_repo: Callable[..., Path],
        entry_sha: str,
        current_sha: str,
    ) -> None:
        """A pre-#2210 record carries no reviewed sha; that is not drift.

        There is nothing to compare against, so the conservative direction is
        the one that preserves the suppression the operator already recorded
        rather than manufacturing drift out of a missing field.
        """
        worktree = make_git_repo(f"wt-2232-blank-{entry_sha}-{current_sha}")
        assert disposition_drifted(worktree, entry_sha, current_sha, "f.py") is False

    def test_no_worktree_is_the_inert_default(self) -> None:
        """``worktree=None`` is the fail-safe floor every unthreaded caller gets."""
        assert disposition_drifted(None, "abc1234", "def5678", "f.py") is False

    def test_an_unresolvable_sha_fails_toward_surfacing(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = make_git_repo("wt-2232-badsha")
        assert (
            disposition_drifted(worktree, "0" * 40, "1" * 40, "src/cw/foo.py") is True
        )

    def test_a_failed_git_invocation_fails_toward_surfacing(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError (no git on PATH, vanished worktree) reads as drift too."""
        worktree = make_git_repo("wt-2232-oserror")

        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "git is gone"
            raise OSError(msg)

        monkeypatch.setattr("cw._git.run_git", _raise)
        assert disposition_drifted(worktree, "aaa", "bbb", "src/cw/foo.py") is True

    def test_an_inherited_git_dir_cannot_redirect_the_diff(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#2232 MUST_FIX 1: the hook hazard, on the drift path.

        Under a hook-set ``GIT_DIR`` an unsanitized ``git diff`` runs against
        the HOOK's repository, which cannot resolve either of these shas — so
        it exits 128 and the record reads as drifted. The answer would be
        wrong in the *safe* direction, but it would be wrong about a different
        repository entirely, and that is what decides whether a settled
        finding stays suppressed.
        """
        worktree = make_git_repo("wt-2232-hostile-gitdir")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        first = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/other.py", "b = 2\n")
        second = git_in(worktree, "rev-parse", "HEAD")
        decoy = make_git_repo("wt-2232-hostile-decoy")
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

        assert disposition_drifted(worktree, first, second, "src/cw/foo.py") is False


class TestSuppressAdjudicatedFindingsDriftSurfacing:
    """#2232: a settle whose code moved re-raises instead of suppressing.

    ADR-0016 named the gap: the exact tier's identity is deliberately NOT
    evidence-anchored, so a suppression outlives the code it was granted for.
    This does not expire the ledger entry — it stops applying it for this pass
    and says so, on the verdict and in the event log.
    """

    def _drifted(
        self, make_git_repo: Callable[..., Path], name: str
    ) -> tuple[Path, Finding, dict[str, FindingDisposition], str]:
        worktree = make_git_repo(name)
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        first = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 2  # reworked\n")
        second = git_in(worktree, "rev-parse", "HEAD")
        finding = _make_finding(severity="MUST_FIX", file="src/cw/foo.py")
        ledger = _ledger(finding, reviewed_sha=first)
        return worktree, finding, ledger, second

    def test_a_drifted_match_is_not_suppressed(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree, finding, ledger, head = self._drifted(
            make_git_repo, "wt-2232-notsuppressed"
        )
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            reviewed_sha=head,
            worktree=worktree,
        )

        assert result.blocking is True
        assert [f.summary for f in result.must_fix] == [finding.summary]
        assert result.accepted[0].disposition == "fixed"

    def test_the_drifted_record_is_reported_on_the_verdict(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree, finding, ledger, head = self._drifted(
            make_git_repo, "wt-2232-reported"
        )
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            reviewed_sha=head,
            worktree=worktree,
        )

        ((key, entry),) = ledger.items()
        assert [r.key for r in result.stale_dispositions] == [key]
        assert result.stale_dispositions[0].reviewed_sha == entry.reviewed_sha
        assert result.stale_dispositions[0].current_sha == head

    def test_the_drift_emits_a_stale_event(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree, finding, ledger, head = self._drifted(make_git_repo, "wt-2232-event")
        suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            reviewed_sha=head,
            worktree=worktree,
        )

        events = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_STALE]
        )
        assert len(events) == 1
        assert events[0].correlation_id == _TICKET
        ((key, entry),) = ledger.items()
        assert events[0].payload["key"] == key
        assert events[0].payload["file"] == finding.file
        assert events[0].payload["summary"] == finding.summary
        assert events[0].payload["reviewed_sha"] == entry.reviewed_sha
        assert events[0].payload["current_sha"] == head
        # #2232 MUST_FIX 2: consumer parity with the settle/revert events.
        # Someone triaging a drifted suppression is asking WHO silenced this
        # finding and WHY; two shas cannot answer either.
        assert events[0].payload["outcome"] == entry.outcome
        assert events[0].payload["reason"] == entry.rationale
        assert events[0].payload["actor"] == entry.actor
        assert events[0].payload["recorded_at"] == entry.recorded_at
        assert set(events[0].payload) == {
            *disposition_event_payload(key, entry),
            "current_sha",
        }
        # Nothing was suppressed, so no suppression event fired for it.
        assert (
            read_events(
                event_types=[
                    OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED
                ]
            )
            == []
        )

    def test_an_undrifted_match_still_suppresses_with_a_worktree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = make_git_repo("wt-2232-undrifted")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        first = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/other.py", "b = 2\n")
        second = git_in(worktree, "rev-parse", "HEAD")
        finding = _make_finding(severity="MUST_FIX", file="src/cw/foo.py")
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            _ledger(finding, reviewed_sha=first),
            ticket_id=_TICKET,
            reviewed_sha=second,
            worktree=worktree,
        )

        assert result.blocking is False
        assert result.stale_dispositions == []
        assert (
            read_events(
                event_types=[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_STALE]
            )
            == []
        )

    def test_the_default_worktree_is_inert(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """A caller that threads no worktree suppresses exactly as before.

        The regression guard for every pre-#2232 call path: the new parameter
        must not change one byte of behaviour when it is not supplied.
        """
        _worktree, finding, ledger, head = self._drifted(make_git_repo, "wt-2232-inert")
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            reviewed_sha=head,
        )

        assert result.blocking is False
        assert result.stale_dispositions == []

    def test_the_gate_off_suppresses_a_drifted_match(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#2232: the gate, not merely the worktree, controls the drift check."""
        worktree, finding, ledger, head = self._drifted(make_git_repo, "wt-2232-gated")
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            reviewed_sha=head,
            worktree=worktree,
            disposition_drift_check_enabled=False,
        )

        assert result.blocking is False
        assert result.stale_dispositions == []
        assert (
            read_events(
                event_types=[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_STALE]
            )
            == []
        )

    def test_a_stale_event_write_failure_does_not_abort_the_pass(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Advisory record: NOT suppressing is already safe without the event."""
        worktree, finding, ledger, head = self._drifted(
            make_git_repo, "wt-2232-emitfail"
        )

        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "event inbox is read-only"
            raise OSError(msg)

        monkeypatch.setattr("cw.events.record_event", _raise)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = suppress_adjudicated_findings(
                _verdict(_accepted(finding)),
                ledger,
                ticket_id=_TICKET,
                reviewed_sha=head,
                worktree=worktree,
            )

        assert result.blocking is True
        assert [r.key for r in result.stale_dispositions] == list(ledger)
        assert any(_TICKET in record.getMessage() for record in caplog.records)
