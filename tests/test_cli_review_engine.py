"""Tests for ``cw review engine validate`` (#3036), through Click's ``CliRunner``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.cli.review.engine import MAX_VALIDATE_ERROR_LINES
from cw.events import inbox_path
from cw.models.review_engine_summary import (
    MAX_ENGINE_EVIDENCE_LENGTH,
    MAX_ENGINE_SUMMARY_BYTES,
)
from tests._review_engine_helpers import (
    make_finding,
    make_summary,
    pad_to,
    summary_bytes,
    write_summary,
)
from tests.conftest import _norm

if TYPE_CHECKING:
    from pathlib import Path

_EVIDENCE = "SENTINEL-EVIDENCE-41aa"
_SUMMARY = "SENTINEL-SUMMARY-77bb"


def _invoke(*args: str, stdin: str | None = None) -> Result:
    return CliRunner().invoke(
        main, ["review", "engine", "validate", *args], input=stdin
    )


def _stderr_lines(result: Result) -> list[str]:
    return result.stderr.splitlines()


def _bad_findings(count: int) -> dict[str, object]:
    return make_summary(
        [make_finding(fingerprint=f"fp-{i:06d}", file="") for i in range(count)]
    )


def test_valid_summary(tmp_path: Path) -> None:
    path = write_summary(tmp_path, make_summary([make_finding()]))

    result = _invoke(str(path))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "review engine summary valid: schema_version=1 verdict=PASS coverage=COMPLETE"
    ]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("build", "line_prefix"),
    [
        (lambda tmp: tmp / "absent.json", "unreadable: -: "),
        (lambda tmp: tmp, "unreadable: -: "),
        (
            lambda tmp: write_summary(
                tmp,
                pad_to(summary_bytes(make_summary()), MAX_ENGINE_SUMMARY_BYTES + 1),
            ),
            "oversized: -: ",
        ),
        (lambda tmp: write_summary(tmp, b"\xff\xfe{}"), "not_utf8: -: "),
        (lambda tmp: write_summary(tmp, b"{nope"), "not_json: -: "),
        (lambda tmp: write_summary(tmp, b"[1, 2]"), "not_object: -: "),
        (
            lambda tmp: write_summary(tmp, make_summary(schema_version=2)),
            "unsupported_schema_version: schema_version: ",
        ),
        (
            lambda tmp: write_summary(tmp, make_summary([make_finding(file="N/A")])),
            "schema_invalid: findings.0.file: ",
        ),
        (
            lambda tmp: write_summary(tmp, make_summary(verdict="FAIL", bogus=True)),
            "schema_invalid: bogus: ",
        ),
    ],
    ids=[
        "missing",
        "directory",
        "oversized",
        "not_utf8",
        "not_json",
        "not_object",
        "unsupported_schema_version",
        "schema_invalid",
        "schema_invalid-extra",
    ],
)
def test_rejection_reasons(tmp_path: Path, build: object, line_prefix: str) -> None:
    assert callable(build)
    path = build(tmp_path)

    result = _invoke(str(path))

    assert result.exit_code == 1
    assert result.stdout == ""
    lines = _stderr_lines(result)
    assert len(lines) == 1
    assert lines[0].startswith(line_prefix)


def test_inconsistent_duplicate_fingerprint(tmp_path: Path) -> None:
    path = write_summary(tmp_path, make_summary([make_finding(), make_finding()]))

    result = _invoke(str(path))

    assert result.exit_code == 1
    assert _stderr_lines(result) == [
        "inconsistent: findings.1.fingerprint: duplicate fingerprint 'fp-000001'"
    ]


def test_never_echoes_evidence_or_summary(tmp_path: Path) -> None:
    finding = make_finding(evidence=_EVIDENCE, summary=_SUMMARY, file="")
    path = write_summary(tmp_path, make_summary([finding]))

    result = _invoke(str(path))

    assert result.exit_code == 1
    assert _EVIDENCE not in result.output
    assert _SUMMARY not in result.output
    assert _EVIDENCE not in result.stderr
    assert _SUMMARY not in result.stderr


def test_never_echoes_oversized_evidence(tmp_path: Path) -> None:
    evidence = _EVIDENCE * (MAX_ENGINE_EVIDENCE_LENGTH // len(_EVIDENCE) + 1)
    finding = make_finding(evidence=evidence, summary=_SUMMARY)
    path = write_summary(tmp_path, make_summary([finding]))

    result = _invoke(str(path))

    assert result.exit_code == 1
    assert _stderr_lines(result)[0].startswith("schema_invalid: findings.0.evidence: ")
    assert _EVIDENCE not in result.stderr
    assert _SUMMARY not in result.stderr


@pytest.mark.parametrize(
    ("count", "trailer"),
    [
        (25, "... 5 more line(s) omitted"),
        (21, "... 1 more line(s) omitted"),
        (20, None),
    ],
)
def test_error_lines_truncated(tmp_path: Path, count: int, trailer: str | None) -> None:
    path = write_summary(tmp_path, _bad_findings(count))

    result = _invoke(str(path))

    assert result.exit_code == 1
    lines = _stderr_lines(result)
    issue_lines = [line for line in lines if line.startswith("schema_invalid: ")]
    assert len(issue_lines) == MAX_VALIDATE_ERROR_LINES == 20
    if trailer is None:
        assert len(lines) == MAX_VALIDATE_ERROR_LINES
    else:
        assert lines[-1] == trailer
        assert len(lines) == MAX_VALIDATE_ERROR_LINES + 1


def test_dash_is_not_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = _invoke("-", stdin=summary_bytes(make_summary()).decode())

    assert result.exit_code == 1
    assert _stderr_lines(result)[0].startswith("unreadable: -: ")


@pytest.mark.parametrize(
    "args",
    [("--json", "x.json"), (), ("a.json", "b.json")],
    ids=["json-flag", "no-file", "two-files"],
)
def test_usage_errors_exit_two(args: tuple[str, ...]) -> None:
    result = _invoke(*args)

    assert result.exit_code == 2


def test_emits_no_event_and_writes_nothing(tmp_path: Path) -> None:
    work = tmp_path / "engine-work"
    good = write_summary(work, make_summary())
    bad = write_summary(work, make_summary(bogus=1), name="bad.json")
    good_bytes, bad_bytes = good.read_bytes(), bad.read_bytes()
    before = sorted(work.rglob("*"))
    assert not inbox_path().exists()

    assert _invoke(str(good)).exit_code == 0
    assert _invoke(str(bad)).exit_code == 1

    assert sorted(work.rglob("*")) == before
    assert not inbox_path().exists()
    assert good.read_bytes() == good_bytes
    assert bad.read_bytes() == bad_bytes


def test_engine_group_help() -> None:
    result = CliRunner().invoke(main, ["review", "engine", "--help"])

    assert result.exit_code == 0
    help_text = _norm(result.output)
    assert "Validate and inspect repo review engine summaries (RFC 0017)." in help_text
    assert "validate" in help_text


def test_validate_help() -> None:
    result = CliRunner().invoke(main, ["review", "engine", "validate", "--help"])

    assert result.exit_code == 0
    assert (
        "Validate an engine summary JSON file against the cw-owned schema. "
        "Exits 0 when valid, 1 on any rejection."
    ) in _norm(result.output)


def test_review_help_lists_engine() -> None:
    result = CliRunner().invoke(main, ["review", "--help"])

    assert result.exit_code == 0
    assert "engine" in result.output
