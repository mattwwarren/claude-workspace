"""Tests for the cw-validate-result skill's validate_sentinel.py script.

validate_sentinel.py is a standalone skill script (not part of the cw
package), so it is loaded by path -- mirrors test_preflight.py's pattern.
Only the Health-field derivation (#1597 Item B) is covered here; the
bootstrap-under-bare-python smoke test lives in test_skill_script_bootstrap.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from cw.auto_dev_result import Health

_VALIDATE_SENTINEL = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "cw-validate-result"
    / "scripts"
    / "validate_sentinel.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "validate_sentinel_under_test", _VALIDATE_SENTINEL
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestExpectedHealthFields:
    """#1597 Item B: expected Health field names derive from canonical."""

    def test_expected_health_fields_matches_current_set(self) -> None:
        mod = _load()
        assert {
            "lowest_agent_confidence",
            "any_incomplete_risk",
            "recommendation",
        } == mod._EXPECTED_HEALTH_FIELDS

    def test_expected_health_fields_is_subset_of_health_model_fields(self) -> None:
        mod = _load()
        assert mod._EXPECTED_HEALTH_FIELDS.issubset(Health.model_fields)

    def test_build_checks_health_present_passes_with_expected_keys(self) -> None:
        mod = _load()
        parser_output = {
            "raw_payload": {
                "status": "shipped",
                "health": {
                    "lowest_agent_confidence": "HIGH",
                    "any_incomplete_risk": False,
                    "recommendation": "PROCEED",
                },
                "pr": {
                    "number": 1,
                    "url": "https://example/pr/1",
                    "auto_merge": True,
                    "base": "main",
                },
                "next_actions": ["wait_for_ci"],
            },
            "result": {"status": "shipped"},
            "result_kind": "AutoDevResult",
        }
        checks = mod._build_checks(parser_output, "valid")
        health_check = next(c for c in checks if c["name"] == "health_present")
        assert health_check["passed"] is True

    def test_build_checks_health_present_fails_with_missing_key(self) -> None:
        mod = _load()
        parser_output = {
            "raw_payload": {
                "status": "blocked",
                "health": {
                    "any_incomplete_risk": False,
                    "recommendation": "PROCEED",
                },
                "blocker": {"stage": "s2_impl", "reason": "agent_block"},
            },
            "result": {"status": "blocked"},
            "result_kind": "AutoDevResult",
        }
        checks = mod._build_checks(parser_output, "invalid_sentinel")
        health_check = next(c for c in checks if c["name"] == "health_present")
        assert health_check["passed"] is False
