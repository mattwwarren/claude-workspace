"""``cw review converge delta-confirm``: the cw-recorded resolution round (#2980).

The reviewer runs through the real ``run_backend_round`` seam of ``cw review
confirm`` against scripted ``FakeCodexRunner`` / ``FakeClaudeRunner`` results
(the shared :class:`~tests._review_confirm_harness.Harness`), over a real
feature branch whose merge of ``origin/main`` carries a hand-resolved
conflict next to hunks each side made alone. Refusals that need a git shape
real git cannot cheaply build answer git from the default-deny
:class:`ScriptedMergeGit`.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from cw import _git
from cw.claude_review import ClaudeRunResult
from cw.cli import main
from cw.cli.review.confirm import RoundOutcome
from cw.cli.review.converge_delta import delta_defer_rationale
from cw.codex_review import make_codex_blocked
from cw.codex_runner import CodexRunResult
from cw.merge_probe import merge_tree
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    ResolutionSpec,
    load_ledger,
    record_delta_round,
    round_verdict_path,
)
from tests._claude_review_helpers import review_envelope
from tests._codex_review_helpers import _doc_json, _finding_payload, _ok_result
from tests._review_confirm_harness import Harness, make_harness
from tests._review_convergence_helpers import (
    BRANCH_ONLY_TEXT,
    MAIN_ONLY_TEXT,
    RESOLUTION_FILE,
    RESOLUTION_MARKER,
    TICKET_ID,
    ResolutionFixture,
    ScriptedMergeGit,
    branch_with_overlap_merge,
    branch_with_resolution_and_unrelated_hunks,
    codex_audit_fixture,
    consolidated_verdict,
    engine_pass_ledger,
    fresh_ledger,
    non_default_parent_resolution_script,
    octopus_resolution_script,
    operator_finding,
    record,
    resolution_ledger,
    round_finding,
    write_ledger,
)
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from cw.review_convergence import ConvergenceLedger
    from tests._review_convergence_helpers import GitScript

_CLIENT = "acme"
_SESSION = "sess-delta"
_ARGV = ("review", "converge", "delta-confirm")
_FROZEN = "2026-11-04T12:00:00+00:00"
_UNAVAILABLE = 3
_ROUND_FAILED = 4
_ON = {"convergence": "clean"}
#: A claude-native loop confirmed by codex, and a codex loop confirmed by
#: Claude: the rule is the same on every loop backend.
_CODEX_CONFIRM = {"loop_backend": "claude-native", "confirm": "codex"}
_CLAUDE_CONFIRM = {"loop_backend": "codex", "confirm": "claude"}

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


def _resolved(harness: Harness, tmp_path: Path, **ledger: Any) -> ResolutionFixture:
    """The resolution merge on the harness branch, with a ledger converged before it."""
    found = branch_with_resolution_and_unrelated_hunks(
        harness.repo, tmp_path / "main-clone"
    )
    shape = ledger or _CODEX_CONFIRM
    write_ledger(harness.repo, resolution_ledger(clean=found.clean, **shape))
    return found


def _ledger_bytes(harness: Harness) -> bytes:
    return (harness.repo / LEDGER_RELATIVE_PATH).read_bytes()


def _stored(harness: Harness) -> ConvergenceLedger:
    ledger = load_ledger(harness.repo / LEDGER_RELATIVE_PATH)
    assert ledger is not None
    return ledger


def _out(result: Result) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _resolution_finding(severity: str) -> dict[str, object]:
    """A finding of *severity* anchored on the line the resolution wrote."""
    return _finding_payload(
        severity=severity,
        file=RESOLUTION_FILE,
        line_start=1,
        line_end=1,
        summary="The resolution drops main's edit",
        evidence=RESOLUTION_MARKER.strip(),
    )


def _codex_finding(severity: str) -> CodexRunResult:
    return _ok_result(findings=[_resolution_finding(severity)])


def _claude_ok() -> ClaudeRunResult:
    return ClaudeRunResult(returncode=0, stdout=review_envelope(_doc_json()), stderr="")


def _claude_finding(severity: str) -> ClaudeRunResult:
    document = _doc_json(findings=[_resolution_finding(severity)])
    return ClaudeRunResult(returncode=0, stdout=review_envelope(document), stderr="")


def _verify_tip(harness: Harness) -> Result:
    """``cw review converge verify-tip`` against the harness worktree."""
    return harness.cli.invoke(
        main, ["review", "converge", "verify-tip", "--worktree", str(harness.repo)]
    )


def _tip_context(harness: Harness) -> None:
    """The worktree's cw-context naming ``main``, tolerance on (as cw writes it)."""
    context = harness.repo / HOOK_CONTEXT_RELATIVE_PATH
    context.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 15,
        "session_id": _SESSION,
        "ticket_id": TICKET_ID,
        "headless": True,
        "review": {"convergence": "clean", "max_cycles": None},
        "world_state_snapshot": {"origin_main_branch": "main"},
    }
    context.write_text(json.dumps(body), encoding="utf-8")


