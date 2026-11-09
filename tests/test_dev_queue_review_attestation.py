"""The git-free attestation builder (#3040, RFC 0017 II1).

Every test runs with git and subprocesses wired to fail, so any git use in
the builder fails the test.
"""

from __future__ import annotations

import ast
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

from cw.dev_queue import review_attestation as builder_mod
from cw.dev_queue.review_attestation import (
    attest_review_exit,
    record_review_attestation,
)
from cw.models import MustFixOverride, OrchestratorEventType
from cw.models.review_attestation import (
    MAX_ATTESTATION_BYTES,
    MAX_ATTESTATION_FINDINGS,
    AttestationOverride,
    ReviewAttestation,
)
from cw.models.review_engine_summary import ReviewEngineSummary
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    FindingIdentity,
    SettledEntry,
)
from cw.review_convergence._models import FixedEntry
from cw.review_finding_dispositions import FindingDisposition
from tests._review_attestation_helpers import (
    LOGGER,
    OTHER_SHA,
    WRITTEN_AT,
    NoSubprocessError,
    attestation_for,
    clean_engine_summary,
    clean_sentinel,
    converged_worktree_ledger,
    engine_client,
    forbidden_call,
    install_no_git_guard,
    review_row,
    with_review_backend,
)
from tests._review_convergence_helpers import (
    confirmed_ledger,
    fresh_ledger,
    make_review_client,
    write_ledger,
)
from tests._review_engine_helpers import make_summary
from tests._ticket_view_helpers import events_of

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger

SHA = "a" * 40
OTHER = OTHER_SHA
NOW = WRITTEN_AT
RECORDED = OrchestratorEventType.REVIEW_ATTESTATION_RECORDED
REJECTED = OrchestratorEventType.REVIEW_ATTESTATION_REJECTED
_boom = forbidden_call
_NoSubprocessError = NoSubprocessError
_summary = clean_engine_summary
_engine_client = engine_client


@pytest.fixture(autouse=True)
def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    install_no_git_guard(monkeypatch)


def _ledger(sha: str = SHA, ticket_id: str = "T-1") -> ConvergenceLedger:
    return confirmed_ledger(sha=sha, ticket_id=ticket_id)


def _clean_client(tmp_path: Path) -> ClientConfig:
    return make_review_client(tmp_path)


def _blocking_client(tmp_path: Path) -> ClientConfig:
    return make_review_client(tmp_path, convergence="blocking")


def _backend_client(tmp_path: Path, backend: str) -> ClientConfig:
    return with_review_backend(_blocking_client(tmp_path), backend)


def _build(
    task: TicketTask,
    *,
    last_result: dict[str, object] | None = None,
    client: ClientConfig | None,
    ledger: ConvergenceLedger | None = None,
    engine_summary: ReviewEngineSummary | None = None,
    now: datetime | None = NOW,
    refresh_only: bool = False,
) -> ReviewAttestation | None:
    return record_review_attestation(
        task,
        last_result=clean_sentinel(SHA) if last_result is None else last_result,
        engine_summary=engine_summary,
        ledger=ledger,
        client=client,
        now=now,
        refresh_only=refresh_only,
    )


def _identity(summary: str, severity: str = "SHOULD_FIX") -> FindingIdentity:
    return FindingIdentity(
        severity=severity, file="src/a.py", summary_normalized=summary
    )


def _settled(
    summary: str,
    outcome: str,
    *,
    origin: str = "adjudication",
    actor: str | None = None,
    settled_at: str | None = None,
) -> SettledEntry:
    return SettledEntry.model_validate(
        {
            "identity": _identity(summary).model_dump(),
            "outcome": outcome,
            "rationale": f"why {summary}",
            "round": 1,
            "origin": origin,
            "actor": actor,
            "settled_at": settled_at,
        }
    )


