"""Hermetic dogfood of ``cw doctor``'s capability rows (RFC 0019 H2b R12, #3744).

The operator-run dogfood checklist (``docs/dispatch-runbook.md``) starts with
``cw doctor`` listing every lane's ``harness-capabilities`` row. This drives
the real CLI over a fixture config with one lane per harness and pins that
shape: one row per (client, lane, backend), each naming its declared stages and
its tickets-binding state, and none of the retired ``tracker-mcp`` or
``all-codex-lane`` rows.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from cw.cli import main
from cw.doctor._shared import CheckResult
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    Stage,
)
from tests._doctor_helpers import make_client, make_lane, stub_daemon_roster

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_ALL = [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
#: Each lane routes only the stages its harness declares.
_LANES = {
    "claude": (CLAUDE_NATIVE_BACKEND, _ALL),
    "codex": (CODEX_BACKEND, _ALL),
    "opencode": (OPENCODE_BACKEND, [Stage.PLAN, Stage.IMPL, Stage.REVIEW]),
    "local": (LOCAL_BACKEND, [Stage.IMPL]),
}


def _doctor_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    lanes = [
        make_lane(
            name,
            backend=backend,
            executors=dict.fromkeys(stages, backend),
            stages=stages,
        )
        for name, (backend, stages) in _LANES.items()
    ]
    client = make_client(*lanes, name="dogfood", workspace_path=tmp_path)
    monkeypatch.setattr("cw.doctor._deps.load_clients", lambda: {"dogfood": client})
    monkeypatch.setattr(
        "cw.doctor.core._check_claude_version",
        lambda: CheckResult("claude-version", ok=True, detail="stubbed"),
    )
    monkeypatch.setattr(
        "cw.doctor.core._check_codex_capability",
        lambda: CheckResult("codex-capability", ok=True, detail="stubbed"),
    )
    stub_daemon_roster(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    checks: list[dict] = json.loads(result.output)["checks"]
    return checks


def test_every_lane_has_one_capability_row_naming_stages_and_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
) -> None:
    checks = _doctor_json(tmp_path, monkeypatch)

    rows = {c["name"]: c for c in checks if c["name"].startswith("harness-capab")}
    assert sorted(rows) == sorted(
        f"harness-capabilities/dogfood/{lane}/{backend}"
        for lane, (backend, _) in _LANES.items()
    )
    for row in rows.values():
        assert row["ok"] is True, row
        assert "declares stages" in row["detail"]
        assert "tickets binding " in row["detail"]


def test_no_retired_check_rows_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
) -> None:
    names = [c["name"] for c in _doctor_json(tmp_path, monkeypatch)]

    assert not [n for n in names if n.startswith(("tracker-mcp/", "all-codex-lane/"))]
