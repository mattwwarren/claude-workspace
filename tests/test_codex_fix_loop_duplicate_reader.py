"""Tests for cw.codex_fix_loop.duplicate_reader, the duplicate-reader signal (#2667)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from cw._git import run_git
from cw.codex_fix_loop import commit as commit_module
from cw.codex_fix_loop import duplicate_reader
from cw.codex_fix_loop.baseline import cycle_touched_paths
from cw.codex_fix_loop.commit import _GrowthGuards
from cw.codex_fix_loop.duplicate_reader import (
    added_defs,
    duplicated_names,
    is_reader_name,
)
from cw.codex_fix_loop.fence import (
    LEFT_STAGED_HINT,
    SETTLE_FINDING_HINT,
    FenceBreach,
)
from cw.codex_fix_loop.growth import LoopGrowthBudget, effective_budget
from cw.codex_fix_loop.posted_text import POSTED_TEXT_MAX_CHARS, TRUNCATION_MARKER
from cw.codex_review import (
    CODEX_FIX_GROWTH_BUDGET,
    CODEX_FIX_LOOP_GROWTH_GUARD_KEY,
    _parse_unified_diff,
)
from tests._codex_review_helpers import (
    _check_duplicate_reader,
    _duplicate_reader_repo,
    _head_baseline,
    _write,
)
from tests.conftest import _make_finding, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_fix_loop.commit import _CycleContext
    from cw.review_findings import Finding

_DUP = "duplicates the native parser `_read_roster_workers`"


def _diff(path: str, added: list[str], removed: tuple[str, ...] = ()) -> str:
    """A minimal ``-U0`` unified diff of one file (our own contract)."""
    body = [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            f"--- a/{path}",
            f"+++ b/{path}",
            f"@@ -1,{len(removed)} +1,{len(added)} @@",
            *body,
        ]
    )


def _defs(*diffs: str) -> list[str]:
    file_diffs, added, _window, _changed = _parse_unified_diff("\n".join(diffs))
    return [d.name for d in added_defs(file_diffs, added)]


def _finding(
    *,
    file: str = "src/hold.py",
    summary: str = _DUP,
    suggested_fix: str = "Reuse it.",
    consequence: str = "Two parsers drift.",
    evidence: str = "def read_roster_snapshot(x):",
) -> Finding:
    return _make_finding(
        file=file,
        summary=summary,
        suggested_fix=suggested_fix,
        consequence=consequence,
        evidence=evidence,
    )


def _helper_body(name: str) -> str:
    return f"def {name}(roster):\n    return roster\n"


class TestIsReaderName:
    @pytest.mark.parametrize(
        "name",
        [
            "_read_roster_snapshot",
            "load_x",
            "parse_y",
            "decode_z",
            "extract_a",
            "snapshot_b",
        ],
    )
    def test_reader_stems_match(self, name: str) -> None:
        assert is_reader_name(name)

    @pytest.mark.parametrize("name", ["build_x", "download_y", "thread_pool", "run"])
    def test_non_reader_names_do_not_match(self, name: str) -> None:
        assert not is_reader_name(name)


class TestAddedDefs:
    @pytest.mark.parametrize(
        "name", ["_read_roster_snapshot", "load_x", "parse_y", "extract_a"]
    )
    def test_reader_def_detected(self, name: str) -> None:
        assert _defs(_diff("src/a.py", [f"def {name}(x):"])) == [name]

    def test_async_def_detected(self) -> None:
        assert _defs(_diff("src/a.py", ["async def decode_z(x):"])) == ["decode_z"]

    def test_non_reader_and_indented_and_class_ignored(self) -> None:
        diff = _diff(
            "src/a.py",
            ["def build_x():", "    def read_inner(self):", "class ParserThing:"],
        )
        assert _defs(diff) == []

    def test_tests_docs_and_non_py_ignored(self) -> None:
        assert _defs(_diff("tests/test_a.py", ["def read_t(x):"])) == []
        assert _defs(_diff("docs/a.py", ["def read_d(x):"])) == []
        assert _defs(_diff("src/a.md", ["def read_m(x):"])) == []

    def test_same_name_removed_def_cancels(self) -> None:
        diff = _diff("src/a.py", ["def read_b(x):"], removed=("def read_b(x):",))
        assert _defs(diff) == []

    def test_cross_file_move_cancels(self) -> None:
        moved = "\n".join(
            [
                _diff("src/b.py", [], removed=("def read_b(x):",)),
                _diff("src/a.py", ["def read_b(x):"]),
            ]
        )
        assert _defs(moved) == []

    def test_edit_before_added_def_still_reports_it(self) -> None:
        # A count-based cancel would drop read_b here; name-based keeps it.
        diff = "\n".join(
            [
                _diff("src/a.py", ["def edited_a(x):"], removed=("def edited_a(x):",)),
                _diff("src/a.py", ["def read_b(x):"]),
            ]
        )
        assert _defs(diff) == ["read_b"]


class TestDuplicatedNames:
    @pytest.mark.parametrize(
        "phrase",
        [
            "duplicates",
            "re-implements",
            "reimplementation",
            "already exists",
            "copy of",
            "parallel parser",
        ],
    )
    def test_duplication_phrase_matches(self, phrase: str) -> None:
        finding = _finding(summary=f"this {phrase} `read_x`")
        assert duplicated_names(finding) == ["read_x"]

    def test_no_duplication_language_returns_empty(self) -> None:
        assert duplicated_names(_finding(summary="fine, see `read_x`")) == []

    def test_extraction_forms(self) -> None:
        finding = _finding(
            summary=(
                "duplicates `cw.native_daemon._read_roster_workers`, "
                "also parse_items() and `load_thing()`"
            )
        )
        assert duplicated_names(finding) == [
            "_read_roster_workers",
            "parse_items",
            "load_thing",
        ]

    def test_prose_word_before_parenthesis_is_not_a_call(self) -> None:
        finding = _finding(
            summary="duplicates the existing parser (see `_read_roster_workers`)"
        )
        names = duplicated_names(finding)
        assert names == ["_read_roster_workers"]
        assert "parser" not in names

    @pytest.mark.parametrize(
        "token", ["`abc`", "`__init__`", "`a.*`", "`-e`", "`foo bar`"]
    )
    def test_rejected_tokens(self, token: str) -> None:
        assert duplicated_names(_finding(summary=f"duplicates {token}")) == []

    def test_dedupe_and_cap(self) -> None:
        names = [f"read_thing{i}" for i in range(7)]
        summary = "duplicates " + " ".join(f"`{n}`" for n in names + names[:2])
        assert duplicated_names(_finding(summary=summary)) == names[:5]

    def test_evidence_is_ignored(self) -> None:
        finding = _finding(summary="duplicates the parser", evidence="`read_x`")
        assert duplicated_names(finding) == []


class TestCheckDuplicateReader:
    def test_acceptance_new_helper_duplicating_existing_parks(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(
            repo / "src" / "hold.py",
            "HOLD = 1\n" + _helper_body("read_roster_snapshot"),
        )

        breach = _check_duplicate_reader(repo, [_finding()])

        assert breach is not None
        assert breach.reason == CODEX_FIX_GROWTH_BUDGET
        assert "_read_roster_workers" in breach.details
        assert "src/native.py:1" in breach.details
        assert "src/hold.py:2" in breach.details
        assert "reuse it" in breach.details
        assert LEFT_STAGED_HINT in breach.recovery_hint
        assert SETTLE_FINDING_HINT in breach.recovery_hint
        assert CODEX_FIX_LOOP_GROWTH_GUARD_KEY in breach.recovery_hint
        assert breach.paths == ("src/hold.py", "src/native.py")

    def test_no_duplication_finding_returns_none_without_git(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))

        message = "git must not be called"

        def _boom(*_a: object, **_k: object) -> str:
            raise AssertionError(message)

        monkeypatch.setattr(duplicate_reader, "diff_since", _boom)
        monkeypatch.setattr(duplicate_reader, "cycle_diff", _boom)
        assert _check_duplicate_reader(repo, [_finding(summary="fine")]) is None

    def test_unrelated_duplication_finding_returns_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        # The named function really exists, so only the relevance filter (not
        # name resolution) can return None: other file, helper never mentioned.
        other = _finding(file="src/other.py")
        assert "read_roster_snapshot" not in other.summary
        assert _check_duplicate_reader(repo, [other]) is None

    def test_relevance_by_name_in_other_file_breaches(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        finding = _finding(
            file="src/other.py",
            summary="read_roster_snapshot duplicates `_read_roster_workers`",
        )
        assert _check_duplicate_reader(repo, [finding]) is not None

    def test_loop_wide_helper_committed_earlier_breaches(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        start = _head_baseline(repo).head_sha
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "cycle 1 helper")
        _write(repo / "src" / "other.py", "OTHER = 2\n")
        budget = LoopGrowthBudget(start, 40, effective_budget(40, 1))

        assert (
            _check_duplicate_reader(repo, [_finding()], loop_budget=budget) is not None
        )

    def test_no_loop_budget_ignores_earlier_commit(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "cycle 1 helper")
        _write(repo / "src" / "other.py", "OTHER = 2\n")

        assert _check_duplicate_reader(repo, [_finding()]) is None

    def test_no_loop_budget_breaches_when_cycle_adds_helper(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        assert _check_duplicate_reader(repo, [_finding()], loop_budget=None) is not None

    def test_cycle_removing_helper_returns_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        start = _head_baseline(repo).head_sha
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "cycle 1 helper")
        _write(repo / "src" / "hold.py", "HOLD = 1\n")
        budget = LoopGrowthBudget(start, 40, effective_budget(40, 1))

        assert _check_duplicate_reader(repo, [_finding()], loop_budget=budget) is None

    def test_pre_loop_helper_returns_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "pre-loop helper")
        start = _head_baseline(repo).head_sha
        _write(repo / "src" / "other.py", "OTHER = 2\n")
        budget = LoopGrowthBudget(start, 40, effective_budget(40, 1))

        assert _check_duplicate_reader(repo, [_finding()], loop_budget=budget) is None

    def test_hallucinated_name_returns_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        finding = _finding(summary="duplicates `read_nothing_here_zz`")
        assert _check_duplicate_reader(repo, [finding]) is None

    def test_name_resolving_only_in_tests_returns_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "tests" / "test_x.py", "def read_fake_thing(x):\n    return x\n")
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "test-only def")
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        finding = _finding(summary="duplicates `read_fake_thing`")
        assert _check_duplicate_reader(repo, [finding]) is None

    def test_self_reference_returns_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        finding = _finding(summary="duplicates `read_roster_snapshot`")
        assert _check_duplicate_reader(repo, [finding]) is None

    def test_finding_naming_freshly_added_non_reader_def_returns_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        _write(repo / "src" / "builder.py", _helper_body("build_roster"))
        finding = _finding(summary="duplicates `build_roster`")
        assert _check_duplicate_reader(repo, [finding]) is None

    def test_async_existing_function_resolves(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "native.py", "async def load_thing(x):\n    return x\n")
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "async existing")
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        finding = _finding(summary="duplicates `load_thing`")
        breach = _check_duplicate_reader(repo, [finding])
        assert breach is not None
        assert "`load_thing`" in breach.details

    def test_method_existing_function_resolves(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(
            repo / "src" / "native.py",
            "class Store:\n    def parse_items(self, x):\n        return x\n",
        )
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "method existing")
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        finding = _finding(summary="duplicates `parse_items`")
        breach = _check_duplicate_reader(repo, [finding])
        assert breach is not None
        assert "src/native.py:2" in breach.details

    def test_first_unresolved_candidate_skipped(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        finding = _finding(
            summary="duplicates `read_ghost_zz`, see `_read_roster_workers`"
        )
        breach = _check_duplicate_reader(repo, [finding])
        assert breach is not None
        assert "`_read_roster_workers`" in breach.details

    def test_git_nonzero_returns_none(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))

        def _fail(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 128, stdout="", stderr="boom")

        monkeypatch.setattr(duplicate_reader, "run_git", _fail)
        assert _check_duplicate_reader(repo, [_finding()]) is None

    def test_git_timeout_returns_none(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))

        def _timeout(*_a: object, **_k: object) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(duplicate_reader, "run_git", _timeout)
        assert _check_duplicate_reader(repo, [_finding()]) is None

    def test_untrusted_text_is_redacted_and_capped(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        summary = f'{_DUP}. password = "hunter2"\nsecond line injected ' + "x" * (
            POSTED_TEXT_MAX_CHARS * 2
        )
        breach = _check_duplicate_reader(repo, [_finding(summary=summary)])
        assert breach is not None
        assert "hunter2" not in breach.details
        assert "hunter2" not in breach.recovery_hint
        assert len(breach.details) <= POSTED_TEXT_MAX_CHARS
        assert TRUNCATION_MARKER in breach.details

    def test_not_mutated(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        head = _head_baseline(repo).head_sha
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        assert _check_duplicate_reader(repo, [_finding()]) is not None
        assert _head_baseline(repo).head_sha == head
        assert (repo / "src" / "hold.py").exists()
        staged = run_git(
            ["diff", "--cached", "--name-only"], cwd=repo, capture_output=True
        )
        assert "src/hold.py" in staged.stdout

    def test_check_stages_cycle_before_measuring(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        head = _head_baseline(repo)
        assert "src/hold.py" in cycle_touched_paths(repo, head)


_ROOMY_BUDGET_LINES = 40


def _ctx(repo: Path, cycle: int = 2) -> _CycleContext:
    """The two fields ``_growth_breach`` reads from a cycle context."""
    return cast("_CycleContext", SimpleNamespace(worktree=repo, cycle=cycle))


def _hook(
    repo: Path,
    *,
    enabled: bool = True,
    allowed_files: frozenset[str] | None = frozenset({"src/hold.py"}),
    budget_lines: int = _ROOMY_BUDGET_LINES,
    loop_budget: LoopGrowthBudget | None = None,
    findings: list[Finding] | None = None,
) -> object:
    """Stage the cycle against HEAD, then run the real ``_growth_breach`` hook."""
    baseline = _head_baseline(repo)
    cycle_touched_paths(repo, baseline)
    guards = _GrowthGuards(
        enabled=enabled,
        budget_lines=budget_lines,
        constraints=None,
        loop_budget=loop_budget,
    )
    return commit_module._growth_breach(
        _ctx(repo),
        baseline,
        findings=[_finding()] if findings is None else findings,
        allowed_files=allowed_files,
        guards=guards,
    )


class TestDriverIntegration:
    """The hook in ``commit._growth_breach`` runs, forwards and stays gated (#2667).

    Called directly with a ``_GrowthGuards`` (the plan's stated fallback): the
    real driver would need a delta-admitted re-review doc to carry the finding.
    """

    def test_enabled_manifest_parks_naming_existing_function(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", "HOLD = 1\n" + _helper_body("read_snapshot"))

        breach = _hook(repo)

        assert isinstance(breach, FenceBreach)
        assert breach.reason == CODEX_FIX_GROWTH_BUDGET
        assert "`_read_roster_workers`" in breach.details
        assert "src/native.py:1" in breach.details

    def test_loop_budget_is_forwarded(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        start = _head_baseline(repo).head_sha
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "cycle 1 helper")
        _write(repo / "src" / "other.py", "OTHER = 2\n")
        budget = LoopGrowthBudget(start, 40, effective_budget(40, 1))

        # Without the loop budget the earlier commit is invisible (no park).
        assert _hook(repo) is None
        breach = _hook(repo, loop_budget=budget)
        assert isinstance(breach, FenceBreach)
        assert "`_read_roster_workers`" in breach.details

    def test_switched_off_never_parks(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        assert _hook(repo, enabled=False) is None

    def test_no_manifest_never_parks(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        assert _hook(repo, allowed_files=None) is None

    def test_growth_breach_short_circuits_duplicate_check(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _duplicate_reader_repo(make_git_repo)
        _write(repo / "src" / "hold.py", _helper_body("read_roster_snapshot"))
        message = "duplicate-reader check must not run after a growth breach"

        def _boom(*_a: object, **_k: object) -> None:
            raise AssertionError(message)

        monkeypatch.setattr(commit_module, "check_duplicate_reader", _boom)

        breach = _hook(repo, budget_lines=0)

        assert isinstance(breach, FenceBreach)
        assert breach.reason == CODEX_FIX_GROWTH_BUDGET
        assert "exceeds the budget" in breach.details
