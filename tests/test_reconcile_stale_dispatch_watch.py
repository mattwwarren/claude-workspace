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

import logging
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dev_queue.crud import register_or_adopt_watched_pr
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorEvent,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    TicketTask,
    WatchedPr,
)
from cw.models.pr_ref import PrRef
from cw.models.tasks.store import occupies_lane_slot
from cw.reconcile.stale_dispatch_watch import (
    _WATCHED_PR_SOURCE_STALE_DISPATCH_PARK,
    register_stale_dispatch_watched_prs,
)
from cw.reconcile.tasks.stale_gate import _is_variant_b_gate_task
from tests._reconcile_helpers import park_via_routing
from tests._shared.session_seeding import _make_ticket_task
from tests._ticket_view_helpers import events_of

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


# --- #3634 D7: the own-repo bound on a worker-supplied explicit ref --------

_OTHER_5 = PrRef(repo="other/repo", number=5)
_LOGGER = "cw.reconcile.stale_dispatch_watch"


def _dep_park(ref: object, *, session_id: str | None = "sess-dh") -> TicketTask:
    return _park(
        ticket_id="DH-1",
        disposition="dependency_hold",
        blocked_reason="dependency_unmerged",
        blocked_on_pr=ref,
        session_id=session_id,
    )


def _routed_park(workspace: Path, ref: str) -> TicketTask:
    return park_via_routing(
        "DH-1",
        "client-a",
        workspace,
        "blocked",
        {
            "status": "blocked",
            "blocker": {
                "stage": "s4_finalize",
                "reason": "dependency_unmerged",
                "depends_on_pr_ref": ref,
            },
        },
    )


def _row() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == "DH-1")


def _refusal_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING
    ]


def _refusal_pages() -> list[OrchestratorEvent]:
    return [
        e
        for e in events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
        if e.payload.get("paused_status") == "dependency_ref_not_own_repo"
    ]


class TestWorkerRefBound:
    def test_own_repo_ref_is_normalised_and_watched(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _routed_park(tmp_path, "FOO/Bar#5")
        _no_clients(monkeypatch)
        calls = _stub_slug(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert register_stale_dispatch_watched_prs() == ["DH-1"]

        assert _row().blocked_on_pr == PrRef(number=5)
        [watch] = load_dev_queue().watched_prs
        assert watch.repo == _SLUG
        assert watch.cross_repo is False
        assert len(calls) == 1
        assert _refusal_warnings(caplog) == []
        assert _refusal_pages() == []

    def test_own_repo_watch_is_found_by_a_later_operator_hold(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_dep_park(PrRef(repo=_SLUG, number=5))]))
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch)
        register_stale_dispatch_watched_prs()

        outcome = register_or_adopt_watched_pr(
            WatchedPr(
                pr_url="https://github.com/foo/bar/pull/5",
                repo=_SLUG,
                pr_number=5,
                client="client-a",
                source="dependency_hold",
            )
        )

        assert outcome == "already_active"

    def test_other_repo_ref_is_refused_once(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        routed = _routed_park(tmp_path, "other/repo#5")
        _no_clients(monkeypatch)
        calls = _stub_slug(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert register_stale_dispatch_watched_prs() == []
            assert register_stale_dispatch_watched_prs() == []

        row = _row()
        assert row.blocked_on_pr is None
        assert row.disposition == "awaiting_operator"
        assert occupies_lane_slot(row) is True
        assert _is_variant_b_gate_task(row) is False
        assert load_dev_queue().watched_prs == []
        assert len(calls) == 1
        [warning] = _refusal_warnings(caplog)
        assert warning.args == ("DH-1", "client-a", "other/repo#5")
        [event] = _refusal_pages()
        assert event.payload["paused_status"] == "dependency_ref_not_own_repo"
        assert event.payload["client"] == "client-a"
        assert event.payload["ticket_id"] == "DH-1"
        assert event.payload["session_id"] == routed.session_id
        assert event.payload["lane"] == row.lane
        assert event.payload["crashed"] is False
        assert event.payload["session_name"] == ""
        assert event.payload["claude_session_id"] is None
        assert event.payload["breadcrumbs"] == (
            "worker-supplied dependency ref other/repo#5 is not in this client's"
            " repo foo/bar; a worker may arm an auto-release only for its own"
            " repo. Hold it with: cw dev-queue hold DH-1"
            " --until-pr-merged other/repo#5 -c client-a"
        )
        assert event.correlation_id == "DH-1"

    def test_unresolvable_own_slug_is_indeterminate(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_dep_park(_OTHER_5)]))
        before = load_dev_queue().model_dump_json()
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch, slug=None)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert register_stale_dispatch_watched_prs() == []

        assert load_dev_queue().model_dump_json() == before
        assert _refusal_warnings(caplog) == []
        assert _refusal_pages() == []

        save_dev_queue(DevQueueStore(tasks=[_dep_park(PrRef(repo=_SLUG, number=5))]))
        _stub_slug(monkeypatch)
        assert register_stale_dispatch_watched_prs() == ["DH-1"]

    def test_operator_hold_is_exempt(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[_dep_park(PrRef(repo="o/r", number=5), session_id=None)]
            )
        )
        _no_clients(monkeypatch)
        calls = _stub_slug(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert register_stale_dispatch_watched_prs() == ["DH-1"]

        [watch] = load_dev_queue().watched_prs
        assert watch.repo == "o/r"
        assert watch.cross_repo is True
        assert watch.pr_url == "https://github.com/o/r/pull/5"
        assert calls == []
        assert _row().blocked_on_pr == PrRef(repo="o/r", number=5)
        assert _refusal_warnings(caplog) == []
        assert _refusal_pages() == []

    @pytest.mark.parametrize("ref", [PrRef(repo=_SLUG, number=5), _OTHER_5])
    def test_lost_race_to_an_operator_hold_writes_nothing(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        ref: PrRef,
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_dep_park(ref)]))
        _no_clients(monkeypatch)

        def _slug_then_operator_hold(git_dir: Path) -> str:
            store = load_dev_queue()
            store.tasks[0].session_id = None
            save_dev_queue(store)
            return _SLUG

        monkeypatch.setattr(
            "cw.reconcile.stale_dispatch_watch._resolve_repo_slug",
            _slug_then_operator_hold,
        )

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert register_stale_dispatch_watched_prs() == []

        row = _row()
        assert row.session_id is None
        assert row.blocked_on_pr == ref
        assert row.disposition == "dependency_hold"
        assert load_dev_queue().watched_prs == []
        assert _refusal_warnings(caplog) == []
        assert _refusal_pages() == []

        assert register_stale_dispatch_watched_prs() == ["DH-1"]
        [watch] = load_dev_queue().watched_prs
        assert watch.cross_repo is True

    def test_candidate_key_is_the_ref_key(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        own_watch = WatchedPr(
            pr_url="https://github.com/foo/bar/pull/70",
            repo=_SLUG,
            pr_number=70,
            client="client-a",
            source="dependency_hold",
        )
        hold = _dep_park(PrRef(repo="o/r", number=70), session_id=None)
        save_dev_queue(DevQueueStore(tasks=[hold], watched_prs=[own_watch]))
        _no_clients(monkeypatch)
        _stub_slug(monkeypatch)

        assert register_stale_dispatch_watched_prs() == ["DH-1"]
        assert [(w.repo, w.cross_repo) for w in load_dev_queue().watched_prs] == [
            (_SLUG, False),
            ("o/r", True),
        ]
