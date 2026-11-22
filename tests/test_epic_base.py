"""``cw.dev_queue.epic_base``: the ``--base`` origin check and size ceiling (#3783).

Real git throughout (``epic_repo_with_origin``): the three-dot-versus-two-dot
distinction and the exact ``refs/heads/<base>`` compare only mean something on
a real remote.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

import cw.worktree._freshness as freshness
from cw._lock_guard import held_locks
from cw.dev_queue import epic_base
from cw.dev_queue.epic_base import (
    ENQUEUE_NET_TIMEOUT_S,
    EpicBaseRefusedError,
    check_epic_size_ceiling,
    refuse_redundant_base,
    verify_base_on_origin,
)
from cw.exceptions import GitTimeoutError
from cw.models import ClientConfig
from cw.worktree import FetchOutcome, FetchResult
from tests._shared.base_branch_builders import epic_repo_with_origin
from tests._shared.git_repos import git_in, init_bare_origin

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

BASE = "epic/x"
FILES = (("big.py", 50), ("uv.lock", 40))
FULL = 1 + sum(n for _, n in FILES)  # base_work.py plus the sized files


def _client(repo: Path, **extra: object) -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "name": "acme",
            "repo_path": str(repo),
            "branch": "main",
            "default_branch": "main",
            **extra,
        }
    )


@pytest.fixture
def epic_repo(make_git_repo: Callable[..., Path]) -> tuple[Path, Path]:
    return epic_repo_with_origin(make_git_repo, BASE, files=FILES)


# -- origin check -----------------------------------------------------------


def test_origin_present_passes(epic_repo: tuple[Path, Path]) -> None:
    repo, _ = epic_repo
    verify_base_on_origin(_client(repo), BASE)


def test_base_absent_on_origin_is_refused(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("acme")
    init_bare_origin(repo)
    git_in(repo, "push", "origin", "main")
    with pytest.raises(EpicBaseRefusedError) as info:
        verify_base_on_origin(_client(repo), "epic/missing")
    assert str(info.value) == (
        "Refusing --base 'epic/missing': no such branch on origin for client"
        " 'acme'; push `epic/missing` to origin first; the orchestration skill"
        " creates it, RFC 0020 D-6."
    )


def test_tail_match_is_not_a_match(make_git_repo: Callable[..., Path]) -> None:
    """Origin has only ``x/epic/x``; asking for ``epic/x`` must be refused."""
    repo = make_git_repo("acme")
    init_bare_origin(repo)
    git_in(repo, "checkout", "-b", "x/epic/x")
    git_in(repo, "push", "origin", "x/epic/x")
    with pytest.raises(EpicBaseRefusedError, match="no such branch on origin"):
        verify_base_on_origin(_client(repo), "epic/x")


def test_ls_remote_broken_remote_is_could_not_verify(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("acme")
    git_in(repo, "remote", "add", "origin", str(repo / "does-not-exist"))
    with pytest.raises(EpicBaseRefusedError) as info:
        verify_base_on_origin(_client(repo), "epic/x")
    assert str(info.value).startswith(
        "Refusing --base 'epic/x': could not verify `epic/x` on origin; retry."
        " (git ls-remote exited "
    )


def test_ls_remote_timeout_is_could_not_verify(
    epic_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = epic_repo
    seen: dict[str, object] = {}

    def _timeout(*args: str, **kwargs: object) -> None:
        seen["args"] = args
        seen["timeout"] = kwargs.get("timeout")
        msg = "ls-remote ran out of time"
        raise GitTimeoutError(msg)

    monkeypatch.setattr(epic_base, "_run_git", _timeout)
    with pytest.raises(EpicBaseRefusedError) as info:
        verify_base_on_origin(_client(repo), BASE)
    assert str(info.value) == (
        "Refusing --base 'epic/x': could not verify `epic/x` on origin; retry."
        f" (git ls-remote timed out after {ENQUEUE_NET_TIMEOUT_S:g}s)"
    )
    assert seen["timeout"] == ENQUEUE_NET_TIMEOUT_S
    assert seen["args"] == ("ls-remote", "--exit-code", "--heads", "origin", BASE)


def test_os_error_is_could_not_verify(
    epic_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = epic_repo

    def _missing(*_a: object, **_kw: object) -> None:
        msg = "workspace gone"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(epic_base, "_run_git", _missing)
    with pytest.raises(
        EpicBaseRefusedError, match=r"could not verify .*workspace gone"
    ):
        verify_base_on_origin(_client(repo), BASE)


def test_ls_remote_stdout_without_exact_ref_is_absent(
    epic_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = epic_repo
    fake = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="abc\trefs/heads/x/epic/x\n", stderr=""
    )
    monkeypatch.setattr(epic_base, "_run_git", lambda *_a, **_kw: fake)
    with pytest.raises(EpicBaseRefusedError, match="no such branch on origin"):
        verify_base_on_origin(_client(repo), BASE)


# -- redundant base ---------------------------------------------------------


def test_redundant_base_is_refused() -> None:
    client = ClientConfig.model_validate(
        {"name": "acme", "workspace_path": "/unused", "default_branch": "main"}
    )
    with pytest.raises(EpicBaseRefusedError) as info:
        refuse_redundant_base(client, "main")
    assert str(info.value) == (
        "Refusing --base 'main': it is the default branch of client 'acme'; a row"
        " without --base already merges there. Drop --base."
    )


def test_different_base_passes_redundancy() -> None:
    client = ClientConfig.model_validate(
        {"name": "acme", "workspace_path": "/unused", "default_branch": "main"}
    )
    refuse_redundant_base(client, BASE)


# -- size ceiling -----------------------------------------------------------


def test_ceiling_unset_makes_no_git_call(
    epic_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = epic_repo

    def _forbidden(*_a: object, **_kw: object) -> None:
        msg = "no fetch or diff when the ceiling is unset"
        raise AssertionError(msg)

    monkeypatch.setattr(epic_base, "fetch_default_branch", _forbidden)
    monkeypatch.setattr(epic_base, "fetch_base_branch", _forbidden)
    monkeypatch.setattr(epic_base, "_run_git", _forbidden)
    check_epic_size_ceiling(_client(repo), BASE)


def test_under_ceiling_passes(epic_repo: tuple[Path, Path]) -> None:
    repo, _ = epic_repo
    check_epic_size_ceiling(_client(repo, epic={"max_diff_lines": FULL + 10}), BASE)


def test_equal_to_ceiling_passes(epic_repo: tuple[Path, Path]) -> None:
    """The ceiling is strict ``>``: a base exactly at it is allowed."""
    repo, _ = epic_repo
    check_epic_size_ceiling(_client(repo, epic={"max_diff_lines": FULL}), BASE)


def test_ceiling_counts_deleted_lines(make_git_repo: Callable[..., Path]) -> None:
    """The count is additions plus deletions: a file the epic removes is counted."""
    deleted = 30
    repo, _ = epic_repo_with_origin(
        make_git_repo, BASE, files=FILES, deleting=(("old.py", deleted),)
    )
    # Between the additions-only total (FULL) and additions plus deletions.
    ceiling = FULL + deleted - 1
    with pytest.raises(EpicBaseRefusedError) as info:
        check_epic_size_ceiling(_client(repo, epic={"max_diff_lines": ceiling}), BASE)
    assert f"{FULL + deleted} changed lines" in str(info.value)


def test_over_ceiling_is_refused_with_full_text(epic_repo: tuple[Path, Path]) -> None:
    repo, _ = epic_repo
    with pytest.raises(EpicBaseRefusedError) as info:
        check_epic_size_ceiling(_client(repo, epic={"max_diff_lines": 10}), BASE)
    assert str(info.value) == (
        f"Refusing to enqueue on base 'epic/x': {FULL} changed lines (merge-base"
        " diff against 'main', additions plus deletions, merge_gate_ignore_paths"
        " excluded) exceed this client's epic.max_diff_lines of 10. Remedy: land"
        " the epic now (open its PR to 'main' and adopt it: cw dev-queue add"
        " <parent> --adopt-pr <N>), or open a second epic branch from 'main' for"
        " this ticket."
    )


def test_three_dot_diff_ignores_main_divergence(epic_repo: tuple[Path, Path]) -> None:
    """A two-dot diff would also count ``main_work.py``; three-dot does not."""
    repo, _ = epic_repo
    check_epic_size_ceiling(_client(repo, epic={"max_diff_lines": FULL}), BASE)


def test_ignored_path_is_excluded_from_the_measure(
    epic_repo: tuple[Path, Path],
) -> None:
    repo, _ = epic_repo
    ignored = _client(
        repo,
        epic={"max_diff_lines": 60},
        merge_gate_ignore_paths=["uv.lock"],
    )
    check_epic_size_ceiling(ignored, BASE)
    unignored = _client(repo, epic={"max_diff_lines": 60})
    with pytest.raises(EpicBaseRefusedError, match="exceed this client"):
        check_epic_size_ceiling(unignored, BASE)


@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        (FetchOutcome.FAILED, "rc=128: x", "fetch of origin/main failed: rc=128: x"),
        (
            FetchOutcome.BRANCH_ABSENT,
            None,
            "fetch of origin/main failed: branch_absent",
        ),
    ],
)
def test_default_fetch_not_fetched_is_cannot_measure(
    epic_repo: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    outcome: FetchOutcome,
    reason: str | None,
    expected: str,
) -> None:
    repo, _ = epic_repo
    monkeypatch.setattr(
        epic_base, "fetch_default_branch", lambda _c: FetchResult(outcome, reason)
    )
    client = _client(repo, epic={"max_diff_lines": 10})
    with pytest.raises(EpicBaseRefusedError) as info:
        check_epic_size_ceiling(client, BASE)
    assert str(info.value) == (
        f"Refusing to enqueue on base 'epic/x': cannot measure its size against"
        f" 'main' ({expected}); this client sets epic.max_diff_lines, so an"
        " unmeasured base is refused. Retry once origin is reachable."
    )


def test_base_fetch_failed_is_cannot_measure(
    epic_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = epic_repo
    monkeypatch.setattr(
        epic_base,
        "fetch_base_branch",
        lambda _c, _b: FetchResult(FetchOutcome.FAILED, "rc=128: y"),
    )
    with pytest.raises(
        EpicBaseRefusedError, match=r"fetch of origin/epic/x failed: rc=128: y"
    ):
        check_epic_size_ceiling(_client(repo, epic={"max_diff_lines": 10}), BASE)


def test_base_branch_absent_after_fetch_is_cannot_measure(
    epic_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = epic_repo
    monkeypatch.setattr(
        epic_base,
        "fetch_base_branch",
        lambda _c, _b: FetchResult(FetchOutcome.BRANCH_ABSENT),
    )
    with pytest.raises(
        EpicBaseRefusedError, match="origin/epic/x is absent after fetch"
    ):
        check_epic_size_ceiling(_client(repo, epic={"max_diff_lines": 10}), BASE)


def test_diff_failure_is_cannot_measure(
    epic_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FETCHED outcome whose ref is missing locally makes ``git diff`` fail."""
    repo, _ = epic_repo
    monkeypatch.setattr(
        epic_base, "fetch_default_branch", lambda _c: FetchResult(FetchOutcome.FETCHED)
    )
    monkeypatch.setattr(
        epic_base, "fetch_base_branch", lambda _c, _b: FetchResult(FetchOutcome.FETCHED)
    )
    git_in(repo, "update-ref", "-d", "refs/remotes/origin/epic/x")
    with pytest.raises(EpicBaseRefusedError, match=r"git diff exited \d+"):
        check_epic_size_ceiling(_client(repo, epic={"max_diff_lines": 10}), BASE)


