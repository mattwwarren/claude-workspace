"""Tests for cw.codex_finalize._pr: detector guard, bind, probe, push, create (#3062).

``gh`` is a scripted :class:`FakeGhCli`; git runs for real in sandboxed repos
with a bare origin through ``tests/_git_sandbox``. The default-branch probe is
the imported C2 ``probe_default_branch``, whose seam is
``cw.codex_impl._feedback.probe_default_merge``.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.codex_finalize import _pr
from cw.codex_finalize._pr import (
    BEHIND_FRICTION,
    PROBE_UNAVAILABLE,
    BoundPr,
    DetectedPr,
    apply_override_section,
    bind_live_pr,
    check_mergeability,
    create_pr_for,
    detect_branch_pr,
    probe_default,
    push_ahead,
)
from cw.codex_finalize._result import (
    IMPL_NOT_PUSHED,
    MERGE_CONFLICT_POST_PUSH,
    PR_ALREADY_TERMINAL,
    PUSH_AUTH_FAILED,
    FinalizeBlock,
)
from cw.codex_impl._feedback import PROBE_UNAVAILABLE as C2_PROBE_UNAVAILABLE
from cw.merge_probe import MergeProbe
from tests._codex_review_helpers import fresh_impl_repo
from tests._shared.git_repos import git_in
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from collections.abc import Callable

_BRANCH = "dev/42"
_REPO = "o/r"
_NUMBER = 7
_URL = f"https://github.com/{_REPO}/pull/{_NUMBER}"
_HEAD = "a" * 40
_PROBE = "cw.codex_impl._feedback.probe_default_merge"


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    return FakeGhCli().install(monkeypatch)


@pytest.fixture
def worktree(make_git_repo: Callable[..., Path]) -> Path:
    repo, _origin = fresh_impl_repo(make_git_repo, "pr-wt", _BRANCH)
    return repo


def _pr_list(*items: tuple[int, str, str | None]) -> str:
    return json.dumps(
        [
            {"number": n, "url": f"https://github.com/{_REPO}/pull/{n}", "state": s}
            | {"mergedAt": merged}
            for n, s, merged in items
        ]
    )


def _view(**overrides: object) -> str:
    body: dict[str, object] = {
        "number": _NUMBER,
        "url": _URL,
        "headRefName": _BRANCH,
        "headRefOid": _HEAD,
    }
    body.update(overrides)
    return json.dumps(body)


def _script_live(gh: FakeGhCli, **overrides: object) -> FakeGhCli:
    gh.reply("repo", "view", stdout=json.dumps({"nameWithOwner": _REPO}))
    gh.reply("pr", "view", _URL, stdout=_view(**overrides))
    return gh


def _bind(
    worktree: Path,
    *,
    url: str = _URL,
    number: int = _NUMBER,
    branch: str = _BRANCH,
    require: bool = True,
) -> BoundPr | FinalizeBlock:
    return bind_live_pr(
        worktree,
        pr_number=number,
        pr_url=url,
        branch=branch,
        head_sha=_HEAD,
        require_head_match=require,
    )


# --- Detector Guard -----------------------------------------------------------


class TestDetectBranchPr:
    def test_no_pr_is_none(self, gh: FakeGhCli, worktree: Path) -> None:
        gh.reply("pr", "list", stdout="[]")
        assert detect_branch_pr(worktree, _BRANCH) is None
        (call,) = gh.argvs()
        assert call[:5] == ["gh", "pr", "list", "--head", _BRANCH]
        assert "--state" in call
        assert gh.records[0].cwd == worktree

    def test_an_open_pr_is_reused(self, gh: FakeGhCli, worktree: Path) -> None:
        gh.reply("pr", "list", stdout=_pr_list((7, "OPEN", None)))
        assert detect_branch_pr(worktree, _BRANCH) == DetectedPr(7, _URL, "OPEN")

    def test_open_wins_over_merged_and_closed(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        gh.reply(
            "pr",
            "list",
            stdout=_pr_list(
                (5, "CLOSED", None),
                (6, "MERGED", "2026-11-01T00:00:00Z"),
                (7, "OPEN", None),
            ),
        )
        found = detect_branch_pr(worktree, _BRANCH)
        assert isinstance(found, DetectedPr)
        assert (found.number, found.state) == (7, "OPEN")

    def test_a_merged_pr_is_found(self, gh: FakeGhCli, worktree: Path) -> None:
        gh.reply("pr", "list", stdout=_pr_list((6, "MERGED", "2026-11-01T00:00:00Z")))
        found = detect_branch_pr(worktree, _BRANCH)
        assert isinstance(found, DetectedPr)
        assert found.state == "MERGED"

    def test_closed_only_is_pr_already_terminal(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        gh.reply("pr", "list", stdout=_pr_list((5, "CLOSED", None)))
        block = detect_branch_pr(worktree, _BRANCH)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == PR_ALREADY_TERMINAL
        assert block.retry_eligible is False
        assert "PR #5" in block.details
        assert "no duplicate" in block.details

    def test_a_lookup_failure_is_operator_unavailable(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        gh.reply("pr", "list", returncode=1, stderr="HTTP 502")
        block = detect_branch_pr(worktree, _BRANCH)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True
        assert _BRANCH in block.details


# --- live bind ----------------------------------------------------------------


class TestBindLivePr:
    def test_all_matching_binds_to_the_canonical_url(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        _script_live(gh)
        assert _bind(worktree) == BoundPr(_NUMBER, _URL)
        view = [a for a in gh.argvs() if a[:3] == ["gh", "pr", "view"]]
        assert view[0][3] == _URL
        assert "number,url,headRefName,headRefOid" in view[0]

    def test_a_noncanonical_printed_url_binds_to_the_canonical_one(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        _script_live(gh)
        bound = _bind(worktree, url=f"{_URL}?x=1")
        assert bound == BoundPr(_NUMBER, _URL)

    def test_an_unreadable_live_repo_is_operator_unavailable(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        gh.reply("repo", "view", returncode=1, stderr="no")
        block = _bind(worktree)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True

    def test_an_unreadable_live_pr_is_operator_unavailable(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        gh.reply("repo", "view", stdout=json.dumps({"nameWithOwner": _REPO}))
        gh.reply("pr", "view", returncode=1)
        block = _bind(worktree)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True

    @pytest.mark.parametrize(
        ("url", "number"),
        [
            ("https://github.com/other/repo/pull/7", 7),
            (_URL, 8),
            ("not a pr url", 7),
        ],
    )
    def test_identity_mismatch_is_not_retryable(
        self, gh: FakeGhCli, worktree: Path, url: str, number: int
    ) -> None:
        gh.reply("repo", "view", stdout=json.dumps({"nameWithOwner": _REPO}))
        block = _bind(worktree, url=url, number=number)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "external_state_block"
        assert block.retry_eligible is False
        assert "does not bind to the live repo" in block.details
        assert [a[:3] for a in gh.argvs()] == [["gh", "repo", "view"]]

    @pytest.mark.parametrize(
        "overrides",
        [{"url": "https://github.com/o/r/pull/99"}, {"headRefName": "dev/other"}],
    )
    def test_a_live_view_that_disagrees_is_not_retryable(
        self, gh: FakeGhCli, worktree: Path, overrides: dict[str, object]
    ) -> None:
        _script_live(gh, **overrides)
        block = _bind(worktree)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "external_state_block"
        assert block.retry_eligible is False
        assert "nothing was armed" in block.details

    def test_a_head_oid_mismatch_is_retryable(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        _script_live(gh, headRefOid="b" * 40)
        block = _bind(worktree)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "external_state_block"
        assert block.retry_eligible is True
        assert "b" * 40 in block.details
        assert _HEAD in block.details

    def test_a_merged_pr_does_not_compare_the_oid(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        _script_live(gh, headRefOid="b" * 40)
        assert _bind(worktree, require=False) == BoundPr(_NUMBER, _URL)

    def test_a_merged_pr_still_checks_identity(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        _script_live(gh, headRefName="dev/other")
        block = _bind(worktree, require=False)
        assert isinstance(block, FinalizeBlock)
        assert block.retry_eligible is False


# --- default-branch probe -----------------------------------------------------


class TestProbeDefault:
    def test_a_clean_probe_continues(self, worktree: Path) -> None:
        clean = MergeProbe("clean", "a" * 40, (), "")
        with patch(_PROBE, return_value=clean):
            assert probe_default(worktree, default_branch="main", pr_exists=False) == (
                None,
                (),
            )

    def test_an_up_to_date_branch_continues_on_a_real_repo(
        self, worktree: Path
    ) -> None:
        assert probe_default(worktree, default_branch="main", pr_exists=False) == (
            None,
            (),
        )

    @pytest.mark.parametrize(("pr_exists", "retry"), [(False, False), (True, True)])
    def test_a_conflict_is_merge_conflict_post_push(
        self, worktree: Path, pr_exists: bool, retry: bool
    ) -> None:
        conflict = MergeProbe("conflict", "a" * 40, ("clash.py", "z.py"), "")
        with patch(_PROBE, return_value=conflict):
            block, friction = probe_default(
                worktree, default_branch="main", pr_exists=pr_exists
            )
        assert block is not None
        assert block.reason == MERGE_CONFLICT_POST_PUSH
        assert block.retry_eligible is retry
        assert "clash.py, z.py" in block.details
        assert "origin/main" in block.details
        assert ("the open PR" in block.details) is pr_exists
        assert friction == ()

    def test_a_conflict_never_touches_the_tree(self, worktree: Path) -> None:
        head = git_in(worktree, "rev-parse", "HEAD")
        conflict = MergeProbe("conflict", "a" * 40, ("clash.py",), "")
        with patch(_PROBE, return_value=conflict):
            probe_default(worktree, default_branch="main", pr_exists=False)
        assert git_in(worktree, "rev-parse", "HEAD") == head
        assert git_in(worktree, "status", "--porcelain") == ""

    def test_an_unavailable_probe_is_friction_and_continues(
        self, worktree: Path
    ) -> None:
        unavailable = MergeProbe("unavailable", None, (), "merge-tree broke")
        with patch(_PROBE, return_value=unavailable):
            block, friction = probe_default(
                worktree, default_branch="main", pr_exists=False
            )
        assert block is None
        assert len(friction) == 1
        assert friction[0].startswith(PROBE_UNAVAILABLE)
        assert "merge-tree broke" in friction[0]

    def test_an_unavailable_fetch_is_friction_and_continues(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        no_origin = make_git_repo("no-origin")
        block, friction = probe_default(
            no_origin, default_branch="main", pr_exists=False
        )
        assert block is None
        assert friction
        assert friction[0].startswith(PROBE_UNAVAILABLE)

    def test_the_friction_literal_is_the_imported_c2_constant(self) -> None:
        assert PROBE_UNAVAILABLE is C2_PROBE_UNAVAILABLE
        assert PROBE_UNAVAILABLE == "merge_probe_unavailable"

    def test_the_module_defines_no_fetch_or_probe_helper_of_its_own(self) -> None:
        source = Path(_pr.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert not {n for n in defined if "fetch" in n or "merge_tree" in n}
        assert "merge_tree" not in source
        assert '"fetch"' not in source
        assert "probe_default_merge" not in source


# --- ahead-push ---------------------------------------------------------------


def _commit(repo: Path, name: str = "feature.py") -> str:
    (repo / name).write_text("x = 1\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", f"add {name}")
    return git_in(repo, "rev-parse", "HEAD")


class TestPushAhead:
    def test_pushes_and_verifies_the_head(self, worktree: Path) -> None:
        head = _commit(worktree)
        assert push_ahead(worktree, branch=_BRANCH, head_sha=head) is None
        assert git_in(worktree, "rev-parse", f"origin/{_BRANCH}") == head

    def test_a_signature_match_is_push_auth_failed(self, worktree: Path) -> None:
        error = subprocess.CalledProcessError(
            128, "git push", stderr="fatal: Authentication failed for 'x'"
        )
        with patch("cw.codex_finalize._pr.push_and_verify_head", side_effect=error):
            block = push_ahead(worktree, branch=_BRANCH, head_sha=_HEAD)
        assert block is not None
        assert block.reason == PUSH_AUTH_FAILED
        assert block.retry_eligible is True
        assert "unavailability signature" in block.details

    def test_any_other_failure_is_impl_not_pushed(self, worktree: Path) -> None:
        git_in(worktree, "remote", "remove", "origin")
        head = _commit(worktree)
        block = push_ahead(worktree, branch=_BRANCH, head_sha=head)
        assert block is not None
        assert block.reason == IMPL_NOT_PUSHED
        assert block.retry_eligible is False
        assert f"git push origin {_BRANCH} failed" in block.details

    def test_an_oserror_is_impl_not_pushed(self, worktree: Path) -> None:
        with patch(
            "cw.codex_finalize._pr.push_and_verify_head", side_effect=OSError("boom")
        ):
            block = push_ahead(worktree, branch=_BRANCH, head_sha=_HEAD)
        assert block is not None
        assert block.reason == IMPL_NOT_PUSHED
        assert "boom" in block.details

    def test_a_remote_tip_mismatch_is_impl_not_pushed(self, worktree: Path) -> None:
        _commit(worktree)
        block = push_ahead(worktree, branch=_BRANCH, head_sha="c" * 40)
        assert block is not None
        assert block.reason == IMPL_NOT_PUSHED

    def test_the_branch_is_re_asserted_before_any_push(
        self, worktree: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        git_in(worktree, "checkout", "-b", "dev/elsewhere")
        head = _commit(worktree)
        before = git_in(worktree, "ls-remote", "origin")
        with patch("cw.codex_finalize._pr.push_and_verify_head") as push:
            block = push_ahead(worktree, branch=_BRANCH, head_sha=head)
        assert block is not None
        assert block.reason == "external_state_block"
        assert block.retry_eligible is False
        push.assert_not_called()
        assert git_in(worktree, "ls-remote", "origin") == before


# --- gh pr create -------------------------------------------------------------


class TestCreatePr:
    def test_builds_the_command_with_a_body_file_and_no_draft(
        self, gh: FakeGhCli, worktree: Path, tmp_path: Path
    ) -> None:
        body = tmp_path / "body.md"
        body.write_text("the body\n", encoding="utf-8")
        gh.reply("pr", "create", stdout=f"warning: x\n{_URL}\n")
        created = create_pr_for(
            worktree, base="main", branch=_BRANCH, title="feat: x (#42)", body_file=body
        )
        assert created == (_NUMBER, _URL)
        (call,) = gh.records
        assert call.argv == [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            _BRANCH,
            "--title",
            "feat: x (#42)",
            "--body-file",
            str(body),
        ]
        assert "--draft" not in call.argv
        assert call.body_file == "the body\n"
        assert call.cwd == worktree

    def test_a_failed_create_is_operator_unavailable(
        self, gh: FakeGhCli, worktree: Path, tmp_path: Path
    ) -> None:
        gh.reply("pr", "create", returncode=1, stderr="HTTP 502")
        body = tmp_path / "b.md"
        body.write_text("b", encoding="utf-8")
        block = create_pr_for(
            worktree, base="main", branch=_BRANCH, title="t", body_file=body
        )
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True
        assert "HTTP 502" in block.details

    def test_a_create_that_cannot_run_is_operator_unavailable(
        self, gh: FakeGhCli, worktree: Path, tmp_path: Path
    ) -> None:
        gh.reply("pr", "create", raises=OSError("no gh"))
        body = tmp_path / "b.md"
        body.write_text("b", encoding="utf-8")
        block = create_pr_for(
            worktree, base="main", branch=_BRANCH, title="t", body_file=body
        )
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "operator_unavailable"

    @pytest.mark.parametrize("stdout", ["", "created something\n"])
    def test_output_with_no_pr_url_is_operator_unavailable(
        self, gh: FakeGhCli, worktree: Path, tmp_path: Path, stdout: str
    ) -> None:
        gh.reply("pr", "create", stdout=stdout)
        body = tmp_path / "b.md"
        body.write_text("b", encoding="utf-8")
        block = create_pr_for(
            worktree, base="main", branch=_BRANCH, title="t", body_file=body
        )
        assert isinstance(block, FinalizeBlock)
        assert block.reason == "operator_unavailable"
        assert "no PR URL" in block.details


# --- mergeability -------------------------------------------------------------


def _merge_view(mergeable: str, state: str) -> str:
    return json.dumps({"mergeable": mergeable, "mergeStateStatus": state})


class TestCheckMergeability:
    @pytest.mark.parametrize(
        ("mergeable", "state"),
        [
            ("CONFLICTING", "DIRTY"),
            ("CONFLICTING", "CLEAN"),
            ("UNKNOWN", "DIRTY"),
        ],
    )
    def test_conflicting_or_dirty_blocks(
        self, gh: FakeGhCli, worktree: Path, mergeable: str, state: str
    ) -> None:
        gh.reply("pr", "view", _URL, stdout=_merge_view(mergeable, state))
        block, friction = check_mergeability(worktree, _URL)
        assert block is not None
        assert block.reason == MERGE_CONFLICT_POST_PUSH
        assert block.retry_eligible is True
        assert friction == []

    @pytest.mark.parametrize(
        ("mergeable", "state", "friction"),
        [
            ("MERGEABLE", "CLEAN", []),
            ("MERGEABLE", "BLOCKED", []),
            ("MERGEABLE", "BEHIND", [BEHIND_FRICTION]),
            ("UNKNOWN", "UNKNOWN", []),
        ],
    )
    def test_behind_mergeable_and_blocked_proceed(
        self,
        gh: FakeGhCli,
        worktree: Path,
        mergeable: str,
        state: str,
        friction: list[str],
    ) -> None:
        gh.reply("pr", "view", _URL, stdout=_merge_view(mergeable, state))
        assert check_mergeability(worktree, _URL) == (None, friction)
        assert BEHIND_FRICTION == "behind_default_no_action"

    def test_an_unreadable_view_is_operator_unavailable(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        gh.reply("pr", "view", returncode=1)
        block, friction = check_mergeability(worktree, _URL)
        assert block is not None
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True
        assert friction == []

    def test_it_reads_by_the_bound_url(self, gh: FakeGhCli, worktree: Path) -> None:
        gh.reply("pr", "view", _URL, stdout=_merge_view("MERGEABLE", "CLEAN"))
        check_mergeability(worktree, _URL)
        assert gh.argvs()[0][:4] == ["gh", "pr", "view", _URL]


# --- operator override section ------------------------------------------------


class TestApplyOverrideSection:
    SECTION = "## Operator override\n\nMUST_FIX waived by the operator.\n"

    def test_appends_when_the_body_has_no_region(self) -> None:
        out = apply_override_section("Body text\n", self.SECTION)
        assert out == f"Body text\n\n{self.SECTION}"

    def test_a_bare_section_gets_the_heading(self) -> None:
        out = apply_override_section("Body\n", "waived\n")
        assert out == "Body\n\n## Operator override\n\nwaived\n"

    def test_replaces_the_region_up_to_the_next_heading(self) -> None:
        body = "Intro\n\n## Operator override\n\nold text\n\n## Testing\n\nran\n"
        out = apply_override_section(body, self.SECTION)
        assert "old text" not in out
        assert out.startswith("Intro\n\n## Operator override\n\nMUST_FIX waived")
        assert out.endswith("\n## Testing\n\nran\n")
        assert out.count("## Operator override") == 1

    def test_replaces_the_region_up_to_a_signoff_marker(self) -> None:
        body = "## Operator override\n\nold\n<!-- cw-review-signoff: abc -->\nsigned\n"
        out = apply_override_section(body, self.SECTION)
        assert "old" not in out
        assert "<!-- cw-review-signoff: abc -->\nsigned\n" in out

    def test_replaces_a_region_that_runs_to_the_end(self) -> None:
        body = "Intro\n\n## Operator override\n\nold\n"
        out = apply_override_section(body, self.SECTION)
        assert out == "Intro\n\n" + self.SECTION.rstrip("\n")

    def test_applying_twice_is_idempotent(self) -> None:
        body = "Intro\n\n## Testing\n\nran\n"
        once = apply_override_section(body, self.SECTION)
        twice = apply_override_section(once, self.SECTION)
        assert twice.rstrip() == once.rstrip()
        assert twice.count("## Operator override") == 1
