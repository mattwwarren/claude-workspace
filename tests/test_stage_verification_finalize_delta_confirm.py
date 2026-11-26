"""The FINALIZE Step 4.0b delta-confirm round (#3542).

``confirm_resolution`` drives ``cw review converge confirm-job delta`` through
the injected ``run_cli`` (a :class:`~tests._tool_fakes.FakeCwCli` here) and
re-runs ``verify-tip`` only after exit 0 ``clean``, exit 1 or ``None``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.stage_verification import finalize_delta_confirm as delta
from tests._tool_fakes import FakeCwCli, GhReply

if TYPE_CHECKING:
    from pathlib import Path

_START = "review converge confirm-job delta"
_TIP = "review converge verify-tip"
_ELIGIBLE = json.dumps(
    {
        "decision": "rereview_required",
        "reason": "commits_after_clean_round",
        "delta_confirm_eligible": True,
        "resolution_files": ["src/a.py"],
    }
)
_TIP_OK = GhReply(0, json.dumps({"decision": "accepted", "reason": None}))
_TIP_STALE = GhReply(1, _ELIGIBLE)


def _round(decision: str) -> GhReply:
    return GhReply(0, json.dumps({"decision": decision, "resolution_files": []}))


def _run(
    tmp_path: Path, start: list[GhReply | None], tip: GhReply | None = _TIP_OK
) -> tuple[str | None, FakeCwCli]:
    fake = FakeCwCli({_START: list(start), _TIP: tip})
    outcome = delta.confirm_resolution(
        tmp_path, ticket_id="T-1", client_name="acme", run_cli=fake
    )
    return outcome, fake


def _tip_calls(fake: FakeCwCli) -> int:
    return sum(1 for call in fake.calls if " ".join(call).startswith(_TIP))


@pytest.fixture(autouse=True)
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    pauses: list[float] = []
    monkeypatch.setattr(delta.time, "sleep", pauses.append)
    return pauses


@pytest.mark.parametrize(
    ("reply", "tip", "expected", "tip_calls"),
    [
        (_round("clean"), _TIP_OK, None, 1),
        (_round("clean"), _TIP_STALE, "still_stale", 1),
        (_round("continue"), _TIP_OK, "findings", 0),
        (_round("stop_not_converging"), _TIP_OK, "stopped", 0),
        (_round("stop_confirm_must_fix"), _TIP_OK, "stopped", 0),
        (_round("confirm_required"), _TIP_OK, "round_failed", 0),
        (GhReply(0, "not json"), _TIP_OK, "round_failed", 0),
        (GhReply(0, "[1, 2]"), _TIP_OK, "round_failed", 0),
        (GhReply(0, json.dumps({"decision": 3})), _TIP_OK, "round_failed", 0),
        (GhReply(1, "", "interrupted"), _TIP_OK, None, 1),
        (GhReply(1, "", "interrupted"), _TIP_STALE, "refused", 1),
        (None, _TIP_OK, None, 1),
        (None, None, "refused", 1),
        (GhReply(3), _TIP_OK, "backend_unavailable", 0),
        (GhReply(4), _TIP_OK, "round_failed", 0),
        (GhReply(2), _TIP_OK, "round_failed", 0),
        (GhReply(9), _TIP_OK, "round_failed", 0),
        (GhReply(-9), _TIP_OK, "round_failed", 0),
    ],
)
def test_each_result_maps_to_its_outcome(
    tmp_path: Path,
    reply: GhReply | None,
    tip: GhReply | None,
    expected: str | None,
    tip_calls: int,
) -> None:
    outcome, fake = _run(tmp_path, [reply], tip)
    assert outcome == expected
    assert _tip_calls(fake) == tip_calls
    assert not any("finalize" in call for call in fake.calls)


def test_pending_with_a_job_id_polls_that_job(
    tmp_path: Path, pauses: list[float]
) -> None:
    pending = GhReply(delta.RUNNING_EXIT, "", "CONFIRM_JOB_ID=job-7\n")
    outcome, fake = _run(tmp_path, [pending, pending, _round("clean")])
    assert outcome is None
    starts = [c for c in fake.calls if " ".join(c).startswith(_START)]
    assert starts[0] == [
        "review",
        "converge",
        "confirm-job",
        "delta",
        "--ticket-id",
        "T-1",
        "-c",
        "acme",
    ]
    assert starts[1][-2:] == ["--job-id", "job-7"]
    assert starts[2][-2:] == ["--job-id", "job-7"]
    assert pauses == []


def test_pending_without_a_job_id_restarts_after_a_pause(
    tmp_path: Path, pauses: list[float]
) -> None:
    pending = GhReply(delta.RUNNING_EXIT, "", "warming up\nCONFIRM_JOB_ID=\n")
    outcome, fake = _run(tmp_path, [pending, _round("continue")])
    assert outcome == "findings"
    starts = [c for c in fake.calls if " ".join(c).startswith(_START)]
    assert len(starts) == 2
    assert "--job-id" not in starts[1]
    assert pauses == [delta.PENDING_PAUSE_SECONDS]


def test_pending_with_no_job_id_line_restarts(
    tmp_path: Path, pauses: list[float]
) -> None:
    pending = GhReply(delta.RUNNING_EXIT, "", "noise only\n")
    outcome, _fake = _run(tmp_path, [pending, _round("stop_max_cycles")])
    assert outcome == "stopped"
    assert pauses == [delta.PENDING_PAUSE_SECONDS]


@pytest.mark.parametrize(
    ("stdout", "eligible"),
    [
        (_ELIGIBLE, True),
        (_ELIGIBLE.replace("true", "false"), False),
        (json.dumps({"reason": "commits_after_clean_round"}), False),
        (
            json.dumps({"reason": "no_clean_round", "delta_confirm_eligible": True}),
            False,
        ),
        ("not json", False),
        ("[]", False),
    ],
)
def test_delta_eligible(stdout: str, eligible: bool) -> None:
    assert delta.delta_eligible(stdout) is eligible


# -- lockstep with the CLI -----------------------------------------------------


def test_running_exit_matches_the_job_runner() -> None:
    from cw.confirmation_jobs import RUNNING_EXIT

    assert delta.RUNNING_EXIT == RUNNING_EXIT


def test_job_id_prefix_matches_what_the_cli_writes() -> None:
    import inspect

    from cw.cli.review import confirm_job

    assert f'f"{delta.JOB_ID_PREFIX}{{job_id}}"' in inspect.getsource(confirm_job)


def test_eligible_key_matches_review_convergence() -> None:
    from cw.review_convergence import DELTA_CONFIRM_ELIGIBLE_KEY

    assert delta.DELTA_CONFIRM_ELIGIBLE_KEY == DELTA_CONFIRM_ELIGIBLE_KEY


def test_confirm_job_and_its_options_exist_on_converge() -> None:
    from cw.cli.review.confirm_job import confirm_job
    from cw.cli.review.converge import converge

    assert converge.commands["confirm-job"] is confirm_job
    opts = {o for p in confirm_job.params for o in getattr(p, "opts", [])}
    assert {"--ticket-id", "-c", "--job-id"} <= opts
