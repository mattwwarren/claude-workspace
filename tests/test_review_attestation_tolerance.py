"""Tolerated later commits after an attested head (#3040, RFC 0017 II1).

``first_untolerated_commit`` walks ``A..B`` first-parent and requires each
commit to fall wholly into one class: (a) a clean default-branch merge (only
with a tolerance), (b) CHANGELOG-only, (c) a pure rename, (d) a migration
renumber. Hermetic ``tmp_path`` repos.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from cw import review_attestation_tolerance as tol
from cw.models.client import RenumberPattern
from cw.review_attestation_tolerance import (
    DEFAULT_RENUMBER_PATTERNS,
    Offender,
    SpanTooLarge,
    first_untolerated_commit,
)
from cw.review_convergence import MergeTolerance, _merge_tolerance
from tests._review_approval_helpers import advance_main, head, sync_merge
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MAIN = MergeTolerance("main")
_MIGRATION = "app/alembic/versions/0001_create.py"
_MIGRATION_BODY = (
    '"""create\n\nRevision ID: a1\nRevises: \n"""\n'
    'revision = "a1"\ndown_revision = None\n\n\ndef upgrade():\n    pass\n'
)


class Repo:
    """A feature worktree pushed to a bare origin, attested at ``attested``."""

    def __init__(self, worktree: Path, origin: Path, side: Path) -> None:
        self.worktree = worktree
        self.origin = origin
        self.side = side
        self.attested = head(worktree)

    def write(self, rel: str, text: str) -> None:
        path = self.worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str = "change") -> str:
        git_in(self.worktree, "add", "-A")
        git_in(self.worktree, "commit", "-m", message)
        return head(self.worktree)

    def check(
        self,
        tolerance: MergeTolerance | None = MAIN,
        patterns: tuple[RenumberPattern, ...] = DEFAULT_RENUMBER_PATTERNS,
    ) -> Offender | SpanTooLarge | None:
        return first_untolerated_commit(
            self.worktree, self.attested, head(self.worktree), tolerance, patterns
        )


@pytest.fixture
def repo(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> Repo:
    worktree, origin = make_git_repo_with_origin("wt-attest")
    built = Repo(worktree, origin, tmp_path / "side")
    built.write("CHANGELOG.md", "# Changelog\n")
    built.write(_MIGRATION, _MIGRATION_BODY)
    built.write("db/migrate/1_add.rb", "version: 1\nadd_column :x\n")
    built.commit("baseline")
    built.attested = head(worktree)
    return built


def _merge_main(repo: Repo) -> str:
    advance_main(repo.origin, repo.worktree, repo.side, "main_only.py")
    sync_merge(repo.worktree)
    return head(repo.worktree)


def _renumber(repo: Repo, revision_line: str = 'revision = "a2"') -> None:
    new = "app/alembic/versions/0002_create.py"
    git_in(repo.worktree, "mv", _MIGRATION, new)
    body = _MIGRATION_BODY.replace('revision = "a1"', revision_line).replace(
        "Revision ID: a1", "Revision ID: a2"
    )
    repo.write(new, body)


class TestEachClassPasses:
    def test_clean_default_merge_passes_with_tolerance(self, repo: Repo) -> None:
        _merge_main(repo)
        assert repo.check() is None

    def test_changelog_only_commit_passes(self, repo: Repo) -> None:
        repo.write("CHANGELOG.md", "# Changelog\n\n- entry\n")
        repo.commit()
        assert repo.check() is None

    def test_pure_r100_rename_passes(self, repo: Repo) -> None:
        git_in(repo.worktree, "mv", "feature.py", "renamed.py")
        repo.commit()
        assert repo.check() is None

    def test_alembic_style_renumber_passes(self, repo: Repo) -> None:
        _renumber(repo)
        repo.commit()
        assert repo.check() is None

    def test_client_extended_pattern_passes(self, repo: Repo) -> None:
        git_in(repo.worktree, "mv", "db/migrate/1_add.rb", "db/migrate/2_add.rb")
        repo.write("db/migrate/2_add.rb", "version: 2\nadd_column :x\n")
        repo.commit()
        rule = RenumberPattern(path_glob="db/migrate/*.rb", line_regex=r"^version:")
        assert repo.check(patterns=(*DEFAULT_RENUMBER_PATTERNS, rule)) is None
        offender = repo.check()
        assert isinstance(offender, Offender)


class TestEachClassFails:
    def test_rename_with_content_change(self, repo: Repo) -> None:
        git_in(repo.worktree, "mv", "feature.py", "renamed.py")
        repo.write("renamed.py", "changed\n")
        sha = repo.commit()
        offender = repo.check()
        assert isinstance(offender, Offender)
        assert offender.commit == sha

    def test_renumber_touching_a_non_revision_line(self, repo: Repo) -> None:
        _renumber(repo)
        new = repo.worktree / "app/alembic/versions/0002_create.py"
        new.write_text(new.read_text().replace("pass", "drop_everything()"))
        sha = repo.commit()
        offender = repo.check()
        assert isinstance(offender, Offender)
        assert offender.commit == sha

    def test_code_commit_names_commit_and_path(self, repo: Repo) -> None:
        repo.write("src/app.py", "print(1)\n")
        sha = repo.commit()
        assert repo.check() == Offender(
            sha, "src/app.py", tol.REASON_UNTOLERATED_CHANGE
        )

    def test_conflict_resolution_merge(self, repo: Repo) -> None:
        advance_main(repo.origin, repo.worktree, repo.side, "main_only.py")
        git_in(repo.worktree, "merge", "--no-commit", "--no-ff", "origin/main")
        repo.write("feature.py", "hand edit in the merge\n")
        git_in(repo.worktree, "add", "feature.py")
        git_in(repo.worktree, "commit", "--no-edit")
        merge = head(repo.worktree)
        offender = repo.check()
        assert offender == Offender(merge, None, tol.REASON_MERGE_RESOLUTION)

    def test_merge_of_a_non_default_ref(self, repo: Repo) -> None:
        git_in(repo.worktree, "checkout", "-b", "other")
        repo.write("other.py", "x\n")
        repo.commit()
        git_in(repo.worktree, "checkout", "feature")
        git_in(repo.worktree, "merge", "--no-ff", "--no-edit", "other")
        merge = head(repo.worktree)
        offender = repo.check()
        assert isinstance(offender, Offender)
        assert offender.commit == merge
        assert offender.reason.startswith("second_parent_not_on_default")

    def test_mixed_changelog_and_rename_commit_is_invalid(self, repo: Repo) -> None:
        repo.write("CHANGELOG.md", "# Changelog\n\n- x\n")
        git_in(repo.worktree, "mv", "feature.py", "renamed.py")
        sha = repo.commit()
        offender = repo.check()
        assert isinstance(offender, Offender)
        assert offender.commit == sha
        assert offender.reason == tol.REASON_MIXED_CLASSES

    def test_empty_commit_is_invalid(self, repo: Repo) -> None:
        git_in(repo.worktree, "commit", "--allow-empty", "-m", "empty")
        offender = repo.check()
        assert isinstance(offender, Offender)
        assert offender.reason == tol.REASON_EMPTY_COMMIT

    def test_first_offender_is_named(self, repo: Repo) -> None:
        repo.write("CHANGELOG.md", "# Changelog\n\n- x\n")
        repo.commit()
        repo.write("a.py", "1\n")
        first = repo.commit()
        repo.write("b.py", "2\n")
        repo.commit()
        offender = repo.check()
        assert offender == Offender(first, "a.py", tol.REASON_UNTOLERATED_CHANGE)

    def test_unreadable_span_fails_closed(self, repo: Repo) -> None:
        offender = first_untolerated_commit(
            repo.worktree, "0" * 40, head(repo.worktree), MAIN, ()
        )
        assert isinstance(offender, Offender)
        assert offender.reason == tol.REASON_GIT_UNREADABLE


class TestToleranceGatesClassA:
    def test_clean_default_merge_is_offender_without_tolerance(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        merge = _merge_main(repo)

        def _no_merge_tree(*_a: object, **_k: object) -> object:
            msg = "no merge-tree call without a tolerance"
            raise AssertionError(msg)

        monkeypatch.setattr(_merge_tolerance, "merge_tree", _no_merge_tree)
        assert repo.check(tolerance=None) == Offender(merge, None, "tolerance_off")

    def test_non_merge_classes_still_pass_without_tolerance(self, repo: Repo) -> None:
        repo.write("CHANGELOG.md", "# Changelog\n\n- x\n")
        repo.commit()
        git_in(repo.worktree, "mv", "feature.py", "renamed.py")
        repo.commit()
        _renumber(repo)
        repo.commit()
        assert repo.check(tolerance=None) is None

    def test_mixed_span_names_the_merge_when_tolerance_is_off(self, repo: Repo) -> None:
        merge = _merge_main(repo)
        repo.write("CHANGELOG.md", "# Changelog\n\n- x\n")
        repo.commit()
        git_in(repo.worktree, "mv", "feature.py", "renamed.py")
        repo.commit()
        assert repo.check(tolerance=None) == Offender(merge, None, "tolerance_off")
        assert repo.check(tolerance=MAIN) is None


class TestSpanCap:
    def _changelog_commits(self, repo: Repo, count: int) -> None:
        for index in range(count):
            repo.write("CHANGELOG.md", f"# Changelog\n\n- {index}\n")
            repo.commit()

    def test_cap_exceeded_is_span_too_large(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tol, "MAX_TOLERANCE_SPAN_COMMITS", 2)
        self._changelog_commits(repo, 3)
        calls: list[object] = []
        real = tol._git_out

        def _counting(repo_path: Path, *argv: str) -> str | None:
            calls.append(argv)
            return real(repo_path, *argv)

        monkeypatch.setattr(tol, "_git_out", _counting)
        assert repo.check() == SpanTooLarge(3)
        assert len(calls) == 1

    def test_exactly_the_cap_still_classifies(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tol, "MAX_TOLERANCE_SPAN_COMMITS", 2)
        self._changelog_commits(repo, 2)
        assert repo.check() is None


@pytest.mark.parametrize(
    "order",
    [
        "import cw.review_approval; import cw.review_attestation_tolerance",
        "import cw.review_attestation_tolerance; import cw.review_approval",
    ],
)
def test_import_order_has_no_cycle(order: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", order],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
