"""Acceptance property for #2672: each daemon worker sees its own TMPDIR.

``claude --bg`` does not forward the caller's environment to the worker it
starts: the first spawn's environment seeds the long-lived supervisor, and every
later worker inherits that frozen value. Before #2672, ``_spawn_clean_env``
exported the *spawning* worktree's ``.cw/tmp`` as TMPDIR, so the first worktree
to seed the supervisor became every worker's TMPDIR, and removing it pulled the
scratch dir out from under the whole fleet.

The fix moves the per-worker value into ``<worktree>/.claude/settings.local.json``
(``env`` block) and makes the supervisor-seeding env worktree-independent. These
tests drive :func:`cw.spawn.spawn_create_impl` through the fake daemon and
model the worker's effective TMPDIR explicitly (see
:func:`_effective_worker_tmpdir`).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from cw.native_daemon import _spawn_clean_env
from cw.spawn import spawn_create_impl

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import ClientConfig
    from cw.native_daemon import FakeNativeDaemonClient


def _spawn(
    client: ClientConfig, worktree: Path, daemon: FakeNativeDaemonClient
) -> str:
    """Spawn one daemon worker on *worktree* through the real spawn path."""
    return spawn_create_impl(
        client=client,
        worktree=worktree,
        prompt="/auto-dev 2672 --headless",
        label=f"auto-dev-2672-{worktree.name}",
        native_daemon=daemon,
        ticket_id="2672",
        lane="fast",
    )


def _effective_worker_tmpdir(supervisor_env: dict[str, str], cwd: Path) -> str:
    """The TMPDIR a worker homed at *cwd* would see, under an ASSUMPTION.

    Encodes the hypothesis -- not a fact about the daemon, and not provable in a
    unit test -- that the worker's environment is the supervisor's seeded env
    overlaid by the ``env`` block of the project-level
    ``<cwd>/.claude/settings.local.json``. The assumption is verified live
    (issue #2672's post-change probe reads a REPL child's environ or captures
    ``echo $TMPDIR`` from the worker), not here.
    """
    settings_path = cwd / ".claude" / "settings.local.json"
    settings_env: dict[str, str] = {}
    if settings_path.exists():
        raw: object = json.loads(settings_path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        block: object = raw.get("env", {})
        assert isinstance(block, dict)
        settings_env = {str(k): str(v) for k, v in block.items()}
    return {**supervisor_env, **settings_env}["TMPDIR"]


def _own_scratch(worktree: Path) -> str:
    return str(worktree.resolve() / ".cw" / "tmp")


def test_two_back_to_back_spawns_from_different_worktrees_each_see_their_own_tmpdir(
    sample_client: ClientConfig,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    wt_a = make_git_repo("wt-a")
    wt_b = make_git_repo("wt-b")

    _spawn(sample_client, wt_a, mock_native_daemon)
    supervisor_env = _spawn_clean_env(wt_a)
    _spawn(sample_client, wt_b, mock_native_daemon)

    assert _effective_worker_tmpdir(supervisor_env, wt_a) == _own_scratch(wt_a)
    assert _effective_worker_tmpdir(supervisor_env, wt_b) == _own_scratch(wt_b)


def test_spawn_order_does_not_change_per_worktree_tmpdir(
    sample_client: ClientConfig,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    wt_a = make_git_repo("wt-a")
    wt_b = make_git_repo("wt-b")

    _spawn(sample_client, wt_b, mock_native_daemon)
    supervisor_env = _spawn_clean_env(wt_b)
    _spawn(sample_client, wt_a, mock_native_daemon)

    assert _effective_worker_tmpdir(supervisor_env, wt_a) == _own_scratch(wt_a)
    assert _effective_worker_tmpdir(supervisor_env, wt_b) == _own_scratch(wt_b)


def test_removing_one_worktree_does_not_change_the_other_workers_tmpdir(
    sample_client: ClientConfig,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    wt_a = make_git_repo("wt-a")
    wt_b = make_git_repo("wt-b")

    _spawn(sample_client, wt_a, mock_native_daemon)
    supervisor_env = _spawn_clean_env(wt_a)
    _spawn(sample_client, wt_b, mock_native_daemon)
    before = _effective_worker_tmpdir(supervisor_env, wt_b)
    resolved_a = wt_a.resolve()

    shutil.rmtree(wt_a)

    assert _effective_worker_tmpdir(supervisor_env, wt_b) == before
    assert before == _own_scratch(wt_b)
    assert Path(before).is_dir()
    supervisor_tmpdir = Path(supervisor_env["TMPDIR"])
    assert supervisor_tmpdir.is_dir()
    assert not supervisor_tmpdir.resolve().is_relative_to(resolved_a)
