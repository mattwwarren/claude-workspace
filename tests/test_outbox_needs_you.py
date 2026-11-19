"""The unreachable-binding gate filer and its reason-aware record text (O2b, R25).

HOME is isolated per test (the autouse fixture), so ``needs_you.py`` is only
ever a stub written here. No real script is reached.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.outbox_needs_you import (
    UnreachableCause,
    build_gate_payload,
    file_gate_record,
)

if TYPE_CHECKING:
    from pathlib import Path

_STUB = """#!/bin/sh
cat > "{out}"
exit {code}
"""


def _script(home: Path) -> Path:
    return home / ".claude" / "scripts" / "needs_you.py"


def _install(home: Path, *, code: int = 0) -> Path:
    script = _script(home)
    script.parent.mkdir(parents=True, exist_ok=True)
    out = home / "recorded.json"
    script.write_text(_STUB.format(out=out, code=code), encoding="utf-8")
    script.chmod(0o755)
    return out


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestFileGateRecord:
    def test_present_script_exit_zero_returns_true_and_records_stdin(
        self, home: Path
    ) -> None:
        out = _install(home)
        payload = build_gate_payload(
            "acme", "tickets", UnreachableCause.NO_REALIZATION, system="linear"
        )

        assert file_gate_record(payload) is True

        recorded = json.loads(out.read_text(encoding="utf-8"))
        assert recorded["kind"] == "gate"
        for key in ("title", "question", "why_it_matters", "recommendation"):
            assert recorded[key]

    def test_absent_script_returns_false(self, home: Path) -> None:
        assert file_gate_record({"kind": "gate"}) is False

    def test_non_zero_exit_returns_false(self, home: Path) -> None:
        _install(home, code=3)
        assert file_gate_record({"kind": "gate"}) is False

    def test_hang_is_a_timeout_returns_false(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(home)

        def _hang(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="needs_you.py", timeout=10)

        monkeypatch.setattr("cw.outbox_needs_you.subprocess.run", _hang)
        assert file_gate_record({"kind": "gate"}) is False

    def test_os_error_on_exec_returns_false(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(home)

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "exec failed"
            raise OSError(msg)

        monkeypatch.setattr("cw.outbox_needs_you.subprocess.run", _boom)
        assert file_gate_record({"kind": "gate"}) is False


class TestBuildGatePayload:
    @pytest.mark.parametrize("cause", list(UnreachableCause))
    def test_every_cause_names_client_and_binding(
        self, cause: UnreachableCause
    ) -> None:
        payload = build_gate_payload("acme", "tickets", cause, system="linear")

        assert payload["kind"] == "gate"
        assert "acme" in payload["title"]
        assert "tickets" in payload["title"]

    def test_tool_unauthenticated_names_the_marker_in_title_and_question(self) -> None:
        payload = build_gate_payload(
            "acme",
            "tickets",
            UnreachableCause.TOOL_UNAUTHENTICATED,
            system="linear",
        )

        assert "linear_mcp_unauthenticated" in payload["title"]
        assert "linear_mcp_unauthenticated" in payload["question"]
        assert "apps.<id>.default_tools_enabled=true" in payload["question"]

    def test_config_unreadable_names_the_detail(self) -> None:
        payload = build_gate_payload(
            "acme",
            "tickets",
            UnreachableCause.CONFIG_UNREADABLE,
            system="linear",
            detail="/home/u/.codex/config.toml: is unreadable (boom)",
        )

        assert "/home/u/.codex/config.toml" in payload["question"]
        assert "Fix or remove" in payload["question"]

    def test_no_realization_says_so(self) -> None:
        payload = build_gate_payload(
            "acme", "tickets", UnreachableCause.NO_REALIZATION, system="linear"
        )

        assert "no codex realization" in payload["question"]

    def test_non_tickets_role_has_no_none_text(self) -> None:
        payload = build_gate_payload(
            "acme", "knowledge", UnreachableCause.CODEX_UNAVAILABLE, system=None
        )

        assert "None" not in json.dumps(payload)
        assert "codex" in payload["question"]

    def test_each_cause_has_its_own_question(self) -> None:
        questions = {
            build_gate_payload("acme", "tickets", cause, system="linear")["question"]
            for cause in UnreachableCause
        }

        assert len(questions) == len(UnreachableCause)
