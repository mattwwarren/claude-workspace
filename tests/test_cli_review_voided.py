"""Tests for ``cw review check-voided`` in ``cw.cli.review.voided`` (#1814).

Moved out of ``tests/test_cli_review_commands.py`` with the command itself
(#2319), so the test modules keep mirroring the ``src/cw/cli/review/``
package seams one-to-one. #2319's unmatched-``new_voided_entries`` check is
covered here too.

Every CLI test reads JSON from ``result.stdout`` and warnings from
``result.stderr``: Click's ``result.output`` interleaves the two streams, and
the refused exit writes to both.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.review.voided import _partition_new_entries, _write_voided_record
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_adjudication import (
    VoidedFinding,
    parse_voided_findings_block,
    render_voided_findings_block,
)
from cw.review_findings import ReviewVerdict
from tests._cli_review_helpers import _TICKET, _accepted_payload, _verdict_payload
from tests.conftest import _cmd

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result


# The user-facing strings, copied verbatim from the approved plan (#2319).
_WARN = (
    "warning: new_voided_entries entry matched no accepted finding: "
    "severity={severity} file={file} summary={summary!r}"
)
_REFUSE = (
    "error: unmatched new_voided_entries: {n}; exiting 1. The verdict JSON on "
    "stdout is complete. Copy severity, file, summary and evidence verbatim "
    "from the finding and re-run, or pass --allow-unmatched-voided to accept "
    "them."
)
_ALLOWED = (
    "note: unmatched new_voided_entries: {n}; continuing because "
    "--allow-unmatched-voided was passed."
)
_ALLOW_HELP = (
    "Exit 0 even when a new_voided_entries entry matches no accepted finding "
    "in this verdict (the default is to print the full JSON and then exit 1). "
    "Each unmatched entry is still warned about on stderr and counted in "
    "verdict.unmatched_voided_count, and is also written to "
    "--voided-findings-out. Use only to deliberately void a finding this "
    "verdict does not carry."
)
_OUT_HELP = (
    "Also render the merged voided-findings record to this path, as a "
    "postable '## Voided Review Findings' ticket comment. Nothing is written "
    "when there is no void to record. When an unmatched new entry makes the "
    "command exit 1, the record is still written first and holds the prior "
    "voids plus only the new entries that matched."
)
_SKILL_STEP_3_5 = (
    "- A non-zero exit is a hard pipeline error, same as step 3. This call "
    'always passes `"new_voided_entries": []`, so it can never exit 1 for an '
    "unmatched new entry (#2319) and the printed `verdict.unmatched_voided_count` "
    "is always 0 here; that count can only become non-zero in the adjudication "
    "step 5 below."
)
_SKILL_STEP_5 = (
    "Then post (or update) that file's contents as a ticket comment. It already "
    "carries its own `## Voided Review Findings` header and the "
    "machine-readable sentinel — post it verbatim; do NOT re-wrap it. This runs "
    "**regardless of whether Stage 3 continues to Stage 4 or exits `blocked`**: "
    "an exit is exactly the path where the next pass re-derives the finding. "
    "**An exit 1 whose stdout still parses as `{verdict, adjudications}` and "
    "whose stderr ends with `error: unmatched new_voided_entries: <N>; ...` is "
    "not a pipeline error (#2319):** it means one or more `new_voided_entries` "
    "matched no accepted finding in that verdict, and stderr names each as "
    "`warning: new_voided_entries entry matched no accepted finding: "
    "severity=<S> file=<F> summary=<...>`. An exit 1 with `field.path: message` "
    "lines on stderr and no stdout JSON remains a hard pipeline error, same as "
    "step 3. The full JSON is still printed, and when there is anything to "
    "record `.cw/voided-findings-comment.md` is still written before the exit, "
    "holding the prior voids plus only the entries that matched — post it as "
    "above. When the prior voids and matched entries are both empty nothing is "
    "written, so do not post a stale file from an earlier run. Then fix each "
    "named entry by copying `severity`/`file`/`summary`/`evidence` verbatim off "
    "the finding it settles and re-run, or re-run with `--allow-unmatched-voided` "
    "only when you are deliberately voiding a finding this verdict does not "
    "carry (that run also records the unmatched entries in the file). Append "
    '`"voided findings recorded: <N>"` to `friction_highlights`, and when the '
    "printed `verdict.unmatched_voided_count` is > 0 also append "
    '`"voided_unmatched_count: <N>"`.'
)
_UNMATCHED_SUMMARY = "a different bug"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _voided_payload(**overrides: object) -> dict[str, Any]:
    """A raw ``VoidedFinding`` dict lined up with ``_accepted_payload``."""
    payload: dict[str, Any] = {
        "severity": "MUST_FIX",
        "file": "src/cw/foo.py",
        "summary": "Bug here",
        "evidence": "def broken():",
        "operator_comment_id": "mattwwarren@2026-08-11T02:43:30Z",
        "operator_comment_excerpt": "intentional; do not re-raise",
        "voided_at": "2026-08-11T02:43:30Z",
        "original_rationale": "deliberate design choice",
    }
    payload.update(overrides)
    return payload


def _check_voided_payload(**overrides: object) -> dict[str, Any]:
    """The ``check-voided`` request envelope around one ``Bug here`` finding."""
    payload: dict[str, Any] = {
        "verdict": _verdict_payload(_accepted_payload(line_start=2, line_end=2)),
        "ticket_id": _TICKET,
        "comment_bodies": [],
        "new_voided_entries": [],
    }
    payload.update(overrides)
    return payload


def _invoke(runner: CliRunner, payload: dict[str, Any], *options: str) -> Result:
    """Run ``cw review check-voided [options] -`` with *payload* on stdin."""
    return runner.invoke(
        main, ["review", "check-voided", *options, "-"], input=json.dumps(payload)
    )


def _prior_block(*entries: dict[str, Any]) -> str:
    """A prior pass's posted voided-findings comment holding *entries*."""
    return render_voided_findings_block(
        [VoidedFinding.model_validate(entry) for entry in entries]
    )


