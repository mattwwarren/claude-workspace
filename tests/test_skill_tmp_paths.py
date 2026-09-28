"""Doc guard: auto-dev skill fences never write scratch files to bare ``/tmp/``.

#2470: a tmpfs-backed ``/tmp`` shared by every worker on the host ran out of
inodes (ENOSPC across the host). Workers now get ``<worktree>/.cw/tmp`` as
their TMPDIR, and the auto-dev command fences write scratch files either to
that exported ``$TMPDIR`` or to the session worktree's own ``.cw/``.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import _appendix, _cmd

# A literal ``/tmp/`` path. The lookbehind excludes ``/var/tmp/`` (a word char
# before the slash), a ``$VAR/tmp/`` expansion, and a backtick-quoted prose
# mention such as auto-dev-impl.md's "Never write this cache to a fixed
# `/tmp/...` path" caution.
_BARE_TMP_PATH = re.compile(r"(?<![\w$`])/tmp/")


@pytest.mark.parametrize(
    "doc",
    ["auto-dev-finalize.md", "auto-dev.md", "auto-dev-impl.md"],
)
def test_no_bare_tmp_path_in_auto_dev_command(doc: str) -> None:
    content = _cmd(doc)
    hits = [
        line for line in content.splitlines() if _BARE_TMP_PATH.search(line) is not None
    ]
    assert hits == [], f"{doc}: bare /tmp/ scratch path(s): {hits}"


def test_no_bare_tmp_path_in_impl_appendix() -> None:
    content = _appendix("impl")
    assert _BARE_TMP_PATH.search(content) is None


def test_finalize_conflicted_files_live_in_session_worktree() -> None:
    """Writer and reader of the conflicted-file list agree on one path."""
    content = _cmd("auto-dev-finalize.md")
    path = '"$(git rev-parse --show-toplevel)/.cw/conflicted-files-$CW_SESSION"'
    assert f"> {path}" in content
    assert f"--conflicted-files {path}" in content


def test_monolith_gate_scratch_files_use_exported_tmpdir() -> None:
    content = _cmd("auto-dev.md")
    for scratch in ("touched_files-$$", "planned_files-$$", "test.log-$$"):
        assert f'"$TMPDIR/{scratch}"' in content


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("sort > /tmp/touched_files-$$", True),
        ('TMPWT="${CW_GATE_ROOT:-/var/tmp}/cw-gate-wt-$CW_SESSION"', False),
        ("Never write this cache to a fixed `/tmp/...` path", False),
        ('> "$TMPDIR/tmp/x"', False),
        ("# Off /tmp: a tmpfs-backed /tmp is exhausted", False),
    ],
)
def test_regex_flags_only_literal_tmp_paths(line: str, flagged: bool) -> None:
    assert (_BARE_TMP_PATH.search(line) is not None) is flagged
