"""Real-git release-cut scenarios for the CHANGELOG tests (#2771, #3190).

Light on purpose: stdlib plus ``tests.conftest`` only, so the freeze script's
tests and ``test_cli_worktree`` can build a release cut without importing the
reconcile internals ``tests._changelog_repair_helpers`` pulls in.

- :func:`build_release_moved_repo` builds the release-moved history (bare
  origin, a client clone, ``.gitattributes: CHANGELOG.md merge=union``).
- :func:`cut_straddle_repo` is that history as a worker worktree: the branch
  checked out, green on the pre-cut base, with ``origin/main`` at the cut.
- :func:`squash_onto_origin_main` is what GitHub's squash merge writes.
- :func:`release_cut_pr_repo` is the release-cut PR itself.

This module is the one home of the moved builders: tests import them from
here, never from ``tests._changelog_repair_helpers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from tests import _git_sandbox
from tests._shared.git_repos import git_in, init_bare_origin

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

TICKET = "GEN-1"
BRANCH = f"dev/{TICKET}"

PREAMBLE = "# Changelog\n\n"
RELEASED_1_0_0 = "## [1.0.0] - 2026-01-01\n\n### Added\n\n- Initial (#0).\n"
CL_UNRELEASED_ONLY = "# Changelog\n\n## [Unreleased]\n"

CL_BASE = (
    PREAMBLE
    + "## [Unreleased]\n\n### Added\n\n- Alpha feature (#1).\n\n"
    + "### Fixed\n\n- Beta fix (#2).\n\n"
    + RELEASED_1_0_0
)
CL_BRANCH = (
    PREAMBLE
    + "## [Unreleased]\n\n### Added\n\n- Alpha feature (#1).\n\n"
    + "### Fixed\n\n- Beta fix (#2).\n\n- Gamma fix (#3).\n\n"
    + RELEASED_1_0_0
)
RELEASED_1_1_0 = (
    "## [1.1.0] - 2026-02-01\n\n### Added\n\n- Alpha feature (#1).\n\n"
    "### Fixed\n\n- Beta fix (#2).\n\n"
)
CL_MAIN = PREAMBLE + "## [Unreleased]\n\n" + RELEASED_1_1_0 + RELEASED_1_0_0
CL_EXPECTED = (
    PREAMBLE
    + "## [Unreleased]\n\n### Fixed\n\n- Gamma fix (#3).\n\n"
    + RELEASED_1_1_0
    + RELEASED_1_0_0
)
#: Main after the cut with a populated ``[Unreleased]`` (``### Fixed`` exists).
CL_MAIN_POPULATED = (
    PREAMBLE
    + "## [Unreleased]\n\n### Fixed\n\n- Delta fix (#4).\n\n"
    + RELEASED_1_1_0
    + RELEASED_1_0_0
)
CL_EXPECTED_POPULATED = (
    PREAMBLE
    + "## [Unreleased]\n\n### Fixed\n\n- Delta fix (#4).\n\n- Gamma fix (#3).\n\n"
    + RELEASED_1_1_0
    + RELEASED_1_0_0
)
GAMMA = "- Gamma fix (#3)."


def pyproject(version: str) -> str:
    """A fixture ``pyproject.toml`` freezing every release since ``v1.0.0``."""
    return (
        f'[project]\nname = "fixture"\nversion = "{version}"\n\n'
        '[tool.cw.changelog_freeze]\nsince_tag = "v1.0.0"\n'
    )


class ReleaseMovedRepo(NamedTuple):
    clone: Path
    origin: Path
    base_sha: str
    main_sha: str
    branch_sha: str


def commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    for relpath, content in files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git_in(repo, "add", relpath)
    git_in(repo, "commit", "-m", message)
    return git_in(repo, "rev-parse", "HEAD")


def build_release_moved_repo(
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    *,
    branch_code: str = "def app():\n    return 2\n",
    main_code_file: str = "src/other.py",
    main_code: str = "OTHER = 2\n",
    union: bool = True,
    split_branch_commits: bool = False,
    changelog_eol: str = "\n",
    main_eol: str | None = None,
    main_changelog: str = CL_MAIN,
    extra_base_files: dict[str, str] | None = None,
    tags: bool = False,
) -> ReleaseMovedRepo:
    """Base commit; main renames Unreleased to 1.1.0 (plus a code edit); the
    branch adds Gamma under the old Unreleased (plus a code change).

    The clone is left on ``main`` at the base commit, with both refs pushed to
    the bare origin; ``origin`` sits beside the clone.

    *union* writes ``.gitattributes: CHANGELOG.md merge=union``; without it a
    rebase stops on CHANGELOG.md. *split_branch_commits* makes the branch two
    commits, the second touching only CHANGELOG.md. *changelog_eol* /
    *main_eol* set the line endings of the base+branch / main CHANGELOG.md.
    *tags* tags the base ``v1.0.0`` and main ``v1.1.0`` (pushed to origin).
    """
    eol = changelog_eol
    main_nl = eol if main_eol is None else main_eol
    clone = make_git_repo("client")
    base_files = {
        "CHANGELOG.md": CL_BASE.replace("\n", eol),
        "src/app.py": "def app():\n    return 1\n",
        "src/other.py": "OTHER = 1\n",
        **(extra_base_files or {}),
    }
    if union:
        base_files[".gitattributes"] = "CHANGELOG.md merge=union\n"
    base_sha = commit_files(clone, base_files, "base")
    if tags:
        git_in(clone, "tag", "v1.0.0")
    origin = init_bare_origin(clone)
    git_in(clone, "push", "origin", "main")
    git_in(clone, "checkout", "-b", BRANCH)
    branch_changelog = CL_BRANCH.replace("\n", eol)
    if split_branch_commits:
        commit_files(clone, {"src/app.py": branch_code}, "feat: gamma code (#3)")
        branch_sha = commit_files(
            clone, {"CHANGELOG.md": branch_changelog}, "docs: gamma entry (#3)"
        )
    else:
        branch_sha = commit_files(
            clone,
            {"CHANGELOG.md": branch_changelog, "src/app.py": branch_code},
            "feat: gamma (#3)",
        )
    git_in(clone, "push", "origin", BRANCH)
    git_in(clone, "checkout", "main")
    main_sha = commit_files(
        clone,
        {
            "CHANGELOG.md": main_changelog.replace("\n", main_nl),
            main_code_file: main_code,
        },
        "chore(release): 1.1.0",
    )
    if tags:
        git_in(clone, "tag", "v1.1.0")
    git_in(clone, "push", "origin", "main")
    if tags:
        git_in(clone, "push", "origin", "--tags")
    git_in(clone, "reset", "--hard", base_sha)
    git_in(clone, "branch", "-D", BRANCH)
    return ReleaseMovedRepo(clone, origin, base_sha, main_sha, branch_sha)


def cut_straddle_repo(
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    *,
    populated_unreleased: bool = False,
    changelog_eol: str = "\n",
    main_changelog: str | None = None,
) -> ReleaseMovedRepo:
    """A worker worktree whose PR was green before a release cut (#3190).

    The clone is checked out on :data:`BRANCH` at the pre-cut branch commit;
    ``origin/main`` is the post-cut main (tags ``v1.0.0`` and ``v1.1.0``
    exist and are pushed). *populated_unreleased* gives main's
    ``[Unreleased]`` a ``### Fixed`` / ``- Delta fix (#4).`` (case "moved");
    the default empty ``[Unreleased]`` is case "created-subsection".
    *changelog_eol* and a non-``None`` *main_changelog* pass straight through
    to :func:`build_release_moved_repo`.
    """
    if main_changelog is None:
        main_changelog = CL_MAIN_POPULATED if populated_unreleased else CL_MAIN
    scenario = build_release_moved_repo(
        tmp_path,
        make_git_repo,
        tags=True,
        extra_base_files={"pyproject.toml": pyproject("1.0.0")},
        main_code_file="pyproject.toml",
        main_code=pyproject("1.1.0"),
        main_changelog=main_changelog,
        changelog_eol=changelog_eol,
    )
    git_in(scenario.clone, "fetch", "origin")
    git_in(scenario.clone, "checkout", "-q", "-b", BRANCH, scenario.branch_sha)
    return scenario


def squash_onto_origin_main(scenario: ReleaseMovedRepo) -> str:
    """Squash :data:`BRANCH` onto ``origin/main`` on branch ``squashed``."""
    git_in(scenario.clone, "checkout", "-q", "-B", "squashed", "origin/main")
    git_in(scenario.clone, "merge", "--squash", BRANCH)
    git_in(scenario.clone, "commit", "-m", "squash")
    return git_in(scenario.clone, "rev-parse", "HEAD")


def release_cut_pr_repo(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> ReleaseMovedRepo:
    """The release-cut PR: it adds ``## [1.1.0]`` and bumps pyproject.

    ``origin/main`` is the base (populated ``[Unreleased]``, tag ``v1.0.0``);
    the clone is checked out on ``release/1.1.0``, which is ahead of it.
    """
    clone = make_git_repo("client")
    base_sha = commit_files(
        clone,
        {
            "CHANGELOG.md": CL_BASE,
            "pyproject.toml": pyproject("1.0.0"),
            ".gitattributes": "CHANGELOG.md merge=union\n",
        },
        "base",
    )
    git_in(clone, "tag", "v1.0.0")
    origin = init_bare_origin(clone)
    git_in(clone, "push", "origin", "main", "--tags")
    git_in(clone, "fetch", "origin")
    git_in(clone, "checkout", "-q", "-b", "release/1.1.0")
    branch_sha = commit_files(
        clone,
        {"CHANGELOG.md": CL_MAIN, "pyproject.toml": pyproject("1.1.0")},
        "chore(release): 1.1.0",
    )
    return ReleaseMovedRepo(clone, origin, base_sha, base_sha, branch_sha)


#: The sibling epic child's bullet (#3861): same ``### Fixed`` hunk as GAMMA.
DELTA = "- Delta fix (#4)."


def _with_bullet_after_beta(bullet: str) -> str:
    return CL_BASE.replace("- Beta fix (#2).\n\n", f"- Beta fix (#2).\n\n{bullet}\n\n")


#: The ``[Unreleased]`` heading text the derived constants below are built from.
UNRELEASED_HEADING = "## [Unreleased]\n\n"
#: A brand-new released heading no reviewed head ever carried (#3375).
RELEASED_9_9_9 = "## [9.9.9] - 2026-03-01\n\n"
_BETA_BLOCK = "- Beta fix (#2).\n\n"
_DELTA_BLOCK = "- Delta fix (#4).\n\n"
_GAMMA_BLOCK = "- Gamma fix (#3).\n\n"

#: Main after a sibling PR landed its bullet first: no release cut (#3375).
CL_MAIN_SIBLING = CL_BASE.replace(_BETA_BLOCK, _BETA_BLOCK + _DELTA_BLOCK)
#: The union rebuild of the raced pair: the branch bullet filed after the sibling's.
CL_REBUILT_SIBLING = CL_MAIN_SIBLING.replace(_DELTA_BLOCK, _DELTA_BLOCK + _GAMMA_BLOCK)
#: A released heading placed at the tail of the ``[Unreleased]`` span.
CL_NEW_RELEASED_HEADING_AT_TAIL = CL_REBUILT_SIBLING.replace(
    RELEASED_1_0_0, RELEASED_9_9_9 + RELEASED_1_0_0
)
#: A released section whose bullet was edited.
CL_NEW_RELEASED_SECTION_CHANGED = CL_REBUILT_SIBLING.replace(
    "- Initial (#0).", "- Initial (#0), edited."
)
#: One text line added in the preamble, above ``[Unreleased]``.
CL_NEW_PREAMBLE_ADDITION = CL_REBUILT_SIBLING.replace(
    "# Changelog\n", "# Changelog\nIntro text.\n", 1
)
#: One text line added directly above the ``[Unreleased]`` heading.
CL_NEW_BEFORE_UNRELEASED = CL_REBUILT_SIBLING.replace(
    UNRELEASED_HEADING, "Intro text.\n" + UNRELEASED_HEADING, 1
)
#: One extra blank line added in the preamble.
CL_NEW_BLANK_OUTSIDE_SPAN = CL_REBUILT_SIBLING.replace(
    "# Changelog\n\n", "# Changelog\n\n\n", 1
)
#: One bullet added inside the released ``1.0.0`` section.
CL_NEW_OUT_OF_SPAN = CL_REBUILT_SIBLING.replace(
    "- Initial (#0).\n", "- Initial (#0).\n- Extra (#8).\n"
)
#: A bullet inside the span that the reviewed head never added.
CL_NEW_UNATTESTED_BULLET = CL_REBUILT_SIBLING.replace(
    _GAMMA_BLOCK, _GAMMA_BLOCK + "- Foreign fix (#9).\n\n"
)
#: A ``### Removed`` heading inside the span, absent from the reviewed head.
CL_NEW_FOREIGN_HEADING = CL_REBUILT_SIBLING.replace(
    _GAMMA_BLOCK, _GAMMA_BLOCK + "### Removed\n\n"
)
#: Main's Delta bullet removed by the rebuild.
CL_NEW_MAIN_LINE_DELETED = CL_REBUILT_SIBLING.replace(_DELTA_BLOCK, "")


def epic_sibling_repo(
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
) -> Path:
    """An epic-child clone (#3861): ``origin/epic/1`` carries a sibling bullet.

    ``main`` holds CL_BASE with ``CHANGELOG.md merge=union``. ``origin/epic/1``
    adds ``DELTA`` to the ``### Fixed`` hunk. The clone sits on ``dev/GEN-1``,
    branched from the base, adding ``GAMMA`` to the same hunk and changing
    ``src/app.py``. Both sides add a bullet at the same spot, so a plain
    textual merge conflicts while git's union driver merges cleanly.
    ``origin/HEAD`` names ``main``.
    """
    clone = make_git_repo("epic-client")
    base_sha = commit_files(
        clone,
        {
            "CHANGELOG.md": CL_BASE,
            ".gitattributes": "CHANGELOG.md merge=union\n",
            "src/app.py": "def app():\n    return 1\n",
        },
        "base",
    )
    init_bare_origin(clone)
    git_in(clone, "push", "origin", "main")
    git_in(clone, "checkout", "-b", "epic/1")
    commit_files(
        clone, {"CHANGELOG.md": _with_bullet_after_beta(DELTA)}, "feat: delta (#4)"
    )
    git_in(clone, "push", "origin", "epic/1")
    git_in(clone, "checkout", "-b", BRANCH, base_sha)
    commit_files(
        clone,
        {
            "CHANGELOG.md": _with_bullet_after_beta(GAMMA),
            "src/app.py": "def app():\n    return 2\n",
        },
        "feat: gamma (#3)",
    )
    git_in(clone, "remote", "set-head", "origin", "main")
    return clone


def push_base_moved_without_changelog(clone: Path, base: str) -> None:
    """Push *base* to origin: ``origin/main`` plus a commit that skips CHANGELOG.

    The base has moved ahead of the clone's branch (so it is not an ancestor of
    HEAD), yet its ``CHANGELOG.md`` still equals the merge-base's. The clone is
    returned to the branch it was on.
    """
    current = git_in(clone, "rev-parse", "--abbrev-ref", "HEAD")
    git_in(clone, "checkout", "-q", "-b", base, "origin/main")
    commit_files(clone, {"src/other.py": "OTHER = 1\n"}, "feat: base moves on")
    git_in(clone, "push", "origin", base)
    git_in(clone, "checkout", "-q", current)


def github_would_conflict(clone: Path, head_ref: str, base_ref: str) -> bool:
    """Whether GitHub's conflict check would report ``CHANGELOG.md`` conflicting.

    GitHub ignores the ``merge=union`` driver, so the model is a plain
    ``git merge-file`` (no ``--union``) of the three CHANGELOG blobs.
    Non-zero from ``merge-file`` is a conflict.
    """
    base = git_in(clone, "merge-base", head_ref, base_ref)
    scratch = clone.parent / "merge-file"
    scratch.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, rev in (("ours", head_ref), ("base", base), ("theirs", base_ref)):
        text = _git_sandbox.run(
            "show", f"{rev}:CHANGELOG.md", cwd=clone, check=True, text=True
        ).stdout
        path = scratch / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    ours, base_file, theirs = paths
    merged = _git_sandbox.run(
        "merge-file",
        "-p",
        str(ours),
        str(base_file),
        str(theirs),
        cwd=clone,
        check=False,
        text=True,
    )
    return merged.returncode != 0


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


#: ``CL_BASE`` with CRLF line endings (a CRLF changelog, #3375).
CL_BASE_CRLF = _crlf(CL_BASE)
#: ``CL_BRANCH`` with CRLF line endings.
CL_BRANCH_CRLF = _crlf(CL_BRANCH)
#: ``CL_MAIN`` (a release cut emptied ``[Unreleased]``) with CRLF line endings.
CL_MAIN_CRLF = _crlf(CL_MAIN)
#: ``CL_EXPECTED`` (the rebuild re-creating ``### Fixed``) with CRLF line endings.
CL_EXPECTED_CRLF = _crlf(CL_EXPECTED)
_BARE_CR_TAIL = "\r- Extra fix (#9).\n"
#: The branch's Gamma bullet carries a bare CR and a second bullet after it.
CL_BRANCH_BARE_CR = CL_BRANCH.replace(GAMMA + "\n", GAMMA + _BARE_CR_TAIL)
#: The raced-pair rebuild of ``CL_BRANCH_BARE_CR``: the same bare-CR line added.
CL_REBUILT_BARE_CR = CL_REBUILT_SIBLING.replace(GAMMA + "\n", GAMMA + _BARE_CR_TAIL)
