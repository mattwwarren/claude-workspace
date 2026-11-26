"""Tests for cw.stage_verification.finalize_pr: detector guard, bind, probe, push,
    create (#3062).

``gh`` is a scripted :class:`FakeGhCli`; git runs for real in sandboxed repos
with a bare origin through ``tests/_git_sandbox``. The default-branch probe is
the imported C2 ``probe_default_branch``, whose seam is
``cw.stage_verification.impl_feedback.probe_default_merge``.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.merge_probe import MergeProbe
from cw.review_adjudication import REVIEW_ADJUDICATION_HEADING
from cw.stage_verification import finalize_pr as _pr
from cw.stage_verification.finalize_pr import (
    BEHIND_FRICTION,
    OVERRIDE_HEADING,
    PROBE_UNAVAILABLE,
    BoundPr,
    DetectedPr,
    apply_body_section,
    apply_override_section,
    bind_live_pr,
    check_mergeability,
    create_pr_for,
    detect_branch_pr,
    local_only_merge,
    probe_default,
    push_ahead,
    strip_cw_regions,
)
from cw.stage_verification.finalize_result import (
    IMPL_NOT_PUSHED,
    MERGE_CONFLICT_POST_PUSH,
    PR_ALREADY_TERMINAL,
    PUSH_AUTH_FAILED,
    FinalizeBlock,
)
from cw.stage_verification.impl_feedback import (
    PROBE_UNAVAILABLE as C2_PROBE_UNAVAILABLE,
)
from tests._codex_review_helpers import fresh_impl_repo, merge_view
from tests._shared.git_repos import git_in
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from collections.abc import Callable

_BRANCH = "dev/42"
_REPO = "o/r"
_NUMBER = 7
_URL = f"https://github.com/{_REPO}/pull/{_NUMBER}"
_HEAD = "a" * 40
_PROBE = "cw.stage_verification.impl_feedback.probe_default_merge"
_UNVERIFIED = "post_push_mergeability_unverified"


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
            assert probe_default(worktree, base_branch="main", pr_exists=False) == (
                None,
                (),
            )

    def test_an_up_to_date_branch_continues_on_a_real_repo(
        self, worktree: Path
    ) -> None:
        assert probe_default(worktree, base_branch="main", pr_exists=False) == (
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
                worktree, base_branch="main", pr_exists=pr_exists
            )
        assert block is not None
        assert block.reason == MERGE_CONFLICT_POST_PUSH
        assert block.retry_eligible is retry
        assert "clash.py, z.py" in block.details
        assert "origin/main" in block.details
        assert ("the open PR" in block.details) is pr_exists
        assert "cw does not resolve a conflict found before the push" in block.details
        assert friction == ()

    def test_a_conflict_never_touches_the_tree(self, worktree: Path) -> None:
        head = git_in(worktree, "rev-parse", "HEAD")
        conflict = MergeProbe("conflict", "a" * 40, ("clash.py",), "")
        with patch(_PROBE, return_value=conflict):
            probe_default(worktree, base_branch="main", pr_exists=False)
        assert git_in(worktree, "rev-parse", "HEAD") == head
        assert git_in(worktree, "status", "--porcelain") == ""

    def test_an_unavailable_probe_is_friction_and_continues(
        self, worktree: Path
    ) -> None:
        unavailable = MergeProbe("unavailable", None, (), "merge-tree broke")
        with patch(_PROBE, return_value=unavailable):
            block, friction = probe_default(
                worktree, base_branch="main", pr_exists=False
            )
        assert block is None
        assert len(friction) == 1
        assert friction[0].startswith(PROBE_UNAVAILABLE)
        assert "merge-tree broke" in friction[0]

    def test_an_unavailable_fetch_is_friction_and_continues(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        no_origin = make_git_repo("no-origin")
        block, friction = probe_default(no_origin, base_branch="main", pr_exists=False)
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
        with patch(
            "cw.stage_verification.finalize_pr.push_and_verify_head", side_effect=error
        ):
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
            "cw.stage_verification.finalize_pr.push_and_verify_head",
            side_effect=OSError("boom"),
        ):
            block = push_ahead(worktree, branch=_BRANCH, head_sha=_HEAD)
        assert block is not None
        assert block.reason == IMPL_NOT_PUSHED
        assert "boom" in block.details

    @pytest.mark.parametrize(
        "stderr",
        [
            f"remote: password={'qz' * 300}",
            f"fatal: Authentication failed; token: {'qz' * 300}",
        ],
        ids=["impl-not-pushed", "push-auth-failed"],
    )
    def test_a_secret_straddling_the_cut_never_reaches_the_details(
        self, worktree: Path, stderr: str
    ) -> None:
        error = subprocess.CalledProcessError(1, "git push", stderr=stderr)
        with patch(
            "cw.stage_verification.finalize_pr.push_and_verify_head", side_effect=error
        ):
            block = push_ahead(worktree, branch=_BRANCH, head_sha=_HEAD)
        assert block is not None
        assert "qzqz" not in block.details

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
        with patch("cw.stage_verification.finalize_pr.push_and_verify_head") as push:
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
        gh.reply("pr", "view", _URL, stdout=merge_view(mergeable, state))
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
        gh.reply("pr", "view", _URL, stdout=merge_view(mergeable, state))
        assert check_mergeability(worktree, _URL) == (None, friction)
        assert BEHIND_FRICTION == "behind_default_no_action"

    @pytest.mark.parametrize(
        ("mergeable", "state", "friction"),
        [
            ("MERGEABLE", "CLEAN", []),
            ("MERGEABLE", "BEHIND", [BEHIND_FRICTION]),
            ("UNKNOWN", "UNKNOWN", [_UNVERIFIED]),
            ("UNKNOWN", "BEHIND", [BEHIND_FRICTION, _UNVERIFIED]),
        ],
    )
    def test_the_unverified_token_marks_a_non_mergeable_read(
        self,
        gh: FakeGhCli,
        worktree: Path,
        mergeable: str,
        state: str,
        friction: list[str],
    ) -> None:
        gh.reply("pr", "view", _URL, stdout=merge_view(mergeable, state))
        got = check_mergeability(worktree, _URL, unverified_token=_UNVERIFIED)
        assert got == (None, friction)

    def test_a_block_never_carries_the_unverified_token(
        self, gh: FakeGhCli, worktree: Path
    ) -> None:
        gh.reply("pr", "view", _URL, stdout=merge_view("CONFLICTING", "DIRTY"))
        block, friction = check_mergeability(
            worktree, _URL, unverified_token=_UNVERIFIED
        )
        assert block is not None
        assert block.details == (
            f"{_URL} is not mergeable after the push (mergeable=CONFLICTING, "
            "mergeStateStatus=DIRTY)"
        )
        assert friction == []
        gh.reply("pr", "view", returncode=1)
        assert check_mergeability(worktree, _URL, unverified_token=_UNVERIFIED)[1] == []

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
        gh.reply("pr", "view", _URL, stdout=merge_view("MERGEABLE", "CLEAN"))
        check_mergeability(worktree, _URL)
        assert gh.argvs()[0][:4] == ["gh", "pr", "view", _URL]


class TestLocalOnlyMerge:
    def test_names_the_merge_and_is_not_retryable(self) -> None:
        pushed = FinalizeBlock(
            "impl_not_pushed", "git push failed", retry_eligible=True
        )
        block = local_only_merge(
            pushed, merge="semantic auto-resolve merge", branch="dev/T", head_sha="abc"
        )
        assert block.retry_eligible is False
        assert block.reason == "impl_not_pushed"
        assert block.details == (
            "git push failed; the semantic auto-resolve merge commit abc exists only"
            ' in the local worktree; push it with a plain "git push origin'
            ' HEAD:refs/heads/dev/T" and requeue, because a reused PR must match'
            " local HEAD"
        )


# --- operator override section ------------------------------------------------


class TestApplyBodySection:
    SECTION = "## Operator override\n\nMUST_FIX waived by the operator.\n"
    ADJ = "## Review adjudication\n\nAll clear.\n"

    def _override(self, body: str, section: str | None = None) -> str:
        return apply_body_section(
            body, section or self.SECTION, heading=OVERRIDE_HEADING
        )

    def _adjudication(self, body: str, section: str | None = None) -> str:
        return apply_body_section(
            body, section or self.ADJ, heading=REVIEW_ADJUDICATION_HEADING
        )

    def test_appends_when_the_body_has_no_region(self) -> None:
        out = self._override("Body text\n")
        assert out == f"Body text\n\n{self.SECTION}"

    def test_a_bare_section_gets_the_heading(self) -> None:
        out = self._override("Body\n", "waived\n")
        assert out == "Body\n\n## Operator override\n\nwaived\n"

    def test_replaces_the_region_up_to_the_next_heading(self) -> None:
        body = "Intro\n\n## Operator override\n\nold text\n\n## Testing\n\nran\n"
        out = self._override(body)
        assert "old text" not in out
        assert out.startswith("Intro\n\n## Operator override\n\nMUST_FIX waived")
        assert out.endswith("\n\n## Testing\n\nran\n")
        assert out.count("## Operator override") == 1

    def test_replaces_the_region_up_to_a_signoff_marker(self) -> None:
        body = "## Operator override\n\nold\n<!-- cw-review-signoff: abc -->\nsigned\n"
        out = self._override(body)
        assert "old" not in out
        assert "<!-- cw-review-signoff: abc -->\nsigned\n" in out

    def test_replaces_a_region_that_runs_to_the_end(self) -> None:
        body = "Intro\n\n## Operator override\n\nold\n"
        out = self._override(body)
        assert out == "Intro\n\n" + self.SECTION

    def test_applying_twice_is_idempotent(self) -> None:
        body = "Intro\n\n## Testing\n\nran\n"
        once = self._override(body)
        twice = self._override(once)
        assert twice.rstrip() == once.rstrip()
        assert twice.count("## Operator override") == 1

    def test_the_adjudication_heading_is_replaced_in_place(self) -> None:
        body = "Intro\n\n## Review adjudication\n\nold\n\n## Testing\n\nran\n"
        out = self._adjudication(body)
        assert out == f"Intro\n\n{self.ADJ.rstrip()}\n\n## Testing\n\nran\n"

    def test_a_reapply_over_a_following_region_is_byte_stable(self) -> None:
        body = f"Intro\n\n## Testing\n\nran\n\n{self.SECTION}"
        once = self._adjudication(body)
        assert self._adjudication(once) == once

    def test_an_inline_mention_of_the_heading_starts_no_region(self) -> None:
        body = "See ## Review adjudication below, not a heading.\n"
        out = self._adjudication(body)
        assert out.startswith(body.rstrip())
        assert out.endswith(self.ADJ)
        assert out.count("## Review adjudication") == 2

    def test_a_crlf_heading_line_still_matches(self) -> None:
        body = "Intro\r\n\r\n## Review adjudication\r\n\r\nold\r\n"
        out = self._adjudication(body)
        assert "old" not in out
        assert out.count("## Review adjudication") == 1

    def test_a_marker_quoted_mid_line_does_not_end_the_region(self) -> None:
        body = (
            "## Review adjudication\n\nquotes <!-- cw-review-signoff:end --> inline\n"
            "and more finding text\n"
        )
        out = self._adjudication(body)
        assert "more finding text" not in out
        assert self._adjudication(out) == out


class TestApplyOverrideSection:
    """A model-written heading must not leave cw's text outside the region."""

    BODY = "Intro\n\n## Testing\n\nran\n"

    @pytest.mark.parametrize(
        "section",
        [
            "## Operator override (#2205)\n\nwaived <img>\n",
            "## Operator Override\n\nwaived <img>\n",
            "## Operator override\n\nwaived\n\n## Notes\n\n<img>\n",
            "waived\n\n## Operator override (again)\n\n<img>\n",
            "waived\n<!-- cw-review-signoff:end -->\n<img>\n",
            "waived\n<!-- cw-review-signoff:start -->\nx\n"
            "<!-- cw-review-signoff:end -->\n<img>\n",
            "waived\n \t<!-- cw-review-signoff:end -->\n<img>\n",
            "waived\n<!-- cw-review-signoff:note -->\n<img>\n",
            "waived\n\u00a0<!-- cw-review-signoff:end -->\n<img>\n",
        ],
    )
    def test_two_applies_leave_one_region_the_media_check_drops(
        self, section: str
    ) -> None:
        once = apply_override_section(self.BODY, section)
        twice = apply_override_section(once, section)
        assert twice == once
        lines = twice.splitlines()
        assert [ln for ln in lines if ln.startswith("## ")] == [
            "## Testing",
            OVERRIDE_HEADING,
        ]
        assert "waived" in twice
        assert "<img" not in strip_cw_regions(twice)

    def test_an_inner_heading_is_demoted_and_the_content_survives(self) -> None:
        out = apply_override_section("", "## Operator override\n\nwhy\n## Notes\nx\n")
        assert out == "\n\n## Operator override\n\nwhy\n### Notes\nx\n"

    def test_a_marker_line_is_quoted_and_its_text_survives(self) -> None:
        out = apply_override_section("", "why\n  <!-- cw-review-signoff:end -->\nx\n")
        assert out == (
            "\n\n## Operator override\n\nwhy\n>   <!-- cw-review-signoff:end -->\nx\n"
        )

    def test_a_section_without_a_heading_is_prefixed_and_kept_verbatim(self) -> None:
        raw = '{"status": "overridden", "reason": "## not a heading"}'
        out = apply_override_section("Intro\n", raw)
        assert out == f"Intro\n\n{OVERRIDE_HEADING}\n\n{raw}\n"