class TestCleanExit:
    def test_engineless_clean_exit_writes_at_reviewed_sha(self, tmp_path: Path) -> None:
        task = review_row(None)
        att = _build(task, client=_clean_client(tmp_path), ledger=_ledger())
        assert att is not None
        assert task.review_attestation == att
        assert att.head_sha == SHA
        assert att.engine is None
        assert att.reviewer_backend == "claude-native"
        assert att.verdict == "PASS"
        assert att.counts.model_dump() == {
            "must_fix": 0,
            "should_fix": 0,
            "debt": 0,
            "nit": 0,
            "principle": 0,
        }
        assert att.written_at == NOW
        assert att.ticket_id == task.ticket_id
        assert att.client == task.client

    def test_head_is_the_sentinel_value_not_any_worktree_head(
        self, tmp_path: Path
    ) -> None:
        """No git is consulted: the sentinel's reviewed_sha is the head."""
        task = review_row(tmp_path)
        att = _build(task, client=_clean_client(tmp_path), ledger=_ledger())
        assert att is not None
        assert att.head_sha == SHA

    def test_now_defaults_to_the_clock(self, tmp_path: Path) -> None:
        task = review_row(None)
        att = _build(task, client=_blocking_client(tmp_path), now=None)
        assert att is not None
        assert att.written_at.tzinfo == UTC

    def test_review_pending_approval_is_success(self, tmp_path: Path) -> None:
        result = clean_sentinel(SHA, status="review_pending_approval")
        att = _build(
            review_row(None), last_result=result, client=_blocking_client(tmp_path)
        )
        assert att is not None

    @pytest.mark.parametrize("backend", ["codex", "opencode"])
    def test_other_supported_backends(self, tmp_path: Path, backend: str) -> None:
        att = _build(review_row(None), client=_backend_client(tmp_path, backend))
        assert att is not None
        assert att.reviewer_backend == backend


class TestNotClean:
    @pytest.mark.parametrize(
        ("result", "client_kind", "ledger_kind"),
        [
            (clean_sentinel(SHA, status="blocked"), "blocking", None),
            (
                clean_sentinel(SHA, recommendation="EXIT_FOR_HUMAN_REVIEW"),
                "blocking",
                None,
            ),
            (clean_sentinel(SHA, agents_run=0), "blocking", None),
            (clean_sentinel(None), "blocking", None),
            (clean_sentinel("a" * 39), "blocking", None),
            (clean_sentinel(SHA), "local", None),
            (clean_sentinel(SHA), "clean", None),
            (clean_sentinel(SHA), "clean", "foreign"),
            (clean_sentinel(SHA), "clean", "unconverged"),
            (clean_sentinel(SHA), "clean", "other_tip"),
        ],
    )
    def test_writes_nothing(
        self,
        tmp_path: Path,
        result: dict[str, object],
        client_kind: str,
        ledger_kind: str | None,
    ) -> None:
        clients = {
            "blocking": _blocking_client(tmp_path),
            "local": _backend_client(tmp_path, "local"),
            "clean": _clean_client(tmp_path),
        }
        ledgers: dict[str | None, ConvergenceLedger | None] = {
            None: None,
            "foreign": _ledger(ticket_id="T-other"),
            "unconverged": fresh_ledger(ticket_id="T-1"),
            "other_tip": _ledger(sha=OTHER),
        }
        task = review_row(None)
        att = _build(
            task,
            last_result=result,
            client=clients[client_kind],
            ledger=ledgers[ledger_kind],
        )
        assert att is None
        assert task.review_attestation is None
        assert events_of(RECORDED) == []

    def test_blocking_client_never_reads_a_stray_ledger(self, tmp_path: Path) -> None:
        task = review_row(tmp_path)
        write_ledger(tmp_path, _ledger(sha=OTHER))
        att = attest_review_exit(
            task, last_result=clean_sentinel(SHA), client=_blocking_client(tmp_path)
        )
        assert att is not None
        assert att.findings is None
        assert att.deferrals is None


