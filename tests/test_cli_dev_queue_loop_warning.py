"""The stale-dispatch-loop stderr line on ``cw dev-queue approve`` (#2729).

``approve`` and ``resolve`` call ``warn_if_dispatch_loop_stale()`` before they
write. These cases drive ``approve`` against an empty queue: the warning prints
before the row lookup fails, so no queue seeding is needed. ``resolve`` is
covered in ``test_cli_dev_queue_resolve.py``. Every case runs from an
interactive (``headless: false``) session, because the suite may itself run
inside a headless dispatch worker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.config import dispatch_loop_lock_file
from tests._cli_review_helpers import _enter_operator_machine
from tests._loop_identity_helpers import (
    COMMIT_A,
    COMMIT_B,
    patch_checkout_identity,
    write_loop_record,
)
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    install_acme_client,
    invoke_dev_queue,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

_RESTART = "restart `cw dev-queue serve`"


@pytest.fixture(autouse=True)
def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)


def _approve(*extra: str) -> Result:
    return invoke_dev_queue("approve", ACME_TICKET, "-c", ACME, *extra)


def _stale(monkeypatch: pytest.MonkeyPatch) -> None:
    write_loop_record(commit=COMMIT_A)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)


def test_stale_loop_warns_without_changing_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _approve()
    _stale(monkeypatch)
    result = _approve()
    assert result.stderr.count("warning: dispatch loop") == 1
    assert _RESTART in result.stderr
    assert COMMIT_A[:12] in result.stderr
    assert result.exit_code == baseline.exit_code


def test_matching_loop_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    write_loop_record()
    patch_checkout_identity(monkeypatch)
    assert "dispatch loop" not in _approve().stderr


def test_no_loop_is_silent() -> None:
    assert "dispatch loop" not in _approve().stderr


def test_identity_unknown_prints_a_note_not_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_loop_record(with_identity=False)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    stderr = _approve().stderr
    assert "note: dispatch loop" in stderr
    assert "identity unknown" in stderr
    assert "warning: dispatch loop" not in stderr


def _raise_from_assess(exc: Exception) -> Callable[[], object]:
    def _boom() -> object:
        raise exc

    return _boom


@pytest.mark.parametrize(
    "exc", [PermissionError("denied"), ValueError("corrupt package metadata")]
)
def test_assessment_failure_drops_the_advisory_and_keeps_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    baseline = _approve()
    monkeypatch.setattr(
        "cw.cli.dev_queue._loop_warning.assess_dispatch_loop",
        _raise_from_assess(exc),
    )
    result = _approve()
    assert result.exit_code == baseline.exit_code
    assert "dispatch loop" not in result.stderr
    assert "Traceback" not in result.output
    assert result.stderr == baseline.stderr


def test_unreadable_lock_path_raises_nothing() -> None:
    baseline = _approve()
    dispatch_loop_lock_file().mkdir(parents=True)
    result = _approve()
    assert result.exit_code == baseline.exit_code
    assert type(result.exception) is type(baseline.exception)
    assert result.stderr == baseline.stderr
    assert "dispatch loop" not in result.stderr


@pytest.mark.parametrize(
    "extra",
    [("--scope-drift", "src/x.py"), ("--override-must-fix", "--reason", "x")],
)
def test_other_approve_write_paths_also_warn(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...]
) -> None:
    _stale(monkeypatch)
    assert "warning: dispatch loop" in _approve(*extra).stderr


@pytest.mark.parametrize(
    "args",
    [
        ("revoke-plan-approval", ACME_TICKET, "-c", ACME),
        ("requeue", ACME_TICKET, "-c", ACME),
    ],
)
def test_skipped_commands_stay_silent(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    _stale(monkeypatch)
    assert "dispatch loop" not in invoke_dev_queue(*args).stderr