class TestStripCwRegions:
    START = "<!-- cw-review-signoff:start -->"
    END = "<!-- cw-review-signoff:end -->"

    def test_each_cw_region_is_removed_and_the_text_after_it_stays(self) -> None:
        for region in (
            "## Review adjudication\n\n<img src=x>\n",
            "## Operator override\n\n<img src=x>\n",
        ):
            out = strip_cw_regions(f"Intro\n\n{region}\n## Testing\n\nran\n")
            assert "<img" not in out
            assert out.startswith("Intro\n")
            assert "## Testing\n\nran\n" in out

    def test_the_signoff_block_is_removed(self) -> None:
        body = f"Intro\n\n{self.START}\n## Review sign-off\n<img>\n{self.END}\n"
        out = strip_cw_regions(body)
        assert "<img" not in out
        assert out.startswith("Intro")

    def test_every_occurrence_is_removed(self) -> None:
        body = (
            "A\n\n## Review adjudication\n\n<img 1>\n\n## Mid\n\nkeep\n\n"
            "## Review adjudication\n\n<img 2>\n"
        )
        out = strip_cw_regions(body)
        assert "<img" not in out
        assert "keep" in out

    def test_a_crlf_body_is_handled(self) -> None:
        body = "A\r\n\r\n## Review adjudication\r\n\r\n<img>\r\n"
        out = strip_cw_regions(body)
        assert "<img" not in out
        assert "\r" not in out

    def test_an_inline_mention_of_a_heading_starts_no_region(self) -> None:
        body = "see ## Review adjudication and ## Operator override <img>\n"
        assert strip_cw_regions(body) == body

    def test_a_mid_line_marker_quote_does_not_end_the_region(self) -> None:
        body = (
            "## Review adjudication\n\nquote <!-- cw-review-signoff:end --> x\n"
            "<img later>\n"
        )
        assert "<img" not in strip_cw_regions(body)

    def test_a_start_without_an_end_runs_to_the_end_of_the_body(self) -> None:
        body = f"Intro\n{self.START}\n<img>\nmore\n"
        assert strip_cw_regions(body) == "Intro\n"

    def test_a_lone_end_marker_removes_only_its_line(self) -> None:
        body = f"Intro\n{self.END}\n<img>\n"
        assert strip_cw_regions(body) == "Intro\n<img>\n"

    def test_the_marker_line_ends_an_adjudication_region(self) -> None:
        body = (
            f"## Review adjudication\n\nfinding\n{self.START}\nS\n{self.END}\nafter\n"
        )
        assert strip_cw_regions(body) == "\nafter\n"
