"""Tests for cw.outbox_prose_plan: producer-side redaction and splitting (#3707)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from cw.models import ClientConfig
from cw.outbox_prose_plan import plan_prose_parts
from cw.tracker_post import PartPlan
from tests._doctor_helpers import make_client

_PART_LABEL = re.compile(r"\(part (\d+) of (\d+)\)")


def _client(tmp_path: Path) -> ClientConfig:
    return make_client(name="acme", workspace_path=tmp_path / "workspace")


def test_short_text_one_part_unlabelled(tmp_path: Path) -> None:
    plan = plan_prose_parts("short plan", _client(tmp_path), tmp_path)

    assert plan.bodies == ("short plan",)
    assert plan.redacted is False
    assert _PART_LABEL.search(plan.bodies[0]) is None


def test_long_text_splits_with_part_i_of_n_labels(tmp_path: Path) -> None:
    text = "\n".join(f"line {n} " + "w" * 80 for n in range(2000))

    plan = plan_prose_parts(text, _client(tmp_path), tmp_path)

    total = len(plan.bodies)
    assert total > 1
    for index, body in enumerate(plan.bodies, start=1):
        assert f"(part {index} of {total})" in body
        assert "cw-agent" not in body
        assert "<!--" not in body


def test_redaction_flag_follows_partplan(tmp_path: Path) -> None:
    home = str(Path.home())
    text = f"edited {home}/secret/notes.txt"

    plan = plan_prose_parts(text, _client(tmp_path), tmp_path)

    assert plan.redacted is True
    assert all(home not in body for body in plan.bodies)


def test_unplannable_text_raises_valueerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> PartPlan:
        msg = "cannot plan"
        raise ValueError(msg)

    monkeypatch.setattr("cw.outbox_prose_plan.plan_parts", refuse)

    with pytest.raises(ValueError, match="cannot plan"):
        plan_prose_parts("text", _client(tmp_path), tmp_path)


def test_anchors_use_worktree_and_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def spy(text: str, **kwargs: Any) -> PartPlan:
        captured.update(kwargs)
        return PartPlan((text,), False)

    monkeypatch.setattr("cw.outbox_prose_plan.plan_parts", spy)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    client = _client(tmp_path)

    plan_prose_parts("text", client, worktree)

    assert str(worktree) in captured["worktrees"]
    assert str(client.workspace_path) in captured["worktrees"]
    assert captured["single"] is False
