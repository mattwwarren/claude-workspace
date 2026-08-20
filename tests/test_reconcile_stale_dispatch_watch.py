"""Tests for ``cw.reconcile.stale_dispatch_watch`` (GitHub #1927).

Mirrors ``tests/test_dispatch_pr_gate.py``'s shape for the sibling
``dispatch/pr_gate.py`` module: the unit under test is the *registration
decision* (which ``stale_dispatch`` parks get a ``WatchedPr``), not the git
subprocess — ``_resolve_repo_slug`` is stubbed, exactly as ``pr_gate``'s tests
stub ``pr_exists_for_branch``.

The pass is a full per-tick rescan (binding A2): a park stamped by #1902's
routing code BEFORE this feature existed must still be registered
retroactively, so every fixture below persists its park with no pre-existing
``WatchedPr`` and calls the registration function as a standalone step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    ClientConfig,
    DevQueueStore,
    PrState,
    QueueItemStatus,
    TicketTask,
    WatchedPr,
)
from cw.reconcile.stale_dispatch_watch import (
    _WATCHED_PR_SOURCE_STALE_DISPATCH_PARK,
    register_stale_dispatch_watched_prs,
)
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

_SLUG = "foo/bar"


def _park(
    ticket_id: str = "SG-SD1",
    client: str = "client-a",
    **overrides: object,
) -> TicketTask:
    """A BLOCKED_ON_USER ``stale_dispatch``/``pr_already_open`` park row."""
    kwargs: dict[str, object] = {
        "ticket_id": ticket_id,
        "client": client,
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": "stale_dispatch",
        "blocked_reason": "pr_already_open",
        "blocked_on_pr": 70,
        "session_id": "sess-sd1",
    }
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def _stub_slug(
    monkeypatch: pytest.MonkeyPatch,
    slug: str | None = _SLUG,
) -> list[Path]:
    """Stub ``_resolve_repo_slug``; returns the list of git dirs it was called with."""
    calls: list[Path] = []

    def _fake(git_dir: Path) -> str | None:
        calls.append(git_dir)
        return slug

    monkeypatch.setattr("cw.reconcile.stale_dispatch_watch._resolve_repo_slug", _fake)
    return calls


def _no_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cw.reconcile.stale_dispatch_watch.load_clients", dict)


def _with_clients(
    monkeypatch: pytest.MonkeyPatch, clients: dict[str, ClientConfig]
) -> None:
    monkeypatch.setattr(
        "cw.reconcile.stale_dispatch_watch.load_clients", lambda: clients
    )


class TestRegisterStaleDispatchWatchedPrs:
    """The per-tick rescan that gives a stale_dispatch park an independent
    PR-state source (#1927)."""

    def test_registers_watch_for_preexisting_park(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retroactive backfill (binding A2): a park persisted with no
        WatchedPr — i.e. stamped before this feature existed — is registered
        by a plain rescan, not only at stamp time."""
        save_dev_queue(DevQueueStore(tasks=[_park()]))
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch)

        assert register_stale_dispatch_watched_prs() == ["SG-SD1"]

        watched = load_dev_queue().watched_prs
        assert len(watched) == 1
        assert watched[0].pr_url == "https://github.com/foo/bar/pull/70"
        assert watched[0].repo == _SLUG
        assert watched[0].pr_number == 70
        assert watched[0].client == "client-a"
        assert watched[0].source == _WATCHED_PR_SOURCE_STALE_DISPATCH_PARK
        assert watched[0].status == "active"

    def test_second_call_is_a_no_op(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Idempotent: the pre-filter short-circuits before any git call."""
        save_dev_queue(DevQueueStore(tasks=[_park()]))
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch)
        register_stale_dispatch_watched_prs()

        calls = _stub_slug(monkeypatch)
        assert register_stale_dispatch_watched_prs() == []
        assert calls == []
        assert len(load_dev_queue().watched_prs) == 1

    def test_watch_with_null_client_does_not_suppress_registration(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-#1927 operator watch (client=None) is not a match for the
        (client, pr_number) pre-filter — a different repo entirely could own
        that bare number."""
        save_dev_queue(
            DevQueueStore(
                tasks=[_park()],
                watched_prs=[
                    WatchedPr(
                        pr_url="https://github.com/other/repo/pull/70",
                        repo="other/repo",
                        pr_number=70,
                        source="cli",
                    )
                ],
            )
        )
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch)

        assert register_stale_dispatch_watched_prs() == ["SG-SD1"]
        assert len(load_dev_queue().watched_prs) == 2

    def test_watch_with_null_client_same_repo_is_adopted_not_shadowed(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual collision the (client, pr_number) pre-filter cannot see:
        a pre-#1927 operator watch for the SAME (repo, pr_number) the park is
        blocked on. register_watched_pr's (repo, pr_number) dedup would
        silently drop this registration; register_or_adopt_watched_pr instead
        tags the existing entry with this park's client in place, so the park
        can still self-release (#1927 Data Safety finding)."""
        save_dev_queue(
            DevQueueStore(
                tasks=[_park()],
                watched_prs=[
                    WatchedPr(
                        pr_url="https://github.com/foo/bar/pull/70",
                        repo=_SLUG,
                        pr_number=70,
                        source="cli",
                    )
                ],
            )
        )
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch)

        assert register_stale_dispatch_watched_prs() == ["SG-SD1"]

        watched = load_dev_queue().watched_prs
        assert len(watched) == 1
        assert watched[0].client == "client-a"
        assert watched[0].source == "cli"

    def test_two_clients_same_repo_same_pr_is_a_collision_for_the_second(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Unlike test_two_clients_same_pr_number_register_distinct_watches
        (different repos, no collision), two clients genuinely mapped to the
        SAME repo colliding on the SAME PR number cannot both get a
        client-tagged watch for one (repo, pr_number) row. The second
        candidate is excluded from the returned (self-release-eligible) list,
        not silently treated as registered."""
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _park(ticket_id="SG-A", client="client-a", blocked_on_pr=70),
                    _park(ticket_id="SG-B", client="client-b", blocked_on_pr=70),
                ]
            )
        )
        _with_clients(
            monkeypatch,
            {
                "client-a": ClientConfig(name="client-a", workspace_path=repo_a),
                "client-b": ClientConfig(name="client-b", workspace_path=repo_b),
            },
        )
        monkeypatch.setattr(
            "cw.reconcile.stale_dispatch_watch._resolve_repo_slug",
            lambda _git_dir: "acme/shared",
        )

        result = register_stale_dispatch_watched_prs()

        assert result == ["SG-A"]
        watched = load_dev_queue().watched_prs
        assert len(watched) == 1
        assert watched[0].client == "client-a"

    def test_resolve_repo_slug_memoized_per_client_within_one_call(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Performance finding (#1927): two candidates for the SAME client
        (the retroactive-backfill worst case) must resolve the git remote
        once, not once per candidate."""
        repo = tmp_path / "repo-a"
        repo.mkdir()
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _park(ticket_id="SG-A", client="client-a", blocked_on_pr=70),
                    _park(ticket_id="SG-B", client="client-a", blocked_on_pr=71),
                ]
            )
        )
        _with_clients(
            monkeypatch,
            {"client-a": ClientConfig(name="client-a", workspace_path=repo)},
        )
        calls = _stub_slug(monkeypatch)

        result = register_stale_dispatch_watched_prs()

        assert set(result) == {"SG-A", "SG-B"}
        assert calls == [repo]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("disposition", "merge_gate_blocked"),
            ("blocked_reason", "some_other_reason"),
            ("blocked_on_pr", None),
            ("status", QueueItemStatus.PENDING),
        ],
        ids=["disposition", "blocked_reason", "blocked_on_pr", "status"],
    )
    def test_skips_rows_failing_each_predicate_leg(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_park(**{field: value})]))
        _no_clients(monkeypatch)
        calls = _stub_slug(monkeypatch)

        assert register_stale_dispatch_watched_prs() == []
        assert calls == []
        assert load_dev_queue().watched_prs == []

    def test_unresolvable_remote_skips_then_retries_next_call(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best-effort: no raise, no failure cached — the next rescan retries."""
        save_dev_queue(DevQueueStore(tasks=[_park()]))
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch, slug=None)

        assert register_stale_dispatch_watched_prs() == []
        assert load_dev_queue().watched_prs == []

        _stub_slug(monkeypatch)
        assert register_stale_dispatch_watched_prs() == ["SG-SD1"]
        assert len(load_dev_queue().watched_prs) == 1

    def test_dangling_client_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#1269: a populated clients.yaml missing this client is drift, not
        single-tenant mode — never fall back to the ambient CWD's remote."""
        save_dev_queue(DevQueueStore(tasks=[_park(client="ghost")]))
        _with_clients(
            monkeypatch,
            {"client-a": ClientConfig(name="client-a", workspace_path=tmp_path)},
        )
        calls = _stub_slug(monkeypatch)

        assert register_stale_dispatch_watched_prs() == []
        assert calls == []
        assert load_dev_queue().watched_prs == []

    def test_configured_client_resolves_against_its_own_git_dir(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo-a"
        repo.mkdir()
        save_dev_queue(DevQueueStore(tasks=[_park()]))
        _with_clients(
            monkeypatch,
            {"client-a": ClientConfig(name="client-a", workspace_path=repo)},
        )
        calls = _stub_slug(monkeypatch)

        assert register_stale_dispatch_watched_prs() == ["SG-SD1"]
        assert calls == [repo]

    def test_two_clients_same_pr_number_register_distinct_watches(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#1269 cross-client collision guard: a bare PR number is only
        unambiguous within one client's repo."""
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _park(ticket_id="SG-A", client="client-a", blocked_on_pr=42),
                    _park(ticket_id="SG-B", client="client-b", blocked_on_pr=42),
                ]
            )
        )
        _with_clients(
            monkeypatch,
            {
                "client-a": ClientConfig(name="client-a", workspace_path=repo_a),
                "client-b": ClientConfig(name="client-b", workspace_path=repo_b),
            },
        )
        slugs = {repo_a: "acme/alpha", repo_b: "acme/beta"}
        monkeypatch.setattr(
            "cw.reconcile.stale_dispatch_watch._resolve_repo_slug",
            lambda git_dir: slugs[git_dir],
        )

        assert register_stale_dispatch_watched_prs() == ["SG-A", "SG-B"]

        watched = load_dev_queue().watched_prs
        assert {(w.client, w.repo, w.pr_number) for w in watched} == {
            ("client-a", "acme/alpha", 42),
            ("client-b", "acme/beta", 42),
        }

    def test_existing_watch_with_hydrated_state_is_left_untouched(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rescan must never clobber an already-hydrated pr_state."""
        save_dev_queue(
            DevQueueStore(
                tasks=[_park()],
                watched_prs=[
                    WatchedPr(
                        pr_url="https://github.com/foo/bar/pull/70",
                        repo=_SLUG,
                        pr_number=70,
                        client="client-a",
                        source=_WATCHED_PR_SOURCE_STALE_DISPATCH_PARK,
                        pr_state=PrState(state="MERGED"),
                    )
                ],
            )
        )
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch)

        assert register_stale_dispatch_watched_prs() == []

        watched = load_dev_queue().watched_prs
        assert len(watched) == 1
        assert watched[0].pr_state is not None
        assert watched[0].pr_state.state == "MERGED"