class TestLedgerAgreement:
    def test_attest_review_exit_reads_ledger_at_derived_path(
        self, tmp_path: Path
    ) -> None:
        from tests._review_attestation_helpers import clean_client_wt

        client, worktree = clean_client_wt(tmp_path, "T-1")
        converged_worktree_ledger(worktree, SHA, "T-1")
        task = review_row(None)
        att = attest_review_exit(task, last_result=clean_sentinel(SHA), client=client)
        assert att is not None
        assert att.deferrals == []

    def test_disagreeing_ledger_writes_nothing(self, tmp_path: Path) -> None:
        from tests._review_attestation_helpers import clean_client_wt

        client, worktree = clean_client_wt(tmp_path, "T-1")
        converged_worktree_ledger(worktree, OTHER, "T-1")
        task = review_row(None)
        att = attest_review_exit(task, last_result=clean_sentinel(SHA), client=client)
        assert att is None

    def test_unreadable_ledger_is_absent(self, tmp_path: Path) -> None:
        path = tmp_path / LEDGER_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        task = review_row(tmp_path)
        att = attest_review_exit(
            task, last_result=clean_sentinel(SHA), client=_clean_client(tmp_path)
        )
        assert att is None

    def test_no_client_resolves_no_worktree(self) -> None:
        assert (
            attest_review_exit(
                review_row(None), last_result=clean_sentinel(SHA), client=None
            )
            is None
        )


class TestEngineSummary:
    def test_matching_summary_fills_engine_fields(self, tmp_path: Path) -> None:
        task = review_row(None)
        summary = _summary(
            counts={"must_fix": 0, "should_fix": 0, "debt": 0, "nit": 0, "principle": 0}
        )
        att = _build(
            task,
            client=_engine_client(tmp_path),
            ledger=_ledger(),
            engine_summary=summary,
        )
        assert att is not None
        assert att.engine == "code-review"
        assert att.risk == "LOW"
        assert [(a.id, a.status) for a in att.acceptance or []] == [
            ("AC-1", "NOT_APPLICABLE"),
            ("AC-2", "PASS"),
        ]
        assert [(g.name, g.status) for g in att.gate_signals or []] == [
            ("tests", "PASS")
        ]
        assert att.counts.model_dump() == summary.counts.model_dump()
        assert att.found_during_review is None

    def test_counts_copied_verbatim_from_summary(self, tmp_path: Path) -> None:
        from tests._review_engine_helpers import make_finding

        raw = make_summary([make_finding()], head_sha=SHA)
        summary = ReviewEngineSummary.model_validate(raw)
        att = _build(
            review_row(None),
            client=_engine_client(tmp_path),
            ledger=_ledger(),
            engine_summary=summary,
        )
        assert att is not None
        assert att.counts.nit == 1
        assert (att.counts.must_fix, att.counts.should_fix) == (0, 0)

    @pytest.mark.parametrize(
        "overrides",
        [{"head_sha": OTHER}, {"coverage": "PARTIAL"}, {"verdict": "FAIL"}],
    )
    def test_disagreeing_summary_writes_nothing(
        self, tmp_path: Path, overrides: dict[str, object]
    ) -> None:
        att = _build(
            review_row(None),
            client=_engine_client(tmp_path),
            ledger=_ledger(),
            engine_summary=_summary(**overrides),
        )
        assert att is None

    def test_summary_without_engine_config_writes_nothing(self, tmp_path: Path) -> None:
        att = _build(
            review_row(None),
            client=_clean_client(tmp_path),
            ledger=_ledger(),
            engine_summary=_summary(),
        )
        assert att is None


