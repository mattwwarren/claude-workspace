"""Tests for `scripts/install.sh`'s uv invocation (#2186).

`install.sh` used `uv tool install --from "$PROJECT_DIR" ... "claude-workspace[mcp]"`,
which uv >= 0.11 rejects: `--from` no longer exists on `uv tool install`, so a
local-clone install aborted before `install-skills.sh` ever ran. The fix is the
PEP 508 direct-reference form, `claude-workspace[mcp] @ file://<path>`, which
requires percent-encoding the path because a space (and `#`, `?`, `%`) is
URL-significant inside a requirement string.

These tests are hermetic: a stub `uv` on `$PATH` records its own `argv` and
exits with a caller-chosen code, so no real install, network access or tool-dir
mutation happens. A stub can only assert what our own script writes — the real
uv CLI is exercised by the `package-smoke` job in `.github/workflows/ci.yml`.

Follows the subprocess-shell convention of `tests/test_install_skills.py` and
`tests/test_release_sh.py`. Every helper here is private to this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
REAL_INSTALL_SH = ROOT / "scripts" / "install.sh"

_STUB_UV_BODY = """#!/usr/bin/env bash
for arg in "$@"; do
  printf '%s\\n' "$arg" >> "$STUB_UV_ARGV_FILE"
done
exit "${STUB_UV_EXIT:-0}"
"""

_STUB_SKILLS_BODY = """#!/usr/bin/env bash
touch "$(dirname "$0")/skills.ran"
"""


def _build_fake_repo(tmp_path: Path, dirname: str = "repo") -> tuple[Path, Path]:
    """Scaffold a fake clone plus a stub-`uv` bin dir. Returns (repo, bin_dir).

    `install.sh` derives PROJECT_DIR as dirname(dirname(BASH_SOURCE[0])), so a
    copy of the real script at <repo>/scripts/install.sh makes PROJECT_DIR
    resolve to <repo>. `install-skills.sh` is stubbed to drop a marker file so
    a test can prove whether the sync stage was reached.
    """
    if not REAL_INSTALL_SH.exists():
        pytest.fail(f"install.sh not found at {REAL_INSTALL_SH}")
    repo = tmp_path / dirname
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    script_copy = scripts_dir / "install.sh"
    shutil.copy2(str(REAL_INSTALL_SH), str(script_copy))
    script_copy.chmod(0o755)

    skills_stub = scripts_dir / "install-skills.sh"
    skills_stub.write_text(_STUB_SKILLS_BODY, encoding="utf-8")
    skills_stub.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_stub = bin_dir / "uv"
    uv_stub.write_text(_STUB_UV_BODY, encoding="utf-8")
    # 0o755 is load-bearing: a non-executable stub is skipped by bash's $PATH
    # lookup and the test would silently exercise the host's real uv.
    uv_stub.chmod(0o755)
    return repo, bin_dir


def _run(
    repo: Path, bin_dir: Path, *, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the fake clone's install.sh with the stub `uv` first on `$PATH`."""
    argv_file = repo.parent / "uv_argv.txt"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_UV_ARGV_FILE": str(argv_file),
        **(extra_env or {}),
    }
    proc = subprocess.run(
        ["/bin/bash", str(repo / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )
    argv = (
        argv_file.read_text(encoding="utf-8").splitlines() if argv_file.exists() else []
    )
    return proc, argv


def test_uses_direct_reference_not_from(tmp_path: Path) -> None:
    """The uv invocation is the PEP 508 direct reference, with no `--from`."""
    repo, bin_dir = _build_fake_repo(tmp_path)
    proc, argv = _run(repo, bin_dir)

    assert proc.returncode == 0, proc.stderr
    assert "--from" not in argv
    assert argv == [
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--no-cache",
        f"claude-workspace[mcp] @ file://{repo}",
    ]


@pytest.mark.parametrize("dirname", ["my repo", "a#b", "100%", "q?x"])
def test_path_special_chars_are_percent_encoded(tmp_path: Path, dirname: str) -> None:
    """URL-significant characters in the clone path are percent-encoded."""
    repo, bin_dir = _build_fake_repo(tmp_path, dirname=dirname)
    proc, argv = _run(repo, bin_dir)

    assert proc.returncode == 0, proc.stderr
    requirement = argv[-1]
    url = requirement.split(" @ ", 1)[1]
    assert not any(char in url for char in (" ", "#", "?"))
    decoded = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    assert decoded == str(repo)


def test_uv_failure_aborts_before_skills_sync(tmp_path: Path) -> None:
    """A failing uv aborts under `set -euo pipefail`, before the skills sync."""
    repo, bin_dir = _build_fake_repo(tmp_path)
    proc, _argv = _run(repo, bin_dir, extra_env={"STUB_UV_EXIT": "2"})

    assert proc.returncode != 0
    assert not (repo / "scripts" / "skills.ran").exists()


def test_success_runs_skills_sync(tmp_path: Path) -> None:
    """A successful uv install is followed by the skills sync."""
    repo, bin_dir = _build_fake_repo(tmp_path)
    proc, _argv = _run(repo, bin_dir)

    assert proc.returncode == 0, proc.stderr
    assert (repo / "scripts" / "skills.ran").exists()
