"""Review configuration is read from the default branch (#3038, RFC 0017 I2).

``.claude/review-extras.md``, ``.claude/review-policy.md`` and the
sensitive-files manifests are read from ``origin/<default>`` (else the local
``<default>``) through ``cw.codex_review._context._default_branch_config``,
never from the reviewed branch's working tree. Git is real throughout: every
scenario seeds the default branch with ``_seed_default_branch_config`` and then
branches, so "the branch edits its own configuration" is a real commit.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, ClassVar

import pytest

from cw.codex_review import _prepare_review_pass
from cw.codex_review._context import _load_review_policy, _load_sensitive_hits
from cw.codex_review._context._default_branch_config import (
    REVIEW_CONFIG_PATHS,
    REVIEW_EXTRAS_PATH,
    REVIEW_POLICY_PATH,
    SENSITIVE_CLAUDE_PATH,
    SENSITIVE_GITHUB_PATH,
    ReviewConfigSource,
    branch_modified_review_config,
    resolve_review_config_source,
    review_config_status,
)
from cw.codex_runner import FakeCodexRunner
from tests._codex_review_helpers import _seed_default_branch_config, _task
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

_LOGGER = "cw.codex_review._context._default_branch_config"
_CONFIG_RUN_GIT = "cw.codex_review._context._default_branch_config.run_git"
_CONFIG_GIT_STDOUT = (
    "cw.codex_review._context._default_branch_config.git_stdout_or_none"
)
_DIFF_BASE_RUN_GIT = "cw.review_convergence._diff_base.run_git"
_MISSING = "no-such-branch"

_MANIFEST = (
    'sensitive_files:\n  - path: "mod.py"\n    category: auth\n    reason: "gate"\n'
)
_EMPTY_MANIFEST = "sensitive_files: []\n"
_POLICY = "## Code Quality Reviewer\nDEFAULT-POLICY\n"
_BRANCH_POLICY = "## Architecture Reviewer\nBRANCH-POLICY\n"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER and record.levelno == logging.WARNING
    ]


def _seeded(
    make_git_repo: Callable[..., Path], name: str, files: dict[str, str]
) -> Path:
    repo = make_git_repo(name)
    _seed_default_branch_config(repo, files)
    return repo


def _branch(repo: Path, files: dict[str, str] | None = None) -> None:
    """Check out ``feature`` and commit ``mod.py`` plus any *files*."""
    git_in(repo, "checkout", "-b", "feature")
    for relpath, content in {"mod.py": "x = 1\n", **(files or {})}.items():
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        git_in(repo, "add", relpath)
    git_in(repo, "commit", "-m", "branch work")


def _source(repo: Path, branch: str = "main") -> ReviewConfigSource:
    return resolve_review_config_source(repo, branch)


class TestPathConstants:
    def test_the_four_paths_are_bytewise_sorted(self) -> None:
        assert tuple(sorted(REVIEW_CONFIG_PATHS)) == REVIEW_CONFIG_PATHS
        assert set(REVIEW_CONFIG_PATHS) == {
            REVIEW_EXTRAS_PATH,
            REVIEW_POLICY_PATH,
            SENSITIVE_CLAUDE_PATH,
            SENSITIVE_GITHUB_PATH,
        }


class TestResolveChain:
    def test_origin_resolves_first(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _seeded(make_git_repo, "chain-origin", {REVIEW_EXTRAS_PATH: "E\n"})

        source = _source(repo)

        assert source.resolved
        assert source.ref == "origin/main"
        assert source.commit == git_in(repo, "rev-parse", "origin/main")

    def test_origin_wins_over_a_local_main_that_is_ahead(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "chain-ahead", {REVIEW_EXTRAS_PATH: "ORIGIN\n"})
        (repo / REVIEW_EXTRAS_PATH).write_text("LOCAL-AHEAD\n", encoding="utf-8")
        git_in(repo, "commit", "-am", "local only")

        source = _source(repo)

        assert source.ref == "origin/main"
        assert source.read(REVIEW_EXTRAS_PATH) == "ORIGIN\n"

    def test_a_repo_with_no_origin_reads_the_local_branch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("chain-no-origin")
        (repo / REVIEW_EXTRAS_PATH).parent.mkdir(parents=True)
        (repo / REVIEW_EXTRAS_PATH).write_text("LOCAL\n", encoding="utf-8")
        git_in(repo, "add", REVIEW_EXTRAS_PATH)
        git_in(repo, "commit", "-m", "extras")

        source = _source(repo)

        assert source.ref == "main"
        assert source.read(REVIEW_EXTRAS_PATH) == "LOCAL\n"
        assert review_config_status(source).resolved

    def test_local_fallback_when_the_tracking_ref_is_deleted(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "chain-deleted", {REVIEW_EXTRAS_PATH: "E\n"})
        git_in(repo, "update-ref", "-d", "refs/remotes/origin/main")

        source = _source(repo)

        assert source.ref == "main"
        assert source.commit == git_in(repo, "rev-parse", "refs/heads/main")

    def test_a_tag_named_like_the_branch_does_not_shadow_it(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "chain-tag", {REVIEW_EXTRAS_PATH: "E\n"})
        branch_sha = git_in(repo, "rev-parse", "refs/heads/main")
        git_in(repo, "update-ref", "-d", "refs/remotes/origin/main")
        git_in(repo, "commit", "--allow-empty", "-m", "tag target")
        tag_target = git_in(repo, "rev-parse", "HEAD")
        git_in(repo, "reset", "--hard", branch_sha)
        git_in(repo, "tag", "main", tag_target)

        source = _source(repo)

        assert source.ref == "main"
        assert source.commit == branch_sha
        assert source.commit != tag_target

    def test_a_local_branch_named_origin_main_does_not_shadow_the_tracking_ref(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "chain-shadow", {REVIEW_EXTRAS_PATH: "E\n"})
        tracking = git_in(repo, "rev-parse", "refs/remotes/origin/main")
        git_in(repo, "commit", "--allow-empty", "-m", "other")
        git_in(repo, "update-ref", "refs/heads/origin/main", "HEAD")

        source = _source(repo)

        assert source.commit == tracking

    def test_neither_ref_resolving_is_unresolved_with_one_warning(
        self,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = _seeded(make_git_repo, "chain-none", {REVIEW_EXTRAS_PATH: "E\n"})

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            source = _source(repo, _MISSING)

        assert source.commit is None
        assert source.ref is None
        assert source.resolved is False
        messages = _warnings(caplog)
        assert len(messages) == 1
        assert (
            "neither origin/no-such-branch nor no-such-branch resolves in"
            in (messages[0])
        )
        assert str(repo) in messages[0]
        for path in REVIEW_CONFIG_PATHS:
            assert source.read(path) is None
            assert source.exists(path) is False

    def test_a_short_hex_default_branch_is_unresolved(
        self,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = _seeded(make_git_repo, "chain-short", {REVIEW_EXTRAS_PATH: "E\n"})
        short = git_in(repo, "rev-parse", "--short=7", "HEAD")

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            source = _source(repo, short)

        assert source.commit is None
        messages = _warnings(caplog)
        assert len(messages) == 1
        assert f"neither origin/{short} nor {short} resolves in" in messages[0]

    @pytest.mark.parametrize("commit_id", ["a" * 40, "b" * 64])
    def test_a_full_commit_id_is_unresolved_and_runs_no_git(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        commit_id: str,
    ) -> None:
        repo = make_git_repo("chain-commit-id")

        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "git must not run for a commit id"
            raise AssertionError(msg)

        monkeypatch.setattr(_CONFIG_RUN_GIT, _boom)
        monkeypatch.setattr(_DIFF_BASE_RUN_GIT, _boom)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            source = _source(repo, commit_id)

        assert source.commit is None
        messages = _warnings(caplog)
        assert len(messages) == 1
        assert "is a commit id, not a default branch name" in messages[0]
        assert commit_id in messages[0]
        assert source.read(REVIEW_EXTRAS_PATH) is None

    def test_git_that_cannot_run_is_swallowed_with_one_warning(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = make_git_repo("chain-no-git")

        def _no_git(*_args: object, **_kwargs: object) -> object:
            msg = "no git"
            raise OSError(msg)

        monkeypatch.setattr(_DIFF_BASE_RUN_GIT, _no_git)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            source = _source(repo)

        assert source.commit is None
        messages = _warnings(caplog)
        assert len(messages) == 1
        assert "git could not run in" in messages[0]
        assert str(repo) in messages[0]


class TestReadAndExists:
    def test_present_absent_and_blank(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _seeded(
            make_git_repo,
            "read-basic",
            {REVIEW_EXTRAS_PATH: "EXTRAS\n", REVIEW_POLICY_PATH: ""},
        )
        source = _source(repo)

        assert source.read(REVIEW_EXTRAS_PATH) == "EXTRAS\n"
        assert source.exists(REVIEW_EXTRAS_PATH) is True
        assert source.read(REVIEW_POLICY_PATH) == ""
        assert source.read(SENSITIVE_CLAUDE_PATH) is None
        assert source.exists(SENSITIVE_CLAUDE_PATH) is False

    def test_a_directory_at_the_path_reads_none_but_exists(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(
            make_git_repo, "read-dir", {f"{REVIEW_EXTRAS_PATH}/inner.txt": "x\n"}
        )
        source = _source(repo)

        assert source.read(REVIEW_EXTRAS_PATH) is None
        assert source.exists(REVIEW_EXTRAS_PATH) is True

    def test_a_non_utf8_blob_reads_none_but_exists(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("read-binary")
        target = repo / SENSITIVE_CLAUDE_PATH
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\xff\xfe\x00bad")
        git_in(repo, "add", SENSITIVE_CLAUDE_PATH)
        git_in(repo, "commit", "-m", "bad bytes")
        _seed_default_branch_config(repo, {})
        source = _source(repo)

        assert source.read(SENSITIVE_CLAUDE_PATH) is None
        assert source.exists(SENSITIVE_CLAUDE_PATH) is True

    def test_utf8_is_pinned_under_the_c_locale(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _seeded(make_git_repo, "read-locale", {REVIEW_EXTRAS_PATH: "café\n"})
        monkeypatch.setenv("LC_ALL", "C")
        monkeypatch.setenv("LANG", "C")

        assert _source(repo).read(REVIEW_EXTRAS_PATH) == "café\n"

    def test_crlf_reads_as_lf(self, make_git_repo: Callable[..., Path]) -> None:
        repo = make_git_repo("read-crlf")
        target = repo / REVIEW_EXTRAS_PATH
        target.parent.mkdir(parents=True)
        target.write_bytes(b"a\r\nb\rc\n")
        git_in(repo, "add", REVIEW_EXTRAS_PATH)
        git_in(repo, "commit", "-m", "crlf")
        _seed_default_branch_config(repo, {})

        assert _source(repo).read(REVIEW_EXTRAS_PATH) == "a\nb\nc\n"

    def test_an_unresolved_source_reads_nothing(self, tmp_path: Path) -> None:
        source = ReviewConfigSource(tmp_path, "main", None, None)

        assert source.read(REVIEW_EXTRAS_PATH) is None
        assert source.exists(REVIEW_EXTRAS_PATH) is False

    @pytest.mark.parametrize(
        "failure",
        [
            OSError("no git"),
            subprocess.TimeoutExpired(cmd="git", timeout=1),
            subprocess.CompletedProcess(["git"], 128, stdout=b"", stderr=b"boom"),
        ],
        ids=["oserror", "timeout", "nonzero"],
    )
    def test_a_git_failure_reads_as_absent_without_raising(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        failure: object,
    ) -> None:
        repo = _seeded(make_git_repo, "read-fail", {REVIEW_EXTRAS_PATH: "E\n"})
        source = _source(repo)

        def _fail(*_args: object, **_kwargs: object) -> object:
            if isinstance(failure, BaseException):
                raise failure
            return failure

        monkeypatch.setattr(_CONFIG_RUN_GIT, _fail)

        assert source.read(REVIEW_EXTRAS_PATH) is None
        assert source.exists(REVIEW_EXTRAS_PATH) is False

    def test_the_read_bypasses_the_filesystem(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "read-no-fs", {REVIEW_EXTRAS_PATH: "E\n"})
        (repo / REVIEW_EXTRAS_PATH).unlink()

        assert _source(repo).read(REVIEW_EXTRAS_PATH) == "E\n"

    @pytest.mark.parametrize("relpath", REVIEW_CONFIG_PATHS)
    def test_branch_and_worktree_edits_do_not_change_the_read(
        self, make_git_repo: Callable[..., Path], relpath: str
    ) -> None:
        repo = _seeded(make_git_repo, "read-edits", {relpath: "DEFAULT\n"})
        _branch(repo, {relpath: "BRANCH\n"})
        (repo / relpath).write_text("WORKTREE\n", encoding="utf-8")

        assert _source(repo).read(relpath) == "DEFAULT\n"


class TestBranchModifiedReviewConfig:
    @pytest.mark.parametrize("relpath", REVIEW_CONFIG_PATHS)
    def test_modified_added_deleted_and_renamed(
        self, make_git_repo: Callable[..., Path], relpath: str
    ) -> None:
        # modified
        repo = _seeded(make_git_repo, "mod-edit", {relpath: "A\n"})
        _branch(repo, {relpath: "B\n"})
        assert branch_modified_review_config(_source(repo)) == (relpath,)
        # added: the default branch has no copy
        repo = _seeded(make_git_repo, "mod-add", {"keep.txt": "k\n"})
        _branch(repo, {relpath: "NEW\n"})
        assert branch_modified_review_config(_source(repo)) == (relpath,)
        # deleted
        repo = _seeded(make_git_repo, "mod-del", {relpath: "A\n"})
        git_in(repo, "checkout", "-b", "feature")
        git_in(repo, "rm", relpath)
        git_in(repo, "commit", "-m", "delete config")
        assert branch_modified_review_config(_source(repo)) == (relpath,)
        # renamed away (--no-renames reports the deletion)
        repo = _seeded(make_git_repo, "mod-rename", {relpath: "A distinctive body\n"})
        git_in(repo, "checkout", "-b", "feature")
        git_in(repo, "mv", relpath, "elsewhere.txt")
        git_in(repo, "commit", "-m", "rename config away")
        assert branch_modified_review_config(_source(repo)) == (relpath,)

    def test_unchanged_is_empty(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _seeded(make_git_repo, "mod-none", {REVIEW_EXTRAS_PATH: "A\n"})
        _branch(repo)

        assert branch_modified_review_config(_source(repo)) == ()

    def test_default_branch_advances_are_not_the_branchs_edit(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "mod-advance", {REVIEW_EXTRAS_PATH: "A\n"})
        _branch(repo)
        git_in(repo, "checkout", "main")
        (repo / REVIEW_EXTRAS_PATH).write_text("ADVANCED\n", encoding="utf-8")
        git_in(repo, "commit", "-am", "main edits extras")
        git_in(repo, "push", "origin", "main")
        git_in(repo, "checkout", "feature")

        assert branch_modified_review_config(_source(repo)) == ()

    def test_merging_the_default_branch_in_keeps_only_the_branchs_own_edits(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(
            make_git_repo,
            "mod-merged",
            {REVIEW_EXTRAS_PATH: "A\n", REVIEW_POLICY_PATH: "P\n"},
        )
        _branch(repo, {REVIEW_POLICY_PATH: "BRANCH-P\n"})
        git_in(repo, "checkout", "main")
        (repo / REVIEW_EXTRAS_PATH).write_text("ADVANCED\n", encoding="utf-8")
        git_in(repo, "commit", "-am", "main edits extras")
        git_in(repo, "push", "origin", "main")
        git_in(repo, "checkout", "feature")
        git_in(repo, "merge", "--no-edit", "main")

        assert branch_modified_review_config(_source(repo)) == (REVIEW_POLICY_PATH,)

    def test_multiple_paths_come_back_in_config_order(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "mod-many", {})
        _branch(
            repo,
            {
                SENSITIVE_GITHUB_PATH: "g\n",
                REVIEW_EXTRAS_PATH: "e\n",
                SENSITIVE_CLAUDE_PATH: "c\n",
            },
        )

        assert branch_modified_review_config(_source(repo)) == (
            REVIEW_EXTRAS_PATH,
            SENSITIVE_CLAUDE_PATH,
            SENSITIVE_GITHUB_PATH,
        )

    def test_unrelated_histories_record_none_with_one_warning(
        self,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = _seeded(make_git_repo, "mod-orphan", {REVIEW_EXTRAS_PATH: "A\n"})
        git_in(repo, "checkout", "--orphan", "island")
        git_in(repo, "commit", "--allow-empty", "-m", "island root")
        source = _source(repo)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert branch_modified_review_config(source) == ()

        messages = _warnings(caplog)
        assert len(messages) == 1
        assert (
            "could not detect branch edits to the review configuration" in (messages[0])
        )
        assert "git merge-base against origin/main failed" in messages[0]

    def test_a_failed_diff_records_none_with_one_warning(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = _seeded(make_git_repo, "mod-diff-fail", {REVIEW_EXTRAS_PATH: "A\n"})
        _branch(repo, {REVIEW_EXTRAS_PATH: "B\n"})
        source = _source(repo)
        from cw.review_convergence import git_stdout_or_none as real

        def _stub(worktree: Path, argv: Sequence[str]) -> str | None:
            return None if argv[0] == "diff" else real(worktree, list(argv))

        monkeypatch.setattr(_CONFIG_GIT_STDOUT, _stub)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert branch_modified_review_config(source) == ()

        messages = _warnings(caplog)
        assert len(messages) == 1
        assert "git diff against origin/main failed" in messages[0]

    def test_an_unresolved_source_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        source = ReviewConfigSource(tmp_path, "main", None, None)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert branch_modified_review_config(source) == ()

        assert _warnings(caplog) == []


class TestReviewConfigStatus:
    def test_present_lists_exactly_the_paths_that_exist(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(
            make_git_repo,
            "status-present",
            {REVIEW_EXTRAS_PATH: "e\n", SENSITIVE_GITHUB_PATH: "g\n"},
        )
        _branch(repo, {REVIEW_EXTRAS_PATH: "edited\n"})

        status = review_config_status(_source(repo))

        assert status.default_branch == "main"
        assert status.ref == "origin/main"
        assert status.commit == git_in(repo, "rev-parse", "origin/main")
        assert status.present == [REVIEW_EXTRAS_PATH, SENSITIVE_GITHUB_PATH]
        assert status.modified == [REVIEW_EXTRAS_PATH]

    def test_unresolved_status_carries_no_commit(self, tmp_path: Path) -> None:
        status = review_config_status(ReviewConfigSource(tmp_path, "main", None, None))

        assert status.commit is None
        assert status.ref is None
        assert status.present == []
        assert status.modified == []


def _outside_diff(prompt: str) -> str:
    """The prompt up to its ``## Diff`` section: a branch's own edit to a
    configuration file is ordinary diff content there, never configuration."""
    return prompt.split("## Diff\n", 1)[0]


def _prompts(
    repo: Path,
    *,
    scope_hint: str = "small",
    config_branch: str = "main",
    delta_from_sha: str | None = None,
) -> dict[str, str]:
    prepared = _prepare_review_pass(
        _task(scope_hint=scope_hint),
        repo,
        "main",
        runner=FakeCodexRunner(),
        session_id="s-config",
        config_branch=config_branch,
        delta_from_sha=delta_from_sha,
    )
    return dict(prepared.prompts_by_role)


class TestPreparedPass:
    @pytest.mark.parametrize("worktree_edit", [False, True], ids=["commit", "dirty"])
    def test_extras_come_from_the_default_branch(
        self, make_git_repo: Callable[..., Path], worktree_edit: bool
    ) -> None:
        repo = _seeded(
            make_git_repo, "pass-extras", {REVIEW_EXTRAS_PATH: "DEFAULT-EXTRAS\n"}
        )
        if worktree_edit:
            _branch(repo)
            (repo / REVIEW_EXTRAS_PATH).write_text("BRANCH-EXTRAS\n", encoding="utf-8")
        else:
            _branch(repo, {REVIEW_EXTRAS_PATH: "BRANCH-EXTRAS\n"})

        prompts = _prompts(repo)

        assert prompts
        for prompt in prompts.values():
            assert "## Project Rubrics\nDEFAULT-EXTRAS" in prompt
            assert "BRANCH-EXTRAS" not in _outside_diff(prompt)

    def test_extras_added_by_the_branch_are_not_applied(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "pass-extras-add", {"keep.txt": "k\n"})
        _branch(repo, {REVIEW_EXTRAS_PATH: "BRANCH-EXTRAS\n"})

        for prompt in _prompts(repo).values():
            assert "## Project Rubrics" not in prompt
            assert "BRANCH-EXTRAS" not in _outside_diff(prompt)

    def test_policy_comes_from_the_default_branch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "pass-policy", {REVIEW_POLICY_PATH: _POLICY})
        _branch(repo, {REVIEW_POLICY_PATH: _BRANCH_POLICY})

        prompts = _prompts(repo, scope_hint="large")

        quality = prompts["Code Quality Reviewer"]
        assert "## Repo Policy for Code Quality Reviewer\nDEFAULT-POLICY" in quality
        for prompt in prompts.values():
            assert "BRANCH-POLICY" not in _outside_diff(prompt)

    @pytest.mark.parametrize("scope_hint", ["small", "large"])
    def test_a_branch_that_empties_the_manifest_is_still_scrutinised(
        self, make_git_repo: Callable[..., Path], scope_hint: str
    ) -> None:
        repo = _seeded(
            make_git_repo, "pass-sensitive", {SENSITIVE_CLAUDE_PATH: _MANIFEST}
        )
        _branch(repo, {SENSITIVE_CLAUDE_PATH: _EMPTY_MANIFEST})
        (repo / SENSITIVE_CLAUDE_PATH).write_text(_EMPTY_MANIFEST, encoding="utf-8")

        prompts = _prompts(repo, scope_hint=scope_hint)

        assert prompts
        assert all("SENSITIVE FILES TOUCHED" in p for p in prompts.values())

    @pytest.mark.parametrize("scope_hint", ["small", "large"])
    def test_a_branch_manifest_entry_is_not_applied(
        self, make_git_repo: Callable[..., Path], scope_hint: str
    ) -> None:
        repo = _seeded(
            make_git_repo,
            "pass-sensitive-add",
            {SENSITIVE_CLAUDE_PATH: _EMPTY_MANIFEST},
        )
        _branch(repo, {SENSITIVE_CLAUDE_PATH: _MANIFEST})

        prompts = _prompts(repo, scope_hint=scope_hint)

        assert all("SENSITIVE FILES TOUCHED" not in p for p in prompts.values())

    def test_the_github_manifest_is_read_from_the_default_branch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "pass-github", {SENSITIVE_GITHUB_PATH: _MANIFEST})
        _branch(repo, {SENSITIVE_GITHUB_PATH: _EMPTY_MANIFEST})

        prompts = _prompts(repo, scope_hint="large")

        assert all("SENSITIVE FILES TOUCHED" in p for p in prompts.values())

    def test_a_present_but_unreadable_claude_manifest_does_not_fall_through(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("pass-unreadable")
        bad = repo / SENSITIVE_CLAUDE_PATH
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"\xff\xfe\x00")
        git_in(repo, "add", SENSITIVE_CLAUDE_PATH)
        git_in(repo, "commit", "-m", "bad manifest")
        _seed_default_branch_config(repo, {SENSITIVE_GITHUB_PATH: _MANIFEST})
        source = _source(repo)

        assert source.exists(SENSITIVE_CLAUDE_PATH)
        assert _load_sensitive_hits(source, ["src/mod.py", "mod.py"], "large") == []

    def test_a_repo_with_no_config_behaves_as_before(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "pass-absent", {"keep.txt": "k\n"})
        _branch(repo)

        for prompt in _prompts(repo, scope_hint="large").values():
            assert "## Project Rubrics" not in prompt
            assert "## Repo Policy" not in prompt
            assert "SENSITIVE FILES TOUCHED" not in prompt

    def test_the_record_lists_modified_paths(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "pass-record", {REVIEW_EXTRAS_PATH: "D\n"})
        _branch(repo, {REVIEW_EXTRAS_PATH: "B\n"})

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-record",
            config_branch="main",
        )

        assert prepared.review_config is not None
        assert prepared.review_config.modified == [REVIEW_EXTRAS_PATH]
        assert prepared.review_config.present == [REVIEW_EXTRAS_PATH]
        assert prepared.review_config.ref == "origin/main"

    def test_an_unchanged_branch_records_no_modification(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "pass-record-clean", {REVIEW_EXTRAS_PATH: "D\n"})
        _branch(repo)

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-record-clean",
            config_branch="main",
        )

        assert prepared.review_config is not None
        assert prepared.review_config.modified == []

    def test_a_delta_pass_still_records_earlier_branch_edits_and_uses_main(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(
            make_git_repo, "pass-delta", {REVIEW_EXTRAS_PATH: "DEFAULT-EXTRAS\n"}
        )
        _branch(repo, {REVIEW_EXTRAS_PATH: "BRANCH-EXTRAS\n"})
        delta_from = git_in(repo, "rev-parse", "HEAD")
        (repo / "later.py").write_text("y = 2\n", encoding="utf-8")
        git_in(repo, "add", "later.py")
        git_in(repo, "commit", "-m", "later")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-delta",
            config_branch="main",
            delta_from_sha=delta_from,
        )

        assert prepared.changed_files == ("later.py",)
        assert prepared.review_config is not None
        assert prepared.review_config.modified == [REVIEW_EXTRAS_PATH]
        for prompt in prepared.prompts_by_role.values():
            assert "## Project Rubrics\nDEFAULT-EXTRAS" in prompt
            assert "BRANCH-EXTRAS" not in _outside_diff(prompt)

    def test_an_unresolved_config_branch_applies_no_configuration(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "pass-unresolved", {REVIEW_EXTRAS_PATH: "D\n"})
        _branch(repo)

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-unresolved",
            config_branch=_MISSING,
        )

        assert prepared.review_config is not None
        assert prepared.review_config.commit is None
        for prompt in prepared.prompts_by_role.values():
            assert "## Project Rubrics" not in prompt

    def test_the_claude_path_reads_the_real_branch_not_a_pinned_sha(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seeded(make_git_repo, "pass-claude", {REVIEW_EXTRAS_PATH: "D\n"})
        _branch(repo)
        pinned = git_in(repo, "merge-base", "origin/main", "HEAD")

        ok = _prepare_review_pass(
            _task(),
            repo,
            pinned,
            runner=FakeCodexRunner(),
            session_id="s-claude-ok",
            config_branch="main",
        )
        sha_as_branch = _prepare_review_pass(
            _task(),
            repo,
            pinned,
            runner=FakeCodexRunner(),
            session_id="s-claude-sha",
            config_branch=pinned,
        )

        assert ok.review_config is not None
        assert ok.review_config.ref == "origin/main"
        assert sha_as_branch.review_config is not None
        assert sha_as_branch.review_config.commit is None


class _RecordingSource(ReviewConfigSource):
    """A resolved source that records every access instead of running git."""

    accessed: ClassVar[list[str]] = []

    def read(self, relpath: str) -> str | None:
        self.accessed.append(relpath)
        return None

    def exists(self, relpath: str) -> bool:
        self.accessed.append(relpath)
        return False


class TestSmallTierPolicyReadsNothing:
    def test_small_policy_never_touches_the_source(self, tmp_path: Path) -> None:
        _RecordingSource.accessed.clear()
        source = _RecordingSource(tmp_path, "main", "origin/main", "a" * 40)

        assert _load_review_policy(source, "small") == {}
        assert _RecordingSource.accessed == []

    def test_large_policy_reads_only_the_policy_path(self, tmp_path: Path) -> None:
        _RecordingSource.accessed.clear()
        source = _RecordingSource(tmp_path, "main", "origin/main", "a" * 40)

        assert _load_review_policy(source, "large") == {}
        assert _RecordingSource.accessed == [REVIEW_POLICY_PATH]