class TestModes:
    def test_blocking_mode_reports_nothing_even_with_a_sentinel_deferral(
        self, tmp_path: Path
    ) -> None:
        result = clean_sentinel(SHA)
        review = result["review"]
        assert isinstance(review, dict)
        review["deferred"] = 3
        att = _build(
            review_row(None), last_result=result, client=_blocking_client(tmp_path)
        )
        assert att is not None
        assert att.findings is None
        assert att.deferrals is None

    def test_clean_mode_digest_deferrals_and_overrides(self, tmp_path: Path) -> None:
        ledger = _ledger()
        ledger = ledger.model_copy(
            update={
                "settled": [
                    _settled("zeta", "defer"),
                    _settled(
                        "alpha",
                        "reject",
                        origin="operator",
                        actor="op",
                        settled_at="2026-11-08T10:00:00Z",
                    ),
                    _settled("beta", "defer", origin="operator", settled_at="bogus"),
                ],
                "fixed": [
                    FixedEntry(
                        identity=_identity("gamma", "MUST_FIX"),
                        round=1,
                        reviewed_sha=SHA,
                    )
                ],
            }
        )
        task = review_row(None)
        task.must_fix_override = MustFixOverride(
            actor="octo",
            reason="ship",
            reviewed_sha=SHA,
            finding_ids=[("src/a.py", "x")],
            recorded_at=datetime(2026, 11, 8, 9, 0, tzinfo=UTC),
        )
        task.finding_dispositions = {
            "k1": FindingDisposition(
                outcome="REJECTED",
                rationale="nope",
                actor="a1",
                recorded_at="2026-11-08T08:00:00",
            ),
            "k2": FindingDisposition(outcome="REVERSED", rationale="undo"),
        }
        att = _build(task, client=_clean_client(tmp_path), ledger=ledger)
        assert att is not None
        assert att.counts.must_fix == 0
        deferrals = att.deferrals or []
        assert [d.identity for d in deferrals] == [
            "SHOULD_FIX::src/a.py::beta",
            "SHOULD_FIX::src/a.py::zeta",
        ]
        assert all(d.ticket_id is None and d.ticket_url is None for d in deferrals)
        assert [(f.identity, f.disposition) for f in att.findings or []] == [
            ("MUST_FIX::src/a.py::gamma", "fixed"),
            ("SHOULD_FIX::src/a.py::alpha", "rejected"),
            ("SHOULD_FIX::src/a.py::beta", "deferred"),
            ("SHOULD_FIX::src/a.py::zeta", "deferred"),
        ]
        assert [(o.kind, o.actor, o.recorded_at) for o in att.overrides] == [
            ("operator_settle", "", ""),
            ("finding_disposition", "a1", "2026-11-08T08:00:00+00:00"),
            ("must_fix_override", "octo", "2026-11-08T09:00:00+00:00"),
            ("operator_settle", "op", "2026-11-08T10:00:00+00:00"),
        ]

    def test_fingerprint_identity_sets_fingerprint_only(self, tmp_path: Path) -> None:
        ident = FindingIdentity(
            severity="SHOULD_FIX",
            file="a.py",
            summary_normalized="s",
            fingerprint="fp-1234567",
        )
        settled = SettledEntry(identity=ident, outcome="defer", rationale="r", round=1)
        ledger = _ledger().model_copy(update={"settled": [settled]})
        att = _build(review_row(None), client=_clean_client(tmp_path), ledger=ledger)
        assert att is not None
        deferral = (att.deferrals or [])[0]
        assert (deferral.fingerprint, deferral.identity) == ("fp-1234567", None)


class TestFoundDuringReview:
    def test_initial_counts_go_to_found_during_review(self, tmp_path: Path) -> None:
        result = clean_sentinel(SHA, must_fix_initial=3, should_fix=2)
        att = _build(
            review_row(None), last_result=result, client=_blocking_client(tmp_path)
        )
        assert att is not None
        assert (att.counts.must_fix, att.counts.should_fix) == (0, 0)
        assert att.found_during_review is not None
        assert att.found_during_review.model_dump() == {"must_fix": 3, "should_fix": 2}

    @pytest.mark.parametrize(
        ("mfi", "sf"),
        [(None, 0), ("3", 0), (True, 0), (-1, 0), (0, False), (0, None)],
    )
    def test_bad_initial_counts_still_write_with_unknown(
        self, tmp_path: Path, mfi: object, sf: object
    ) -> None:
        result = clean_sentinel(SHA, must_fix_initial=mfi, should_fix=sf)
        review = result["review"]
        assert isinstance(review, dict)
        if mfi is None:
            del review["must_fix_initial"]
        if sf is None:
            del review["should_fix"]
        att = _build(
            review_row(None), last_result=result, client=_blocking_client(tmp_path)
        )
        assert att is not None
        assert att.found_during_review is None
        assert len(events_of(RECORDED)) == 1


