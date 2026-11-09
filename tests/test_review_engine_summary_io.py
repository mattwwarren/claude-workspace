"""Tests for the bounded review engine summary loader (#3036, RFC 0017 S1)."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.models.review_engine_summary import (
    MAX_ENGINE_SUMMARY_BYTES,
    ReviewEngineSummary,
)
from cw.review_engine_summary_io import (
    MAX_ISSUE_TEXT_CHARS,
    EngineSummaryError,
    EngineSummaryIssue,
    EngineSummaryRejection,
    _sanitize,
    load_review_engine_summary,
    parse_review_engine_summary,
)
from tests._review_engine_helpers import (
    make_finding,
    make_summary,
    pad_to,
    summary_bytes,
    write_summary,
)

if TYPE_CHECKING:
    from pathlib import Path

_EVIDENCE = "SENTINEL-EVIDENCE-7f3a"
_SUMMARY = "SENTINEL-SUMMARY-9c1d"


def _secret_finding(**overrides: object) -> dict[str, object]:
    return make_finding(evidence=_EVIDENCE, summary=_SUMMARY, **overrides)


def _reject_bytes(raw: bytes) -> EngineSummaryError:
    with pytest.raises(EngineSummaryError) as excinfo:
        parse_review_engine_summary(raw)
    return excinfo.value


def _reject_payload(payload: dict[str, object]) -> EngineSummaryError:
    return _reject_bytes(summary_bytes(payload))


def _reject_path(path: Path) -> EngineSummaryError:
    with pytest.raises(EngineSummaryError) as excinfo:
        load_review_engine_summary(path)
    return excinfo.value


def _assert_clean(error: EngineSummaryError) -> None:
    assert isinstance(error, CwError)
    text = " ".join(
        [str(error), *(f"{i.field_path} {i.message}" for i in error.issues)]
    )
    assert _EVIDENCE not in text
    assert _SUMMARY not in text


class TestRejectionEnum:
    def test_members(self) -> None:
        assert {member.value for member in EngineSummaryRejection} == {
            "unreadable",
            "oversized",
            "not_utf8",
            "not_json",
            "not_object",
            "unsupported_schema_version",
            "schema_invalid",
            "inconsistent",
        }
        assert issubclass(EngineSummaryRejection, StrEnum)

    def test_error_shape(self) -> None:
        assert EngineSummaryError.__slots__ == ()
        assert issubclass(EngineSummaryError, CwError)

    def test_error_without_issues_carries_one(self) -> None:
        error = EngineSummaryError(
            "boom", reason=EngineSummaryRejection.NOT_JSON, field_path=None
        )

        assert error.reason is EngineSummaryRejection.NOT_JSON
        assert error.field_path is None
        assert error.issues == (
            EngineSummaryIssue(EngineSummaryRejection.NOT_JSON, None, "boom"),
        )
        assert str(error) == "boom"


class TestUnreadable:
    def test_missing_file(self, tmp_path: Path) -> None:
        error = _reject_path(tmp_path / "absent.json")

        assert error.reason is EngineSummaryRejection.UNREADABLE
        assert error.field_path is None
        _assert_clean(error)

    def test_directory(self, tmp_path: Path) -> None:
        error = _reject_path(tmp_path)

        assert error.reason is EngineSummaryRejection.UNREADABLE
        assert error.field_path is None

    def test_dash_is_a_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pathlib import Path

        monkeypatch.chdir(tmp_path)

        error = _reject_path(Path("-"))

        assert error.reason is EngineSummaryRejection.UNREADABLE


class TestOversized:
    def test_exactly_cap_accepted(self, tmp_path: Path) -> None:
        raw = pad_to(summary_bytes(make_summary()), MAX_ENGINE_SUMMARY_BYTES)
        path = write_summary(tmp_path, raw)

        assert isinstance(load_review_engine_summary(path), ReviewEngineSummary)

    def test_cap_plus_one_rejected(self, tmp_path: Path) -> None:
        raw = pad_to(summary_bytes(make_summary()), MAX_ENGINE_SUMMARY_BYTES + 1)
        path = write_summary(tmp_path, raw)

        error = _reject_path(path)

        assert error.reason is EngineSummaryRejection.OVERSIZED
        assert error.field_path is None
        _assert_clean(error)

    def test_much_larger_file_rejected(self, tmp_path: Path) -> None:
        raw = pad_to(summary_bytes(make_summary()), MAX_ENGINE_SUMMARY_BYTES * 3)
        path = write_summary(tmp_path, raw)

        assert _reject_path(path).reason is EngineSummaryRejection.OVERSIZED

    def test_parse_checks_cap_directly(self) -> None:
        raw = pad_to(summary_bytes(make_summary()), MAX_ENGINE_SUMMARY_BYTES + 1)

        assert _reject_bytes(raw).reason is EngineSummaryRejection.OVERSIZED


class TestNotUtf8:
    def test_invalid_bytes(self) -> None:
        error = _reject_bytes(b"\xff\xfe{}")

        assert error.reason is EngineSummaryRejection.NOT_UTF8
        assert error.field_path is None

    def test_bom_prefix_is_not_json(self) -> None:
        error = _reject_bytes(b"\xef\xbb\xbf" + summary_bytes(make_summary()))

        assert error.reason is EngineSummaryRejection.NOT_JSON


class TestNotJson:
    @pytest.mark.parametrize(
        "raw",
        [b"not json at all", b"", b"[" * 100_000, b'{"a": ' + b"1" * 5_000 + b"}"],
        ids=["garbage", "empty", "deep-nesting", "huge-int"],
    )
    def test_rejected(self, raw: bytes) -> None:
        error = _reject_bytes(raw)

        assert error.reason is EngineSummaryRejection.NOT_JSON
        assert error.field_path is None


class TestNotObject:
    @pytest.mark.parametrize("raw", [b"[]", b"42", b'"x"', b"null"])
    def test_rejected(self, raw: bytes) -> None:
        error = _reject_bytes(raw)

        assert error.reason is EngineSummaryRejection.NOT_OBJECT
        assert error.field_path is None


class TestSchemaVersion:
    def test_future_version_rejected_before_fields(self) -> None:
        raw = json.dumps({"schema_version": 2, "findings": "garbage"}).encode()

        error = _reject_bytes(raw)

        assert error.reason is EngineSummaryRejection.UNSUPPORTED_SCHEMA_VERSION
        assert error.field_path == "schema_version"
        assert "2" in str(error)

    @pytest.mark.parametrize(
        ("value", "echo"),
        [(0, "0"), (True, "bool"), ("1", "str"), (1.5, "float")],
        ids=["zero", "bool", "str", "float"],
    )
    def test_bad_version_rejected(self, value: object, echo: str) -> None:
        error = _reject_payload(make_summary(schema_version=value))

        assert error.reason is EngineSummaryRejection.UNSUPPORTED_SCHEMA_VERSION
        assert error.field_path == "schema_version"
        assert echo in str(error)

    def test_missing_version_rejected(self) -> None:
        payload = make_summary()
        del payload["schema_version"]

        error = _reject_payload(payload)

        assert error.reason is EngineSummaryRejection.UNSUPPORTED_SCHEMA_VERSION
        assert error.field_path == "schema_version"
        assert "missing" in str(error)


class TestSchemaInvalid:
    def test_field_error_has_dotted_path(self) -> None:
        error = _reject_payload(make_summary([_secret_finding(file="")]))

        assert error.reason is EngineSummaryRejection.SCHEMA_INVALID
        assert error.field_path == "findings.0.file"
        _assert_clean(error)

    def test_unknown_top_level_key(self) -> None:
        error = _reject_payload(make_summary(bogus=1))

        assert error.reason is EngineSummaryRejection.SCHEMA_INVALID
        assert error.field_path == "bogus"

    def test_non_printable_key_sanitized(self) -> None:
        error = _reject_payload(make_summary(**{"bad\x1b[31mkey": 1}))

        assert error.field_path == "bad?[31mkey"

    def test_long_key_truncated(self) -> None:
        error = _reject_payload(make_summary(**{"k" * 500: 1}))

        assert error.field_path is not None
        assert len(error.field_path) == MAX_ISSUE_TEXT_CHARS

    def test_sanitize_truncates_and_replaces(self) -> None:
        assert _sanitize("a\nb\tc") == "a?b?c"
        assert len(_sanitize("m" * 1_000)) == MAX_ISSUE_TEXT_CHARS


class TestInconsistent:
    @pytest.mark.parametrize(
        ("payload", "field_path", "message"),
        [
            (
                make_summary([_secret_finding(), _secret_finding()]),
                "findings.1.fingerprint",
                "duplicate fingerprint 'fp-000001'",
            ),
            (
                make_summary(
                    [_secret_finding()],
                    counts={
                        "must_fix": 1,
                        "should_fix": 0,
                        "debt": 0,
                        "nit": 0,
                        "principle": 0,
                    },
                ),
                "counts",
                "counts must_fix=1 should_fix=0 debt=0 nit=0 principle=0 do not "
                "match findings tally must_fix=0 should_fix=0 debt=0 nit=1 "
                "principle=0",
            ),
            (
                make_summary([_secret_finding(severity="SHOULD_FIX")], verdict="PASS"),
                "verdict",
                "verdict PASS with 1 MUST_FIX/SHOULD_FIX finding(s)",
            ),
            (
                make_summary(
                    [
                        make_finding(fingerprint="fp-000000"),
                        _secret_finding(file=None, line=7),
                    ]
                ),
                "findings.1.line",
                "line 7 given without file",
            ),
        ],
        ids=["duplicate", "counts", "pass-gating", "line-without-file"],
    )
    def test_rejected(
        self, payload: dict[str, object], field_path: str, message: str
    ) -> None:
        error = _reject_payload(payload)

        assert error.reason is EngineSummaryRejection.INCONSISTENT
        assert error.field_path == field_path
        assert error.issues[0].message == message
        _assert_clean(error)


class TestIssues:
    def test_multiple_errors_give_multiple_issues(self) -> None:
        payload = make_summary(
            [make_finding(file=""), make_finding(fingerprint="fp-000002", line=0)]
        )

        error = _reject_payload(payload)

        assert [issue.field_path for issue in error.issues] == [
            "findings.0.file",
            "findings.1.line",
        ]
        assert error.reason is error.issues[0].reason
        assert error.field_path == error.issues[0].field_path

    def test_schema_invalid_takes_precedence(self) -> None:
        payload = make_summary(
            [
                make_finding(file=None, line=3),
                make_finding(fingerprint="fp-000002", file=""),
            ]
        )

        error = _reject_payload(payload)

        assert error.reason is EngineSummaryRejection.SCHEMA_INVALID
        assert error.field_path == "findings.1.file"
        assert [issue.reason for issue in error.issues] == [
            EngineSummaryRejection.SCHEMA_INVALID,
            EngineSummaryRejection.INCONSISTENT,
        ]


class TestLoadHappyPath:
    def test_load(self, tmp_path: Path) -> None:
        path = write_summary(tmp_path, make_summary([make_finding()]))

        summary = load_review_engine_summary(path)

        assert isinstance(summary, ReviewEngineSummary)
        assert summary.counts.nit == 1

    def test_parse_is_pure(self) -> None:
        raw = summary_bytes(make_summary())

        assert parse_review_engine_summary(raw) == parse_review_engine_summary(raw)


class TestNoWrites:
    def test_loader_leaves_directory_unchanged(self, tmp_path: Path) -> None:
        work = tmp_path / "engine-work"
        good = write_summary(work, make_summary())
        bad = write_summary(work, b"[]", name="bad.json")
        before = sorted(work.rglob("*"))

        load_review_engine_summary(good)
        with pytest.raises(EngineSummaryError):
            load_review_engine_summary(bad)

        assert sorted(work.rglob("*")) == before
