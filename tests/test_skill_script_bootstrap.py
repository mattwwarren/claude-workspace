"""Smoke tests: skill scripts import cw under bare python via sys.path bootstrap.

parse_sentinel.py and validate_sentinel.py both import from cw.auto_dev_result at
module level. Without the bootstrap those imports fail when the scripts are invoked
outside of `uv run`. These tests verify the bootstrap is present and functional.

Test strategy (per #671 decision note):
- Use sys.executable with the -S flag — NOT /usr/bin/python3.
  * sys.executable guarantees the ABI matches the venv's compiled C-extensions
    (e.g. pydantic_core), which the system python3 cannot load on CI due to
    ABI mismatch → ModuleNotFoundError.
  * -S skips site.py processing, so the editable-install .pth file is NOT
    processed and cw is NOT importable unless the bootstrap adds src/ to sys.path.
- Set PYTHONPATH to the venv purelib directory so pydantic is available as a
  plain path entry (no .pth side-effects).
- --help is sufficient: argparse exits(0) after printing help, but the cw module-level
  import still runs first — any import failure surfaces here.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path

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
# Venv purelib provides pydantic (a cw dep) without exposing cw itself:
# the editable-install .pth file is not processed when the path is added via
# PYTHONPATH rather than being a real site-packages directory activated by Python.
_VENV_PURELIB = sysconfig.get_path("purelib")
assert _VENV_PURELIB, "Could not determine venv purelib path for test isolation"


def _bare_env() -> dict[str, str]:
    """Env with pydantic available but cw NOT importable — bootstrap required."""
    return {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": _VENV_PURELIB,
    }


def _run_help(script: Path) -> subprocess.CompletedProcess[str]:
    # sys.executable -S: ABI-matching interpreter + skip site.py (no .pth side-effects)
    return subprocess.run(
        [sys.executable, "-S", str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=_bare_env(),
    )


class TestSkillScriptBootstrap:
    def test_parse_sentinel_help_bare_python(self) -> None:
        result = _run_help(_PARSE_SENTINEL)
        assert result.returncode == 0, (
            f"parse_sentinel.py --help failed under bare python\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "usage" in result.stdout.lower()

    def test_validate_sentinel_help_bare_python(self) -> None:
        result = _run_help(_VALIDATE_SENTINEL)
        assert result.returncode == 0, (
            f"validate_sentinel.py --help failed under bare python\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "usage" in result.stdout.lower()
