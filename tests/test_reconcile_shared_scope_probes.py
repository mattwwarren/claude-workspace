"""Tests for cw.reconcile._shared._scope_probes (#2641).

A salvaged sentinel's self-reported scope is corrected against the branch's
real git diff. The git measurement is captured lock-free before
``sessions_lock``; the in-lock parse only looks it up and applies it. A miss,
a stale capture or a changed sentinel leaves the scope uncorrected (one
WARNING) and never drops the sentinel.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from freezegun import freeze_time

from cw.auto_dev_result import AutoDevResult
from cw.models import CwState, SessionOrigin, SessionStatus
from cw.reconcile._shared import (
    ScopeProbes,
    capture_scope_probe,
    new_scope_probes,
    scope_probe_targets,
)
from cw.reconcile._shared._scope_probes import (
    SCOPE_PROBE_BUDGET_SECONDS,
    SCOPE_PROBE_MAX_AGE_SECONDS,
    CapturedScope,
    ScopeKey,
    apply_prefetched_scope,
)
from cw.reconcile.probe_store import ProbeStoreUnavailableError
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._reconcile_helpers import (
    SCOPE_GUARD_BRANCH,
    SCOPE_GUARD_FILES,
    SCOPE_GUARD_LINES,
    _inflate_scope,
    _make_stale_base_repo,
    _no_op_salvage_payload,
    _shipped_salvage_payload,
    _stage_complete_payload,
)
from tests._shared.session_seeding import _make_daemon_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import ClientConfig, Session

_NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
_COMPUTE = "cw.reconcile._shared._scope_probes.compute_branch_diff_scope"
_RUN_GIT = "cw.worktree._scope._run_git"
_UNAVAILABLE = "salvaged_scope_probe_unavailable"


def _inflated() -> AutoDevResult:
    return AutoDevResult.model_validate(_inflate_scope(_stage_complete_payload()))


def _session(worktree: Path | None, **overrides: object) -> Session:
    fields: dict[str, object] = {
        "id": "sess-scope",
        "name": "client-a/auto-dev/salv-stage",
        "worktree_path": worktree,
    }
    fields.update(overrides)
    return _make_daemon_session(**fields)


def _forbid_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> object:
        msg = "git must not run here"
        raise AssertionError(msg)

    monkeypatch.setattr(_RUN_GIT, _boom)
    monkeypatch.setattr(_COMPUTE, _boom)


def _unavailable_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if _UNAVAILABLE in r.getMessage()]


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    return _make_stale_base_repo(make_git_repo, "wt-2641-scope")


class TestCapture:
    def test_hit_stores_uncorrected_result_branch_and_measurement(
        self, tmp_config_dir: Path, repo: Path
    ) -> None:
        probes = new_scope_probes()
        result = _inflated()

        captured = capture_scope_probe(probes, _session(repo), result)

        assert captured is True
        key = ScopeKey("sess-scope", repo)
        assert probes.keys == frozenset({key})
        stored = probes.lookup(key)
        assert stored.result == result
        assert stored.result.scope.files == 18
        assert stored.default_branch == "main"
        assert stored.measured is not None
        assert stored.measured["branch"] == SCOPE_GUARD_BRANCH
        assert stored.measured["files"] == SCOPE_GUARD_FILES
        assert stored.measured["lines_actual"] == SCOPE_GUARD_LINES

    def test_client_default_branch_is_measured(
        self, tmp_config_dir: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _make_stale_base_repo(
            make_git_repo, "wt-2641-trunk", default_branch="trunk"
        )
        write_clients_yaml(
            ClientSpec("client-a", "/tmp/ws-2641", default_branch="trunk")
        )
        probes = new_scope_probes()

        capture_scope_probe(probes, _session(worktree), _inflated())

        stored = probes.lookup(ScopeKey("sess-scope", worktree))
        assert stored.default_branch == "trunk"
        assert stored.measured is not None
        assert stored.measured["files"] == SCOPE_GUARD_FILES

    def test_config_failure_falls_back_to_main(
        self,
        tmp_config_dir: Path,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from cw.exceptions import CwError

        def _boom() -> dict[str, ClientConfig]:
            msg = "clients.yaml is unreadable"
            raise CwError(msg)

        monkeypatch.setattr("cw.worktree._scope.load_effective_clients", _boom)
        probes = new_scope_probes()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            capture_scope_probe(probes, _session(repo), _inflated())

        stored = probes.lookup(ScopeKey("sess-scope", repo))
        assert stored.default_branch == "main"
        assert stored.measured is not None
        assert stored.measured["files"] == SCOPE_GUARD_FILES
        assert "scope_verification_client_unresolved" in caplog.text
        assert "session=sess-scope" in caplog.text

    def test_missing_client_key_falls_back_to_main(
        self,
        tmp_config_dir: Path,
        repo: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        write_clients_yaml(ClientSpec("some-other-client", "/tmp/ws-other"))
        probes = new_scope_probes()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            capture_scope_probe(probes, _session(repo), _inflated())

        stored = probes.lookup(ScopeKey("sess-scope", repo))
        assert stored.default_branch == "main"
        assert stored.measured is not None
        assert "scope_verification_client_unresolved" in caplog.text
        assert "client=client-a" in caplog.text

    def test_non_git_worktree_stores_none_measurement(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt-nogit"
        worktree.mkdir()
        probes = new_scope_probes()

        capture_scope_probe(probes, _session(worktree), _inflated())

        assert probes.lookup(ScopeKey("sess-scope", worktree)).measured is None

    @pytest.mark.parametrize("case", ["pre_impl", "no_worktree"])
    def test_unmeasurable_target_runs_no_git(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        case: str,
    ) -> None:
        _forbid_git(monkeypatch)
        probes = new_scope_probes()
        if case == "pre_impl":
            result = AutoDevResult.model_validate(_no_op_salvage_payload())
            session = _session(tmp_path)
        else:
            result = _inflated()
            session = _session(None)

        assert capture_scope_probe(probes, session, result) is False
        assert probes.captures == 0
        assert probes.keys == frozenset()

    def test_budget_refusal_escapes_and_stores_nothing(
        self, tmp_config_dir: Path, repo: Path
    ) -> None:
        probes = new_scope_probes(budget_seconds=0.0)

        with pytest.raises(ProbeStoreUnavailableError) as excinfo:
            capture_scope_probe(probes, _session(repo), _inflated())

        assert excinfo.value.reason == "budget"
        assert probes.keys == frozenset()

    def test_store_defaults(self) -> None:
        probes = new_scope_probes()

        assert probes.budget_seconds == SCOPE_PROBE_BUDGET_SECONDS
        assert probes.max_captures is None
        assert probes.max_age_seconds == SCOPE_PROBE_MAX_AGE_SECONDS
        assert new_scope_probes(budget_seconds=None).budget_seconds is None


def _captured_store(session: Session, result: AutoDevResult) -> ScopeProbes:
    probes = new_scope_probes()
    capture_scope_probe(probes, session, result)
    return probes


class TestApply:
    def test_hit_corrects_scope_and_leaves_other_fields(
        self,
        tmp_config_dir: Path,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _session(repo)
        result = _inflated()
        probes = _captured_store(session, result)
        _forbid_git(monkeypatch)

        out = apply_prefetched_scope(result, session, probes)

        assert out.scope.files == SCOPE_GUARD_FILES
        assert out.scope.lines_actual == SCOPE_GUARD_LINES
        assert out.scope.lines_estimate == 60
        assert out.scope.tier == "small"
        assert out.model_dump(exclude={"scope"}) == result.model_dump(exclude={"scope"})

    def test_none_measurement_hit_warns_unavailable_and_keeps_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        worktree = tmp_path / "wt-nogit"
        worktree.mkdir()
        session = _session(worktree)
        result = _inflated()
        probes = _captured_store(session, result)

        with caplog.at_level(logging.WARNING):
            out = apply_prefetched_scope(result, session, probes)

        assert out is result
        assert "scope_verification_unavailable" in caplog.text
        assert _unavailable_records(caplog) == []

    @pytest.mark.parametrize("case", ["pre_impl", "no_worktree"])
    def test_unmeasurable_target_does_not_consult_store(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        case: str,
    ) -> None:
        _forbid_git(monkeypatch)
        if case == "pre_impl":
            result = AutoDevResult.model_validate(_no_op_salvage_payload())
            session = _session(tmp_path)
        else:
            result = _inflated()
            session = _session(None)

        with caplog.at_level(logging.WARNING):
            out = apply_prefetched_scope(result, session, new_scope_probes())

        assert out is result
        assert caplog.text == ""

    def test_none_store_is_silent_and_runs_no_git(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _forbid_git(monkeypatch)
        result = _inflated()

        with caplog.at_level(logging.WARNING):
            out = apply_prefetched_scope(result, _session(tmp_path), None)

        assert out is result
        assert caplog.text == ""

    def test_missing_key_warns_once_and_leaves_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _forbid_git(monkeypatch)
        result = _inflated()

        with caplog.at_level(logging.WARNING):
            out = apply_prefetched_scope(result, _session(tmp_path), new_scope_probes())

        assert out is result
        messages = _unavailable_records(caplog)
        assert len(messages) == 1
        assert "session=sess-scope" in messages[0]
        assert "ticket=salv-stage" in messages[0]
        assert "client=client-a" in messages[0]
        assert "reason=missing" in messages[0]

    def test_budget_exhausted_capture_reads_as_miss(
        self,
        tmp_config_dir: Path,
        repo: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = _session(repo)
        result = _inflated()
        probes = new_scope_probes(budget_seconds=0.0)
        with pytest.raises(ProbeStoreUnavailableError):
            capture_scope_probe(probes, session, result)

        with caplog.at_level(logging.WARNING):
            out = apply_prefetched_scope(result, session, probes)

        assert out is result
        assert len(_unavailable_records(caplog)) == 1

    @pytest.mark.parametrize(
        "offset",
        [
            timedelta(seconds=SCOPE_PROBE_MAX_AGE_SECONDS + 1),
            timedelta(seconds=-1),
        ],
        ids=["aged_out", "negative_age"],
    )
    def test_stale_capture_warns_once_and_leaves_scope(
        self,
        tmp_config_dir: Path,
        repo: Path,
        caplog: pytest.LogCaptureFixture,
        offset: timedelta,
    ) -> None:
        session = _session(repo)
        result = _inflated()
        with freeze_time(_NOW):
            probes = _captured_store(session, result)

        with freeze_time(_NOW + offset), caplog.at_level(logging.WARNING):
            out = apply_prefetched_scope(result, session, probes)

        assert out is result
        messages = _unavailable_records(caplog)
        assert len(messages) == 1
        assert "reason=stale" in messages[0]

    @pytest.mark.parametrize("change", ["scope", "status", "stage"])
    def test_changed_sentinel_is_not_corrected(
        self,
        tmp_config_dir: Path,
        repo: Path,
        caplog: pytest.LogCaptureFixture,
        change: str,
    ) -> None:
        """The worker emitted a different sentinel after capture: stale evidence."""
        session = _session(repo)
        probes = _captured_store(session, _inflated())
        payload: dict[str, Any] = _inflate_scope(_stage_complete_payload())
        if change == "scope":
            payload["scope"]["files"] = 40
        elif change == "status":
            payload = _inflate_scope(_shipped_salvage_payload("salv-stage"))
        else:
            payload["stage_reached"] = "stage3_review"
        later = AutoDevResult.model_validate(payload)

        with caplog.at_level(logging.WARNING):
            out = apply_prefetched_scope(later, session, probes)

        assert out is later
        messages = _unavailable_records(caplog)
        assert len(messages) == 1
        assert "reason=changed" in messages[0]

    @pytest.mark.parametrize("change", ["session_id", "worktree_path"])
    def test_different_session_or_worktree_is_a_miss(
        self,
        tmp_config_dir: Path,
        repo: Path,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        change: str,
    ) -> None:
        result = _inflated()
        probes = _captured_store(_session(repo), result)
        if change == "session_id":
            other = _session(repo, id="sess-other")
        else:
            other_path = tmp_path / "wt-other"
            other_path.mkdir()
            other = _session(other_path)

        with caplog.at_level(logging.WARNING):
            out = apply_prefetched_scope(result, other, probes)

        assert out is result
        messages = _unavailable_records(caplog)
        assert len(messages) == 1
        assert "reason=missing" in messages[0]

    def test_apply_never_runs_git(
        self,
        tmp_config_dir: Path,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _session(repo)
        result = _inflated()
        probes = _captured_store(session, result)

        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "apply must not run git"
            raise AssertionError(msg)

        monkeypatch.setattr(_RUN_GIT, _boom)
        # The branch probe runs its own subprocess.run, not _run_git (#2903).
        monkeypatch.setattr("cw.worktree._git.subprocess.run", _boom)

        out = apply_prefetched_scope(result, session, probes)

        assert out.scope.files == SCOPE_GUARD_FILES

    def test_hand_built_capture_applies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The store holds a plain CapturedScope; apply reads only that."""
        _forbid_git(monkeypatch)
        session = _session(tmp_path)
        result = _inflated()
        probes = new_scope_probes()
        probes.capture(
            ScopeKey("sess-scope", tmp_path),
            read=lambda _at: CapturedScope(
                result=result,
                default_branch="main",
                measured={
                    "branch": "b",
                    "merge_base": "m",
                    "files": 2,
                    "lines_actual": 7,
                },
            ),
        )

        out = apply_prefetched_scope(result, session, probes)

        assert (out.scope.files, out.scope.lines_actual) == (2, 7)


class TestTargets:
    def test_only_live_daemon_sessions_with_a_worktree(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        active = _session(wt, id="active")
        idle = _session(wt, id="idle", status=SessionStatus.IDLE)
        state = CwState(
            sessions=[
                active,
                idle,
                _session(wt, id="completed", status=SessionStatus.COMPLETED),
                _session(wt, id="bg", status=SessionStatus.BACKGROUNDED),
                _session(wt, id="user", origin=SessionOrigin.USER),
                _session(None, id="no-wt"),
            ]
        )

        targets = scope_probe_targets(state)

        assert [s.id for s in targets] == ["active", "idle"]
