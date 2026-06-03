"""Tests for clients.yaml atomic write and file-locking (clients_lock).

Verifies that:
- A concurrent reader never observes a truncated/empty clients.yaml.
- Two concurrent client-add operations both persist (no lost update).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.config import clients_lock, init_client, load_clients

if TYPE_CHECKING:
    from collections.abc import Callable


class TestClientsLockConcurrency:
    def test_concurrent_client_adds_both_survive(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Two threads calling init_client concurrently must both persist.

        init_client acquires clients_lock() internally, so callers do NOT
        wrap it in an additional lock — that would deadlock (flock is
        per open-file-description, and a second LOCK_EX on the same file
        in the same process blocks forever).
        """
        repo_a = make_git_repo("project-a")
        repo_b = make_git_repo("project-b")
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def add(name: str, repo: Path) -> None:
            barrier.wait()
            try:
                init_client(name, repo)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=add, args=("project-a", repo_a))
        t2 = threading.Thread(target=add, args=("project-b", repo_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"init_client raised: {errors}"
        clients = load_clients()
        assert "project-a" in clients, "project-a was lost"
        assert "project-b" in clients, "project-b was lost"

    def test_sequential_adds_succeed(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Sequential init_client calls work without deadlock."""
        repo_a = make_git_repo("alpha")
        repo_b = make_git_repo("beta")

        init_client("alpha", repo_a)
        init_client("beta", repo_b)

        clients = load_clients()
        assert "alpha" in clients
        assert "beta" in clients


class TestClientsAtomicWrite:
    def test_reader_never_sees_empty_file(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Concurrent reader must not observe empty/partial clients.yaml.

        Writes go through init_client which uses atomic_write_text internally,
        so a reader polling the file should never observe a zero-byte file.
        """
        repo = make_git_repo("slow-project")
        empty_snapshots: list[bool] = []
        stop = threading.Event()

        def reader() -> None:
            import cw.config as cfg

            while not stop.is_set():
                path = cfg.clients_file()
                if path.exists():
                    text = path.read_text()
                    if text.strip() == "" and path.stat().st_size == 0:
                        empty_snapshots.append(True)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            # init_client acquires clients_lock and does atomic_write_text
            init_client("slow-project", repo)
        finally:
            stop.set()
            t.join(timeout=2)

        assert not empty_snapshots, (
            "concurrent reader observed empty clients.yaml during write"
        )

    def test_clients_lock_context_manager_is_importable(self) -> None:
        """clients_lock must be importable from cw.config."""
        from cw.config import clients_lock as cl

        assert callable(cl)

    def test_clients_lock_path_constant_exists(self) -> None:
        """CLIENTS_LOCK path constant must exist in cw.config."""
        import cw.config as cfg

        assert hasattr(cfg, "CLIENTS_LOCK")
        assert isinstance(cfg.CLIENTS_LOCK, Path)

    def test_clients_lock_file_in_config_dir(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """CLIENTS_LOCK must live in the config directory, not state dir."""
        import cw.config as cfg

        assert cfg.CLIENTS_LOCK.parent == cfg.CONFIG_DIR

    def test_clients_lock_file_created_under_tmp(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """Acquiring the lock must not write to the user's real config dir."""
        import cw.config as cfg

        # After acquiring, the lock file should be inside tmp_path
        with clients_lock():
            pass
        lock_path = cfg.CLIENTS_LOCK
        assert str(tmp_config_dir) in str(lock_path), (
            f"Lock file {lock_path} is outside tmp_path {tmp_config_dir}"
        )

    @pytest.mark.parametrize("n_threads", [5, 10])
    def test_many_concurrent_adds_all_survive(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
        n_threads: int,
    ) -> None:
        """N concurrent init_client calls each adding a distinct client; all survive."""
        repos = [make_git_repo(f"repo-{i}") for i in range(n_threads)]
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def add(i: int) -> None:
            barrier.wait()
            try:
                init_client(f"repo-{i}", repos[i])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Some threads raised: {errors}"
        clients = load_clients()
        for i in range(n_threads):
            assert f"repo-{i}" in clients, f"repo-{i} was lost"
