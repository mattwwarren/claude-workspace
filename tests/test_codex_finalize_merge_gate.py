"""Tests for cw.codex_finalize._merge_gate: Step 4a as cw-run Python (#3062).

``gh`` is a scripted :class:`FakeGhCli`, ``cw dev-queue adopted-prs`` the
shared :class:`FakeCwCli`; git runs for real in sandboxed repos. The overlap
filter is the repo's real ``check_merge_gate_overlap.py`` unless a test needs a
stale or absent script, in which case a stub is written under the per-test HOME.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.codex_finalize._merge_gate import (
    ABSENT_FRICTION,
    DISABLED_FRICTION,
    OVERLAP_SCRIPT_NAME,
    MergeGateResult,
    run_merge_gate,
)
from cw.merge_probe import MergeTreeResult
from tests._codex_review_helpers import fresh_impl_repo
from tests._shared.git_repos import git_in
from tests._tool_fakes import FakeCwCli, FakeGhCli, GhReply, stub_cw_cli

if TYPE_CHECKING:
    from collections.abc import Callable

_BRANCH = "dev/42"
_PREFIX = "dev"
_CLIENT = "acme"
_ADOPTED = ["dev-queue", "adopted-prs", "-c", _CLIENT]
_REAL_SCRIPT = (
    Path(__file__).resolve().parents[1] / ".claude" / "scripts" / OVERLAP_SCRIPT_NAME
)
_MERGE_TREE = "cw.codex_finalize._merge_gate._merge_tree"


def _heads(*items: tuple[int, str]) -> str:
    return json.dumps([{"number": n, "headRefName": ref} for n, ref in items])


class Harness:
    """One merge-gate run: a branch with ``src/a.py``, scripted gh and cw."""

    def __init__(
        self, worktree: Path, gh: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.worktree = worktree
        self.gh = gh
        self.monkeypatch = monkeypatch
        self.cw: FakeCwCli = FakeCwCli({})
        self.adopted(GhReply(0, "", ""))
        self.fork = git_in(worktree, "rev-parse", "origin/main")

    def adopted(self, reply: GhReply | None) -> Harness:
        """Script ``cw dev-queue adopted-prs`` (``None``: it never finishes)."""
        self.cw = stub_cw_cli(self.monkeypatch, {"dev-queue adopted-prs": reply})
        return self

    def mine(self, *items: tuple[int, str]) -> Harness:
        self.gh.reply(
            "pr", "list", "--state", "open", "--author", "@me", stdout=_heads(*items)
        )
        return self

    def everyone(self, *items: tuple[int, str]) -> Harness:
        self.gh.reply("pr", "list", "--state", "open", "--json", stdout=_heads(*items))
        return self

    def diff(self, number: int, *files: str) -> Harness:
        self.gh.reply("pr", "diff", str(number), stdout="\n".join(files) + "\n")
        return self

    def run(
        self,
        *,
        enabled: bool = True,
        ignore: tuple[str, ...] = (),
        branch: str = _BRANCH,
    ) -> MergeGateResult:
        return run_merge_gate(
            self.worktree,
            enabled=enabled,
            ignore_paths=ignore,
            branch=branch,
            branch_prefix=_PREFIX,
            fork_point=self.fork,
            client_name=_CLIENT,
        )


@pytest.fixture
def h(make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> Harness:
    worktree, _origin = fresh_impl_repo(make_git_repo, "mg-wt", _BRANCH)
    (worktree / "src").mkdir()
    (worktree / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    git_in(worktree, "add", "-A")
    git_in(worktree, "commit", "-m", "branch work")
    gh = FakeGhCli().install(monkeypatch)
    return Harness(worktree, gh, monkeypatch)


@pytest.fixture
def real_script(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "cw.codex_finalize._merge_gate.find_guard_script",
        lambda _worktree, _name: _REAL_SCRIPT,
    )
    return _REAL_SCRIPT


def _home_script(body: str) -> Path:
    path = Path.home() / ".claude" / "scripts" / OVERLAP_SCRIPT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- disabled / clear ---------------------------------------------------------


class TestDisabledAndClear:
    def test_disabled_gives_friction_and_scans_nothing(self, h: Harness) -> None:
        result = h.run(enabled=False)
        assert result == MergeGateResult(friction=[DISABLED_FRICTION])
        assert h.gh.calls == []
        assert h.cw.calls == []

    def test_no_other_open_pr_is_clear(self, h: Harness, real_script: Path) -> None:
        h.mine()
        result = h.run()
        assert result == MergeGateResult()
        assert h.gh.argvs()[0][:6] == [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--author",
        ]
        assert h.cw.calls == [_ADOPTED]

    def test_this_branchs_own_pr_is_never_a_candidate(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((7, _BRANCH))
        assert h.run().conflicts == []
        assert not any(call[:3] == ["gh", "pr", "diff"] for call in h.gh.argvs())

    def test_a_pr_without_the_prefix_is_not_a_candidate(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "feature/other"))
        assert h.run().conflicts == []
        assert not any(call[:3] == ["gh", "pr", "diff"] for call in h.gh.argvs())


# --- overlap, ignore list, merge-tree ----------------------------------------


class TestOverlap:
    def test_overlap_blocks_only_when_merge_tree_conflicts(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py", "src/b.py")
        with patch(_MERGE_TREE, return_value="1") as probe:
            result = h.run()
        assert result.block is None
        (clause,) = result.conflicts
        assert "PR #9 (dev/other)" in clause
        assert "conflicts textually with this branch" in clause
        assert "src/a.py" in clause
        probe.assert_called_once_with(h.worktree, 9)

    def test_overlap_with_a_clean_merge_tree_does_not_block(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        with patch(_MERGE_TREE, return_value="0"):
            assert h.run().conflicts == []

    @pytest.mark.parametrize("probe", ["fetch failed", "128"])
    def test_a_failed_merge_tree_probe_fails_closed(
        self, h: Harness, real_script: Path, probe: str
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        with patch(_MERGE_TREE, return_value=probe):
            (clause,) = h.run().conflicts
        assert "failing closed" in clause
        assert probe in clause

    def test_no_shared_file_skips_the_merge_tree_probe(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "docs/readme.md")
        with patch(_MERGE_TREE) as probe:
            assert h.run().conflicts == []
        probe.assert_not_called()

    def test_an_ignored_overlap_skips_the_merge_tree_probe(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        with patch(_MERGE_TREE) as probe:
            assert h.run(ignore=("src/a.py",)).conflicts == []
        probe.assert_not_called()

    def test_a_pr_diff_failure_is_a_clause_and_the_rest_still_run(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/one"), (10, "dev/two"))
        h.gh.reply("pr", "diff", "9", returncode=1)
        h.diff(10, "src/a.py")
        with patch(_MERGE_TREE, return_value="1"):
            first, second = h.run().conflicts
        assert "PR #9 (dev/one) could not be checked" in first
        assert "failing closed" in first
        assert "PR #10 (dev/two)" in second
        assert "conflicts textually" in second

    def test_the_filter_gets_both_lists_the_ignores_and_json(
        self, h: Harness, tmp_path: Path
    ) -> None:
        record = tmp_path / "argv.json"
        verdict = json.dumps({"blocking": False, "overlap_after_ignore": []})
        _home_script(
            "#!/usr/bin/env python3\n# cw-script-version: 1\n"
            "import json, sys\n"
            "lists = [open(sys.argv[i + 1]).read() for i, a in enumerate(sys.argv)"
            " if a in ('--branch-files', '--pr-files')]\n"
            f"open({str(record)!r}, 'w').write(json.dumps([sys.argv[1:], lists]))\n"
            f"sys.stdout.write({verdict!r})\n"
        )
        h.mine((9, "dev/other")).diff(9, "src/b.py")
        h.run(ignore=("a.lock", "b.lock"))
        argv, lists = json.loads(record.read_text(encoding="utf-8"))
        assert argv[0] == "filter"
        assert argv.count("--ignore-path") == 2
        assert argv[argv.index("--ignore-path") + 1] == "a.lock"
        assert argv[-1] == "--json"
        assert lists == ["src/a.py\n", "src/b.py\n"]


# --- the filter's failure modes ----------------------------------------------


class TestFilterFailures:
    def _stub(self, body: str) -> None:
        _home_script("#!/usr/bin/env python3\n# cw-script-version: 1\n" + body)

    def test_a_filter_exit_two_fails_closed_for_that_pr(self, h: Harness) -> None:
        self._stub("import sys\nsys.stderr.write('bad input')\nsys.exit(2)\n")
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        (clause,) = h.run().conflicts
        assert "exited 2" in clause
        assert "bad input" in clause

    @pytest.mark.parametrize(
        "stdout",
        [
            "not json",
            "[]",
            json.dumps({"blocking": "yes", "overlap_after_ignore": []}),
            json.dumps({"blocking": True, "overlap_after_ignore": ["a"]}),
            json.dumps({"blocking": False, "overlap_after_ignore": ["a"]}),
            json.dumps({"blocking": False, "overlap_after_ignore": [1]}),
            json.dumps({"blocking": False}),
        ],
    )
    def test_a_malformed_verdict_fails_closed(self, h: Harness, stdout: str) -> None:
        self._stub(f"import sys\nsys.stdout.write({stdout!r})\nsys.exit(0)\n")
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        (clause,) = h.run().conflicts
        assert "malformed" in clause
        assert "failing closed" in clause

    def test_a_blocking_exit_with_a_clean_verdict_is_malformed(
        self, h: Harness
    ) -> None:
        verdict = json.dumps({"blocking": False, "overlap_after_ignore": []})
        self._stub(f"import sys\nsys.stdout.write({verdict!r})\nsys.exit(1)\n")
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        (clause,) = h.run().conflicts
        assert "malformed" in clause

    @pytest.mark.parametrize(
        "error", [OSError("boom"), subprocess.TimeoutExpired("filter", 1)]
    )
    def test_a_filter_that_cannot_finish_fails_closed(
        self, h: Harness, real_script: Path, error: BaseException
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        real_run = subprocess.run

        def _fail_the_filter(argv: list[str], *args: Any, **kwargs: Any) -> Any:
            if "filter" in argv:
                raise error
            return real_run(argv, *args, **kwargs)

        with patch(
            "cw.codex_finalize._merge_gate.subprocess.run",
            side_effect=_fail_the_filter,
        ):
            (clause,) = h.run().conflicts
        assert "did not finish" in clause


# --- the script table ---------------------------------------------------------


class TestScriptResolution:
    def test_an_absent_script_falls_back_to_a_raw_intersection(
        self, h: Harness
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py", "src/b.py")
        result = h.run()
        assert result.friction == [ABSENT_FRICTION]
        (clause,) = result.conflicts
        assert "shares files with this branch: src/a.py" in clause

    def test_an_absent_script_with_no_overlap_is_clear(self, h: Harness) -> None:
        h.mine((9, "dev/other")).diff(9, "docs/x.md")
        result = h.run()
        assert result.friction == [ABSENT_FRICTION]
        assert result.conflicts == []

    @pytest.mark.parametrize("marker", ["", "# cw-script-version: 0\n"])
    def test_a_stale_marker_is_agent_block(self, h: Harness, marker: str) -> None:
        _home_script("#!/usr/bin/env python3\n" + marker + "import sys\n")
        h.mine((9, "dev/other"))
        result = h.run()
        assert result.block is not None
        assert result.block.reason == "agent_block"
        assert "cw-script-version" in result.block.details
        assert result.conflicts == []
        assert h.gh.calls == []

    def test_a_branch_file_inventory_failure_is_agent_block(
        self, h: Harness, real_script: Path
    ) -> None:
        h.fork = "not-a-ref"
        result = h.run()
        assert result.block is not None
        assert result.block.reason == "agent_block"
        assert "branch file inventory failed" in result.block.details


# --- inventories fail closed --------------------------------------------------


class TestInventories:
    def test_a_failed_open_pr_listing_is_agent_block(
        self, h: Harness, real_script: Path
    ) -> None:
        h.gh.reply("pr", "list", returncode=1, stderr="HTTP 502")
        result = h.run()
        assert result.block is not None
        assert result.block.reason == "agent_block"
        assert "open-PR inventory failed" in result.block.details
        assert "failing closed" in result.block.details

    def test_a_failed_adopted_inventory_is_agent_block(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine()
        h.adopted(GhReply(1, "", "db locked"))
        result = h.run()
        assert result.block is not None
        assert result.block.reason == "agent_block"
        assert "adopted-PR inventory failed" in result.block.details
        assert "exited 1" in result.block.details
        assert "db locked" in result.block.details

    @pytest.mark.parametrize("code", [127, 2])
    def test_an_old_or_missing_cw_is_a_distinct_inventory_failure(
        self, h: Harness, real_script: Path, code: int
    ) -> None:
        h.mine()
        h.adopted(GhReply(code, "", ""))
        result = h.run()
        assert result.block is not None
        assert "older than this command" in result.block.details

    def test_an_adopted_inventory_that_never_finishes_is_agent_block(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine()
        h.adopted(None)
        result = h.run()
        assert result.block is not None
        assert "did not finish" in result.block.details

    def test_a_failed_listing_behind_adopted_rows_is_agent_block(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine()
        h.adopted(GhReply(0, "12 feature/adopted\n", ""))
        h.gh.reply("pr", "list", "--state", "open", "--json", returncode=1)
        result = h.run()
        assert result.block is not None
        assert result.block.reason == "agent_block"
        assert "open-PR inventory for adopted rows failed" in result.block.details

    def test_adopted_rows_become_candidates_by_number_or_ref(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine()
        h.adopted(GhReply(0, "12 feature/adopted\n13\n", ""))
        h.everyone((12, "feature/adopted"), (13, "feature/by-number"), (14, "dev/x"))
        h.diff(12, "docs/a.md").diff(13, "docs/b.md")
        assert h.run().conflicts == []
        diffs = [a[3] for a in h.gh.argvs() if a[:3] == ["gh", "pr", "diff"]]
        assert diffs == ["12", "13"]

    def test_a_pr_that_is_mine_and_adopted_is_checked_once(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/other"))
        h.adopted(GhReply(0, "9 dev/other\n", ""))
        h.everyone((9, "dev/other"))
        h.diff(9, "docs/a.md")
        h.run()
        diffs = [a for a in h.gh.argvs() if a[:3] == ["gh", "pr", "diff"]]
        assert len(diffs) == 1


# --- the real merge-tree probe ------------------------------------------------


class TestRealMergeTree:
    def _other_pr_ref(self, h: Harness, number: int, text: str) -> None:
        """Publish ``refs/pull/<n>/head``: a commit off main editing ``src/a.py``."""
        work = h.worktree
        git_in(work, "checkout", "-q", "--detach", h.fork)
        (work / "src").mkdir(exist_ok=True)
        (work / "src" / "a.py").write_text(text, encoding="utf-8")
        git_in(work, "add", "-A")
        git_in(work, "commit", "-m", "other pr")
        git_in(work, "push", "origin", f"HEAD:refs/pull/{number}/head")
        git_in(work, "checkout", "-q", _BRANCH)

    def test_a_textual_conflict_blocks(self, h: Harness, real_script: Path) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        self._other_pr_ref(h, 9, "x = 2\n")
        (clause,) = h.run().conflicts
        assert "conflicts textually with this branch" in clause

    def test_an_identical_edit_merges_cleanly_and_does_not_block(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        self._other_pr_ref(h, 9, "x = 1\n")
        assert h.run().conflicts == []

    def test_an_unfetchable_pull_ref_fails_closed(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        (clause,) = h.run().conflicts
        assert "fetch failed" in clause
        assert "failing closed" in clause

    def test_an_unavailable_merge_tree_fails_closed_with_its_detail(
        self, h: Harness, real_script: Path
    ) -> None:
        h.mine((9, "dev/other")).diff(9, "src/a.py")
        self._other_pr_ref(h, 9, "x = 2\n")
        unavailable = MergeTreeResult("unavailable", None, (), "git too old")
        with patch(
            "cw.codex_finalize._merge_gate.merge_tree", return_value=unavailable
        ):
            (clause,) = h.run().conflicts
        assert "merge-tree probe failed" in clause
        assert "unavailable: git too old" in clause
        assert "failing closed" in clause