def _capacity() -> CodexRunResult:
    return CodexRunResult(
        returncode=1,
        stdout=codex_audit_fixture("capacity_turn_failed.jsonl"),
        stderr="",
    )


def _invoke_claude(harness: Harness, results: list[ClaudeRunResult]) -> Result:
    """``delta-confirm`` on a Claude-confirm ledger (no ``--backend`` flag)."""
    harness.patch_claude(results)
    return harness.cli.invoke(main, harness.args())


def _expected_spec(harness: Harness, found: ResolutionFixture) -> ResolutionSpec:
    merge = found.merge
    written = merge_tree(harness.repo, f"{merge}^1", f"{merge}^2")
    assert written.status == "conflict"
    assert written.tree is not None
    return ResolutionSpec(
        merge=merge,
        conflicted_tree=written.tree,
        resolved_tree=git_in(harness.repo, "rev-parse", f"{merge}^{{tree}}"),
        reviewed_sha=found.head,
        branch_base=git_in(harness.repo, "merge-base", f"{merge}^1", f"{merge}^2"),
        branch_tip=git_in(harness.repo, "rev-parse", f"{merge}^1"),
        paths=(RESOLUTION_FILE,),
    )


@needs_codex
class TestCodexRound:
    """A claude-native loop with a codex confirm."""

    def test_a_clean_round_moves_the_clean_sha_to_head(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        found = _resolved(harness, tmp_path)
        calls = harness.spy_run_review()

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 0, result.output
        out = _out(result)
        assert out["decision"] == "clean"
        assert out["resolution_files"] == [RESOLUTION_FILE]
        ledger = _stored(harness)
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == found.head == harness.head()
        assert ledger.rounds[-1].kind == "delta"
        assert ledger.rounds[-1].backend == "codex"
        assert ledger.rounds[-1].recorded_at == _FROZEN
        assert round_verdict_path(
            harness.repo / LEDGER_RELATIVE_PATH, ledger.rounds[-1].round
        ).exists()
        assert calls[0]["resolution"] == _expected_spec(harness, found)

    def test_the_prompt_holds_the_resolution_and_the_branch_context_only(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _resolved(harness, tmp_path)

        result, runner = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 0, result.output
        prompts = [call["stdin"] for call in runner.calls]
        assert prompts
        for prompt in prompts:
            assert isinstance(prompt, str)
            diff, _, after = prompt.partition("## Conflict Resolution Review (#2980)")
            assert f"+{RESOLUTION_MARKER.strip()}" in diff
            assert MAIN_ONLY_TEXT.strip() not in prompt
            assert BRANCH_ONLY_TEXT.strip() not in diff
            assert BRANCH_ONLY_TEXT.strip() in after

    def test_a_clean_round_leaves_verify_tip_accepting_the_tip(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _resolved(harness, tmp_path)
        _tip_context(harness)
        before = _verify_tip(harness)
        assert before.exit_code == 1, before.output
        assert _out(before)["delta_confirm_eligible"] is True

        result, _ = harness.invoke(harness.all_roles(_ok_result()))
        assert result.exit_code == 0, result.output
        assert _out(result)["decision"] == "clean"

        verify = _verify_tip(harness)

        assert verify.exit_code == 0, verify.output
        out = _out(verify)
        assert out["decision"] == "accepted"
        assert "delta_confirm_eligible" not in out
        assert "resolution_files" not in out

    def test_a_should_fix_is_deferred_and_does_not_reopen_the_loop(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        found = _resolved(harness, tmp_path)
        cycles_before = _stored(harness).fix_cycles_used

        result, _ = harness.invoke(
            harness.results(_codex_finding("SHOULD_FIX"), rest=_ok_result())
        )

        assert result.exit_code == 0, result.output
        assert _out(result)["decision"] == "clean"
        ledger = _stored(harness)
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == found.head
        assert ledger.fix_cycles_used == cycles_before
        verdict = json.loads(
            round_verdict_path(
                harness.repo / LEDGER_RELATIVE_PATH, ledger.rounds[-1].round
            ).read_text(encoding="utf-8")
        )
        (accepted,) = verdict["accepted"]
        assert accepted["finding"]["severity"] == "SHOULD_FIX"
        assert accepted["disposition"] == "deferred"
        assert accepted["disposition_detail"] == delta_defer_rationale("codex")

    def test_a_bad_resolution_reopens_the_loop(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        found = _resolved(harness, tmp_path)

        result, _ = harness.invoke(
            harness.results(_codex_finding("MUST_FIX"), rest=_ok_result())
        )

        assert result.exit_code == 0, result.output
        assert _out(result)["decision"] == "continue"
        ledger = _stored(harness)
        assert ledger.status == "in_progress"
        assert ledger.last_clean_sha == found.clean
        assert ledger.fix_cycles_used == 1
        assert ledger.rounds[-1].kind == "delta"

    def test_a_must_fix_on_an_engine_converged_ledger_still_continues(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        # #3042: the engine-confirm router never takes a delta round.
        found = _resolved(harness, tmp_path)
        ledger = engine_pass_ledger(
            fresh_ledger(confirm="codex", ticket_id=TICKET_ID), sha=found.clean
        )
        ledger, report = record(ledger, sha=found.clean, kind="confirm")
        assert report.decision == "clean"
        write_ledger(harness.repo, ledger)

        result, _ = harness.invoke(
            harness.results(_codex_finding("MUST_FIX"), rest=_ok_result())
        )

        assert result.exit_code == 0, result.output
        assert _out(result)["decision"] == "continue"
        stored = _stored(harness)
        assert stored.rounds[-1].kind == "delta"
        assert stored.rounds[-1].engine is False
        assert stored.fix_cycles_used == 1

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
        _resolved(harness, tmp_path)
        before = _ledger_bytes(harness)
        one = (
            _capacity()
            if results == "capacity"
            else dataclasses.replace(_ok_result(), output_file_content="not json")
        )

        result, _ = harness.invoke(harness.all_roles(one))

        assert result.exit_code == code, result.output
        assert _ledger_bytes(harness) == before

    def test_head_moving_during_the_review_is_refused(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _resolved(harness, tmp_path)
        before = _ledger_bytes(harness)
        harness.spy_run_review(after=lambda repo: commit_tracked_file(repo, "late.py"))

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 1, result.output
        assert "delta-confirm refused: HEAD moved" in result.output
        assert _ledger_bytes(harness) == before

    def test_a_ledger_written_during_the_review_is_not_overwritten(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        found = _resolved(harness, tmp_path)
        concurrent = resolution_ledger(clean=found.clean).model_copy(
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
        assert _stored(harness) == concurrent
        assert not round_verdict_path(
            harness.repo / LEDGER_RELATIVE_PATH, len(concurrent.rounds) + 1
        ).exists()

    def test_a_worktree_mutated_during_the_call_is_exit_four(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _resolved(harness, tmp_path)
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
        _resolved(harness, tmp_path)
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
        _resolved(harness, tmp_path)
        calls = harness.spy_run_review()

        result, _ = harness.invoke(harness.all_roles(_ok_result()))

        assert result.exit_code == 0, result.output
        assert calls[0]["reasoning_effort"] == "high"


class TestClaudeRound:
    """A codex loop with a Claude confirm: no ``--backend`` flag is needed."""

    def test_a_clean_round_converges_at_head(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        found = _resolved(harness, tmp_path, **_CLAUDE_CONFIRM)
        calls = harness.spy_claude_review()

        result = _invoke_claude(harness, [_claude_ok() for _ in harness.roles])

        assert result.exit_code == 0, result.output
        assert _out(result)["decision"] == "clean"
        ledger = _stored(harness)
        assert ledger.last_clean_sha == found.head
        assert ledger.rounds[-1].kind == "delta"
        assert ledger.rounds[-1].backend == "claude"
        assert isinstance(calls[0]["resolution"], ResolutionSpec)
        assert calls[0]["max_prompt_chars"] is not None

    def test_a_bad_resolution_reopens_the_loop(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        found = _resolved(harness, tmp_path, **_CLAUDE_CONFIRM)
        results = [_claude_finding("MUST_FIX")] + [
            _claude_ok() for _ in harness.roles[1:]
        ]

        result = _invoke_claude(harness, results)

        assert result.exit_code == 0, result.output
        assert _out(result)["decision"] == "continue"
        ledger = _stored(harness)
        assert ledger.status == "in_progress"
        assert ledger.last_clean_sha == found.clean

    def test_an_oversized_prompt_is_exit_four_with_nothing_recorded(
        self, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _resolved(harness, tmp_path, **_CLAUDE_CONFIRM)
        before = _ledger_bytes(harness)
        monkeypatch.setattr("cw.codex_review.CODEX_MAX_PROMPT_CHARS", 10)

        result = _invoke_claude(harness, [])

        assert result.exit_code == _ROUND_FAILED, result.output
        assert _out(result)["cause"] == "input_too_large"
        assert _ledger_bytes(harness) == before

    def test_the_backend_flag_is_not_accepted(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _resolved(harness, tmp_path, **_CLAUDE_CONFIRM)

        result = harness.cli.invoke(main, harness.args("--backend", "claude"))

        assert result.exit_code == 2, result.output


@needs_codex
def test_a_verdict_for_another_sha_is_refused(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolved(harness, tmp_path)
    before = _ledger_bytes(harness)
    blocked = make_codex_blocked(
        ticket_id=TICKET_ID,
        worktree=harness.repo,
        reason="codex_must_fix_findings",
        retry_eligible=False,
        details="",
    )
    outcome = RoundOutcome(blocked, consolidated_verdict(sha="f" * 40), None)
    monkeypatch.setattr(
        "cw.cli.review.converge_delta.run_backend_round", lambda **_kwargs: outcome
    )

    result, _ = harness.invoke([])

    assert result.exit_code == 1, result.output
    assert "the verdict reviewed " + "f" * 40 in result.output
    assert _ledger_bytes(harness) == before


def _assert_refused(harness: Harness, token: str, before: bytes | None) -> None:
    calls = harness.spy_run_review()
    claude_calls = harness.spy_claude_review()
    result, _ = harness.invoke([])
    assert result.exit_code == 1, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines == [f"Error: delta-confirm refused: {token}"]
    assert calls == claude_calls == []
    if before is not None:
        assert _ledger_bytes(harness) == before


_SHAPES = [
    pytest.param(_CODEX_CONFIRM, id="claude-native+codex"),
    pytest.param(_CLAUDE_CONFIRM, id="codex+claude"),
]


@needs_codex
class TestRefusals:
    @pytest.mark.parametrize(
        "review",
        [
            pytest.param({"convergence": "blocking"}, id="blocking"),
            pytest.param(
                {"convergence": "clean", "default_merge_tolerance": False},
                id="strict",
            ),
        ],
    )
    def test_a_strict_client_is_tolerance_off(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        monkeypatch: pytest.MonkeyPatch,
        review: dict[str, object],
    ) -> None:
        harness = _harness(tmp_path, make_git_repo_with_origin, monkeypatch, review)
        _resolved(harness, tmp_path)
        _assert_refused(harness, "tolerance_off", _ledger_bytes(harness))

    def test_a_context_claiming_the_tolerance_is_not_believed(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cw-context says tolerant, the live client says strict: refused."""
        strict = {"convergence": "clean", "default_merge_tolerance": False}
        harness = _harness(tmp_path, make_git_repo_with_origin, monkeypatch, strict)
        context = harness.repo / HOOK_CONTEXT_RELATIVE_PATH
        context.parent.mkdir(parents=True, exist_ok=True)
        context.write_text(
            json.dumps(
                {
                    "schema_version": 16,
                    "world_state_snapshot": {"origin_main_branch": "main"},
                    "review": {
                        "convergence": "clean",
                        "default_merge_tolerance": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        _resolved(harness, tmp_path)
        _assert_refused(harness, "tolerance_off", _ledger_bytes(harness))

    @pytest.mark.parametrize("shape", _SHAPES)
    def test_head_at_the_clean_sha_is_not_confirmable(
        self, harness: Harness, shape: dict[str, str]
    ) -> None:
        write_ledger(harness.repo, resolution_ledger(clean=harness.head(), **shape))
        _assert_refused(harness, "ledger_not_confirmable", _ledger_bytes(harness))

    @pytest.mark.parametrize("shape", _SHAPES)
    def test_a_clean_merge_is_no_resolution(
        self, harness: Harness, tmp_path: Path, shape: dict[str, str]
    ) -> None:
        found = branch_with_overlap_merge(harness.repo, tmp_path / "main-clone")
        write_ledger(harness.repo, resolution_ledger(clean=found.clean, **shape))
        _assert_refused(harness, "no_resolution", _ledger_bytes(harness))

    @pytest.mark.parametrize("shape", _SHAPES)
    def test_an_own_commit_after_the_merge_is_refused(
        self, harness: Harness, tmp_path: Path, shape: dict[str, str]
    ) -> None:
        _resolved(harness, tmp_path, **shape)
        commit_tracked_file(harness.repo, "late.py")
        own = harness.head()
        _assert_refused(harness, f"non_merge_commit {own[:7]}", _ledger_bytes(harness))

    def test_a_confirm_none_ledger_is_not_confirmable(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        _resolved(harness, tmp_path, loop_backend="claude-native", confirm="none")
        _assert_refused(harness, "ledger_not_confirmable", _ledger_bytes(harness))

    @pytest.mark.parametrize(
        ("script", "token"),
        [
            pytest.param(octopus_resolution_script, "not_two_parent", id="octopus"),
            pytest.param(
                non_default_parent_resolution_script,
                "second_parent_not_on_default",
                id="off-default",
            ),
        ],
    )
    def test_scripted_merge_shapes(
        self,
        harness: Harness,
        monkeypatch: pytest.MonkeyPatch,
        script: Callable[[str, str, str], GitScript],
        token: str,
    ) -> None:
        clean, head, default = "c" * 40, "d" * 40, "e" * 40
        write_ledger(harness.repo, resolution_ledger(clean=clean))
        before = _ledger_bytes(harness)
        answers = script(clean, head, default)
        answers["rev-parse", "HEAD"] = f"{head}\n"
        ScriptedMergeGit(answers).install(monkeypatch)
        _assert_refused(harness, f"{token} {head[:7]}", before)

    def test_an_unreadable_head_is_refused(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(harness.repo, resolution_ledger(clean="c" * 40))
        before = _ledger_bytes(harness)
        ScriptedMergeGit({("rev-parse", "HEAD"): 128}).install(monkeypatch)
        _assert_refused(harness, "git_unreadable", before)

    def test_a_ledger_of_another_ticket_is_a_cwerror(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        found = branch_with_resolution_and_unrelated_hunks(
            harness.repo, tmp_path / "main-clone"
        )
        write_ledger(
            harness.repo, resolution_ledger(clean=found.clean, ticket_id="OTHER-1")
        )
        result, _ = harness.invoke([])
        assert result.exit_code == 1, result.output
        assert "belongs to ticket 'OTHER-1'" in result.output


def test_a_missing_binary_is_exit_three_and_records_nothing(
    harness: Harness, tmp_path: Path
) -> None:
    """``codex`` is absent by default under the optional-binary guard."""
    _resolved(harness, tmp_path)
    before = _ledger_bytes(harness)

    result, _ = harness.invoke([])

    assert result.exit_code == _UNAVAILABLE, result.output
    assert _out(result)["cause"] == "binary_missing"
    assert _ledger_bytes(harness) == before


class TestDecisionSet:
    """The ``decision`` a delta round can yield (#2980).

    ``confirm_required`` is unreachable for kind ``delta``: the engine's clean
    decision needs no further confirm for a non-loop round.
    """

    @pytest.mark.parametrize(
        ("findings", "max_cycles", "decision"),
        [
            pytest.param(0, 8, "clean", id="clean"),
            pytest.param(1, 8, "continue", id="continue"),
            pytest.param(1, 0, "stop_max_cycles", id="max-cycles"),
        ],
    )
    def test_the_decision(self, findings: int, max_cycles: int, decision: str) -> None:
        ledger = resolution_ledger(clean="S").model_copy(
            update={"max_cycles": max_cycles}
        )
        _, report = record_delta_round(
            ledger,
            backend="codex",
            reviewed_sha="S+1",
            findings=[round_finding(summary=f"f{n}") for n in range(findings)],
            roster=["Code Quality Reviewer"],
            now=_FROZEN,
        )

        assert report.decision == decision
        assert report.decision != "confirm_required"

    def test_an_operator_finding_stops_for_the_operator(self) -> None:
        _, report = record_delta_round(
            resolution_ledger(clean="S"),
            backend="codex",
            reviewed_sha="S+1",
            findings=[operator_finding()],
            roster=["Code Quality Reviewer"],
            now=_FROZEN,
        )

        assert report.decision == "stop_operator_action"
