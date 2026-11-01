"""Tests for cw.codex_review._shard and _shard_run: diff sharding (#2864).

A confirm diff over the codex prompt limit is split by file into shards that
each fit, every role reviews every shard, and the shard results merge into one
round. The properties pinned here are the ticket's: every changed file is in
exactly one shard (a dropped or duplicated file fails the runtime guard),
nothing is truncated, a single oversized file fails loud, and a diff under the
limit takes today's path with byte-identical prompts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.codex_review import _prepare_review_pass
from cw.codex_review import _shard as shard_module
from cw.codex_review._diff import _capture_diff
from cw.codex_review._shard import (
    FILE_TOO_LARGE_CAUSE,
    INPUT_TOO_LARGE_CAUSE,
    ReviewInputTooLargeError,
    _ShardPlan,
    plan_prompt_shards,
    plan_shards,
    split_diff_by_file,
)
from cw.codex_review._shard_run import (
    _remaining_seconds,
    _ShardResult,
    annotate_shard_failure,
    merge_shard_documents,
    merge_shard_metrics,
    merge_shard_results,
)
from cw.codex_runner import FakeCodexRunner
from cw.review_findings import (
    CapturedDiff,
    Finding,
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    ReviewerRunMetrics,
)
from tests._codex_review_helpers import _finding_payload, _shard_limit_inputs, _task
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.codex_review import _ReviewPassInputs

_OVERHEAD = 100
_SEEDS = range(12)
_ROLES = ["Role A", "Role B"]
_SIZES = "actual_chars=1379349 max_chars=1048576"


def _content(tag: str, lines: int = 20) -> str:
    return "".join(f"{tag}_{n} = {n}\n" for n in range(lines))


def _multi_file_repo(make_git_repo: Callable[[str], Path], name: str) -> Path:
    """A feature branch changing four files across three directories."""
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    for relpath in ("pkg_a/a1.py", "pkg_a/a2.py", "pkg_b/b1.py", "pkg_c/c1.py"):
        commit_tracked_file(repo, relpath, _content(Path(relpath).stem))
    return repo


def _prepared(repo: Path, session_id: str) -> _ReviewPassInputs:
    return _prepare_review_pass(
        _task(), repo, "main", runner=FakeCodexRunner(), session_id=session_id
    )


def _doc(
    role: str = "Role A",
    *,
    status: str = "ok",
    detail: str = "checked it",
    findings: Sequence[Finding] = (),
) -> ReviewerFindingsDocument:
    return ReviewerFindingsDocument.model_validate(
        {
            "reviewer_role": role,
            "status": status,
            "detail": detail,
            "findings": [f.model_dump() for f in findings],
        }
    )


def _finding(file: str) -> Finding:
    return Finding.model_validate(_finding_payload(file=file, severity="SHOULD_FIX"))


def _plan(index: int, files: Sequence[str]) -> _ShardPlan:
    return _ShardPlan(
        index=index,
        files=tuple(files),
        diff_text="",
        prompts_by_role=dict.fromkeys(_ROLES, "p"),
        prompt_chars=10 * index,
    )


# ---------------------------------------------------------------------------
# split_diff_by_file
# ---------------------------------------------------------------------------


class TestSplitDiffByFile:
    def test_lossless_across_form_feed_and_line_separator(self) -> None:
        line_separator = chr(0x2028)
        text = (
            "diff --git a/one.py b/one.py\n+a = 1\x0c\n"
            f"+b = '{line_separator}'\n"
            "diff --git a/two.py b/two.py\n+c = 3\n"
        )
        # splitlines would break inside both lines; the split must not.
        assert len(text.splitlines()) > text.count("\n")
        chunks = split_diff_by_file(text)
        assert list(chunks) == ["one.py", "two.py"]
        assert "".join(chunks.values()) == text

    def test_keys_equal_changed_files_on_a_real_repo(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-split-real")
        commit_tracked_file(repo, "old_name.py", _content("old", 30))
        commit_tracked_file(repo, "doomed.py", _content("doomed"))
        git_in(repo, "checkout", "-b", "feature")
        git_in(repo, "mv", "old_name.py", "new_name.py")
        git_in(repo, "rm", "-q", "doomed.py")
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00")
        (repo / "my file.py").write_text("spaced = 1\n", encoding="utf-8")
        git_in(repo, "add", "blob.bin", "my file.py")
        git_in(repo, "commit", "-q", "-m", "rename, delete, binary, space")

        diff, _sha, changed_files = _capture_diff(repo, "main")
        chunks = split_diff_by_file(diff.text)

        assert list(chunks) == changed_files
        assert {"new_name.py", "doomed.py", "blob.bin", "my file.py"} <= set(chunks)
        assert "".join(chunks.values()) == diff.text

    def test_empty_diff_has_no_chunks(self) -> None:
        assert split_diff_by_file("") == {}

    def test_a_repeated_path_is_one_entry_holding_both_chunks(self) -> None:
        text = (
            "diff --git a/link b/link\ndeleted file mode 100644\n"
            "diff --git a/link b/link\nnew file mode 120000\n"
        )
        chunks = split_diff_by_file(text)
        assert list(chunks) == ["link"]
        assert chunks["link"] == text

    def test_an_unmatched_header_keeps_its_raw_text_as_key(self) -> None:
        text = 'diff --git "a/caf\\303\\251" "b/caf\\303\\251"\n+x\n'
        chunks = split_diff_by_file(text)
        assert list(chunks) == ['"a/caf\\303\\251" "b/caf\\303\\251"']
        assert "".join(chunks.values()) == text

    def test_text_before_the_first_header_stays_on_the_first_chunk(self) -> None:
        text = "preamble\ndiff --git a/a.py b/a.py\n+x\n"
        chunks = split_diff_by_file(text)
        assert chunks == {"a.py": text}

    def test_text_with_no_header_is_kept_whole(self) -> None:
        assert split_diff_by_file("stray\n") == {"": "stray\n"}


# ---------------------------------------------------------------------------
# plan_shards
# ---------------------------------------------------------------------------


_DIRECTORIES = ("", "a", "a/b", "c", "d/e/f")


def _seeded(seed: int, salt: str, modulo: int) -> int:
    """A deterministic pseudo-random integer in ``[0, modulo)`` (RNG-free, S311)."""
    digest = hashlib.sha256(f"{seed}:{salt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % modulo


def _random_chunks(seed: int) -> dict[str, str]:
    """A seeded mix of 1-30 files of 1-400 chars across five directories."""
    chunks: dict[str, str] = {}
    for n in range(1 + _seeded(seed, "count", 30)):
        directory = _DIRECTORIES[_seeded(seed, f"dir{n}", len(_DIRECTORIES))]
        path = f"{directory}/f{n}.py" if directory else f"f{n}.py"
        chunks[path] = "x" * (1 + _seeded(seed, f"size{n}", 400))
    return chunks


def _cost(path: str, chunks: dict[str, str]) -> int:
    return len(chunks[path]) + len(path) + 1


class TestPlanShards:
    @pytest.mark.parametrize("seed", _SEEDS)
    def test_every_file_is_in_exactly_one_shard_within_budget(self, seed: int) -> None:
        chunks = _random_chunks(seed)
        budget = max(_cost(path, chunks) for path in chunks) * 2
        shards = plan_shards(chunks, overhead=_OVERHEAD, max_chars=_OVERHEAD + budget)

        planned = [path for shard in shards for path in shard]
        assert sorted(planned) == sorted(chunks)
        assert len(planned) == len(set(planned))
        for shard in shards:
            assert sum(_cost(path, chunks) for path in shard) <= budget

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda shards: [shard[1:] for shard in shards],
            lambda shards: [*shards, shards[0][:1]],
        ],
        ids=["dropped", "duplicated"],
    )
    def test_a_planner_that_drops_or_duplicates_a_file_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Callable[[list[list[str]]], list[list[str]]],
    ) -> None:
        real = shard_module._pack_groups
        monkeypatch.setattr(
            shard_module,
            "_pack_groups",
            lambda *args: mutate(real(*args)),
        )
        chunks = {"a/one.py": "x" * 10, "a/two.py": "y" * 10}
        with pytest.raises(ValueError, match="exactly once"):
            plan_shards(chunks, overhead=_OVERHEAD, max_chars=_OVERHEAD + 1000)

    def test_a_directory_that_fits_stays_whole(self) -> None:
        chunks = {
            "lone.py": "z" * 50,
            "pkg/a.py": "a" * 30,
            "pkg/b.py": "b" * 30,
        }
        budget = _cost("pkg/a.py", chunks) + _cost("pkg/b.py", chunks)
        shards = plan_shards(chunks, overhead=_OVERHEAD, max_chars=_OVERHEAD + budget)
        assert ["pkg/a.py", "pkg/b.py"] in shards
        assert ["lone.py"] in shards

    def test_a_directory_over_budget_is_split_file_by_file(self) -> None:
        chunks = {f"big/f{n}.py": "q" * 40 for n in range(5)}
        budget = 2 * _cost("big/f0.py", chunks)
        shards = plan_shards(chunks, overhead=_OVERHEAD, max_chars=_OVERHEAD + budget)
        assert shards == [
            ["big/f0.py", "big/f1.py"],
            ["big/f2.py", "big/f3.py"],
            ["big/f4.py"],
        ]

    def test_deterministic_across_calls(self) -> None:
        chunks = _random_chunks(7)
        budget = max(_cost(path, chunks) for path in chunks) * 3
        first = plan_shards(chunks, overhead=_OVERHEAD, max_chars=_OVERHEAD + budget)
        second = plan_shards(
            dict(reversed(list(chunks.items()))),
            overhead=_OVERHEAD,
            max_chars=_OVERHEAD + budget,
        )
        assert first == second

    def test_one_file_over_budget_is_file_too_large_and_never_truncated(
        self,
    ) -> None:
        chunks = {"small.py": "s" * 10, "huge/blob.py": "h" * 500}
        original = dict(chunks)
        limit = _OVERHEAD + _cost("small.py", chunks) + 1
        with pytest.raises(ReviewInputTooLargeError) as caught:
            plan_shards(chunks, overhead=_OVERHEAD, max_chars=limit)
        exc = caught.value
        assert exc.cause == FILE_TOO_LARGE_CAUSE
        assert exc.file == "huge/blob.py"
        assert exc.actual_chars == _OVERHEAD + _cost("huge/blob.py", chunks)
        assert exc.max_chars == limit
        assert exc.payload() == {
            "actual_chars": exc.actual_chars,
            "max_chars": limit,
            "file": "huge/blob.py",
        }
        assert str(exc) == (
            "huge/blob.py: one changed file does not fit a review prompt: "
            f"actual_chars={exc.actual_chars} max_chars={limit}"
        )
        assert chunks == original

    @pytest.mark.parametrize("limit", [_OVERHEAD, _OVERHEAD - 1])
    def test_no_budget_is_input_too_large_naming_the_overhead(self, limit: int) -> None:
        with pytest.raises(ReviewInputTooLargeError) as caught:
            plan_shards({"a.py": "x"}, overhead=_OVERHEAD, max_chars=limit)
        exc = caught.value
        assert exc.cause == INPUT_TOO_LARGE_CAUSE
        assert exc.actual_chars == _OVERHEAD
        assert exc.file is None
        assert exc.payload() == {"actual_chars": _OVERHEAD, "max_chars": limit}
        assert str(exc) == (
            "review prompt exceeds the limit: "
            f"actual_chars={_OVERHEAD} max_chars={limit}"
        )


# ---------------------------------------------------------------------------
# plan_prompt_shards over a real review pass
# ---------------------------------------------------------------------------


def _shardable_limit(prepared: _ReviewPassInputs) -> int:
    """A limit every file fits but the whole diff does not (asserted)."""
    overhead, costs = _shard_limit_inputs(prepared)
    limit = overhead + 2 * max(costs.values())
    assert max(len(p) for p in prepared.prompts_by_role.values()) > limit
    assert limit >= overhead + max(costs.values())
    return limit


class TestPlanPromptShards:
    def test_under_the_limit_is_no_sharding_and_byte_identical(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _multi_file_repo(make_git_repo, "wt-plan-under")
        prepared = _prepared(repo, "s-plan-under")
        longest = max(len(p) for p in prepared.prompts_by_role.values())

        assert plan_prompt_shards(prepared, longest) == []
        assert (
            prepared.render_prompts(prepared.diff, prepared.changed_files)
            == prepared.prompts_by_role
        )

    def test_over_the_limit_every_shard_prompt_fits_and_covers_the_diff(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _multi_file_repo(make_git_repo, "wt-plan-over")
        prepared = _prepared(repo, "s-plan-over")
        limit = _shardable_limit(prepared)

        plans = plan_prompt_shards(prepared, limit)

        assert len(plans) > 1
        assert [plan.index for plan in plans] == list(range(1, len(plans) + 1))
        for plan in plans:
            assert set(plan.prompts_by_role) == set(prepared.roles)
            assert all(len(p) <= limit for p in plan.prompts_by_role.values())
            assert plan.prompt_chars == max(
                len(p) for p in plan.prompts_by_role.values()
            )
        files = [path for plan in plans for path in plan.files]
        assert sorted(files) == sorted(prepared.changed_files)
        assert "".join(plan.diff_text for plan in plans) != ""

    def test_a_rendered_shard_prompt_over_the_limit_is_input_too_large(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _multi_file_repo(make_git_repo, "wt-plan-overflow")
        prepared = _prepared(repo, "s-plan-overflow")
        limit = _shardable_limit(prepared)
        real = prepared.render_prompts
        padding = "!" * (limit + 7)

        def _stub(diff: CapturedDiff, files: Sequence[str]) -> dict[str, str]:
            prompts = real(diff, files)
            if "pkg_c/c1.py" in files:
                prompts[prepared.roles[0]] += padding
            return prompts

        [padded] = [
            plan
            for plan in plan_prompt_shards(prepared, limit)
            if "pkg_c/c1.py" in plan.files
        ]
        with pytest.raises(ReviewInputTooLargeError) as caught:
            plan_prompt_shards(prepared._replace(render_prompts=_stub), limit)
        exc = caught.value
        assert exc.cause == INPUT_TOO_LARGE_CAUSE
        assert exc.file is None
        assert exc.max_chars == limit
        # The padded prompt is longer than any other shard's whole prompt.
        assert exc.actual_chars == (
            len(padded.prompts_by_role[prepared.roles[0]]) + len(padding)
        )


# ---------------------------------------------------------------------------
# Merging shard results
# ---------------------------------------------------------------------------


class TestMergeShardDocuments:
    def test_findings_from_every_shard_survive_in_shard_order(self) -> None:
        merged = merge_shard_documents(
            [
                (1, _doc(findings=[_finding("a.py")])),
                (2, _doc(findings=[_finding("b.py"), _finding("c.py")])),
            ],
            2,
        )
        assert [f.file for f in merged.findings] == ["a.py", "b.py", "c.py"]
        assert merged.status == "ok"
        assert merged.detail == "shard 1/2: checked it; shard 2/2: checked it"

    def test_a_degraded_shard_degrades_the_merged_document(self) -> None:
        merged = merge_shard_documents(
            [(1, _doc()), (2, _doc(status="degraded", detail="no tests ran"))], 2
        )
        assert merged.status == "degraded"
        assert "shard 2/2: no tests ran" in merged.detail

    @pytest.mark.parametrize(
        ("count", "expected"),
        [(2, "reviewed all 2 shards"), (1, "reviewed 1 of 2 shards")],
    )
    def test_an_ok_document_with_no_findings_and_no_detail_states_coverage(
        self, count: int, expected: str
    ) -> None:
        # A validated ok document with no findings always carries a detail, so
        # the blank one is built unvalidated: the guard is defensive.
        blank = ReviewerFindingsDocument.model_construct(
            reviewer_role="Role A", status="ok", detail="", findings=[]
        )
        merged = merge_shard_documents(
            [(index, blank) for index in range(1, count + 1)], 2
        )
        assert merged.detail == expected

    def test_the_merged_role_is_the_first_shard_document_role(self) -> None:
        merged = merge_shard_documents([(1, _doc("Named")), (2, _doc("Other"))], 2)
        assert merged.reviewer_role == "Named"


class TestAnnotateShardFailure:
    def test_the_shard_is_prefixed_and_the_original_detail_kept(self) -> None:
        failure = ReviewerRunFailure(
            role="R", reason="codex_input_too_large", detail=_SIZES
        )
        annotated = annotate_shard_failure(failure, 2, 3)
        assert annotated.detail == f"shard 2/3: {_SIZES}"
        assert annotated.reason == failure.reason
        assert failure.detail == _SIZES

    def test_an_empty_detail_becomes_the_bare_shard(self) -> None:
        failure = ReviewerRunFailure(role="R", reason="codex_timeout")
        assert annotate_shard_failure(failure, 2, 3).detail == "shard 2/3"


class TestMergeShardMetrics:
    def test_sums_counts_and_takes_first_thread_and_last_terminal(self) -> None:
        first: ReviewerRunMetrics = {
            "thread_id": "t-1",
            "duration_seconds": 1.5,
            "input_tokens": 10,
            "output_tokens": None,
            "terminal_event": "turn.failed",
            "tool_call_counts": {"shell": 1},
            "had_command_evidence": False,
            "unexpected_tool_attempts": ["web"],
        }
        second: ReviewerRunMetrics = {
            "thread_id": "t-2",
            "duration_seconds": 2.0,
            "input_tokens": 5,
            "output_tokens": None,
            "terminal_event": "turn.completed",
            "tool_call_counts": {"shell": 2, "read": 1},
            "had_command_evidence": True,
        }
        merged = merge_shard_metrics([first, second])
        assert merged["thread_id"] == "t-1"
        assert merged["terminal_event"] == "turn.completed"
        assert merged["duration_seconds"] == pytest.approx(3.5)
        assert merged["input_tokens"] == 15
        assert merged["output_tokens"] is None
        assert merged["tool_call_counts"] == {"shell": 3, "read": 1}
        assert merged["had_command_evidence"] is True
        assert merged["unexpected_tool_attempts"] == ["web"]


class TestMergeShardResults:
    def _results(self) -> list[_ShardResult]:
        """Shard 1: both roles ok. Shard 2: A failed doc, B a run failure.

        Shard 3: A degraded, B ok.
        """
        return [
            _ShardResult(
                {"Role A": _doc("Role A"), "Role B": _doc("Role B")},
                [],
                {"Role A": {"input_tokens": 1}, "Role B": {"input_tokens": 2}},
                [],
            ),
            _ShardResult(
                {"Role A": _doc("Role A", status="failed", detail="gave up")},
                [ReviewerRunFailure(role="Role B", reason="codex_timeout")],
                {"Role A": {"input_tokens": 3}},
                [],
            ),
            _ShardResult(
                {
                    "Role A": _doc("Role A", status="degraded", detail="partial"),
                    "Role B": _doc("Role B"),
                },
                [],
                {"Role A": {"input_tokens": 4}, "Role B": {"input_tokens": 5}},
                [],
            ),
        ]

    def test_coverage_records_follow_the_shard_record_contract(self) -> None:
        plans = [_plan(1, ["a.py"]), _plan(2, ["b/b.py"]), _plan(3, ["c/c.py"])]
        run = merge_shard_results(_ROLES, plans, self._results())

        assert [r.index for r in run.coverage] == [1, 2, 3]
        assert [r.files for r in run.coverage] == [["a.py"], ["b/b.py"], ["c/c.py"]]
        assert [r.prompt_chars for r in run.coverage] == [10, 20, 30]
        assert [r.roles_reviewed for r in run.coverage] == [_ROLES, [], _ROLES]
        # A failure or a failed document leaves a shard unreviewed; degraded
        # still counts as reviewed.
        assert [r.reviewed for r in run.coverage] == [True, False, True]

    def test_failed_documents_pass_through_and_failures_are_annotated(self) -> None:
        plans = [_plan(1, ["a.py"]), _plan(2, ["b/b.py"]), _plan(3, ["c/c.py"])]
        run = merge_shard_results(_ROLES, plans, self._results())

        assert [(d.reviewer_role, d.status) for d in run.documents] == [
            ("Role A", "degraded"),
            ("Role A", "failed"),
            ("Role B", "ok"),
        ]
        assert [(f.role, f.detail) for f in run.failures] == [("Role B", "shard 2/3")]
        assert run.metrics_by_role["Role A"]["input_tokens"] == 8
        assert run.metrics_by_role["Role B"]["input_tokens"] == 7

    def test_a_role_that_never_ran_has_no_document_and_no_metrics(self) -> None:
        results = [
            _ShardResult(
                {"Role A": _doc("Role A")},
                [ReviewerRunFailure(role="Role B", reason="budget_exhausted")],
                {"Role A": {}},
                [],
            )
        ]
        run = merge_shard_results(_ROLES, [_plan(1, ["a.py"])], results)
        assert [d.reviewer_role for d in run.documents] == ["Role A"]
        assert "Role B" not in run.metrics_by_role


class TestRemainingSeconds:
    def test_no_deadline_stays_none(self) -> None:
        assert _remaining_seconds(None) is None

    def test_counts_down_and_never_goes_negative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.codex_review._shard_run.time.monotonic", lambda: 100.0)
        assert _remaining_seconds(160.9) == 60
        assert _remaining_seconds(40.0) == 0
