"""Before/after fixture round-trip lock for the pr/operator channel file formats.

GitHub #1304: ``cw_pr_events_server`` and ``cw_operator_events`` migrate their
append/subscribe/broadcast/cursor machinery onto the shared ``EventBus`` core
(``cw.event_bus``, #1303). This test hand-writes the pre-migration on-disk
JSONL/cursor-JSON shape directly, using HARDCODED LITERAL path strings (not the
modules' constants) -- so a regression is caught even if a module constant's
value ever drifts -- then exercises the migrated modules' public functions
against that fixture and asserts the on-disk shape is unchanged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _write_events_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"notification_type": "cw-pr-event", "message": "m0", "title": "t0", "offset": 0},
        {"notification_type": "cw-pr-event", "message": "m1", "title": "t1", "offset": 1},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _write_cursors_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"client-id": 1}))


class TestPrChannelStateFileFormatRoundtrip:
    """Pre/post-migration on-disk shape for the ``channel-*`` (PR) files."""

    def test_events_and_cursors_files_readable_after_migration(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _load_cursors, _read_events_from_offset

        events_path = state_dir() / "channel-events.jsonl"
        cursors_path = state_dir() / "channel-cursors.json"
        _write_events_fixture(events_path)
        _write_cursors_fixture(cursors_path)

        assert events_path.exists()
        assert cursors_path.exists()

        records = _read_events_from_offset(0)
        assert len(records) == 2
        assert records[0] == {
            "notification_type": "cw-pr-event",
            "message": "m0",
            "title": "t0",
            "offset": 0,
        }
        assert records[1]["offset"] == 1

        cursors = _load_cursors()
        assert cursors == {"client-id": 1}

    def test_ack_offset_writes_pre_migration_cursor_shape(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import ack_offset

        cursors_path = state_dir() / "channel-cursors.json"
        _write_cursors_fixture(cursors_path)

        ack_offset("client-id", 5)

        data = json.loads(cursors_path.read_text())
        assert data == {"client-id": 5}
        assert set(data.keys()) == {"client-id"}

    def test_append_event_produces_fixture_shaped_line(
        self, tmp_config_dir: Path
    ) -> None:
        import cw.cw_pr_events_server as _pr_mod
        from cw.config import state_dir
        from cw.cw_pr_events_server import _append_event, _load_offset_from_file

        events_path = state_dir() / "channel-events.jsonl"
        _write_events_fixture(events_path)
        # Seed the in-memory offset counter from the fixture, mirroring the
        # real module-import-time seeding (`_event_offset[0] = _load_offset_from_file()`).
        _pr_mod._event_offset[0] = _load_offset_from_file()

        _append_event(
            {"notification_type": "cw-pr-event", "message": "m2", "title": "t2"}
        )

        lines = events_path.read_text().splitlines()
        assert len(lines) == 3
        appended = json.loads(lines[2])
        assert list(appended.keys()) == ["notification_type", "message", "title", "offset"]
        assert appended["offset"] == 2


class TestOperatorChannelStateFileFormatRoundtrip:
    """Pre/post-migration on-disk shape for the ``operator-channel-*`` files."""

    def test_events_and_cursors_files_readable_after_migration(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import _load_cursors, _read_events_from_offset

        events_path = state_dir() / "operator-channel-events.jsonl"
        cursors_path = state_dir() / "operator-channel-cursors.json"
        _write_events_fixture(events_path)
        _write_cursors_fixture(cursors_path)

        assert events_path.exists()
        assert cursors_path.exists()

        records = _read_events_from_offset(0)
        assert len(records) == 2
        assert records[0] == {
            "notification_type": "cw-pr-event",
            "message": "m0",
            "title": "t0",
            "offset": 0,
        }
        assert records[1]["offset"] == 1

        cursors = _load_cursors()
        assert cursors == {"client-id": 1}

    def test_ack_offset_writes_pre_migration_cursor_shape(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import ack_offset

        cursors_path = state_dir() / "operator-channel-cursors.json"
        _write_cursors_fixture(cursors_path)

        ack_offset("client-id", 5)

        data = json.loads(cursors_path.read_text())
        assert data == {"client-id": 5}
        assert set(data.keys()) == {"client-id"}

    def test_append_event_produces_fixture_shaped_line(
        self, tmp_config_dir: Path
    ) -> None:
        import cw.cw_operator_events as _operator_mod
        from cw.config import state_dir
        from cw.cw_operator_events import _append_event, _load_offset_from_file

        events_path = state_dir() / "operator-channel-events.jsonl"
        _write_events_fixture(events_path)
        # Seed the in-memory offset counter from the fixture, mirroring the
        # real module-import-time seeding (`_event_offset[0] = _load_offset_from_file()`).
        _operator_mod._event_offset[0] = _load_offset_from_file()

        _append_event(
            {"notification_type": "cw-pr-event", "message": "m2", "title": "t2"}
        )

        lines = events_path.read_text().splitlines()
        assert len(lines) == 3
        appended = json.loads(lines[2])
        assert list(appended.keys()) == ["notification_type", "message", "title", "offset"]
        assert appended["offset"] == 2
