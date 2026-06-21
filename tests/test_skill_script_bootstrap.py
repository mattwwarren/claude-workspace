"""Smoke tests: skill scripts import cw under bare python3 via sys.path bootstrap.

parse_sentinel.py and validate_sentinel.py both import from cw.auto_dev_result at
module level. Without the bootstrap those imports fail when the scripts are invoked
outside of `uv run`. These tests verify the bootstrap is present and functional.

Test strategy (per #671 decision note):
- Use /usr/bin/python3 — NOT sys.executable (the venv interpreter already has cw).
- Set PYTHONPATH to the venv site-packages so pydantic is available; .pth files in
  PYTHONPATH directories are NOT processed by the interpreter, so cw is NOT importable
  unless the bootstrap adds src/ to sys.path.
- --help is sufficient: argparse exits(0) after printing help, but the cw module-level
  import still runs first — any import failure surfaces here.
"""

from __future__ import annotations

import subprocess
import sysconfig
from pathlib import Path

import pytest

_SYSTEM_PYTHON = "/usr/bin/python3"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PARSE_SENTINEL = (
    _REPO_ROOT / ".claude" / "skills" / "cw-followup" / "scripts" / "parse_sentinel.py"
)
_VALIDATE_SENTINEL = (
    _REPO_ROOT
    / ".claude"
    / "skills"
    / "cw-validate-result"
    / "scripts"
    / "validate_sentinel.py"
)
# Venv site-packages provides pydantic (a cw dep) without exposing cw itself:
# the editable-install .pth file is not processed when the path is added via
# PYTHONPATH rather than being a real site-packages directory activated by Python.
_VENV_SITE_PACKAGES = sysconfig.get_path("purelib")
assert _VENV_SITE_PACKAGES, (
    "Could not determine venv site-packages path for test isolation"
)


def _bare_env() -> dict[str, str]:
    """Env with pydantic available but cw NOT importable — bootstrap required."""
    return {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": _VENV_SITE_PACKAGES,
    }


def _run_help(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_SYSTEM_PYTHON, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=_bare_env(),
    )


@pytest.mark.skipif(
    not Path(_SYSTEM_PYTHON).exists(),
    reason=f"{_SYSTEM_PYTHON} not found on this platform",
)
class TestSkillScriptBootstrap:
    def test_parse_sentinel_help_bare_python(self) -> None:
        result = _run_help(_PARSE_SENTINEL)
        assert result.returncode == 0, (
            f"parse_sentinel.py --help failed under bare python3\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "usage" in result.stdout.lower()

    def test_validate_sentinel_help_bare_python(self) -> None:
        result = _run_help(_VALIDATE_SENTINEL)
        assert result.returncode == 0, (
            f"validate_sentinel.py --help failed under bare python3\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "usage" in result.stdout.lower()
