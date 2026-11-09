"""Tests for the bounded review engine summary loader (#3036, RFC 0017 S1)."""

from __future__ import annotations

import ast
import inspect
import json
import os
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest

from cw import review_engine_summary_io as io_mod
from cw.exceptions import CwError
from cw.models.review_engine_summary import (
    MAX_ENGINE_SUMMARY_BYTES,
    ReviewEngineSummary,
)
from cw.review_engine.constants import ENGINE_STATE_RELATIVE_DIR
from cw.review_engine_summary_io import (
    MAX_ISSUE_TEXT_CHARS,
    MAX_ROUND_DIRS_SCANNED,
    EngineSummaryError,
    EngineSummaryIssue,
    EngineSummaryRejection,
    _sanitize,
    find_round_summary_for_head,
    load_review_engine_summary,
    parse_review_engine_summary,
)
from tests._review_engine_helpers import (
    SHA,
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


_OTHER = "b" * 40


def _round(
    worktree: Path, name: str, summary: dict[str, object] | bytes, age: int = 0
) -> Path:
    """Write ``<worktree>/.cw/review-engine/<name>/summary.json``; older by *age*."""
    directory = worktree / ENGINE_STATE_RELATIVE_DIR / name
    write_summary(directory, summary)
    stamp = 2_000_000_000 - age
    os.utime(directory, (stamp, stamp))
    return directory


def _verdict(**overrides: object) -> dict[str, object]:
    return make_summary(head_sha=SHA, **overrides)


class TestFindRoundSummaryForHead:
    def test_returns_the_matching_head(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-old", make_summary(head_sha=_OTHER), age=5)
        _round(tmp_path, "r-new", _verdict())

        found = find_round_summary_for_head(tmp_path, SHA)

        assert found is not None
        assert found.head_sha == SHA

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-1", make_summary(head_sha=_OTHER))

        assert find_round_summary_for_head(tmp_path, SHA) is None

    def test_missing_engine_dir_returns_none(self, tmp_path: Path) -> None:
        assert find_round_summary_for_head(tmp_path, SHA) is None

    def test_prefers_complete_pass_over_a_newer_failing_match(
        self, tmp_path: Path
    ) -> None:
        failing = _verdict(verdict="FAIL")
        _round(tmp_path, "r-pass", _verdict(), age=10)
        _round(tmp_path, "r-fail", failing, age=0)

        found = find_round_summary_for_head(tmp_path, SHA)

        assert found is not None
        assert found.verdict == "PASS"

    def test_partial_coverage_loses_to_complete(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-partial", _verdict(coverage="PARTIAL"), age=0)
        _round(tmp_path, "r-full", _verdict(), age=10)

        found = find_round_summary_for_head(tmp_path, SHA)

        assert found is not None
        assert found.coverage == "COMPLETE"

    def test_newest_mtime_wins_among_equals(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-old", _verdict(risk="HIGH"), age=20)
        _round(tmp_path, "r-new", _verdict(risk="MEDIUM"), age=0)

        found = find_round_summary_for_head(tmp_path, SHA)

        assert found is not None
        assert found.risk == "MEDIUM"

    def test_falls_back_to_newest_non_agreeing_match(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-old", _verdict(verdict="FAIL", risk="HIGH"), age=9)
        _round(tmp_path, "r-new", _verdict(verdict="FAIL", risk="MEDIUM"), age=0)

        found = find_round_summary_for_head(tmp_path, SHA)

        assert found is not None
        assert found.verdict == "FAIL"
        assert found.risk == "MEDIUM"

    def test_skips_symlinked_round_dir(self, tmp_path: Path) -> None:
        real = _round(tmp_path / "elsewhere", "real", _verdict())
        engine = tmp_path / ENGINE_STATE_RELATIVE_DIR
        engine.mkdir(parents=True)
        (engine / "linked").symlink_to(real, target_is_directory=True)

        assert find_round_summary_for_head(tmp_path, SHA) is None

    def test_skips_symlinked_summary_file(self, tmp_path: Path) -> None:
        real = write_summary(tmp_path / "elsewhere", _verdict())
        directory = tmp_path / ENGINE_STATE_RELATIVE_DIR / "r-1"
        directory.mkdir(parents=True)
        (directory / "summary.json").symlink_to(real)

        assert find_round_summary_for_head(tmp_path, SHA) is None

    def test_skips_names_failing_the_round_id_pattern(self, tmp_path: Path) -> None:
        _round(tmp_path, "-bad", _verdict())
        _round(tmp_path, "has space", _verdict())

        assert find_round_summary_for_head(tmp_path, SHA) is None

    def test_skips_a_stray_file_in_the_engine_dir(self, tmp_path: Path) -> None:
        engine = tmp_path / ENGINE_STATE_RELATIVE_DIR
        engine.mkdir(parents=True)
        (engine / "stray").write_text("x", encoding="utf-8")
        _round(tmp_path, "r-1", _verdict())

        assert find_round_summary_for_head(tmp_path, SHA) is not None

    def test_skips_unusable_files_and_keeps_scanning(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-garbage", b"not json", age=0)
        _round(tmp_path, "r-oversized", b" " * (MAX_ENGINE_SUMMARY_BYTES + 1), age=1)
        _round(tmp_path, "r-invalid", {"schema_version": 1}, age=2)
        (tmp_path / ENGINE_STATE_RELATIVE_DIR / "r-empty").mkdir()
        _round(tmp_path, "r-good", _verdict(), age=3)

        found = find_round_summary_for_head(tmp_path, SHA)

        assert found is not None
        assert found.head_sha == SHA

    def test_scan_is_capped_at_the_newest_dirs(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-match", _verdict(), age=1000)
        for index in range(MAX_ROUND_DIRS_SCANNED):
            _round(tmp_path, f"r-{index:03d}", make_summary(head_sha=_OTHER), age=index)

        assert find_round_summary_for_head(tmp_path, SHA) is None

    def test_a_match_inside_the_cap_is_found(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-match", _verdict(), age=MAX_ROUND_DIRS_SCANNED - 1)
        for index in range(MAX_ROUND_DIRS_SCANNED - 1):
            _round(tmp_path, f"r-{index:03d}", make_summary(head_sha=_OTHER), age=index)

        assert find_round_summary_for_head(tmp_path, SHA) is not None

    def test_writes_nothing(self, tmp_path: Path) -> None:
        _round(tmp_path, "r-1", _verdict())
        before = sorted(tmp_path.rglob("*"))

        find_round_summary_for_head(tmp_path, SHA)

        assert sorted(tmp_path.rglob("*")) == before

    def test_module_imports_no_subprocess_git_or_gh(self) -> None:
        tree = ast.parse(inspect.getsource(io_mod))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
                imported.update(f"{node.module}.{a.name}" for a in node.names)
        assert not {"subprocess", "cw._git", "cw.gh"} & imported
