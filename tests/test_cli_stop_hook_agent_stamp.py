"""Tests for ``cw.cli.stop_hook.agent_stamp``'s ``entries`` capture (#2688).

A named ``Agent`` spawn becomes an addressable in-process teammate that goes
idle instead of returning and keeps the Stop hook's ``background_tasks``
non-empty, stranding the worker's session. Nothing records what such an
entry looks like, so the Stop hook now persists a *structural* summary of
the list under ``agent_spawn_stamp.entries``: key names, value type names,
and short scalar values for an allow-list of structural keys only. Free text
is never written, not even truncated. Follow-up #2710 widens the allow-list
once a teammate entry is captured on a guard-off lane.

Fixture shapes: the only observed entry shapes are the documented
``{"type": "subagent", "status": "running", ...}`` and the existing test
literals ``{"id": "task-1", "description": "..."}``. No teammate shape is
invented here. Inputs labelled *adversarial* probe totality, not the
harness's real payloads.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.cli.stop_hook.agent_stamp import (
    _ALLOWED_VALUE_KEYS,
    _MAX_ENTRIES,
    _MAX_KEYS_PER_ENTRY,
    _MAX_VALUE_CHARS,
    _snapshot_agent_spawn_stamp,
    _summarize_background_tasks,
)
from cw.config import load_state
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
)
from tests.conftest import (
    _invoke_hook_command,
    _read_stamp,
    _seed_session,
    _stop_payload,
    _write_hook_context_file,
)

if TYPE_CHECKING:
    from pathlib import Path

_FREE_TEXT = "FREE-TEXT-SENTINEL"
_ENTRIES_KEY = "entries"


def _only(summaries: list[dict[str, object]]) -> dict[str, object]:
    assert len(summaries) == 1
    return summaries[0]


def _dict_field(summary: dict[str, object], field: str) -> dict[str, object]:
    value = summary[field]
    assert isinstance(value, dict)
    return value


def _list_field(summary: dict[str, object], field: str) -> list[object]:
    value = summary[field]
    assert isinstance(value, list)
    return value


class TestSummarizeBackgroundTasks:
    @pytest.mark.parametrize("raw", [None, "x", {}, 7])
    def test_non_list_yields_no_summaries(self, raw: object) -> None:
        assert _summarize_background_tasks(raw) == []

    def test_entries_are_capped(self) -> None:
        raw = [{"id": f"task-{i}"} for i in range(20)]

        assert len(_summarize_background_tasks(raw)) == _MAX_ENTRIES == 8

    def test_dict_entry_records_structure_and_allow_listed_scalars_only(
        self,
    ) -> None:
        raw = [
            {
                "id": "task-1",
                "type": "subagent",
                "status": "running",
                "description": f"{_FREE_TEXT}-DESC",
                "prompt": f"{_FREE_TEXT}-PROMPT",
            }
        ]

        summary = _only(_summarize_background_tasks(raw))

        assert summary["entry_type"] == "dict"
        assert summary["keys"] == sorted(
            ["id", "type", "status", "description", "prompt"]
        )
        assert _dict_field(summary, "types") == {
            "description": "str",
            "id": "str",
            "prompt": "str",
            "status": "str",
            "type": "str",
        }
        assert summary["values"] == {
            "id": "task-1",
            "type": "subagent",
            "status": "running",
        }
        assert "extra_keys" not in summary
        assert _FREE_TEXT not in json.dumps(summary)

    def test_long_allow_listed_value_is_dropped_not_truncated(self) -> None:
        summary = _only(_summarize_background_tasks([{"status": "x" * 500}]))

        assert "status" not in _dict_field(summary, "values")
        assert _dict_field(summary, "types")["status"] == "str"
        assert "x" * 20 not in json.dumps(summary)

    def test_value_at_the_char_cap_is_kept(self) -> None:
        value = "y" * _MAX_VALUE_CHARS

        summary = _only(_summarize_background_tasks([{"id": value}]))

        assert _dict_field(summary, "values") == {"id": value}

    @pytest.mark.parametrize(
        ("value", "type_name"),
        [({"nested": _FREE_TEXT}, "dict"), ([_FREE_TEXT], "list")],
    )
    def test_nested_allow_listed_value_is_dropped(
        self, value: object, type_name: str
    ) -> None:
        summary = _only(_summarize_background_tasks([{"type": value}]))

        assert _dict_field(summary, "values") == {}
        assert _dict_field(summary, "types") == {"type": type_name}
        assert _FREE_TEXT not in json.dumps(summary)
        assert "nested" not in json.dumps(summary)

    def test_non_allow_listed_short_scalar_is_dropped_from_values(self) -> None:
        summary = _only(
            _summarize_background_tasks(
                [{"name": "impl-2688", "description": "short", "id": "t1"}]
            )
        )

        assert summary["keys"] == ["description", "id", "name"]
        assert _dict_field(summary, "types") == {
            "description": "str",
            "id": "str",
            "name": "str",
        }
        assert _dict_field(summary, "values") == {"id": "t1"}
        assert "impl-2688" not in json.dumps(summary)

    @pytest.mark.parametrize(
        ("value", "kept"),
        [
            (True, True),
            (3, True),
            (1.5, True),
            (float("nan"), False),
            (float("inf"), False),
            (None, False),
        ],
    )
    def test_allow_listed_scalar_types(self, value: object, kept: bool) -> None:
        summary = _only(_summarize_background_tasks([{"state": value}]))

        assert ("state" in _dict_field(summary, "values")) is kept

    def test_free_text_keys_are_never_allow_listed(self) -> None:
        """Regression pin on the allow-list itself (#2688 A6)."""
        free_text = {
            "description",
            "prompt",
            "message",
            "name",
            "summary",
            "output",
            "result",
        }

        assert free_text.isdisjoint(_ALLOWED_VALUE_KEYS)

    def test_key_overflow_keeps_allow_listed_keys_first(self) -> None:
        entry: dict[str, object] = {f"a{i:02d}": i for i in range(14)}
        entry["type"] = "subagent"
        entry["status"] = "running"

        summary = _only(_summarize_background_tasks([entry]))

        keys = _list_field(summary, "keys")
        assert len(keys) == _MAX_KEYS_PER_ENTRY == 12
        assert {"type", "status"} <= set(keys)
        assert summary["extra_keys"] == len(entry) - _MAX_KEYS_PER_ENTRY
        assert _dict_field(summary, "values") == {
            "type": "subagent",
            "status": "running",
        }

    def test_overlong_key_name_is_not_recorded(self) -> None:
        """A key name past the name cap is itself free text; it is dropped."""
        long_key = "k" * 49

        summary = _only(_summarize_background_tasks([{long_key: 1, "id": "t1"}]))

        assert summary["keys"] == ["id"]
        assert long_key not in json.dumps(summary)

    @pytest.mark.parametrize("item", ["s", 7, None, [1]])
    def test_non_dict_entry_records_only_its_type(self, item: object) -> None:
        summary = _only(_summarize_background_tasks([item]))

        assert summary == {"entry_type": type(item).__name__}

    def test_adversarial_values_never_raise_and_stay_json_safe(self) -> None:
        """Adversarial, not a harness shape: totality under odd values."""
        entry: dict[object, object] = {
            "type": set(),
            "status": b"x",
            "id": object(),
            "state": float("nan"),
            "kind": float("inf"),
            7: "non-str key",
            "": "empty key",
        }

        summaries = _summarize_background_tasks([entry, float("nan"), object()])

        encoded = json.dumps(summaries, allow_nan=False)
        assert "non-str key" not in encoded
        summary = summaries[0]
        assert _dict_field(summary, "values") == {}
        assert _dict_field(summary, "types") == {
            "id": "object",
            "kind": "float",
            "state": "float",
            "status": "bytes",
            "type": "set",
        }
        assert summaries[1:] == [{"entry_type": "float"}, {"entry_type": "object"}]


_SAMPLE_ENTRIES: list[dict[str, object]] = [
    {"entry_type": "dict", "keys": ["id"], "types": {"id": "str"}, "values": {}}
]


class TestSnapshotEntries:
    def test_snapshot_with_entries_persists_them(self) -> None:
        context: dict[str, object] = {}

        _snapshot_agent_spawn_stamp(context, 2, entries=_SAMPLE_ENTRIES)

        stamp = context[AGENT_SPAWN_STAMP_KEY]
        assert isinstance(stamp, dict)
        assert stamp[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 2
        assert stamp[AGENT_SPAWN_LAST_STAMPED_AT_KEY] is not None
        assert stamp[_ENTRIES_KEY] == _SAMPLE_ENTRIES

    @pytest.mark.parametrize("entries", [None, [], _SAMPLE_ENTRIES])
    def test_zero_count_never_persists_entries(
        self, entries: list[dict[str, object]] | None
    ) -> None:
        context: dict[str, object] = {}

        _snapshot_agent_spawn_stamp(context, 0, entries=entries)

        stamp = context[AGENT_SPAWN_STAMP_KEY]
        assert isinstance(stamp, dict)
        assert _ENTRIES_KEY not in stamp

    def test_positive_count_without_entries_omits_the_key(self) -> None:
        context: dict[str, object] = {}

        _snapshot_agent_spawn_stamp(context, 3)

        stamp = context[AGENT_SPAWN_STAMP_KEY]
        assert isinstance(stamp, dict)
        assert _ENTRIES_KEY not in stamp

    def test_snapshot_logs_one_info_line_with_the_entries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        entries = _summarize_background_tasks(
            [{"id": "task-1", "description": _FREE_TEXT}]
        )

        with caplog.at_level("INFO", logger="cw.cli.stop_hook"):
            _snapshot_agent_spawn_stamp({}, 2, entries=entries)

        snapshot_records = [
            r for r in caplog.records if "agent_spawn_stamp snapshot" in r.message
        ]
        assert len(snapshot_records) == 1
        message = snapshot_records[0].message
        assert message.startswith(
            "agent_spawn_stamp snapshot: unresolved_count=2 entries="
        )
        assert json.dumps(entries, sort_keys=True, default=str) in message
        assert all(_FREE_TEXT not in r.message for r in caplog.records)

    def test_zero_count_snapshot_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger="cw.cli.stop_hook"):
            _snapshot_agent_spawn_stamp({}, 0)

        assert not [r for r in caplog.records if "snapshot" in r.message]


def test_signal_stop_persists_capped_structural_entries(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """End to end: the deferral Stop writes the summary, the drain drops it.

    ``unresolved_count`` stays the true list length, not the entries cap, and
    the session row is untouched (the deferral path's existing contract).
    """
    session = _seed_session(tmp_path)
    assert session.worktree_path is not None
    worktree = session.worktree_path
    _write_hook_context_file(worktree, workspace_path=session.workspace_path)
    pre_state = load_state()
    pre_snapshot = next(
        s for s in pre_state.sessions if s.id == session.id
    ).model_dump()
    # Mixed shapes inside the cap: a free-text str, an int, then dicts whose
    # description is free text. Ten in all, so the cap (8) also bites.
    background_tasks: list[object] = [f"{_FREE_TEXT}-str", 7]
    background_tasks += [
        {"id": f"task-{i}", "description": f"{_FREE_TEXT}-{i}"} for i in range(8)
    ]

    result = _invoke_hook_command(
        "signal-stop", _stop_payload(worktree, background_tasks=background_tasks)
    )

    assert result.exit_code == 0, result.output
    stamp = _read_stamp(worktree)
    assert stamp[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 10
    entries = stamp[_ENTRIES_KEY]
    assert isinstance(entries, list)
    assert len(entries) == _MAX_ENTRIES
    assert entries[:2] == [{"entry_type": "str"}, {"entry_type": "int"}]
    raw_context = (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    assert _FREE_TEXT not in raw_context
    post_state = load_state()
    post_target = next(s for s in post_state.sessions if s.id == session.id)
    assert post_target.model_dump() == pre_snapshot

    drained = _invoke_hook_command(
        "signal-stop", _stop_payload(worktree, background_tasks=[])
    )

    assert drained.exit_code == 0, drained.output
    cleared = _read_stamp(worktree)
    assert cleared[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 0
    assert _ENTRIES_KEY not in cleared
