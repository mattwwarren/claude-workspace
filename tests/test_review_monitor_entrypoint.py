"""Entry-point tests for ``.claude/scripts/review_monitor.py`` (#2499).

Each test launches the real script in a subprocess with ``sys.executable``,
``PYTHONPATH`` removed, ``PYTHONDONTWRITEBYTECODE=1`` and every state path the
script reads pointed under ``tmp_path`` (the working directory too, since the
legacy state file is cwd-relative).

- E1: ``--help`` lists every subcommand of the golden CLI contract.
- E2: the installed layout. ``~/.claude/scripts`` is a symlink to a
  global-claude ``scripts`` dir, which holds a per-file symlink to the repo
  script and a real ``utils/`` dir of per-file links to the repo's ``utils``
  (mirroring the operator's install), so the script is launched through a
  symlink in a different directory from the repo copy.
- E3: the same layout plus a stale/decoy ``review_monitor_lib/`` package and a
  decoy ``utils/runtime_paths.py`` beside the symlink. The script resolves its
  own path before touching ``sys.path``, so neither decoy is ever imported.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests import _review_monitor_helpers as helpers


def _run(script: Path, tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        GLOBAL_CLAUDE_REVIEW_MONITOR_DIR=str(tmp_path / "state"),
        GLOBAL_CLAUDE_DESKTOP_QUEUE_DIR=str(tmp_path / "desktop-queue"),
    )
    workdir = tmp_path / "cwd"
    workdir.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(script), *argv],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _installed_layout(tmp_path: Path) -> Path:
    """Build home/.claude/scripts -> global/scripts -> repo; return the launcher."""
    global_scripts = tmp_path / "global" / "scripts"
    global_utils = global_scripts / "utils"
    global_utils.mkdir(parents=True)
    (global_scripts / "review_monitor.py").symlink_to(helpers.ENTRY_SCRIPT)
    for name in ("__init__.py", "runtime_paths.py"):
        (global_utils / name).symlink_to(helpers.SCRIPTS_DIR / "utils" / name)
    claude_dir = tmp_path / "home" / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "scripts").symlink_to(global_scripts, target_is_directory=True)
    return claude_dir / "scripts" / "review_monitor.py"


def test_e1_help_lists_every_subcommand(tmp_path: Path) -> None:
    golden = json.loads(helpers.CLI_CONTRACT_FIXTURE.read_text())

    result = _run(helpers.ENTRY_SCRIPT, tmp_path, "--help")

    assert result.returncode == 0, result.stderr
    missing = [name for name in golden["commands"] if name not in result.stdout]
    assert missing == []


def test_e2_installed_symlink_layout_runs(tmp_path: Path) -> None:
    launcher = _installed_layout(tmp_path)
    assert launcher.resolve() == helpers.ENTRY_SCRIPT.resolve()
    assert launcher.parent.resolve() != helpers.SCRIPTS_DIR.resolve()

    help_result = _run(launcher, tmp_path, "register", "--help")
    status_result = _run(launcher, tmp_path, "status", "--repo", "x/y", "--json")

    assert help_result.returncode == 0, help_result.stderr
    assert help_result.stdout.startswith("usage:")
    assert "--repo-path" in help_result.stdout
    assert status_result.returncode == 0, status_result.stderr
    assert json.loads(status_result.stdout) == {"monitored": {}, "completed": {}}


_DECOY = 'raise SystemExit("DECOY")\n'


def test_e3_decoys_beside_the_symlink_are_never_imported(tmp_path: Path) -> None:
    launcher = _installed_layout(tmp_path)
    global_scripts = tmp_path / "global" / "scripts"
    decoy_lib = global_scripts / "review_monitor_lib"
    decoy_lib.mkdir()
    (decoy_lib / "__init__.py").write_text(_DECOY)
    (decoy_lib / "cli.py").write_text(_DECOY)
    decoy_paths = global_scripts / "utils" / "runtime_paths.py"
    decoy_paths.unlink()
    decoy_paths.write_text(_DECOY)

    result = _run(launcher, tmp_path, "register", "--help")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage:")
    assert "DECOY" not in result.stdout
    assert "DECOY" not in result.stderr
