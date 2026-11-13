"""Executed and doc tests for gate 3's ``tests.prepare`` step (#2179).

Gate 3 runs the project-declared prepare command in the clean gate worktree
before the claimed test command. Executed tests run the real fence under real
``bash`` with the ``cw`` shim ``GateBed`` links, against a real detached
worktree. Exit contract: 3 = prepare value could not be read, 4 = prepare
command failed. Both are ``impl_failed`` setup failures, never a
"claim did not reproduce" finding.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests._shared.step25_gates import GateBed, fill_gate_fence, impl_gate3_fence
from tests._tool_fakes import write_bin_stub

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [
    pytest.mark.usefixtures("clean_gate_env"),
    pytest.mark.platform_sensitive,
]

_TEST_MARKER = "ran-test"
_PREPARE_OK = "touch prepared"


def _config(tmpwt: Path, prepare_yaml: str) -> None:
    cfg = tmpwt / ".claude" / "project-config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(prepare_yaml, encoding="utf-8")


def _prepare_yaml(command: str) -> str:
    return f"tests:\n  prepare: {json.dumps(command)}\n"


def _gate3(
    bed: GateBed,
    repo: Path,
    tmpwt: Path,
    test_command: str,
) -> subprocess.CompletedProcess[str]:
    tmpwt.mkdir(exist_ok=True)
    return bed.run(fill_gate_fence(impl_gate3_fence(), test_command=test_command), repo)


# --- Doc tests -------------------------------------------------------------


def test_cw_tests_prepare_precedes_test_command_in_the_fence() -> None:
    fence = impl_gate3_fence()
    assert "cw tests-prepare" in fence
    test_run = fence.index('cd "$TMPWT" && <test_command>')
    assert fence.index("cw tests-prepare") < test_run
    assert "PREPARE_RC" in fence


def test_prepare_failure_is_never_a_claim_finding() -> None:
    assert "did not reproduce" not in impl_gate3_fence()


# --- Executed tests --------------------------------------------------------


def test_absent_config_runs_test_command_only(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    result = _gate3(bed, repo, tmpwt, "printf RAN")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RAN" in result.stdout


def test_absent_key_unchanged(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    tmpwt.mkdir()
    _config(tmpwt, "quality_gates: {}\n")
    result = bed.run(fill_gate_fence(impl_gate3_fence(), "printf RAN"), repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RAN" in result.stdout
    assert "tests.prepare" not in result.stdout


def test_prepare_succeeds_then_test_runs(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    tmpwt.mkdir()
    _config(tmpwt, _prepare_yaml(_PREPARE_OK))
    result = bed.run(
        fill_gate_fence(impl_gate3_fence(), "test -f prepared && printf OK"), repo
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_prepare_sees_gate_tmpdir_and_cwd(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, tmpdir, _ = bed.paths(repo)
    tmpwt.mkdir()
    _config(tmpwt, _prepare_yaml('printf "%s|%s" "$PWD" "$TMPDIR" > prepared.txt'))
    result = bed.run(fill_gate_fence(impl_gate3_fence(), "cat prepared.txt"), repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"{tmpwt}|{tmpdir}"


def test_prepare_failure_blocks_with_exit_and_command(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    tmpwt.mkdir()
    _config(tmpwt, _prepare_yaml("echo boom >&2; exit 7"))
    result = bed.run(fill_gate_fence(impl_gate3_fence(), f"touch {_TEST_MARKER}"), repo)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "tests.prepare" in result.stdout
    assert "exited 7" in result.stdout
    assert "did not reproduce" not in result.stdout
    assert not (tmpwt / _TEST_MARKER).exists()


@pytest.mark.parametrize(
    "prepare_yaml",
    ["tests:\n  prepare:\n    - a\n", "tests:\n  prepare: 7\n"],
    ids=["list", "int"],
)
def test_malformed_value_blocks_before_anything_runs(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    prepare_yaml: str,
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    tmpwt.mkdir()
    _config(tmpwt, prepare_yaml)
    result = bed.run(fill_gate_fence(impl_gate3_fence(), f"touch {_TEST_MARKER}"), repo)
    assert result.returncode == 3, result.stdout + result.stderr
    assert (
        "IMPL_FAILED: Step 2.5 gate 3: tests.prepare could not be read" in result.stdout
    )
    assert not (tmpwt / _TEST_MARKER).exists()


def test_blank_prepare_is_absent(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    tmpwt.mkdir()
    _config(tmpwt, "tests:\n  prepare: '   '\n")
    result = bed.run(fill_gate_fence(impl_gate3_fence(), "printf RAN"), repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RAN" in result.stdout


def test_missing_gate_worktree_is_reported_as_such(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    assert not tmpwt.exists()
    result = bed.run(fill_gate_fence(impl_gate3_fence(), "printf RAN"), repo)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "could not be read" in result.stdout
    assert "gate worktree" in result.stdout


def test_cw_without_the_command_fails_closed(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    tmpwt.mkdir()
    _config(tmpwt, _prepare_yaml("true"))
    write_bin_stub(bed.bin_dir.parent, "cw", "#!/bin/sh\nexit 2\n")
    result = bed.run(fill_gate_fence(impl_gate3_fence(), "printf RAN"), repo)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "could not be read" in result.stdout
    assert "RAN" not in result.stdout


def test_cw_without_the_command_is_harmless_without_the_key(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    # A repo that never declares tests.prepare must run no new code, even on a
    # host whose installed cw predates the command (R1).
    bed = GateBed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, _ = bed.paths(repo)
    tmpwt.mkdir()
    _config(tmpwt, "quality_gates: {}\n")
    write_bin_stub(bed.bin_dir.parent, "cw", "#!/bin/sh\nexit 2\n")
    result = bed.run(fill_gate_fence(impl_gate3_fence(), "printf RAN"), repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RAN" in result.stdout