class TestOverflow:
    def _wide_ledger(self, entries: int, summary_len: int) -> ConvergenceLedger:
        fixed = [
            FixedEntry(
                identity=_identity(f"{n:04d}" + "x" * summary_len),
                round=1,
                reviewed_sha=SHA,
            )
            for n in range(entries)
        ]
        return _ledger().model_copy(update={"fixed": fixed})

    @pytest.mark.parametrize(
        ("entries", "summary_len"),
        [(MAX_ATTESTATION_FINDINGS + 1, 0), (150, MAX_ATTESTATION_BYTES // 100)],
    )
    def test_overflow_writes_nothing_and_rejects(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        entries: int,
        summary_len: int,
    ) -> None:
        caplog.set_level(logging.INFO, logger=LOGGER)
        task = review_row(None)
        att = _build(
            task,
            client=_clean_client(tmp_path),
            ledger=self._wide_ledger(entries, summary_len),
        )
        assert att is None
        assert task.review_attestation is None
        assert events_of(RECORDED) == []
        rejected = events_of(REJECTED)
        assert len(rejected) == 1
        payload = rejected[0].payload
        assert set(payload) == {
            "ticket_id",
            "client",
            "head_sha",
            "reason",
            "finding_count",
            "size_bytes",
            "max_findings",
            "max_bytes",
        }
        assert payload["reason"] == "attestation_oversized"
        assert payload["finding_count"] == entries
        assert payload["max_findings"] == MAX_ATTESTATION_FINDINGS
        assert payload["max_bytes"] == MAX_ATTESTATION_BYTES
        assert payload["head_sha"] == SHA
        assert rejected[0].correlation_id == task.ticket_id
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "reason=attestation_oversized" in message
        assert task.ticket_id in message
        assert SHA in message
        assert "xxxx" not in message
        assert [r for r in caplog.records if r.levelno == logging.INFO] == []


class TestStrengthRule:
    def test_engine_backed_not_replaced_by_engineless_same_head(
        self, tmp_path: Path
    ) -> None:
        task = review_row(None)
        stored = attestation_for(SHA, engine="code-review")
        task.review_attestation = stored
        assert _build(task, client=_clean_client(tmp_path), ledger=_ledger()) is None
        assert task.review_attestation == stored

    def test_different_head_replaces(self, tmp_path: Path) -> None:
        task = review_row(None)
        task.review_attestation = attestation_for(OTHER, engine="code-review")
        att = _build(task, client=_clean_client(tmp_path), ledger=_ledger())
        assert att is not None
        assert task.review_attestation == att

    def test_equal_strength_refreshes(self, tmp_path: Path) -> None:
        task = review_row(None)
        task.review_attestation = attestation_for(
            SHA, written_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        att = _build(task, client=_clean_client(tmp_path), ledger=_ledger())
        assert att is not None
        assert att.written_at == NOW

    def test_engine_backed_replaces_engineless(self, tmp_path: Path) -> None:
        task = review_row(None)
        task.review_attestation = attestation_for(SHA)
        att = _build(
            task,
            client=_engine_client(tmp_path),
            ledger=_ledger(),
            engine_summary=_summary(),
        )
        assert att is not None
        assert att.engine == "code-review"


class TestRefreshOnly:
    def _override(self) -> MustFixOverride:
        return MustFixOverride(
            actor="octo",
            reason="ship it",
            reviewed_sha=SHA,
            finding_ids=[],
            recorded_at=datetime(2026, 11, 8, 9, 0, tzinfo=UTC),
        )

    def test_refresh_rebuilds_overrides_on_same_head(self) -> None:
        task = review_row(None, review_approved_sha=SHA)
        settle = AttestationOverride(
            kind="operator_settle", actor="op", reason="r", recorded_at=""
        )
        task.review_attestation = attestation_for(SHA, overrides=[settle], engine="e")
        task.must_fix_override = self._override()
        att = _build(task, last_result={}, client=None, refresh_only=True)
        assert att is not None
        assert [o.kind for o in att.overrides] == [
            "operator_settle",
            "must_fix_override",
        ]
        assert att.engine == "e"
        assert att.counts == task.review_attestation.counts
        (event,) = events_of(RECORDED)
        assert event.payload["refresh"] is True

    def test_refresh_without_attestation_is_a_noop(self) -> None:
        task = review_row(None, review_approved_sha=SHA)
        task.must_fix_override = self._override()
        assert _build(task, client=None, refresh_only=True) is None
        assert task.review_attestation is None

    def test_refresh_at_another_head_is_a_noop(self) -> None:
        task = review_row(None, review_approved_sha=SHA)
        stored = attestation_for(OTHER)
        task.review_attestation = stored
        task.must_fix_override = self._override()
        assert _build(task, client=None, refresh_only=True) is None
        assert task.review_attestation == stored

    def test_unchanged_refresh_emits_nothing(self) -> None:
        task = review_row(None, review_approved_sha=SHA)
        task.review_attestation = attestation_for(SHA)
        assert _build(task, client=None, refresh_only=True) is None
        assert events_of(RECORDED) == []


class TestEvents:
    def test_recorded_payload(self, tmp_path: Path) -> None:
        task = review_row(None)
        att = _build(task, client=_clean_client(tmp_path), ledger=_ledger())
        assert att is not None
        (event,) = events_of(RECORDED)
        assert event.correlation_id == task.ticket_id
        assert event.payload == {
            "ticket_id": task.ticket_id,
            "client": task.client,
            "head_sha": SHA,
            "engine": None,
            "reviewer_backend": "claude-native",
            "counts": att.counts.model_dump(),
            "verdict": "PASS",
            "refresh": False,
        }

    def test_builder_takes_no_sessions_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.config.sessions_lock", _boom)
        assert _build(
            review_row(None), client=_clean_client(tmp_path), ledger=_ledger()
        )

    def test_retry_is_idempotent(self, tmp_path: Path) -> None:
        task = review_row(None)
        first = _build(task, client=_clean_client(tmp_path), ledger=_ledger())
        second = _build(
            task,
            client=_clean_client(tmp_path),
            ledger=_ledger(),
            now=datetime(2026, 11, 8, 13, 0, tzinfo=UTC),
        )
        assert first is not None
        assert second is not None
        assert first.model_copy(update={"written_at": NOW}) == second.model_copy(
            update={"written_at": NOW}
        )


class SkipCase(NamedTuple):
    """One drive of the builder into exactly one skip branch."""

    token: str
    client: ClientConfig | None
    last_result: dict[str, object] | None = None
    ledger: ConvergenceLedger | None = None
    engine_summary: ReviewEngineSummary | None = None
    now: datetime | None = NOW
    refresh_only: bool = False
    stored: ReviewAttestation | None = None
    via_exit: bool = False
    none_result: bool = False


def _skip_cases(tmp_path: Path) -> list[SkipCase]:
    from tests._review_engine_helpers import make_finding

    blocking = _blocking_client(tmp_path)
    clean = _clean_client(tmp_path)
    engine = _engine_client(tmp_path)
    no_review = clean_sentinel(SHA)
    del no_review["review"]
    failing = _summary(
        verdict="FAIL",
        counts={"must_fix": 1, "should_fix": 0, "debt": 0, "nit": 0, "principle": 0},
        findings=[make_finding(severity="MUST_FIX")],
    )
    naive = datetime.fromisoformat("2026-11-08T12:00:00")
    return [
        SkipCase("no_client", None),
        SkipCase("not_success", blocking, clean_sentinel(SHA, status="blocked")),
        SkipCase("not_success", blocking, none_result=True),
        SkipCase(
            "health_degraded",
            blocking,
            clean_sentinel(SHA, recommendation="EXIT_FOR_HUMAN_REVIEW"),
        ),
        SkipCase("health_degraded", blocking, clean_sentinel(SHA, agents_run=0)),
        SkipCase("missing_reviewed_sha", blocking, clean_sentinel(None)),
        SkipCase("missing_reviewed_sha", blocking, no_review),
        SkipCase("bad_reviewed_sha", blocking, clean_sentinel("a" * 39)),
        SkipCase("bad_reviewed_sha", blocking, clean_sentinel("A" * 40)),
        SkipCase("unsupported_backend", _backend_client(tmp_path, "local")),
        SkipCase("ledger_absent", clean),
        SkipCase("ledger_absent", clean, via_exit=True),
        SkipCase("ledger_disagrees", clean, ledger=_ledger(ticket_id="T-x")),
        SkipCase("ledger_disagrees", clean, ledger=fresh_ledger(ticket_id="T-1")),
        SkipCase("ledger_disagrees", clean, ledger=_ledger(sha=OTHER)),
        SkipCase(
            "no_engine_config", clean, ledger=_ledger(), engine_summary=_summary()
        ),
        SkipCase(
            "engine_mismatch",
            engine,
            ledger=_ledger(),
            engine_summary=_summary(head_sha=OTHER),
        ),
        SkipCase(
            "engine_mismatch",
            engine,
            ledger=_ledger(),
            engine_summary=_summary(coverage="FAILED"),
        ),
        SkipCase("engine_mismatch", engine, ledger=_ledger(), engine_summary=failing),
        SkipCase("invalid_model", blocking, now=naive),
        SkipCase(
            "weaker_than_stored", blocking, stored=attestation_for(SHA, engine="e")
        ),
        SkipCase("refresh_no_attestation", None, refresh_only=True),
        SkipCase(
            "refresh_no_attestation",
            None,
            refresh_only=True,
            stored=attestation_for(OTHER),
        ),
        SkipCase(
            "refresh_unchanged", None, refresh_only=True, stored=attestation_for(SHA)
        ),
    ]


def _drive(task: TicketTask, case: SkipCase) -> ReviewAttestation | None:
    if case.via_exit:
        return attest_review_exit(
            task, last_result=clean_sentinel(SHA), client=case.client
        )
    if case.none_result:
        return record_review_attestation(
            task, last_result=None, engine_summary=None, ledger=None, client=case.client
        )
    return _build(
        task,
        last_result=case.last_result,
        client=case.client,
        ledger=case.ledger,
        engine_summary=case.engine_summary,
        now=case.now,
        refresh_only=case.refresh_only,
    )


SKIP_TOKENS = (
    "refresh_no_attestation",
    "refresh_unchanged",
    "no_client",
    "not_success",
    "health_degraded",
    "missing_reviewed_sha",
    "bad_reviewed_sha",
    "unsupported_backend",
    "ledger_absent",
    "ledger_disagrees",
    "no_engine_config",
    "engine_mismatch",
    "invalid_model",
    "weaker_than_stored",
)


class TestSkipLogging:
    def test_every_token_is_covered(self, tmp_path: Path) -> None:
        assert {case.token for case in _skip_cases(tmp_path)} == set(SKIP_TOKENS)

    @pytest.mark.parametrize("index", range(24))
    def test_skip_logs_one_info_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, index: int
    ) -> None:
        cases = _skip_cases(tmp_path)
        assert len(cases) == 24
        case = cases[index]
        task = review_row(None, review_approved_sha=SHA)
        task.review_attestation = case.stored
        caplog.set_level(logging.INFO, logger=LOGGER)
        assert _drive(task, case) is None
        assert task.review_attestation == case.stored
        assert events_of(RECORDED) == []
        assert events_of(REJECTED) == []
        records = [r for r in caplog.records if r.name == LOGGER]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        message = records[0].getMessage()
        assert f"reason={case.token}" in message
        assert f"ticket={task.ticket_id}" in message
        assert SHA not in message
        assert OTHER not in message

    def test_successful_write_logs_no_skip(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=LOGGER)
        assert _build(
            review_row(None), client=_clean_client(tmp_path), ledger=_ledger()
        )
        assert [r for r in caplog.records if r.name == LOGGER] == []


class TestStructure:
    def test_builder_module_imports_no_git_or_subprocess(self) -> None:
        source = Path(builder_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_modules = {
            "subprocess",
            "cw._git",
            "cw.gh",
            "cw.branch_ahead",
            "cw.merge_probe",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & forbidden_modules
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden_modules
        names = (
            {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            | {
                a.name
                for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)
                for a in n.names
            }
        )
        assert not names & {"current_head_sha", "run_git", "_checked_out_branch"}

    def test_counts_derivation_names_no_git_or_resolution(self) -> None:
        source = Path(builder_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {
                "_counts",
                "_found_during_review",
            }:
                called = {
                    getattr(c.func, "id", getattr(c.func, "attr", ""))
                    for c in ast.walk(node)
                    if isinstance(c, ast.Call)
                }
                assert not called & {
                    "resolve_task_worktree",
                    "load_ledger",
                    "run_git",
                    "current_head_sha",
                }
