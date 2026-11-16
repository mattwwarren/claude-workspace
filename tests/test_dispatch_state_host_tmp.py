"""Host-tmp inode latch sidecar (#2470), split out of the dispatch-state tests (#3638).

Moved here with ``cw.dispatch_state_host_tmp``. The class below is the original
round-trip and fail-soft contract; the classes after it pin the primitive's
return value, exact warning text, ``exc_info``, logger name, the process-local
fallback set and pop, and the one-way import direction.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cw.dispatch_state
from cw import dispatch_state_host_tmp
from cw.atomic import atomic_write_text as real_atomic_write_text
from cw.dispatch_state import OpenPrProbeCache, save_open_pr_probe_entry
from cw.dispatch_state_host_tmp import (
    HostTmpProbeCache,
    load_host_tmp_probe_cache,
    save_host_tmp_probe_cache,
)
from tests._shared.sidecar_failures import fail_state_writes

_WARNING_TEXT = "dispatch_state: failed to persist host_tmp_probe (client=acme)"


class TestHostTmpProbeCacheSidecar:
    """Round-trip + fail-soft contract for the #2470 per-client inode latch."""

    def test_round_trip_and_per_client_entries_coexist(
        self, tmp_config_dir: Path
    ) -> None:
        now = datetime.now(UTC)
        save_host_tmp_probe_cache(
            "acme", HostTmpProbeCache(probed_at=now, exhausted=True, latched=True)
        )
        save_host_tmp_probe_cache(
            "other", HostTmpProbeCache(probed_at=now, exhausted=False, latched=False)
        )

        cache = load_host_tmp_probe_cache()

        assert set(cache) == {"acme", "other"}
        assert cache["acme"].latched is True
        assert cache["acme"].exhausted is True
        assert cache["other"].latched is False
        assert abs((cache["acme"].probed_at - now).total_seconds()) < 1

    def test_absent_file_returns_empty(self, tmp_config_dir: Path) -> None:
        assert load_host_tmp_probe_cache() == {}

    def test_corrupt_file_returns_empty(self, tmp_config_dir: Path) -> None:
        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("{not json")

        assert load_host_tmp_probe_cache() == {}

    @pytest.mark.parametrize(
        "payload",
        [["not", "a", "dict"], {"host_tmp_probe": ["not", "a", "dict"]}],
    )
    def test_non_dict_shapes_return_empty(
        self, tmp_config_dir: Path, payload: object
    ) -> None:
        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(json.dumps(payload))

        assert load_host_tmp_probe_cache() == {}

    def test_malformed_entry_is_dropped_not_poisoning_siblings(
        self, tmp_config_dir: Path
    ) -> None:
        save_host_tmp_probe_cache(
            "good",
            HostTmpProbeCache(datetime.now(UTC), exhausted=True, latched=True),
        )
        payload = json.loads(cw.dispatch_state.DISPATCH_STATE_FILE.read_text())
        payload["host_tmp_probe"]["bad-shape"] = {"probed_at": 42}
        payload["host_tmp_probe"]["bad-ts"] = {
            "probed_at": "not-a-timestamp",
            "exhausted": True,
            "latched": True,
        }
        payload["host_tmp_probe"]["not-a-dict"] = "nope"
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(json.dumps(payload))

        assert set(load_host_tmp_probe_cache()) == {"good"}

    def test_write_preserves_sibling_keys(self, tmp_config_dir: Path) -> None:
        save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(datetime.now(UTC), has_open_pr=True)
        )

        save_host_tmp_probe_cache(
            "acme", HostTmpProbeCache(datetime.now(UTC), exhausted=True, latched=True)
        )

        assert "acme/1862" in cw.dispatch_state.load_open_pr_probe_cache()
        assert "acme" in load_host_tmp_probe_cache()

    def test_write_failure_is_swallowed(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fail_state_writes(monkeypatch)

        save_host_tmp_probe_cache(
            "acme", HostTmpProbeCache(datetime.now(UTC), exhausted=True, latched=True)
        )

        # The process-local fallback keeps the just-set latch visible even
        # when the sidecar write fails, preventing repeated attention events.
        assert load_host_tmp_probe_cache()["acme"].latched is True

        save_host_tmp_probe_cache(
            "acme", HostTmpProbeCache(datetime.now(UTC), exhausted=False, latched=False)
        )
        assert load_host_tmp_probe_cache()["acme"].latched is False


class TestSaveHostTmpPrimitiveContract:
    """Return value, exact warning, exc_info, logger and fallback for the save."""

    def test_success_returns_true_and_writes(self, tmp_config_dir: Path) -> None:
        ok = save_host_tmp_probe_cache(
            "acme", HostTmpProbeCache(datetime.now(UTC), exhausted=True, latched=True)
        )

        assert ok is True
        assert "acme" in load_host_tmp_probe_cache()

    def test_failure_returns_false_logs_exact_text_with_traceback(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        attempts = fail_state_writes(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="cw.dispatch_state"):
            ok = save_host_tmp_probe_cache(
                "acme",
                HostTmpProbeCache(datetime.now(UTC), exhausted=True, latched=True),
            )

        assert ok is False
        assert len(attempts) == 1
        assert not cw.dispatch_state.DISPATCH_STATE_FILE.exists()
        records = [r for r in caplog.records if r.name == "cw.dispatch_state"]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].getMessage() == _WARNING_TEXT
        assert isinstance(records[0].exc_info, tuple)

    def test_failure_stores_fallback_and_success_pops_it(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fail_state_writes(monkeypatch)
        key = (str(cw.dispatch_state.DISPATCH_STATE_FILE), "acme")
        latched = HostTmpProbeCache(datetime.now(UTC), exhausted=True, latched=True)

        save_host_tmp_probe_cache("acme", latched)
        assert dispatch_state_host_tmp._HOST_TMP_PROBE_FALLBACK[key] == latched

        # Restore only the writer: undo() would also revert the tmp_config_dir
        # state-dir redirect.
        monkeypatch.setattr(
            "cw.dispatch_state.atomic_write_text", real_atomic_write_text
        )
        ok = save_host_tmp_probe_cache(
            "acme", HostTmpProbeCache(datetime.now(UTC), exhausted=False, latched=False)
        )

        assert ok is True
        assert key not in dispatch_state_host_tmp._HOST_TMP_PROBE_FALLBACK


class TestHostTmpImportDirection:
    """``cw.dispatch_state`` must never import the satellite; both orders load."""

    def test_dispatch_state_does_not_import_host_tmp(self) -> None:
        import ast

        source_path = Path(cw.dispatch_state.__file__)
        tree = ast.parse(source_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "cw.dispatch_state_host_tmp"
                assert not (
                    node.module == "cw"
                    and any(a.name == "dispatch_state_host_tmp" for a in node.names)
                )
            if isinstance(node, ast.Import):
                assert all(a.name != "cw.dispatch_state_host_tmp" for a in node.names)

    @pytest.mark.parametrize(
        "statement",
        [
            "import cw.dispatch_state; import cw.dispatch_state_host_tmp",
            "import cw.dispatch_state_host_tmp; import cw.dispatch_state",
        ],
    )
    def test_both_import_orders_succeed_in_fresh_interpreter(
        self, statement: str
    ) -> None:
        # Interpreter-isolation pattern from CLAUDE.md: same interpreter, no
        # foreign python, so compiled deps stay importable.
        result = subprocess.run(
            [sys.executable, "-I", "-c", statement],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