def test_measurement_runs_outside_every_lock(
    epic_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = epic_repo
    observed: list[tuple[str, tuple[object, ...]]] = []

    real_run = epic_base._run_git
    real_default = freshness.fetch_default_branch
    real_base = freshness.fetch_base_branch

    def _spy_run(
        *args: str, cwd: Path, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        observed.append(("run_git", held_locks()))
        return real_run(*args, cwd=cwd, check=check, timeout=timeout)

    def _spy_default(client: ClientConfig) -> FetchResult:
        observed.append(("fetch_default", held_locks()))
        return real_default(client)

    def _spy_base(client: ClientConfig, base: str) -> FetchResult:
        observed.append(("fetch_base", held_locks()))
        return real_base(client, base)

    monkeypatch.setattr(epic_base, "_run_git", _spy_run)
    monkeypatch.setattr(epic_base, "fetch_default_branch", _spy_default)
    monkeypatch.setattr(epic_base, "fetch_base_branch", _spy_base)
    client = _client(repo, epic={"max_diff_lines": FULL})
    verify_base_on_origin(client, BASE)
    check_epic_size_ceiling(client, BASE)
    assert {name for name, _ in observed} == {"run_git", "fetch_default", "fetch_base"}
    assert all(locks == () for _, locks in observed)
