"""Guard tests for the ssh keepalive on `.claude/commands/ship-it.md` Step 2 (#3222).

The opt-in pre-push `diff-cover` hook runs for about 13 minutes after `git push`
has already opened its ssh connection. An idle connection is dropped, and the
push then fails with `Connection to github.com closed by remote host` while the
hook is green. Step 2 exports a keepalive for the push only when nothing else
configures ssh for it, and only for an ssh remote.

Group A pins the Step 2 prose. Group B extracts the Step 2 bash fence and runs
it against a real temp git repo with a fake `ssh` on PATH, so the test sees the
exact arguments git hands ssh. Group C pins the contributor documentation.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests._shared.git_repos import _clean_git_env, git_in
from tests._tool_fakes import write_bin_stub

ROOT = Path(__file__).parent.parent
SHIP_IT_PATH = ROOT / ".claude" / "commands" / "ship-it.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
INSTALL_MD = ROOT / "docs" / "INSTALL.md"
PRE_COMMIT_CONFIG = ROOT / ".pre-commit-config.yaml"
FENCE = "```bash"
KEEPALIVE = "ServerAliveInterval=20"
KEEPALIVE_COUNT = "ServerAliveCountMax=6"
CLAUDE_MD_START = "The opt-in pre-push hook outlives"

# Fake ssh: `ssh -G <host>` reports the serveraliveinterval from FAKE_SSH_SAI
# (default 0, i.e. no user keepalive). Any other call is logged and refused, so
# a push never reaches the network.
FAKE_SSH = """#!/bin/sh
if [ "$1" = "-G" ]; then echo "serveraliveinterval ${FAKE_SSH_SAI:-0}"; exit 0; fi
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
exit 255
"""

# Fake GIT_SSH wrapper: git calls it as `$GIT_SSH <host> <command>`.
FAKE_GIT_SSH = """#!/bin/sh
printf 'GIT_SSH:%s\\n' "$*" >> "$FAKE_SSH_LOG"
exit 255
"""


def _ship_it_text() -> str:
    return SHIP_IT_PATH.read_text(encoding="utf-8")


def _step2_section() -> str:
    content = _ship_it_text()
    start = content.index("## Step 2: Confirm branch is pushed")
    end = content.index("## Step 3", start)
    return content[start:end]


def _step2_script() -> str:
    """Extract the Step 2 bash fence verbatim."""
    section = _step2_section()
    start = section.index(FENCE)
    end = section.index("```", start + len(FENCE))
    return section[start + len(FENCE) : end]


def _run_step2(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    remote_url: str,
    *,
    env_extra: dict[str, str] | None = None,
    git_config: dict[str, str] | None = None,
) -> str:
    """Run the Step 2 fence in a temp repo; return what the fake ssh logged.

    The push itself fails (the fake ssh exits 255); callers do not assert the
    return code. Returns "" when ssh was never invoked.
    """
    repo = make_git_repo("keepalive")
    git_in(repo, "remote", "add", "origin", remote_url)
    for key, value in (git_config or {}).items():
        git_in(repo, "config", key, value)

    bin_dir = write_bin_stub(tmp_path, "ssh", FAKE_SSH)
    log = tmp_path / "ssh.log"

    base = _clean_git_env()
    base.pop("FAKE_SSH_SAI", None)
    env = {
        **base,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_SSH_LOG": str(log),
        **(env_extra or {}),
    }
    subprocess.run(
        ["/bin/bash", "-c", _step2_script()],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return log.read_text(encoding="utf-8") if log.exists() else ""


# ---------------------------------------------------------------------------
# Group A: Step 2 prose and fence shape
# ---------------------------------------------------------------------------


def test_step2_keeps_plain_push_and_never_forces() -> None:
    script = _step2_script()
    assert 'git push -u origin "$BRANCH" 2>&1' in script
    assert "--force" not in script
    assert " -f " not in script
    section = _step2_section()
    assert "<!-- el-begin: ship-it.confirm-branch-is-pushed -->" in _ship_it_text()
    assert "<!-- el-end: ship-it.confirm-branch-is-pushed -->" in _ship_it_text()
    assert "never force-push" in section


def test_step2_keepalive_values_match_ticket() -> None:
    script = _step2_script()
    assert KEEPALIVE in script
    assert KEEPALIVE_COUNT in script


# ---------------------------------------------------------------------------
# Group B: execute the fence against a real repo with a fake ssh
# ---------------------------------------------------------------------------


def test_scp_style_ssh_remote_gets_keepalive(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    log = _run_step2(make_git_repo, tmp_path, "git@example.test:o/r.git")
    assert "-o ServerAliveInterval=20 -o ServerAliveCountMax=6" in log


def test_ssh_scheme_remote_gets_keepalive(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    log = _run_step2(make_git_repo, tmp_path, "ssh://git@example.test:2222/o/r.git")
    assert "-o ServerAliveInterval=20 -o ServerAliveCountMax=6" in log


def test_preset_git_ssh_command_is_not_clobbered(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    log = _run_step2(
        make_git_repo,
        tmp_path,
        "git@example.test:o/r.git",
        env_extra={"GIT_SSH_COMMAND": "ssh -o Marker=1"},
    )
    assert "Marker=1" in log
    assert "ServerAlive" not in log


def test_preset_git_ssh_is_not_clobbered(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    wrapper_dir = write_bin_stub(tmp_path, "git-ssh-wrap", FAKE_GIT_SSH)
    wrapper = wrapper_dir / "git-ssh-wrap"
    log = _run_step2(
        make_git_repo,
        tmp_path,
        "git@example.test:o/r.git",
        env_extra={"GIT_SSH": str(wrapper)},
    )
    assert "GIT_SSH:" in log
    assert "ServerAlive" not in log


def test_core_sshcommand_is_not_clobbered(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    log = _run_step2(
        make_git_repo,
        tmp_path,
        "git@example.test:o/r.git",
        git_config={"core.sshCommand": "ssh -o Marker=2"},
    )
    assert "Marker=2" in log
    assert "ServerAlive" not in log


def test_user_ssh_config_keepalive_is_respected(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    log = _run_step2(
        make_git_repo,
        tmp_path,
        "git@example.test:o/r.git",
        env_extra={"FAKE_SSH_SAI": "30"},
    )
    assert "ServerAlive" not in log


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://example.test/o/r.git",
        # https with userinfo and a port: a bare `*@*:*` arm would misread it
        # as an scp-style remote whose host is `https`.
        "https://u@example.test:8443/o/r.git",
    ],
)
def test_non_ssh_remote_never_invokes_ssh(
    make_git_repo: Callable[..., Path], tmp_path: Path, remote_url: str
) -> None:
    log = _run_step2(make_git_repo, tmp_path, remote_url)
    assert "ServerAlive" not in log
    assert log == ""


def test_local_path_remote_never_invokes_ssh(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    log = _run_step2(make_git_repo, tmp_path, str(tmp_path / "upstream.git"))
    assert "ServerAlive" not in log
    assert log == ""


# ---------------------------------------------------------------------------
# Group C: documentation prose
# ---------------------------------------------------------------------------


def _claude_md_paragraph(start: str) -> str:
    """The blank-line-delimited CLAUDE.md paragraph beginning with *start*."""
    for paragraph in CLAUDE_MD.read_text(encoding="utf-8").split("\n\n"):
        if paragraph.startswith(start):
            return paragraph
    message = f"no CLAUDE.md paragraph starts with {start!r}"
    raise AssertionError(message)


def test_claude_md_documents_keepalive() -> None:
    paragraph = _claude_md_paragraph(CLAUDE_MD_START)
    for needle in (
        KEEPALIVE,
        KEEPALIVE_COUNT,
        "GIT_SSH_COMMAND",
        "~/.ssh/config",
        "/ship-it",
        "Step 2",
        "90%",
    ):
        assert needle in paragraph, needle


def test_install_md_dev_setup_documents_keepalive() -> None:
    text = INSTALL_MD.read_text(encoding="utf-8")
    dev_section = text[text.index("### For Development") : text.index("### Upgrading")]
    assert "ServerAliveInterval" in dev_section


def test_pre_commit_config_comment_points_at_keepalive() -> None:
    assert "ServerAliveInterval" in PRE_COMMIT_CONFIG.read_text(encoding="utf-8")
