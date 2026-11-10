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
from pathlib import Path

import cw.cw_operator_events as _operator_mod
import cw.cw_pr_events_server as _pr_mod

# Module references are captured here, at collection time, rather than via a
# fresh `import cw.cw_pr_events_server as ...` inside a test body. Why: a
# sibling test (TestLazyStarlette in test_cw_pr_events_server.py) pops and
# reimports this module from sys.modules to exercise the lazy-starlette
# contract; its restoration fixes up ``sys.modules`` but not the ``cw``
# package's own submodule attribute, so `import cw.cw_pr_events_server as x`
# executed *after* that test runs would silently resolve to a stale module
# object. Capturing the reference at collection time (before any test body
# runs) sidesteps that ordering hazard entirely.


def _write_events_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "notification_type": "cw-pr-event",
            "message": "m0",
            "title": "t0",
            "offset": 0,
        },
        {
            "notification_type": "cw-pr-event",
            "message": "m1",
            "title": "t1",
            "offset": 1,
        },
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

        # Frontier is derived from the on-disk log (the in-memory counter is 0).
        _write_events_fixture(state_dir() / "channel-events.jsonl")
        cursors_path = state_dir() / "channel-cursors.json"
        _write_cursors_fixture(cursors_path)

        ack_offset("client-id", 2)

        data = json.loads(cursors_path.read_text())
        assert data == {"client-id": 2}
        assert set(data.keys()) == {"client-id"}

    def test_append_event_produces_fixture_shaped_line(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _append_event, _load_offset_from_file

        events_path = state_dir() / "channel-events.jsonl"
        _write_events_fixture(events_path)
        # Seed the in-memory offset counter from the fixture, mirroring the
        # real module-import-time seeding (`_event_offset[0] =
        # _load_offset_from_file()`).
        _pr_mod._event_offset[0] = _load_offset_from_file()

        _append_event(
            {"notification_type": "cw-pr-event", "message": "m2", "title": "t2"}
        )

        lines = events_path.read_text().splitlines()
        assert len(lines) == 3
        appended = json.loads(lines[2])
        assert list(appended.keys()) == [
            "notification_type",
            "message",
            "title",
            "offset",
        ]
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

        # Frontier is derived from the on-disk log (the in-memory counter is 0).
        _write_events_fixture(state_dir() / "operator-channel-events.jsonl")
        cursors_path = state_dir() / "operator-channel-cursors.json"
        _write_cursors_fixture(cursors_path)

        ack_offset("client-id", 2)

        data = json.loads(cursors_path.read_text())
        assert data == {"client-id": 2}
        assert set(data.keys()) == {"client-id"}

    def test_append_event_produces_fixture_shaped_line(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import _append_event, _load_offset_from_file

        events_path = state_dir() / "operator-channel-events.jsonl"
        _write_events_fixture(events_path)
        # Seed the in-memory offset counter from the fixture, mirroring the
        # real module-import-time seeding (`_event_offset[0] =
        # _load_offset_from_file()`).
        _operator_mod._event_offset[0] = _load_offset_from_file()

        _append_event(
            {"notification_type": "cw-pr-event", "message": "m2", "title": "t2"}
        )

        lines = events_path.read_text().splitlines()
        assert len(lines) == 3
        appended = json.loads(lines[2])
        assert list(appended.keys()) == [
            "notification_type",
            "message",
            "title",
            "offset",
        ]
        assert appended["offset"] == 2


class TestPrChannelV1FileFormat:
    """PR channel schema v1 on the durable events file (#3173)."""

    def test_non_json_legacy_message_is_not_a_v1_message(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.models.pr_channel_event import PREventMessage

        assert PREventMessage.from_wire("m0") is None

    def test_legacy_v0_record_replays_as_v0(self, tmp_config_dir: Path) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _read_events_from_offset
        from cw.models.pr_channel_event import PREventMessage

        fixtures = Path(__file__).parent / "fixtures" / "pr_channel"
        legacy = json.loads((fixtures / "message_v0_legacy.json").read_text())
        legacy["offset"] = 0
        events_path = state_dir() / "channel-events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(json.dumps(legacy) + "\n")

        (record,) = _read_events_from_offset(0)
        message = PREventMessage.from_wire(record["message"])
        assert message is not None
        assert message.schema_version == 0
        assert message.event_id is None
        assert message.source_id is None
        assert message.kind is None
        assert message.received_at is None

    def test_appended_v1_record_keeps_envelope_key_set(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import PREventRequest

        _pr_mod._event_offset[0] = 0
        notif = _pr_mod._build_notification(
            PREventRequest.model_validate(
                {"repo": "acme/widgets", "pr_number": 42, "event_type": "merged"}
            )
        )
        _pr_mod._append_event(notif)

        lines = (state_dir() / "channel-events.jsonl").read_text().splitlines()
        appended = json.loads(lines[-1])
        assert list(appended.keys()) == [
            "notification_type",
            "message",
            "title",
            "offset",
        ]
        message = json.loads(appended["message"])
        assert message["schema_version"] == 1