def _unmatched_warning(**overrides: object) -> str:
    """The expected stderr warning line for one unmatched ``_voided_payload``."""
    entry = _voided_payload(summary=_UNMATCHED_SUMMARY, **overrides)
    return _WARN.format(
        severity=entry["severity"], file=entry["file"], summary=entry["summary"]
    )


def _voided_events() -> int:
    return len(read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED]))


class TestReviewCheckVoidedCommand:
    """#1814: ``cw review check-voided`` is the Claude path's suppression hop."""

    def test_existing_sentinel_comment_suppresses_a_re_derived_finding(
        self, runner: CliRunner
    ) -> None:
        body = render_voided_findings_block(
            [VoidedFinding.model_validate(_voided_payload())]
        )
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(_check_voided_payload(comment_bodies=["prose", body])),
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["verdict"]["blocking"] is False
        assert out["verdict"]["must_fix"] == []
        assert out["verdict"]["accepted"][0]["disposition"] == "rejected"
        assert [a["outcome"] for a in out["adjudications"]] == ["reject"]
        assert out["adjudications"][0]["rationale"].strip()
        # Identity fields come off the matched FINDING, not the void, so a
        # later `cw review adjudicate` pass over the same array still matches.
        assert out["adjudications"][0]["line_start"] == 2

    def test_new_entries_suppress_without_any_prior_comment(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(
                _check_voided_payload(new_voided_entries=[_voided_payload()])
            ),
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["verdict"]["accepted"][0]["disposition"] == "rejected"

    def test_emitted_event_correlates_to_the_payload_ticket_id(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(
                _check_voided_payload(new_voided_entries=[_voided_payload()])
            ),
        )

        assert result.exit_code == 0, result.output
        events = read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED])
        assert len(events) == 1
        assert events[0].correlation_id == _TICKET

    def test_no_match_leaves_the_verdict_blocking(self, runner: CliRunner) -> None:
        # #2319: an unmatched NEW entry now exits 1 (it used to exit 0), with
        # the full JSON still on stdout.
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(
                _check_voided_payload(
                    new_voided_entries=[_voided_payload(summary=_UNMATCHED_SUMMARY)]
                )
            ),
        )

        assert result.exit_code == 1, result.output
        out = json.loads(result.stdout)
        assert out["verdict"]["unmatched_voided_count"] == 1
        assert out["verdict"]["blocking"] is True
        assert out["adjudications"] == []

    def test_malformed_payload_prints_field_path_errors(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps({"ticket_id": "T-1"}),
        )

        assert result.exit_code == 1
        assert "verdict" in result.output

    def test_voided_findings_out_writes_the_merged_block(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "nested" / "voided-findings-comment.md"
        prior = _voided_payload(summary="an earlier void", evidence="def earlier():")
        body = render_voided_findings_block([VoidedFinding.model_validate(prior)])
        result = runner.invoke(
            main,
            [
                "review",
                "check-voided",
                "--voided-findings-out",
                str(out_path),
                "-",
            ],
            input=json.dumps(
                _check_voided_payload(
                    comment_bodies=[body], new_voided_entries=[_voided_payload()]
                )
            ),
        )

        assert result.exit_code == 0, result.output
        merged = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert [entry.summary for entry in merged] == ["an earlier void", "Bug here"]

    def test_voided_findings_out_writes_nothing_when_there_is_nothing_to_record(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "voided-findings-comment.md"
        result = runner.invoke(
            main,
            ["review", "check-voided", "--voided-findings-out", str(out_path), "-"],
            input=json.dumps(_check_voided_payload()),
        )

        assert result.exit_code == 0, result.output
        assert not out_path.exists()

    def test_absent_voided_at_is_stamped_by_the_cli(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The Claude session supplies the judgment; the CLI supplies the clock."""
        out_path = tmp_path / "voided-findings-comment.md"
        entry = _voided_payload()
        del entry["voided_at"]
        result = runner.invoke(
            main,
            ["review", "check-voided", "--voided-findings-out", str(out_path), "-"],
            input=json.dumps(_check_voided_payload(new_voided_entries=[entry])),
        )

        assert result.exit_code == 0, result.output
        written = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert written[0].voided_at != ""


class TestCheckVoidedUnmatchedNewEntries:
    """#2319: a ``new_voided_entries`` anchor that matches nothing is surfaced."""

    def test_unmatched_new_entry_is_reported_and_exits_nonzero(
        self, runner: CliRunner
    ) -> None:
        payload = _check_voided_payload(
            new_voided_entries=[_voided_payload(summary=_UNMATCHED_SUMMARY)]
        )

        result = _invoke(runner, payload)

        assert result.exit_code == 1
        out = json.loads(result.stdout)
        assert set(out) == {"verdict", "adjudications"}
        assert out["verdict"]["unmatched_voided_count"] == 1
        assert result.stderr.splitlines() == [
            _WARN.format(
                severity="MUST_FIX", file="src/cw/foo.py", summary=_UNMATCHED_SUMMARY
            ),
            _REFUSE.format(n=1),
        ]

    def test_matched_new_entry_exits_clean(self, runner: CliRunner) -> None:
        payload = _check_voided_payload(new_voided_entries=[_voided_payload()])

        result = _invoke(runner, payload)

        assert result.exit_code == 0, result.output
        assert result.stderr == ""
        out = json.loads(result.stdout)
        assert out["verdict"]["unmatched_voided_count"] == 0
        assert out["verdict"]["accepted"][0]["disposition"] == "rejected"

    def test_previously_recorded_void_matching_nothing_is_not_counted(
        self, runner: CliRunner
    ) -> None:
        body = _prior_block(_voided_payload(summary=_UNMATCHED_SUMMARY))
        payload = _check_voided_payload(comment_bodies=[body])

        result = _invoke(runner, payload)

        assert result.exit_code == 0, result.output
        assert result.stderr == ""
        assert json.loads(result.stdout)["verdict"]["unmatched_voided_count"] == 0

    def test_allow_unmatched_voided_flag_exits_zero_but_still_reports(
        self, runner: CliRunner
    ) -> None:
        payload = _check_voided_payload(
            new_voided_entries=[_voided_payload(summary=_UNMATCHED_SUMMARY)]
        )

        result = _invoke(runner, payload, "--allow-unmatched-voided")

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["verdict"]["unmatched_voided_count"] == 1
        assert result.stderr.splitlines() == [
            _unmatched_warning(),
            _ALLOWED.format(n=1),
        ]

    @pytest.mark.parametrize(
        ("finding_overrides", "entry_overrides", "expected"),
        [
            ({}, {"severity": "SHOULD_FIX"}, 1),
            ({}, {"file": "src/cw/bar.py"}, 1),
            ({}, {"summary": "Bug there"}, 1),
            ({}, {"evidence": "def fixed():"}, 1),
            # Position is not the identity: the finding's code moved.
            ({"line_start": 40, "line_end": 40}, {}, 0),
            ({}, {"summary": "Bug \n  here", "evidence": "  def   broken():"}, 0),
            # Whitespace collapse only: a leading `-` is content, not a marker.
            ({}, {"evidence": "-def broken():"}, 1),
        ],
        ids=[
            "severity",
            "file",
            "summary",
            "evidence",
            "line_moved",
            "whitespace",
            "leading_dash",
        ],
    )
    def test_unmatched_anchor_uses_content_fingerprint_semantics(
        self,
        runner: CliRunner,
        finding_overrides: dict[str, object],
        entry_overrides: dict[str, object],
        expected: int,
    ) -> None:
        accepted = _accepted_payload(
            **{"line_start": 2, "line_end": 2, **finding_overrides}
        )
        payload = _check_voided_payload(
            verdict=_verdict_payload(accepted),
            new_voided_entries=[_voided_payload(**entry_overrides)],
        )

        result = _invoke(runner, payload, "--allow-unmatched-voided")

        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)
        assert out["verdict"]["unmatched_voided_count"] == expected

    @pytest.mark.parametrize(
        ("entries", "expected_warnings"),
        [
            (
                [
                    _voided_payload(summary=_UNMATCHED_SUMMARY),
                    _voided_payload(summary=_UNMATCHED_SUMMARY),
                ],
                [_unmatched_warning()],
            ),
            # The dedupe key is the matcher's normalized fingerprint, not the
            # raw strings: the first-seen entry's own text is what is named.
            (
                [
                    _voided_payload(
                        summary="a different  bug", evidence="def  broken():"
                    ),
                    _voided_payload(summary=_UNMATCHED_SUMMARY),
                    _voided_payload(
                        summary="a different\nbug", evidence=" def broken(): "
                    ),
                ],
                [
                    _WARN.format(
                        severity="MUST_FIX",
                        file="src/cw/foo.py",
                        summary="a different  bug",
                    )
                ],
            ),
            (
                [
                    _voided_payload(summary=_UNMATCHED_SUMMARY),
                    _voided_payload(summary=_UNMATCHED_SUMMARY, severity="SHOULD_FIX"),
                ],
                [_unmatched_warning(), _unmatched_warning(severity="SHOULD_FIX")],
            ),
        ],
        ids=["identical", "whitespace_variants", "severity_differs"],
    )
    def test_duplicate_unmatched_entries_collapse_to_one_anchor(
        self,
        runner: CliRunner,
        entries: list[dict[str, Any]],
        expected_warnings: list[str],
    ) -> None:
        payload = _check_voided_payload(new_voided_entries=entries)

        result = _invoke(runner, payload)

        assert result.exit_code == 1
        count = len(expected_warnings)
        assert json.loads(result.stdout)["verdict"]["unmatched_voided_count"] == count
        assert result.stderr.splitlines() == [
            *expected_warnings,
            _REFUSE.format(n=count),
        ]

    def test_duplicate_matched_new_entries(self, runner: CliRunner) -> None:
        payload = _check_voided_payload(
            new_voided_entries=[_voided_payload(), _voided_payload()]
        )

        result = _invoke(runner, payload)

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["verdict"]["unmatched_voided_count"] == 0
        assert _voided_events() == 1

    def test_new_entry_duplicating_a_prior_void_matched(
        self, runner: CliRunner
    ) -> None:
        payload = _check_voided_payload(
            comment_bodies=[_prior_block(_voided_payload())],
            new_voided_entries=[_voided_payload()],
        )

        result = _invoke(runner, payload)

        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)
        assert out["verdict"]["unmatched_voided_count"] == 0
        assert len(out["adjudications"]) == 1

    def test_new_entry_duplicating_a_prior_void_unmatched_still_counted(
        self, runner: CliRunner
    ) -> None:
        unmatched = _voided_payload(summary=_UNMATCHED_SUMMARY)
        payload = _check_voided_payload(
            comment_bodies=[_prior_block(unmatched)], new_voided_entries=[unmatched]
        )

        result = _invoke(runner, payload)

        assert result.exit_code == 1
        assert json.loads(result.stdout)["verdict"]["unmatched_voided_count"] == 1

    def test_mixed_matched_and_unmatched(self, runner: CliRunner) -> None:
        payload = _check_voided_payload(
            new_voided_entries=[
                _voided_payload(),
                _voided_payload(summary=_UNMATCHED_SUMMARY),
            ]
        )

        result = _invoke(runner, payload)

        assert result.exit_code == 1
        out = json.loads(result.stdout)
        assert out["verdict"]["unmatched_voided_count"] == 1
        assert out["verdict"]["accepted"][0]["disposition"] == "rejected"
        assert len(out["adjudications"]) == 1
        events = read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED])
        assert len(events) == 1
        assert events[0].correlation_id == _TICKET

    @pytest.mark.parametrize(
        "new_entries", [[], [_voided_payload()]], ids=["no_entries", "matched"]
    )
    def test_count_is_computed_fresh_not_carried_from_input(
        self, runner: CliRunner, new_entries: list[dict[str, Any]]
    ) -> None:
        verdict = _verdict_payload(
            _accepted_payload(line_start=2, line_end=2),
            unmatched_voided_count=7,
            unmatched_adjudication_count=3,
        )
        payload = _check_voided_payload(verdict=verdict, new_voided_entries=new_entries)

        result = _invoke(runner, payload)

        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)
        assert out["verdict"]["unmatched_voided_count"] == 0
        assert out["verdict"]["unmatched_adjudication_count"] == 3

    def test_check_voided_output_feeds_adjudicate_without_losing_the_count(
        self, runner: CliRunner
    ) -> None:
        payload = _check_voided_payload(
            new_voided_entries=[_voided_payload(summary=_UNMATCHED_SUMMARY)]
        )
        checked = _invoke(runner, payload, "--allow-unmatched-voided")
        assert checked.exit_code == 0, checked.output

        result = runner.invoke(
            main, ["review", "adjudicate", "-"], input=checked.stdout
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["unmatched_voided_count"] == 1

    def test_allow_unmatched_voided_is_documented_in_help(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["review", "check-voided", "--help"])

        assert result.exit_code == 0, result.output
        collapsed = " ".join(result.output.split())
        assert _ALLOW_HELP in collapsed
        assert _OUT_HELP in collapsed


class TestCheckVoidedRecordOnRefuse:
    """#2319: ``--voided-findings-out`` is written BEFORE the refused exit."""

    def _matched_and_unmatched(self, **overrides: object) -> dict[str, Any]:
        prior = _voided_payload(summary="an earlier void", evidence="def earlier():")
        payload = _check_voided_payload(
            comment_bodies=[_prior_block(prior)],
            new_voided_entries=[
                _voided_payload(),
                _voided_payload(summary=_UNMATCHED_SUMMARY),
            ],
        )
        payload.update(overrides)
        return payload

    def test_refused_exit_writes_matched_only_record(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "voided-findings-comment.md"

        result = _invoke(
            runner,
            self._matched_and_unmatched(),
            "--voided-findings-out",
            str(out_path),
        )

        assert result.exit_code == 1
        assert set(json.loads(result.stdout)) == {"verdict", "adjudications"}
        assert out_path.exists()
        written = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert [entry.summary for entry in written] == ["an earlier void", "Bug here"]

    def test_refused_exit_with_nothing_matched_writes_no_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "nested" / "voided-findings-comment.md"
        payload = _check_voided_payload(
            new_voided_entries=[_voided_payload(summary=_UNMATCHED_SUMMARY)]
        )

        result = _invoke(runner, payload, "--voided-findings-out", str(out_path))

        assert result.exit_code == 1
        assert not out_path.exists()
        assert not out_path.parent.exists()

    def test_allowed_run_records_the_unmatched_entry_too(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "voided-findings-comment.md"

        result = _invoke(
            runner,
            self._matched_and_unmatched(),
            "--allow-unmatched-voided",
            "--voided-findings-out",
            str(out_path),
        )

        assert result.exit_code == 0, result.output
        written = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert [entry.summary for entry in written] == [
            "an earlier void",
            "Bug here",
            _UNMATCHED_SUMMARY,
        ]

    def test_refused_record_keeps_a_prior_void_the_unmatched_new_entry_duplicates(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "voided-findings-comment.md"
        unmatched = _voided_payload(summary=_UNMATCHED_SUMMARY)
        payload = _check_voided_payload(
            comment_bodies=[_prior_block(unmatched)], new_voided_entries=[unmatched]
        )

        result = _invoke(runner, payload, "--voided-findings-out", str(out_path))

        assert result.exit_code == 1
        written = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert [entry.summary for entry in written] == [_UNMATCHED_SUMMARY]

    def test_refused_exit_still_emits_events_and_a_retry_re_emits(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "voided-findings-comment.md"
        payload = self._matched_and_unmatched()

        first = _invoke(runner, payload, "--voided-findings-out", str(out_path))
        assert first.exit_code == 1
        assert _voided_events() == 1

        retry = _invoke(runner, payload, "--voided-findings-out", str(out_path))
        assert retry.exit_code == 1
        assert _voided_events() == 2


class TestWriteVoidedRecord:
    """#2319: the ``--voided-findings-out`` write, extracted for the refuse path."""

    def test_writes_the_rendered_block_and_creates_parents(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "a" / "b" / "out.md"
        entries = [VoidedFinding.model_validate(_voided_payload())]

        _write_voided_record(path, entries)

        assert path.read_text(encoding="utf-8") == render_voided_findings_block(entries)

    def test_empty_render_writes_no_file_and_no_parent(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "out.md"

        _write_voided_record(path, [])

        assert not path.exists()
        assert not path.parent.exists()

    def test_dedupes_via_the_renderer(self, tmp_path: Path) -> None:
        path = tmp_path / "out.md"
        entry = VoidedFinding.model_validate(_voided_payload())

        _write_voided_record(path, [entry, entry])

        assert path.read_text(encoding="utf-8") == render_voided_findings_block([entry])

    def test_overwrites_an_existing_file_atomically(self, tmp_path: Path) -> None:
        # A dedicated directory: the autouse fixtures populate tmp_path itself.
        out_dir = tmp_path / "record"
        out_dir.mkdir()
        path = out_dir / "out.md"
        path.write_text("stale content from an earlier run\n", encoding="utf-8")
        entries = [VoidedFinding.model_validate(_voided_payload())]

        _write_voided_record(path, entries)

        assert path.read_text(encoding="utf-8") == render_voided_findings_block(entries)
        # No temp file left behind by the write-then-rename.
        assert list(out_dir.iterdir()) == [path]


def _verdict(*accepted: dict[str, Any]) -> ReviewVerdict:
    return ReviewVerdict.model_validate(_verdict_payload(*accepted))


def _entry(**overrides: object) -> VoidedFinding:
    return VoidedFinding.model_validate(_voided_payload(**overrides))


class TestPartitionNewEntries:
    """#2319: new entries split into (matched, deduped unmatched)."""

    def test_splits_matched_from_unmatched_in_order(self) -> None:
        verdict = _verdict(_accepted_payload(line_start=2, line_end=2))
        matched = _entry()
        unmatched = _entry(summary=_UNMATCHED_SUMMARY)

        result = _partition_new_entries(verdict, [unmatched, matched, matched])

        assert result == ([matched, matched], [unmatched])

    def test_unmatched_is_deduped_by_fingerprint_first_seen(self) -> None:
        verdict = _verdict(_accepted_payload(line_start=2, line_end=2))
        first = _entry(summary="a different  bug")
        variant = _entry(summary="a different\nbug")
        other = _entry(summary="another bug")

        result = _partition_new_entries(verdict, [first, other, variant])

        assert result == ([], [first, other])

    def test_no_entries(self) -> None:
        verdict = _verdict(_accepted_payload(line_start=2, line_end=2))

        assert _partition_new_entries(verdict, []) == ([], [])

    def test_no_accepted_findings_makes_everything_unmatched(self) -> None:
        entries = [_entry(), _entry(summary=_UNMATCHED_SUMMARY)]

        assert _partition_new_entries(_verdict(), entries) == ([], entries)


class TestCheckVoidedSkillDocs:
    """#2319: ``/auto-dev-review`` documents the unmatched-entry exit."""

    def test_step_3_5_says_it_never_hits_the_unmatched_exit(self) -> None:
        assert _SKILL_STEP_3_5 in _cmd("auto-dev-review.md")

    def test_step_5_documents_the_exit_and_the_friction_line(self) -> None:
        content = _cmd("auto-dev-review.md")

        assert _SKILL_STEP_5 in content
        assert "--allow-unmatched-voided" in content
        assert "warning: new_voided_entries entry matched no accepted finding" in (
            content
        )
        assert '"voided_unmatched_count: <N>"' in content
