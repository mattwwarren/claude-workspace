"""The head-pinned review sign-off attestation model (RFC 0017 II1, #3040)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from cw.models import TicketTask
from cw.models.review_attestation import (
    ATTESTATION_OVERSIZED,
    MAX_ATTESTATION_BYTES,
    MAX_ATTESTATION_FINDINGS,
    REVIEW_ATTESTATION_KEY,
    REVIEW_ATTESTATION_SCHEMA_VERSION,
    AttestationAcceptance,
    AttestationCounts,
    AttestationDeferral,
    AttestationFinding,
    AttestationGateSignal,
    AttestationOverride,
    FoundDuringReview,
    ReviewAttestation,
    attestation_size_ok,
    sorted_for_wire,
)
from cw.models.tasks import DEV_QUEUE_SCHEMA_VERSION

GOLDEN = Path(__file__).parent / "fixtures" / "review_attestation_golden.json"
HEAD = "a" * 40
WRITTEN = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _base(**overrides: object) -> ReviewAttestation:
    fields: dict[str, object] = {
        "ticket_id": "ACM-1",
        "client": "acme",
        "head_sha": HEAD,
        "written_at": WRITTEN,
        "engine": None,
        "reviewer_backend": "claude-native",
        "verdict": "PASS",
        "counts": AttestationCounts(),
    }
    fields.update(overrides)
    return ReviewAttestation.model_validate(fields)


def _deferral(fp: str | None, ident: str | None, **kw: object) -> AttestationDeferral:
    return AttestationDeferral.model_validate(
        {"fingerprint": fp, "identity": ident, "severity": "SHOULD_FIX", **kw}
    )


def _finding(ident: str, disposition: str, **kw: object) -> AttestationFinding:
    return AttestationFinding.model_validate(
        {
            "identity": ident,
            "severity": "MUST_FIX",
            "file": "src/a.py",
            "disposition": disposition,
            **kw,
        }
    )


def _golden_attestations() -> dict[str, ReviewAttestation]:
    engine_backed = _base(
        engine="code-review",
        counts=AttestationCounts(must_fix=0, should_fix=0, debt=2, nit=1),
        acceptance=[
            AttestationAcceptance(id="AC-2", status="NOT_APPLICABLE"),
            AttestationAcceptance(id="AC-1", status="PASS"),
        ],
        risk="LOW",
        gate_signals=[AttestationGateSignal(name="tests", status="PASS")],
        deferrals=[
            _deferral(
                "fp-00000001",
                None,
                rule_id="style.naming",
                file="a.py",
                line=3,
                debt_key="engine:fp-00000001",
                ticket_id="3101",
                ticket_url="https://github.com/o/r/issues/3101",
            ),
            _deferral(
                "fp-00000002",
                None,
                rule_id="style.naming",
                file="b.py",
                line=9,
                debt_key="b" * 64,
            ),
        ],
        overrides=[],
        findings=[],
    )
    engineless_clean = _base(
        found_during_review=FoundDuringReview(must_fix=3, should_fix=2),
        deferrals=[],
        findings=[
            _finding("MUST_FIX::src/b.py::y", "fixed", file="src/b.py"),
            _finding("MUST_FIX::src/a.py::x", "rejected"),
        ],
        overrides=[
            AttestationOverride(
                kind="operator_settle",
                actor="op",
                reason="false positive",
                recorded_at="2026-11-08T11:00:00+00:00",
            )
        ],
    )
    engineless_blocking = _base(
        reviewer_backend="codex",
        found_during_review=FoundDuringReview(must_fix=1, should_fix=0),
    )
    return {
        "engine_backed": sorted_for_wire(engine_backed),
        "engineless_clean": sorted_for_wire(engineless_clean),
        "engineless_blocking": sorted_for_wire(engineless_blocking),
    }


class TestGolden:
    def test_three_attestations_match_golden(self) -> None:
        produced = {
            name: att.model_dump(mode="json")
            for name, att in _golden_attestations().items()
        }
        assert produced == json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_golden_pins_counts_and_deferrals_contract(self) -> None:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for att in golden.values():
            assert att["counts"]["must_fix"] == 0
            assert att["counts"]["should_fix"] == 0
            assert att["verdict"] == "PASS"
        assert golden["engine_backed"]["found_during_review"] is None
        filled, pending = golden["engine_backed"]["deferrals"]
        assert filled["debt_key"] == "engine:fp-00000001"
        assert filled["ticket_id"] == "3101"
        assert pending["debt_key"] == "b" * 64
        assert pending["ticket_id"] is None
        assert pending["ticket_url"] is None
        assert golden["engineless_clean"]["found_during_review"] == {
            "must_fix": 3,
            "should_fix": 2,
        }
        assert golden["engineless_clean"]["deferrals"] == []
        assert isinstance(golden["engineless_clean"]["findings"], list)
        blocking = golden["engineless_blocking"]
        assert blocking["found_during_review"] == {"must_fix": 1, "should_fix": 0}
        assert blocking["findings"] is None
        assert blocking["deferrals"] is None


class TestValidation:
    @pytest.mark.parametrize("sha", ["a" * 39, "A" * 40, "g" * 40, "a" * 41])
    def test_rejects_non_40_lowercase_hex_head(self, sha: str) -> None:
        with pytest.raises(ValidationError):
            _base(head_sha=sha)

    def test_written_at_requires_timezone(self) -> None:
        with pytest.raises(ValidationError):
            _base(written_at=datetime.fromisoformat("2026-11-08T12:00:00"))

    def test_written_at_normalizes_to_utc(self) -> None:
        plus_two = timezone(timedelta(hours=2))
        att = _base(written_at=datetime(2026, 11, 8, 14, 0, tzinfo=plus_two))
        assert att.written_at == WRITTEN
        assert att.written_at.tzinfo == UTC

    def test_unknown_keys_and_future_schema_load(self) -> None:
        raw = _base().model_dump(mode="json")
        raw["schema_version"] = REVIEW_ATTESTATION_SCHEMA_VERSION + 5
        raw["future_field"] = {"x": 1}
        raw["counts"]["future_count"] = 4
        att = ReviewAttestation.model_validate(raw)
        assert att.schema_version == REVIEW_ATTESTATION_SCHEMA_VERSION + 5

    def test_missing_optional_keys_load_as_none(self) -> None:
        raw = _base().model_dump(mode="json")
        del raw["found_during_review"]
        del raw["deferrals"]
        att = ReviewAttestation.model_validate(raw)
        assert att.found_during_review is None
        assert att.deferrals is None


class TestSortedForWire:
    def test_deferrals_sorted_with_none_low(self) -> None:
        entries = [
            _deferral(None, "SHOULD_FIX::b::x", file="b"),
            _deferral("fp-2", None, file=None, line=None),
            _deferral("fp-1", None, file="z", line=7),
            _deferral("fp-1", None, file="z", line=None),
            _deferral(None, None, file="a"),
        ]
        expected = [entries[4], entries[0], entries[3], entries[2], entries[1]]
        for perm in itertools.permutations(entries):
            att = sorted_for_wire(_base(deferrals=list(perm)))
            assert att.deferrals == expected

    def test_deferrals_none_passes_through(self) -> None:
        assert sorted_for_wire(_base(deferrals=None)).deferrals is None

    def test_findings_sorted_on_anchor_then_tiebreakers(self) -> None:
        tie_a = _finding("k", "fixed", rule_id=None, severity="MUST_FIX")
        tie_b = _finding("k", "fixed", rule_id="r.a", severity="MUST_FIX")
        tie_c = _finding("k", "fixed", rule_id="r.a", severity="SHOULD_FIX")
        first = _finding("a", "open")
        entries = [tie_c, first, tie_b, tie_a]
        for perm in itertools.permutations(entries):
            att = sorted_for_wire(_base(findings=list(perm)))
            assert att.findings == [first, tie_a, tie_b, tie_c]

    def test_overrides_sorted(self) -> None:
        def override(at: str, kind: str, actor: str, reason: str) -> object:
            return AttestationOverride.model_validate(
                {"kind": kind, "actor": actor, "reason": reason, "recorded_at": at}
            )

        a = override("", "operator_settle", "z", "r")
        b = override("2026-01-01", "finding_disposition", "a", "r")
        c = override("2026-01-01", "must_fix_override", "a", "q")
        d = override("2026-01-01", "must_fix_override", "a", "r")
        for perm in itertools.permutations([d, c, b, a]):
            assert sorted_for_wire(_base(overrides=list(perm))).overrides == [
                a,
                b,
                c,
                d,
            ]

    def test_acceptance_and_gate_signals_sorted(self) -> None:
        acc = [
            AttestationAcceptance(id="B", status="PASS"),
            AttestationAcceptance(id="A", status="PASS"),
            AttestationAcceptance(id="A", status="FAIL"),
        ]
        gates = [
            AttestationGateSignal(name="z", status="PASS"),
            AttestationGateSignal(name="a", status="WARN"),
            AttestationGateSignal(name="a", status="FAIL"),
        ]
        att = sorted_for_wire(_base(acceptance=acc, gate_signals=gates))
        assert att.acceptance == [acc[2], acc[1], acc[0]]
        assert att.gate_signals == [gates[2], gates[1], gates[0]]

    def test_serialization_byte_identical_across_permutations(self) -> None:
        findings = [_finding(str(n), "fixed") for n in range(4)]
        dumps = {
            sorted_for_wire(_base(findings=list(perm))).model_dump_json()
            for perm in itertools.permutations(findings)
        }
        assert len(dumps) == 1


class TestBounds:
    def test_constants(self) -> None:
        assert MAX_ATTESTATION_BYTES == 64 * 1024
        assert MAX_ATTESTATION_FINDINGS == 200
        assert ATTESTATION_OVERSIZED == "attestation_oversized"
        assert REVIEW_ATTESTATION_KEY == "review_attestation"

    def test_size_ok_rejects_too_many_findings(self) -> None:
        many = [_finding(str(n), "fixed") for n in range(MAX_ATTESTATION_FINDINGS + 1)]
        assert attestation_size_ok(_base(findings=many[:-1]))
        assert not attestation_size_ok(_base(findings=many))

    def test_size_ok_rejects_too_many_bytes(self) -> None:
        big = [_finding("x" * 400, "fixed", file="f" * 400) for _ in range(180)]
        assert not attestation_size_ok(_base(findings=big))


class TestRowField:
    def test_ticket_task_carries_optional_attestation(self) -> None:
        task = TicketTask(ticket_id="ACM-1", client="acme")
        assert task.review_attestation is None
        task.review_attestation = _base()
        dumped = task.model_dump(mode="json")
        again = TicketTask.model_validate(dumped)
        assert again.review_attestation == _base()

    def test_v55_row_migrates_to_v56_without_attestation(self) -> None:
        from cw.dev_queue.migrate import migrate_dev_queue

        raw: dict[str, object] = {
            "schema_version": 55,
            "tasks": [{"ticket_id": "ACM-1", "client": "acme"}],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 61
        tasks = migrated["tasks"]
        assert isinstance(tasks, list)
        task = TicketTask.model_validate(tasks[0])
        assert task.review_attestation is None
