"""Characterization tests for review-monitor state persistence (#2499).

Covers ``state_path_for_repo``, ``_merge_states``, ``_load_json_state``,
``load_state`` (including legacy migration), ``save_state`` and
``cmd_list_repos`` (moving to ``review_monitor_lib/state.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests import _review_monitor_helpers as helpers

_OLD = "2026-01-01T00:00:00+00:00"
_NEW = "2026-02-01T00:00:00+00:00"


def _state(*prs: Any, completed: dict[str, object] | None = None) -> Any:
    return helpers.get("MonitorState")(
        monitored={f"{pr.repo}#{pr.pr_number}": pr for pr in prs},
        completed=dict(completed or {}),
    )


def test_state_path_for_repo_flattens_owner_separator(
    review_monitor_state_dir: Path,
) -> None:
    assert helpers.get("state_path_for_repo")("acme/widgets") == (
        review_monitor_state_dir / "acme--widgets.json"
    )


def test_merge_states_keeps_newer_entry_and_adds_missing() -> None:
    central = _state(
        helpers.make_pr(pr_number=1, last_checked_at=_NEW, last_seen_sha="c1"),
        helpers.make_pr(pr_number=2, last_checked_at=_OLD, last_seen_sha="c2"),
        completed={"acme/widgets#5": {"reason": "central"}},
    )
    legacy = _state(
        helpers.make_pr(pr_number=1, last_checked_at=_OLD, last_seen_sha="l1"),
        helpers.make_pr(pr_number=2, last_checked_at=_NEW, last_seen_sha="l2"),
        helpers.make_pr(pr_number=3, last_checked_at=_OLD, last_seen_sha="l3"),
        completed={
            "acme/widgets#5": {"reason": "legacy"},
            "acme/widgets#6": {"reason": "legacy"},
        },
    )

    merged = helpers.get("_merge_states")(central, legacy)

    shas = {k: pr.last_seen_sha for k, pr in merged.monitored.items()}
    assert shas == {
        "acme/widgets#1": "c1",
        "acme/widgets#2": "l2",
        "acme/widgets#3": "l3",
    }
    assert merged.completed["acme/widgets#5"] == {"reason": "central"}
    assert merged.completed["acme/widgets#6"] == {"reason": "legacy"}


def test_load_state_missing_files_is_empty(review_monitor_state_dir: Path) -> None:
    state = helpers.get("load_state")(helpers.REPO)

    assert state.monitored == {}
    assert state.completed == {}


def test_load_state_corrupt_shape_starts_fresh_with_warning(
    review_monitor_state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    review_monitor_state_dir.mkdir(parents=True)
    path = helpers.get("state_path_for_repo")(helpers.REPO)
    path.write_text(json.dumps({"monitored": {helpers.KEY: {}}}))

    with caplog.at_level("WARNING"):
        state = helpers.get("load_state")(helpers.REPO)

    assert state.monitored == {}
    assert any("Corrupt monitor state file" in r.message for r in caplog.records)


def test_load_state_migrates_legacy_file_when_no_central(
    review_monitor_state_dir: Path,
) -> None:
    legacy_file = helpers.get("LEGACY_STATE_FILE")
    legacy_file.write_text(json.dumps(_state(helpers.make_pr()).to_dict()))

    state = helpers.get("load_state")(helpers.REPO)

    assert helpers.KEY in state.monitored
    assert not legacy_file.exists()
    assert helpers.stored_pr().last_seen_sha == "deadbeef"


def test_load_state_merges_legacy_into_existing_central(
    review_monitor_state_dir: Path,
) -> None:
    helpers.seed_prs(helpers.make_pr(pr_number=1))
    legacy_file = helpers.get("LEGACY_STATE_FILE")
    legacy_file.write_text(json.dumps(_state(helpers.make_pr(pr_number=2)).to_dict()))

    state = helpers.get("load_state")(helpers.REPO)

    assert set(state.monitored) == {"acme/widgets#1", "acme/widgets#2"}
    assert not legacy_file.exists()


def test_load_state_warns_when_legacy_file_cannot_be_deleted(
    review_monitor_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    legacy_file = helpers.get("LEGACY_STATE_FILE")
    legacy_file.write_text(json.dumps(_state(helpers.make_pr()).to_dict()))
    real_unlink = Path.unlink
    denied = "unlink denied"

    def _unlink(self: Path, missing_ok: bool = False) -> None:
        if self == legacy_file:
            raise PermissionError(denied)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _unlink)
    with caplog.at_level("WARNING"):
        state = helpers.get("load_state")(helpers.REPO)

    assert helpers.KEY in state.monitored
    assert legacy_file.exists()
    assert any(
        "Could not delete legacy state file" in r.message and denied in r.message
        for r in caplog.records
    )


def test_save_state_writes_indented_json_atomically(
    review_monitor_state_dir: Path,
) -> None:
    helpers.seed_prs(helpers.make_pr())

    path = helpers.get("state_path_for_repo")(helpers.REPO)
    text = path.read_text()
    assert text.endswith("}\n")
    assert text.startswith('{\n  "monitored"')
    assert list(review_monitor_state_dir.iterdir()) == [path]


def test_list_repos_missing_dir_is_empty(review_monitor_state_dir: Path) -> None:
    assert helpers.get("cmd_list_repos")() == []


def test_list_repos_unflattens_first_separator_only(
    review_monitor_state_dir: Path,
) -> None:
    review_monitor_state_dir.mkdir(parents=True)
    for name in ("zeta--app.json", "acme--widgets--v2.json", "notes.txt"):
        (review_monitor_state_dir / name).write_text("{}")

    assert helpers.get("cmd_list_repos")() == ["acme/widgets--v2", "zeta/app"]
