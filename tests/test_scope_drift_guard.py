"""Synthetic on-disk replay of an adopted review repair and its refusals."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result

from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
from cw.cli import main
from cw.codex_review import make_codex_blocked
from cw.config import (
    clients_file,
    dev_queue_file,
    get_client,
    orchestrator_config_file,
    state_file,
)
from cw.config import (
    dev_queue_lock as dev_queue_lock_path,
)
from cw.dev_queue import approve_scope_drift_ticket, load_dev_queue, save_dev_queue
from cw.dev_queue.scope_drift_guard import (
    ScopeGrantExpectation,
    ScopeGrantNotAppliedError,
)
from cw.dev_queue.storage import dev_queue_lock
from cw.events import inbox_path, read_events, record_event
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.models.events import PrState
from cw.review_adjudication import Adjudication, apply_adjudication
from cw.review_convergence import (
    bind_confirm_verdict,
    load_bound_round_verdict,
    load_ledger,
    load_round_verdict,
    record_round,
    save_ledger,
    save_round_verdict,
)
from cw.stage_verification.impl_gates import _drift_block
from cw.ticket_view import lock_scope_grant_park
from cw.worktree import worktree_path_for
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._git_sandbox import git
from tests._review_convergence_helpers import (
    engine_confirm_payload,
    engine_confirm_verdict,
    engine_pass_ledger,
)
from tests._shared.scope_grant_builders import drift_details
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    make_task,
    seed_queue,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from typing import IO

    from cw.models import DevQueueStore
    from cw.review_convergence import ConvergenceLedger
    from cw.review_findings import ReviewVerdict

_HEAD = "a" * 40
_URL = "https://github.com/example/widgets/pull/7"
_PATH = "tests/test_widget.py"
_BRANCH = "topic/widget"


@pytest.fixture
def isolated_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object]]:
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "ws"
    write_clients_yaml(
        ClientSpec(ACME, workspace, worktree_base=tmp_path / "dispatched"),
        ensure_workspaces=True,
    )
    git(workspace, "init")
    git(workspace, "remote", "add", "origin", "https://github.com/example/widgets.git")
    worktree = worktree_path_for(get_client(ACME), _BRANCH)
    task = make_task(
        status=QueueItemStatus.BLOCKED_ON_USER,
        stage=Stage.IMPL,
        blocked_reason="plan_scope_drift",
        branch=_BRANCH,
        adopted_head_sha=_HEAD,
        adopted_pr_url=_URL,
        pr_url=_URL,
        worktree_path=worktree,
        session_id="sess-impl",
    )
    session = bound_session_for(task, worktree_path=worktree, stage=Stage.IMPL)
    session.last_result = make_codex_blocked(
        ticket_id=ACME_TICKET,
        worktree=worktree,
        reason="plan_scope_drift",
        details=drift_details([_PATH]),
        stage_reached="stage2_impl",
    ).model_dump(mode="json")
    seed_queue([task], sessions=[session])
    ledger_path = worktree / ".cw/review-convergence.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "ticket_id": ACME_TICKET,
                "fork_point": "f" * 40,
                "max_cycles": 2,
                "loop_backend": "codex",
                "confirm_backend": "claude",
                "checkpoint_3a_sha": _HEAD,
                "status": "stopped",
                "stop_reason": "confirm_must_fix",
                "stop_identities": [
                    {
                        "severity": "MUST_FIX",
                        "file": "src/widget.py",
                        "summary_normalized": "Cover the widget",
                    }
                ],
                "rounds": [
                    {
                        "round": 2,
                        "kind": "confirm",
                        "backend": "claude",
                        "reviewed_sha": _HEAD,
                        "new_accepted": 1,
                        "new_fix": 1,
                        "new_operator_action": 0,
                        "new_settled": 0,
                        "resurfaced_settled": 0,
                        "clean": False,
                        "roster": [],
                        "recorded_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    verdict = engine_confirm_verdict(
        must_fix=["Cover the widget"], sha=_HEAD, file="src/widget.py"
    )
    save_round_verdict(ledger_path, 2, verdict)
    ledger = load_ledger(ledger_path)
    assert ledger is not None
    save_ledger(ledger_path, bind_confirm_verdict(ledger, 2, verdict))
    remote: dict[str, object] = {
        "headRefName": _BRANCH,
        "headRefOid": _HEAD,
        "baseRefName": "main",
        "state": "OPEN",
        "isCrossRepository": False,
    }
    monkeypatch.setattr(
        "cw.dev_queue.scope_drift_guard.fetch_pr_view",
        lambda *_a, **_kw: remote or None,
    )
    monkeypatch.setattr("cw.operator_identity.current_gh_login", lambda **_: "operator")
    return ledger_path, remote


@pytest.fixture
def parked_repair(
    isolated_park: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object], list[object]]:
    ledger_path, remote = isolated_park
    events: list[object] = []
    monkeypatch.setattr(
        "cw.dev_queue.scope_drift.record_event", lambda *args: events.append(args)
    )
    monkeypatch.setattr(
        "cw.cli.dev_queue.approve._close_orphans_after_approve", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        "cw.cli.dev_queue.approve.settle_lagging_row", _unexpected_settle
    )
    return ledger_path, remote, events


def _unexpected_settle(*_args: object) -> None:
    pytest.fail("Guarded requests must not settle a lagging row before comparison")


def _show_token() -> str:
    token = _park_token()
    assert token is not None
    assert len(token) == 64
    return token


def _park_token() -> str | None:
    result = CliRunner().invoke(
        main, ["dev-queue", "show", ACME_TICKET, "-c", ACME, "--json"]
    )
    assert result.exit_code == 0, result.output
    token: str | None = json.loads(result.stdout)["scope_decision"].get("park_token")
    return token


def _approve(token: str, *, files: str = _PATH, head: str = _HEAD) -> Result:
    return CliRunner().invoke(
        main,
        [
            "dev-queue",
            "approve",
            ACME_TICKET,
            "-c",
            ACME,
            "--scope-drift",
            files,
            "--expected-head",
            head,
            "--expected-park",
            token,
        ],
    )


def _park_files(ledger: Path) -> dict[Path, tuple[bytes, int] | None]:
    """Snapshot isolated data files and detect even a same-byte rewrite."""
    paths = [dev_queue_file(), state_file(), ledger, inbox_path()]
    paths.extend(ledger.with_suffix("").glob("round-*-verdict.json"))
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
        for path in paths
    }


def _assert_park_refusal(token: str) -> None:
    result = _approve(token)
    if not result.stdout and result.exception is not None:
        raise result.exception
    assert result.exit_code != 0, result.output
    assert json.loads(result.stdout) == {
        "scope_grant": "not_applied",
        "reason": "park_changed",
    }


def test_dispatched_park_uses_its_session_worktree_after_config_moves(
    isolated_park: tuple[Path, dict[str, object]], tmp_path: Path
) -> None:
    ledger_path, _ = isolated_park
    store = load_dev_queue()
    store.tasks[0].worktree_path = None
    save_dev_queue(store)
    token = _show_token()

    workspace = get_client(ACME).workspace_path
    write_clients_yaml(ClientSpec(ACME, workspace, worktree_base=tmp_path / "moved"))
    other_tree = worktree_path_for(get_client(ACME), _BRANCH)
    other_ledger = other_tree / ".cw/review-convergence.json"
    other_verdict = engine_confirm_verdict(
        must_fix=["Cover another behavior"], sha=_HEAD, file="src/widget.py"
    )
    _record_engine_stop(other_ledger, other_verdict)
    assert _park_token() == token

    ledger_path.unlink()
    before = _park_files(ledger_path) | _park_files(other_ledger)
    assert _park_token() is None
    _assert_park_refusal(token)
    assert _park_files(ledger_path) | _park_files(other_ledger) == before


@pytest.mark.parametrize(
    "mismatch",
    ["client", "ticket", "result-ticket", "reason", "path", "no-path", "no-session"],
)
def test_park_refuses_inconsistent_session_evidence(
    isolated_park: tuple[Path, dict[str, object]], tmp_path: Path, mismatch: str
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    path = state_file()
    state = json.loads(path.read_bytes())
    (session,) = state["sessions"]
    if mismatch == "client":
        session["client"] = "another-client"
    elif mismatch == "ticket":
        session["name"] = f"{ACME}/auto-dev/ACM-2"
    elif mismatch == "result-ticket":
        session["last_result"]["ticket_id"] = "ACM-2"
    elif mismatch == "reason":
        session["last_result"]["blocker"]["reason"] = "impl_failed"
    elif mismatch == "path":
        session["worktree_path"] = str(tmp_path / "elsewhere")
    elif mismatch == "no-path":
        session["worktree_path"] = None
    else:
        state["sessions"].clear()
    path.write_text(json.dumps(state), encoding="utf-8")

    before = _park_files(ledger_path)
    assert _park_token() is None
    _assert_park_refusal(token)
    assert _park_files(ledger_path) == before


@pytest.mark.parametrize(
    ("source", "malformed"),
    [
        (clients_file, b"clients: ["),
        (clients_file, b"\xff"),
        (state_file, b'{"unexpected": true}'),
        (dev_queue_file, b'{"unexpected": true}'),
    ],
    ids=["clients-yaml", "clients-encoding", "session-schema", "queue-schema"],
)
def test_guarded_refusal_normalizes_malformed_inputs(
    isolated_park: tuple[Path, dict[str, object]],
    source: Callable[[], Path],
    malformed: bytes,
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    path = source()
    # Keep the real rows and their evidence so schema corruption alone causes
    # the refusal, even if a migration tries to stamp a new version over it.
    if source in (state_file, dev_queue_file):
        payload = json.loads(path.read_bytes())
        payload["schema_version"] = json.loads(malformed)
        malformed = json.dumps(payload).encode()
    path.write_bytes(malformed)

    before = _park_files(ledger_path)
    _assert_park_refusal(token)
    assert _park_files(ledger_path) == before
    assert path.read_bytes() == malformed


def test_guarded_grant_requeues_once(
    parked_repair: tuple[Path, dict[str, object], list[object]],
) -> None:
    _, _, events = parked_repair
    token = _show_token()
    result = _approve(token)
    assert result.exit_code == 0, result.output
    store = load_dev_queue()
    assert store.tasks[0].status == QueueItemStatus.PENDING
    assert store.tasks[0].stage == Stage.IMPL
    assert store.tasks[0].scope_grants[0].paths == [_PATH]
    assert store.tasks[0].scope_grants[0].head == _HEAD
    assert len(events) == 1
    retry = _approve(token)
    assert retry.exit_code != 0
    assert json.loads(retry.stdout) == {
        "scope_grant": "not_applied",
        "reason": "park_changed",
    }
    assert load_dev_queue() == store
    assert len(events) == 1


def test_competing_guarded_grants_compare_inside_the_real_queue_lock(
    parked_repair: tuple[Path, dict[str, object], list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, remote, events = parked_repair
    expected = ScopeGrantExpectation(_HEAD, _show_token(), lock_scope_grant_park)
    first_in_head_check, second_at_lock, resume_first = Event(), Event(), Event()

    @contextmanager
    def observe_lock(*, bounded: bool = False) -> Iterator[None]:
        if first_in_head_check.is_set():
            second_at_lock.set()
        with dev_queue_lock(bounded=bounded):
            yield

    def pause_during_head_check(*_args: object, **_kw: object) -> dict[str, object]:
        assert not first_in_head_check.is_set(), "retry reached head check"
        first_in_head_check.set()
        assert resume_first.wait(timeout=10)
        return remote

    def apply() -> None:
        approve_scope_drift_ticket(
            ACME_TICKET, ACME, [_PATH], repo="example/widgets", expected=expected
        )

    monkeypatch.setattr("cw.dev_queue.scope_drift._lock", observe_lock)
    monkeypatch.setattr(
        "cw.dev_queue.scope_drift_guard.fetch_pr_view", pause_during_head_check
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(apply)
        try:
            assert first_in_head_check.wait(timeout=10)
            second = pool.submit(apply)
            assert second_at_lock.wait(timeout=10)
            assert not second.done()
        finally:
            resume_first.set()
        first.result(timeout=10)
        with pytest.raises(ScopeGrantNotAppliedError) as refusal:
            second.result(timeout=10)
    assert refusal.value.reason == "park_changed"
    (task,) = load_dev_queue().tasks
    assert task.status == QueueItemStatus.PENDING
    assert len(task.scope_grants) == len(events) == 1


def test_guarded_read_failures_are_structured_without_recreating_config(
    parked_repair: tuple[Path, dict[str, object], list[object]],
) -> None:
    _, _, events = parked_repair
    config = orchestrator_config_file()
    token = _show_token()
    config.unlink()
    assert not config.exists()
    queue = dev_queue_file()
    queue.write_text("{", encoding="utf-8")
    result = _approve(token)
    assert result.exit_code != 0
    assert json.loads(result.stdout) == {
        "scope_grant": "not_applied",
        "reason": "park_changed",
    }
    assert queue.read_text(encoding="utf-8") == "{"
    assert not config.exists()
    assert events == []


@pytest.mark.parametrize(
    ("source", "replacement"),
    [
        (orchestrator_config_file, b"linear_prefix_map: ["),
        (orchestrator_config_file, b"linear_prefix_map: !!int invalid"),
        (orchestrator_config_file, b"linear_prefix_map: false"),
        (orchestrator_config_file, None),
        (dev_queue_file, b"[]"),
        (state_file, b"null"),
        (state_file, b'{"schema_version": 1, "sessions": [{"origin": []}]}'),
    ],
    ids=[
        "config-yaml",
        "config-scalar",
        "config-schema",
        "config-io",
        "queue-array",
        "state-null",
        "state-legacy-origin",
    ],
)
def test_guarded_refusal_for_unloadable_inputs(
    parked_repair: tuple[Path, dict[str, object], list[object]],
    source: Callable[[], Path],
    replacement: bytes | None,
) -> None:
    ledger_path, _, events = parked_repair
    token = _show_token()
    path = source()
    path.parent.mkdir(parents=True, exist_ok=True)
    if replacement is None:
        path.unlink(missing_ok=True)
        path.mkdir()
    else:
        path.write_bytes(replacement)
    files = {dev_queue_file(), state_file(), ledger_path}
    if replacement is not None:
        files.add(path)
    before = {item: item.read_bytes() for item in files}
    audit = read_events()

    result = _approve(token)

    assert result.exit_code != 0, result.output
    assert json.loads(result.stdout) == {
        "scope_grant": "not_applied",
        "reason": "park_changed",
    }
    assert {item: item.read_bytes() for item in files} == before
    if replacement is None:
        assert path.is_dir()
    assert events == []
    assert read_events() == audit


def test_routine_pr_hydration_keeps_the_park_token(
    parked_repair: tuple[Path, dict[str, object], list[object]],
) -> None:
    store = load_dev_queue()
    state = PrState(hydrated_at=datetime(2026, 1, 1, tzinfo=UTC))
    store.tasks[0].pr_state = state
    save_dev_queue(store)
    token = _show_token()
    state.hydrated_at += timedelta(minutes=1)
    save_dev_queue(store)
    assert _show_token() == token
    result = _approve(token)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("moved_head", "stale_head"),
        ("unreadable_head", "head_unavailable"),
        ("replaced_park", "park_changed"),
        ("replaced_verdict", "park_changed"),
        ("missing_ledger", "park_changed"),
        ("corrupt_verdict", "park_changed"),
        ("settled_ledger", "park_changed"),
        ("unbound_ledger", "park_changed"),
        ("missing_row", "park_changed"),
        ("wrong_path", "park_changed"),
        ("human_scope", "park_changed"),
        ("wrong_branch", "park_changed"),
        ("retargeted_pr", "park_changed"),
    ],
)
def test_guarded_refusals_do_not_change_queue_or_audit(
    parked_repair: tuple[Path, dict[str, object], list[object]],
    change: str,
    reason: str,
) -> None:
    ledger_path, remote, events = parked_repair
    token = _show_token()
    if change == "moved_head":
        remote["headRefOid"] = "b" * 40
    if change == "unreadable_head":
        remote.clear()
    if change in {"replaced_park", "human_scope"}:
        store = load_dev_queue()
        if change == "replaced_park":
            store.tasks[0].attempts += 1
        else:
            store.tasks[0].scope_hint = "large"
        save_dev_queue(store)
    if change == "replaced_verdict":
        path = ledger_path.with_suffix("") / "round-2-verdict.json"
        verdict = json.loads(path.read_text(encoding="utf-8"))
        verdict["must_fix"][0]["summary"] = "Cover the widget and a second case"
        path.write_text(json.dumps(verdict), encoding="utf-8")
    if change == "missing_ledger":
        ledger_path.unlink()
    if change == "corrupt_verdict":
        (ledger_path.with_suffix("") / "round-2-verdict.json").write_text(
            "{", encoding="utf-8"
        )
    if change == "settled_ledger":
        ledger = load_ledger(ledger_path)
        assert ledger is not None
        ledger.stop_identities.clear()
        save_ledger(ledger_path, ledger)
    if change == "unbound_ledger":
        ledger = load_ledger(ledger_path)
        assert ledger is not None
        ledger.rounds[-1] = ledger.rounds[-1].model_copy(
            update={"verdict_sha256": None}
        )
        save_ledger(ledger_path, ledger)
    if change == "missing_row":
        store = load_dev_queue()
        store.tasks.clear()
        save_dev_queue(store)
    if change in {"wrong_branch", "retargeted_pr"}:
        remote.update(
            {
                "wrong_branch": {"headRefName": "topic/other"},
                "retargeted_pr": {"baseRefName": "other-base"},
            }[change]
        )
    store = load_dev_queue()
    result = _approve(
        token, files="tests/test_other.py" if change == "wrong_path" else _PATH
    )
    assert result.exit_code != 0, result.output
    assert json.loads(result.stdout) == {"scope_grant": "not_applied", "reason": reason}
    assert load_dev_queue() == store
    assert events == []


def test_undecodable_pr_read_refuses_grant(
    isolated_park: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    before = _park_files(ledger_path)

    def invalid_output(*_args: object, **_kwargs: object) -> None:
        b"\xff".decode()

    monkeypatch.setattr("cw.dev_queue.scope_drift_guard.fetch_pr_view", invalid_output)
    result = _approve(token)

    assert result.exit_code != 0, result.output
    assert json.loads(result.stdout) == {
        "scope_grant": "not_applied",
        "reason": "head_unavailable",
    }
    assert _park_files(ledger_path) == before


@pytest.mark.parametrize(
    ("details", "eligible"),
    [
        (
            "Step 2.5 gate 2: 2 files outside the plan's enumeration (allowed 0). "
            "Extra: " + _PATH + ", docs/notes.md",
            True,
        ),
        (
            "Step 2.5 gate 2: 1 file outside the plan's enumeration (allowed 0). "
            "Extra: docs/report,notes.md",
            False,
        ),
        (drift_details([f"docs/{'x' * 115}...[truncated]"]), False),
        (drift_details(["docs/<redacted>.md"]), False),
        (drift_details(["docs/report,notes.md"]), False),
        (
            _drift_block(
                {"triggered": True, "extra_files": [f"{_PATH}\n"], "allowed_extra": 0},
                "",
            ).details,
            False,
        ),
        (drift_details([f" {_PATH}"]), False),
        (drift_details([_PATH]) + "\nnot a path", False),
    ],
)
def test_guard_requires_lossless_requested_paths(
    isolated_park: tuple[Path, dict[str, object]], details: str, *, eligible: bool
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    state = json.loads(state_file().read_bytes())
    state["sessions"][0]["last_result"]["blocker"]["details"] = details
    state_file().write_text(json.dumps(state))

    assert (_park_token() is not None) is eligible
    before = _park_files(ledger_path)
    _assert_park_refusal(token)
    assert _park_files(ledger_path) == before


def test_guard_flags_not_silently_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main, ["dev-queue", "approve", ACME_TICKET, "--expected-head", _HEAD]
    )
    assert result.exit_code == 2
    assert "--expected-head and --expected-park require --scope-drift" in result.stderr


def test_normal_dirty_confirm_offers_a_token_for_its_recorded_fix(
    parked_repair: tuple[Path, dict[str, object], list[object]],
) -> None:
    ledger_path, _, _ = parked_repair
    verdict = engine_confirm_verdict(must_fix=["Cover the widget"], sha=_HEAD)
    _record_non_engine_confirm(ledger_path, verdict)
    result = _approve(_show_token())
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("rule_id", "settled_severity", "still_actionable"),
    [
        (None, "MUST_FIX", False),
        ("style.naming", "MUST_FIX", False),
        ("style.naming", "SHOULD_FIX", True),
        ("coverage.branch", "MUST_FIX", True),
    ],
    ids=["builtin-reject", "engine-defer", "escalated", "evidence-gap-rule"],
)
def test_normal_dirty_confirm_token_after_operator_settle(
    isolated_park: tuple[Path, dict[str, object]],
    rule_id: str | None,
    settled_severity: str,
    still_actionable: bool,
) -> None:
    ledger_path, _ = isolated_park
    save_ledger(
        ledger_path, engine_pass_ledger(ticket_id=ACME_TICKET, sha=_HEAD, engine=False)
    )
    verdict = engine_confirm_verdict(must_fix=["Same blocking bug"], sha=_HEAD)
    (finding,) = verdict.must_fix
    if rule_id is not None:
        finding = finding.model_copy(
            update={"source": "engine", "fingerprint": "fp-000001", "rule_id": rule_id}
        )
        verdict.must_fix[0] = finding
        verdict.accepted[0].finding = finding
    record = CliRunner().invoke(
        main,
        ["review", "converge", "record-round", "-", "--ledger", str(ledger_path)],
        input=json.dumps(engine_confirm_payload(verdict)),
    )
    assert record.exit_code == 0, record.output
    token = _show_token()
    target = finding.model_dump(include={"file", "summary", "fingerprint"})
    target["severity"] = settled_severity
    settle = CliRunner().invoke(
        main,
        [
            "review",
            "converge",
            "operator-settle",
            "-",
            "--ticket-id",
            ACME_TICKET,
            "-c",
            ACME,
            "--reason",
            "Existing validation covers this case.",
            "--outcome",
            "reject" if rule_id is None else "defer",
        ],
        input=json.dumps({"entries": [target]}),
    )
    assert settle.exit_code == 0, settle.output
    ledger = load_ledger(ledger_path)
    assert ledger is not None
    assert ledger.status == "in_progress"
    assert ledger.fixed
    assert not ledger.rounds[-1].clean
    (settled,) = ledger.settled
    assert settled.origin == "operator"
    assert read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_SETTLED])

    before = _park_files(ledger_path)
    current = _park_token()
    assert (current is not None) == still_actionable
    _assert_park_refusal(token)
    assert _park_files(ledger_path) == before


def test_repeated_dirty_confirm_offers_a_token_for_the_latest_verdict(
    parked_repair: tuple[Path, dict[str, object], list[object]],
) -> None:
    ledger_path, _, _ = parked_repair
    save_ledger(
        ledger_path, engine_pass_ledger(ticket_id=ACME_TICKET, sha=_HEAD, engine=False)
    )
    verdict = engine_confirm_verdict(must_fix=["Same blocking bug"], sha=_HEAD)
    command = ["review", "converge", "record-round", "-", "--ledger", str(ledger_path)]
    payload = json.dumps(engine_confirm_payload(verdict))
    first = CliRunner().invoke(main, command, input=payload)
    assert first.exit_code == 0, first.output
    prior_token = _show_token()
    ledger = load_ledger(ledger_path)
    assert ledger is not None
    awaiting, report = record_round(
        ledger,
        kind="loop",
        backend=ledger.loop_backend,
        reviewed_sha=_HEAD,
        findings=[],
        roster=[],
        now="2026-11-25T00:00:00Z",
    )
    assert report.decision == "confirm_required"
    save_ledger(ledger_path, awaiting)
    second = CliRunner().invoke(main, command, input=payload)
    assert second.exit_code == 0, second.output
    assert _show_token() != prior_token


def _record_engine_stop(ledger_path: Path, verdict: ReviewVerdict) -> ConvergenceLedger:
    save_ledger(ledger_path, engine_pass_ledger(ticket_id=ACME_TICKET, sha=_HEAD))
    result = CliRunner().invoke(
        main,
        ["review", "converge", "record-round", "-", "--ledger", str(ledger_path)],
        input=json.dumps(engine_confirm_payload(verdict)),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["decision"] == "stop_confirm_must_fix"
    ledger = load_ledger(ledger_path)
    assert ledger is not None
    return ledger


@pytest.mark.parametrize(
    ("must_fix", "should_fix"),
    [
        ([], [("Missing branch coverage", "coverage.branch")]),
        ([], [("Missing acceptance check", "acceptance.check")]),
        (
            ["Blocking regression"],
            [
                ("Missing branch coverage", "coverage.branch"),
                ("Missing acceptance check", "acceptance.check"),
            ],
        ),
    ],
    ids=["coverage-only", "acceptance-only", "mixed"],
)
def test_recorded_engine_stop_evidence_offers_a_guarded_grant(
    parked_repair: tuple[Path, dict[str, object], list[object]],
    must_fix: list[str],
    should_fix: list[tuple[str, str]],
) -> None:
    ledger_path, _, events = parked_repair
    verdict = engine_confirm_verdict(
        must_fix=must_fix, should_fix=should_fix, sha=_HEAD
    )
    ledger = _record_engine_stop(ledger_path, verdict)
    assert {identity.summary_normalized for identity in ledger.stop_identities} == {
        *must_fix,
        *(summary for summary, _ in should_fix),
    }
    last = ledger.rounds[-1]
    bound = load_bound_round_verdict(ledger_path, last.round, last.verdict_sha256)
    assert bound is not None
    assert bound.blocking is bool(must_fix)
    assert len(bound.must_fix) == len(must_fix)

    approved = _approve(_show_token())

    assert approved.exit_code == 0, approved.output
    (task,) = load_dev_queue().tasks
    assert task.status == QueueItemStatus.PENDING
    assert len(task.scope_grants) == len(events) == 1


def test_engine_stop_requires_every_recorded_identity_to_remain_park_class(
    parked_repair: tuple[Path, dict[str, object], list[object]],
) -> None:
    ledger_path, _, events = parked_repair
    ledger = _record_engine_stop(
        ledger_path,
        engine_confirm_verdict(
            must_fix=["Blocking regression"],
            should_fix=[("Missing branch coverage", "coverage.branch")],
            sha=_HEAD,
        ),
    )
    token = _show_token()
    round_number = ledger.rounds[-1].round
    verdict = load_round_verdict(ledger_path, round_number)
    _, coverage = verdict.accepted
    coverage.finding.rule_id = "style.naming"
    save_round_verdict(ledger_path, round_number, verdict)
    save_ledger(ledger_path, bind_confirm_verdict(ledger, round_number, verdict))
    queue_before = dev_queue_file().read_bytes()
    audit = read_events()

    shown = CliRunner().invoke(
        main, ["dev-queue", "show", ACME_TICKET, "-c", ACME, "--json"]
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["scope_decision"].get("park_token") is None
    refused = _approve(token)
    assert refused.exit_code != 0, refused.output
    assert json.loads(refused.stdout) == {
        "scope_grant": "not_applied",
        "reason": "park_changed",
    }
    assert dev_queue_file().read_bytes() == queue_before
    assert events == []
    assert read_events() == audit


def test_show_refuses_same_identity_verdict_replaced_before_ledger_commit(
    parked_repair: tuple[Path, dict[str, object], list[object]],
) -> None:
    """A competing writer has replaced the round file but not its ledger."""
    ledger_path, _, events = parked_repair
    token = _show_token()
    store = load_dev_queue()
    ledger_before = ledger_path.read_bytes()
    verdict = load_round_verdict(ledger_path, 2)
    verdict.must_fix[0] = verdict.must_fix[0].model_copy(
        update={"suggested_fix": "Cover the second widget path instead."}
    )
    save_round_verdict(ledger_path, 2, verdict)
    result = CliRunner().invoke(
        main, ["dev-queue", "show", ACME_TICKET, "-c", ACME, "--json"]
    )
    assert result.exit_code == 0, result.output
    assert ledger_path.read_bytes() == ledger_before
    assert json.loads(result.stdout)["scope_decision"].get("park_token") is None
    refused = _approve(token)
    assert refused.exit_code != 0
    assert json.loads(refused.stdout)["reason"] == "park_changed"
    assert load_dev_queue() == store
    assert events == []


def _record_non_engine_confirm(ledger_path: Path, verdict: ReviewVerdict) -> None:
    save_ledger(
        ledger_path, engine_pass_ledger(ticket_id=ACME_TICKET, sha=_HEAD, engine=False)
    )
    result = CliRunner().invoke(
        main,
        ["review", "converge", "record-round", "-", "--ledger", str(ledger_path)],
        input=json.dumps(engine_confirm_payload(verdict)),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["decision"] == "continue"


def test_required_should_fix_is_guardable(
    isolated_park: tuple[Path, dict[str, object]],
) -> None:
    ledger_path, _ = isolated_park
    verdict = engine_confirm_verdict(should_fix=["Required widget check"], sha=_HEAD)
    (accepted,) = verdict.accepted
    finding = accepted.finding
    decided = apply_adjudication(
        verdict,
        [
            Adjudication(
                severity=finding.severity,
                file=finding.file,
                line_start=finding.line_start,
                line_end=finding.line_end,
                outcome="fix",
            )
        ],
    )
    assert decided.accepted[0].adjudicated_fix
    assert not decided.must_fix
    assert not decided.blocking
    _record_non_engine_confirm(ledger_path, decided)

    result = _approve(_show_token())

    assert result.exit_code == 0, result.output
    assert len(load_dev_queue().tasks[0].scope_grants) == 1
    assert len(read_events(event_types=[OrchestratorEventType.TICKET_APPROVED])) == 1


def test_bound_verdict_must_contain_the_recorded_fix(
    isolated_park: tuple[Path, dict[str, object]],
) -> None:
    ledger_path, _ = isolated_park
    verdict = engine_confirm_verdict(must_fix=["Recorded bug"], sha=_HEAD)
    _record_non_engine_confirm(ledger_path, verdict)
    token = _show_token()
    ledger = load_ledger(ledger_path)
    assert ledger is not None
    other = engine_confirm_verdict(must_fix=["Different bug"], sha=_HEAD)
    round_number = ledger.rounds[-1].round
    save_round_verdict(ledger_path, round_number, other)
    save_ledger(ledger_path, bind_confirm_verdict(ledger, round_number, other))
    before = _park_files(ledger_path)

    assert _park_token() is None
    _assert_park_refusal(token)
    assert _park_files(ledger_path) == before


@pytest.mark.parametrize("raw", [b"[]", b"false", b"0", b"''", b"null"])
def test_guarded_approval_refuses_falsey_config_roots(
    isolated_park: tuple[Path, dict[str, object]], raw: bytes
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    config = orchestrator_config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(raw)
    before = _park_files(ledger_path)

    _assert_park_refusal(token)

    assert _park_files(ledger_path) == before
    assert config.read_bytes() == raw


@pytest.mark.parametrize("when", ["before", "during-lookup", "remapped-during-lookup"])
def test_guarded_approval_reports_client_mapping_drift(
    isolated_park: tuple[Path, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    ledger_path, remote = isolated_park
    prefix = ACME_TICKET.split("-")[0]
    config = orchestrator_config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"linear_prefix_map: {{{prefix}: {ACME}}}")
    show = CliRunner().invoke(main, ["dev-queue", "show", ACME_TICKET, "--json"])
    assert show.exit_code == 0, show.output
    token = json.loads(show.stdout)["scope_decision"]["park_token"]
    replacement = (
        f"linear_prefix_map: {{{prefix}: another-client}}".encode()
        if when == "remapped-during-lookup"
        else b"{}"
    )
    if when == "before":
        config.write_bytes(replacement)
    else:

        def change_config(*_args: object, **_kwargs: object) -> dict[str, object]:
            config.write_bytes(replacement)
            return remote

        monkeypatch.setattr(
            "cw.dev_queue.scope_drift_guard.fetch_pr_view", change_config
        )
    before = _park_files(ledger_path)

    result = CliRunner().invoke(
        main,
        [
            "dev-queue",
            "approve",
            ACME_TICKET,
            "--scope-drift",
            _PATH,
            "--expected-head",
            _HEAD,
            "--expected-park",
            token,
        ],
    )

    assert result.exit_code != 0, result.output
    assert result.stdout, result.output
    assert json.loads(result.stdout) == {
        "scope_grant": "not_applied",
        "reason": "park_changed",
    }
    assert _park_files(ledger_path) == before
    assert config.read_bytes() == replacement


@pytest.mark.parametrize("raw", [None, b"", b"# Defaults\n---\n", b"{}"])
def test_guarded_approval_accepts_empty_or_missing_config(
    isolated_park: tuple[Path, dict[str, object]], raw: bytes | None
) -> None:
    config = orchestrator_config_file()
    token = _show_token()
    if raw is None:
        config.unlink()
    else:
        config.write_bytes(raw)
    result = _approve(token)
    assert result.exit_code == 0, result.output
    assert (config.read_bytes() if config.exists() else None) == raw


@pytest.mark.parametrize("files", ["tests/*.py", "   "])
def test_invalid_guarded_paths_keep_preconditions(
    isolated_park: tuple[Path, dict[str, object]], files: str
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    before = _park_files(ledger_path)

    result = _approve(token, files=files)

    assert result.exit_code != 0, result.output
    assert "original command" in result.stderr or (
        "--expected-head" in result.stderr and "--expected-park" in result.stderr
    )
    assert _park_files(ledger_path) == before


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("queue", [False, True], ids=["ledger", "queue"])
def test_guarded_lock_contention_is_a_structured_refusal(
    isolated_park: tuple[Path, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    queue: bool,
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    before = _park_files(ledger_path)
    lock = (
        dev_queue_lock_path()
        if queue
        else ledger_path.with_name(".review-convergence.lock")
    )
    monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0")
    with lock.open("a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _assert_park_refusal(token)
    assert _park_files(ledger_path) == before


def test_guard_does_not_recreate_missing_worktree(
    isolated_park: tuple[Path, dict[str, object]],
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    tree = ledger_path.parent.parent
    tree.rename(tree.with_name("moved"))
    before = _park_files(ledger_path)

    _assert_park_refusal(token)

    assert _park_files(ledger_path) == before
    assert not tree.exists()


def test_guard_refuses_replaced_ledger_lock(
    isolated_park: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw._flock import acquire_flock

    ledger_path, _ = isolated_park
    token = _show_token()
    before = _park_files(ledger_path)

    def replace_lock(
        fd: IO[str], path: Path, *, lock_name: str, bounded: bool, timeout_s: float
    ) -> None:
        acquire_flock(
            fd, path, lock_name=lock_name, bounded=bounded, timeout_s=timeout_s
        )
        path.rename(path.with_suffix(".old"))
        path.touch()

    monkeypatch.setattr("cw.review_convergence._store.acquire_flock", replace_lock)
    _assert_park_refusal(token)
    assert _park_files(ledger_path) == before


@pytest.mark.parametrize(
    "kind", ["lock", "ledger", "verdict-directory", "verdict", "cw"]
)
def test_guard_refuses_symlinked_ledger_evidence(
    isolated_park: tuple[Path, dict[str, object]], kind: str
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    paths = {
        "lock": ledger_path.with_name(".review-convergence.lock"),
        "ledger": ledger_path,
        "verdict-directory": ledger_path.with_suffix(""),
        "verdict": ledger_path.with_suffix("") / "round-2-verdict.json",
        "cw": ledger_path.parent,
    }
    path = paths[kind]
    target = path.rename(path.with_name(f"{path.name}-moved"))
    path.symlink_to(target)
    before = _park_files(ledger_path)

    assert _park_token() is None
    _assert_park_refusal(token)

    assert _park_files(ledger_path) == before


@pytest.mark.parametrize("artifact", ["ledger", "verdict", "lock"])
def test_guard_refuses_nonregular_evidence(
    isolated_park: tuple[Path, dict[str, object]], artifact: str
) -> None:
    ledger_path, _ = isolated_park
    token = _show_token()
    replaced = {
        "ledger": ledger_path,
        "verdict": ledger_path.with_suffix("") / "round-2-verdict.json",
        "lock": ledger_path.with_name(".review-convergence.lock"),
    }[artifact]
    before = _park_files(ledger_path)
    before.pop(replaced, None)
    replaced.unlink()
    os.mkfifo(replaced)

    assert _park_token() is None
    _assert_park_refusal(token)
    assert stat.S_ISFIFO(replaced.lstat().st_mode)
    replaced.unlink()
    after = _park_files(ledger_path)
    after.pop(replaced, None)
    assert after == before


def test_guard_reads_head_after_acquiring_ledger_lock(
    isolated_park: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw._flock import acquire_flock

    ledger_path, remote = isolated_park
    token = _show_token()
    before = _park_files(ledger_path)

    def push_at_lock(
        fd: IO[str], path: Path, *, lock_name: str, bounded: bool, timeout_s: float
    ) -> None:
        acquire_flock(
            fd, path, lock_name=lock_name, bounded=bounded, timeout_s=timeout_s
        )
        remote["headRefOid"] = "b" * 40

    monkeypatch.setattr("cw.review_convergence._store.acquire_flock", push_at_lock)
    result = _approve(token)

    assert result.exit_code != 0, result.output
    assert json.loads(result.stdout) == {
        "scope_grant": "not_applied",
        "reason": "stale_head",
    }
    assert _park_files(ledger_path) == before


def _fail_approval_event(
    kind: OrchestratorEventType,
    payload: dict[str, object],
    *,
    correlation_id: str | None = None,
) -> None:
    if kind == OrchestratorEventType.TICKET_APPROVED:
        msg = "test approval audit failure"
        raise OSError(msg)
    record_event(kind, payload, correlation_id=correlation_id)


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("fail_audit", [False, True], ids=["commit", "compensate"])
def test_settle_cannot_change_evidence_at_guarded_queue_save(
    isolated_park: tuple[Path, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_audit: bool,
) -> None:
    ledger_path, _ = isolated_park
    verdict = engine_confirm_verdict(must_fix=["Same blocking bug"], sha=_HEAD)
    _record_non_engine_confirm(ledger_path, verdict)
    (finding,) = verdict.must_fix
    token = _show_token()
    expected = ScopeGrantExpectation(_HEAD, token, lock_scope_grant_park)
    at_save, settle_waiting_or_done = Event(), Event()
    original_flock = fcntl.flock
    before = _park_files(ledger_path)
    queue_before = dev_queue_file().read_bytes()
    target = finding.model_dump(include={"severity", "file", "summary"})

    def observe_contention(fd: int, operation: int) -> None:
        try:
            original_flock(fd, operation)
        except BlockingIOError:
            settle_waiting_or_done.set()
            raise

    def pause_queue_save(store: DevQueueStore) -> None:
        at_save.set()
        assert settle_waiting_or_done.wait(timeout=10)
        current = _park_files(ledger_path)
        evidence = set(before) - {inbox_path(), dev_queue_file()}
        assert {p: current[p] for p in evidence} == {p: before[p] for p in evidence}
        assert not read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_SETTLED]
        )
        save_dev_queue(store)

    def settle() -> Result:
        try:
            return CliRunner().invoke(
                main,
                [
                    "review",
                    "converge",
                    "operator-settle",
                    "-",
                    "--ticket-id",
                    ACME_TICKET,
                    "-c",
                    ACME,
                    "--reason",
                    "Existing validation.",
                ],
                input=json.dumps({"entries": [target]}),
            )
        finally:
            settle_waiting_or_done.set()

    monkeypatch.setattr(fcntl, "flock", observe_contention)
    monkeypatch.setattr("cw.dev_queue.scope_drift.save_dev_queue", pause_queue_save)
    monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "5")
    if fail_audit:
        monkeypatch.setattr(
            "cw.dev_queue.scope_drift.record_event", _fail_approval_event
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        grant = pool.submit(
            approve_scope_drift_ticket,
            ACME_TICKET,
            ACME,
            [_PATH],
            repo="example/widgets",
            expected=expected,
        )
        assert at_save.wait(timeout=10)
        settled = pool.submit(settle)
        if fail_audit:
            with pytest.raises(OSError, match="test approval audit failure"):
                grant.result(timeout=10)
        else:
            grant.result(timeout=10)
        result = settled.result(timeout=10)

    assert result.exit_code == 0, result.output
    _assert_grant_settle_order(ledger_path, queue_before, fail_audit=fail_audit)


def _assert_grant_settle_order(
    ledger_path: Path, queue_before: bytes, *, fail_audit: bool
) -> None:
    ledger = load_ledger(ledger_path)
    assert ledger is not None
    assert len(ledger.settled) == 1
    if fail_audit:
        assert dev_queue_file().read_bytes() == queue_before
    else:
        assert len(load_dev_queue().tasks[0].scope_grants) == 1
    assert [event.type for event in read_events()] == [
        OrchestratorEventType.TASK_TRANSITION,
        (
            OrchestratorEventType.TICKET_APPROVAL_FAILED
            if fail_audit
            else OrchestratorEventType.TICKET_APPROVED
        ),
        OrchestratorEventType.REVIEW_FINDING_SETTLED,
    ]
