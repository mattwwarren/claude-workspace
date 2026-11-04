"""``cw review converge delta-confirm``: the cw-recorded delta round (#2918).

The reviewer runs through the real ``_run_round`` seam of ``cw review
confirm`` against scripted ``FakeCodexRunner`` results (the hoisted
:class:`~tests._review_confirm_harness.Harness`), over a real feature branch
whose merge of ``origin/main`` overlaps its own file. The refusals that need a
git shape real git cannot cheaply build (an octopus or non-default merge, an
unreadable HEAD) answer git from the default-deny :class:`ScriptedMergeGit`.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from cw import _git
from cw.cli.review.confirm import _RoundOutcome
from cw.codex_review import make_codex_blocked
from cw.codex_runner import CodexRunResult
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    DeltaSpec,
    load_ledger,
    round_verdict_path,
)
from tests._codex_review_helpers import _finding_payload, _ok_result
from tests._review_confirm_harness import Harness, make_harness
from tests._review_convergence_helpers import (
    OVERLAP_FILE,
    TICKET_ID,
    OverlapMerge,
    ScriptedMergeGit,
    branch_with_overlap_merge,
    codex_audit_fixture,
    consolidated_verdict,
    delta_ledger,
    non_default_parent_script,
    octopus_merge_script,
    write_ledger,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from cw.review_convergence import ConvergenceLedger

_CLIENT = "acme"
_SESSION = "sess-delta"
_ARGV = ("review", "converge", "delta-confirm")
_FROZEN = "2026-11-04T12:00:00+00:00"
_UNAVAILABLE = 3
_ROUND_FAILED = 4
_ON = {"convergence": "clean", "default_merge_delta_confirm": True}

needs_codex = pytest.mark.binary_on_path("codex")


def _harness(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    review: dict[str, object],
) -> Harness:
    harness = make_harness(
        tmp_path,
        make_git_repo_with_origin,
        monkeypatch,
        ticket=TICKET_ID,
        client=_CLIENT,
        session=_SESSION,
        argv=_ARGV,
        client_extra={"review": review},
    )
    monkeypatch.setattr("cw.cli.review.converge_delta._utc_now_iso", lambda: _FROZEN)
    return harness


@pytest.fixture
def harness(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    return _harness(tmp_path, make_git_repo_with_origin, monkeypatch, _ON)


def _merged(harness: Harness, tmp_path: Path, **ledger: Any) -> OverlapMerge:
    """The overlap merge on the harness branch, with a ledger converged before it."""
    merged = branch_with_overlap_merge(harness.repo, tmp_path / "main-clone")
    write_ledger(harness.repo, delta_ledger(clean=merged.clean, **ledger))
    return merged


def _ledger_bytes(harness: Harness) -> bytes:
    return (harness.repo / LEDGER_RELATIVE_PATH).read_bytes()


def _stored(harness: Harness) -> ConvergenceLedger:
    ledger = load_ledger(harness.repo / LEDGER_RELATIVE_PATH)
    assert ledger is not None
    return ledger


def _out(result: Result) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _merge_must_fix() -> CodexRunResult:
    """A MUST_FIX anchored on the line the merge brought in."""
    return _ok_result(
        findings=[
            _finding_payload(
                severity="MUST_FIX",
                file=OVERLAP_FILE,
                line_start=20,
                line_end=20,
                summary="The merged edit breaks the branch's change",
                evidence="main edit",
            )
        ]
    )


def _capacity() -> CodexRunResult:
    return CodexRunResult(
        returncode=1,
        stdout=codex_audit_fixture("capacity_turn_failed.jsonl"),
        stderr="",
    )


@needs_codex
class TestRecordedRound:
    def test_a_clean_round_moves_the_clean_sha_to_head(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        merged = _merged(harness, tmp_path)
        calls = harness.spy_run_review()

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 0, result.output
        out = _out(result)
        assert out["decision"] == "clean"
        assert out["overlapping_files"] == [OVERLAP_FILE]
        ledger = _stored(harness)
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == merged.head == harness.head()
        assert ledger.rounds[-1].kind == "delta"
        assert ledger.rounds[-1].backend == "codex"
        assert ledger.rounds[-1].recorded_at == _FROZEN
        verdict_file = round_verdict_path(
            harness.repo / LEDGER_RELATIVE_PATH, ledger.rounds[-1].round
        )
        assert verdict_file.exists()
        branch_base = git_in(harness.repo, "merge-base", merged.clean, merged.main_tip)
        assert calls[0]["delta"] == DeltaSpec(
            from_sha=merged.clean,
            to_sha=merged.head,
            paths=(OVERLAP_FILE,),
            branch_base=branch_base,
        )

    def test_findings_reopen_the_loop(self, harness: Harness, tmp_path: Path) -> None:
        merged = _merged(harness, tmp_path)

        result, _ = harness.invoke(
            harness.results(_merge_must_fix(), rest=_ok_result())
        )

        assert result.exit_code == 0, result.output
        assert _out(result)["decision"] == "continue"
        ledger = _stored(harness)
        assert ledger.status == "in_progress"
        assert ledger.last_clean_sha == merged.clean
        assert ledger.fix_cycles_used == 1

    @pytest.mark.parametrize(
        ("results", "code"),
        [
            pytest.param("capacity", _UNAVAILABLE, id="backend-down"),
            pytest.param("unparseable", _ROUND_FAILED, id="round-failed"),
        ],
    )
    def test_exit_three_and_four_record_nothing(
        self, harness: Harness, tmp_path: Path, results: str, code: int
    ) -> None:
        _merged(harness, tmp_path)
        before = _ledger_bytes(harness)
        one = (
            _capacity()
            if results == "capacity"
            else dataclasses.replace(_ok_result(), output_file_content="not json")
        )

        result, _ = harness.invoke(harness.all_roles(one))

        assert result.exit_code == code, result.output
        assert _ledger_bytes(harness) == before

    def test_head_moving_during_the_review_is_a_cwerror(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _merged(harness, tmp_path)
        before = _ledger_bytes(harness)
        harness.spy_run_review(after=lambda repo: commit_tracked_file(repo, "late.py"))

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 1, result.output
        assert "delta-confirm refused: HEAD moved" in result.output
        assert _ledger_bytes(harness) == before

    def test_a_ledger_written_during_the_review_is_not_overwritten(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        """Another converge command (settle, finalize, init) wrote the ledger
        while the unbounded review ran: the stale snapshot must not clobber it,
        and no verdict file is written under a stale round number."""
        merged = _merged(harness, tmp_path)
        concurrent = delta_ledger(clean=merged.clean).model_copy(
            update={"fix_cycles_used": 2}
        )

        def _write(repo: Path) -> None:
            write_ledger(repo, concurrent)

        harness.spy_run_review(after=_write)

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 1, result.output
        assert (
            "delta-confirm refused: ledger changed during the review" in result.output
        )
        assert "nothing recorded" in result.output
        stored = _stored(harness)
        assert stored == concurrent
        assert not round_verdict_path(
            harness.repo / LEDGER_RELATIVE_PATH, len(concurrent.rounds) + 1
        ).exists()

    def test_a_worktree_mutated_during_the_call_is_exit_four(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _merged(harness, tmp_path)
        before = _ledger_bytes(harness)

        def _mutate(repo: Path) -> None:
            (repo / "stray.txt").write_text("x\n", encoding="utf-8")

        harness.spy_run_review(after=_mutate)

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == _ROUND_FAILED, result.output
        assert _out(result)["cause"] == "worktree_mutated"
        assert _ledger_bytes(harness) == before

    def test_no_fetch_is_issued(
        self, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The network boundary: delta-confirm adds no network git call."""
        _merged(harness, tmp_path)
        argvs: list[list[str]] = []
        real = subprocess.run

        def _record(argv: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(argv, list) and argv[:1] == ["git"]:
                argvs.append(list(argv))
            return real(argv, *args, **kwargs)

        monkeypatch.setattr(_git.subprocess, "run", _record)

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 0, result.output
        assert argvs
        assert not [argv for argv in argvs if "fetch" in argv]

    def test_a_codex_reviewer_gets_the_default_reasoning_effort(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _merged(harness, tmp_path)
        calls = harness.spy_run_review()

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 0, result.output
        assert calls[0]["reasoning_effort"] == "high"


@needs_codex
def test_a_verdict_for_another_sha_is_refused(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _merged(harness, tmp_path)
    before = _ledger_bytes(harness)
    blocked = make_codex_blocked(
        ticket_id=TICKET_ID,
        worktree=harness.repo,
        reason="codex_must_fix_findings",
        retry_eligible=False,
        details="",
    )
    outcome = _RoundOutcome(blocked, consolidated_verdict(sha="f" * 40), None)
    monkeypatch.setattr(
        "cw.cli.review.converge_delta.run_backend_round", lambda **_kwargs: outcome
    )

    result, _ = harness.invoke([])

    assert result.exit_code == 1, result.output
    assert "the verdict reviewed " + "f" * 40 in result.output
    assert _ledger_bytes(harness) == before


def _assert_refused(harness: Harness, token: str, before: bytes | None) -> None:
    calls = harness.spy_run_review()
    result, _ = harness.invoke([])
    assert result.exit_code == 1, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines == [f"Error: delta-confirm refused: {token}"]
    assert calls == []
    if before is not None:
        assert _ledger_bytes(harness) == before


@needs_codex
class TestRefusals:
    def test_a_strict_client_is_tolerance_off(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        strict = _ON | {"default_merge_tolerance": False}
        harness = _harness(tmp_path, make_git_repo_with_origin, monkeypatch, strict)
        _merged(harness, tmp_path)
        _assert_refused(harness, "tolerance_off", _ledger_bytes(harness))

    def test_a_context_claiming_the_flag_is_not_believed(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cw-context says on, the live client says off: refused."""
        harness = _harness(
            tmp_path, make_git_repo_with_origin, monkeypatch, {"convergence": "clean"}
        )
        context = harness.repo / HOOK_CONTEXT_RELATIVE_PATH
        context.parent.mkdir(parents=True, exist_ok=True)
        context.write_text(
            json.dumps(
                {
                    "schema_version": 16,
                    "world_state_snapshot": {"origin_main_branch": "main"},
                    "review": {
                        "convergence": "clean",
                        "default_merge_delta_confirm": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        _merged(harness, tmp_path)
        _assert_refused(harness, "delta_disabled", _ledger_bytes(harness))

    def test_no_merge_after_the_clean_sha_is_not_an_overlap_merge(
        self, harness: Harness
    ) -> None:
        write_ledger(harness.repo, delta_ledger(clean=harness.head()))
        _assert_refused(harness, "not_overlap_merge", _ledger_bytes(harness))

    def test_a_hand_edited_merge_is_not_textual(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        merged = branch_with_overlap_merge(harness.repo, tmp_path / "main-clone")
        git_in(harness.repo, "reset", "--hard", "HEAD~1")
        git_in(harness.repo, "merge", "--no-commit", "--no-ff", merged.main_tip)
        shared = harness.repo / OVERLAP_FILE
        shared.write_text(
            shared.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8"
        )
        git_in(harness.repo, "add", OVERLAP_FILE)
        git_in(harness.repo, "commit", "--no-edit")
        write_ledger(harness.repo, delta_ledger(clean=merged.clean))
        _assert_refused(harness, "not_textual_merge", _ledger_bytes(harness))

    def test_a_codex_loop_ledger_is_not_confirmable(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _merged(harness, tmp_path, loop_backend="codex", confirm="claude")
        _assert_refused(harness, "ledger_not_confirmable", _ledger_bytes(harness))

    @pytest.mark.parametrize(
        ("script", "token"),
        [
            pytest.param(octopus_merge_script, "not_textual_merge", id="octopus"),
            pytest.param(non_default_parent_script, "not_overlap_merge", id="other"),
        ],
    )
    def test_scripted_merge_shapes(
        self,
        harness: Harness,
        monkeypatch: pytest.MonkeyPatch,
        script: Callable[[str, str, str], dict[tuple[str, ...], Any]],
        token: str,
    ) -> None:
        clean, head, default = "c" * 40, "d" * 40, "e" * 40
        write_ledger(harness.repo, delta_ledger(clean=clean))
        before = _ledger_bytes(harness)
        answers = script(clean, head, default)
        answers["rev-parse", "HEAD"] = f"{head}\n"
        ScriptedMergeGit(answers).install(monkeypatch)
        _assert_refused(harness, token, before)

    def test_an_unreadable_head_is_refused(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(harness.repo, delta_ledger(clean="c" * 40))
        before = _ledger_bytes(harness)
        ScriptedMergeGit({("rev-parse", "HEAD"): 128}).install(monkeypatch)
        _assert_refused(harness, "unreadable git state", before)

    def test_a_ledger_of_another_ticket_is_a_cwerror(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        merged = branch_with_overlap_merge(harness.repo, tmp_path / "main-clone")
        write_ledger(
            harness.repo, delta_ledger(clean=merged.clean, ticket_id="OTHER-1")
        )
        result, _ = harness.invoke([])
        assert result.exit_code == 1, result.output
        assert "belongs to ticket 'OTHER-1'" in result.output


def test_a_missing_binary_is_exit_three_and_records_nothing(
    harness: Harness, tmp_path: Path
) -> None:
    """``codex`` is absent by default under the optional-binary guard."""
    _merged(harness, tmp_path)
    before = _ledger_bytes(harness)

    result, _ = harness.invoke([])

    assert result.exit_code == _UNAVAILABLE, result.output
    assert _out(result)["cause"] == "binary_missing"
    assert _ledger_bytes(harness) == before
