"""Live sandbox-read-access probe for ``codex exec --sandbox read-only`` (#1545).

A single, env-gated live test that drives a real ``codex exec`` invocation
through the product's own :func:`cw.codex_review._run_codex_role` seam, with
``cwd`` set to a fixture git worktree (mirroring ``RealCodexRunner.run``'s
real ``cwd=worktree``) containing a sentinel token in a git-committed file.
It answers the empirical question #1545/#1548 raise: can a
``--sandbox read-only`` codex process read a file in its own ``cwd`` without
that file's content being inlined into the prompt?

Only the structural contract is asserted (the run completed without failure,
and a ``ReviewerFindingsDocument`` was parsed). Whether the sentinel token
actually came back in ``finding.evidence`` is *logged*, never asserted -- see
``TestCodexContractDiagnostics.test_installed_version_recorded`` and
``TestCodexContractMissingOutput`` in ``test_codex_contract_live.py`` for the
established record-only pattern this module follows.

Gating: identical ``INTEGRATION_CODEX_LIVE`` gate to the sibling live-contract
suite. Run manually::

    INTEGRATION_CODEX_LIVE=1 \\
        uv run pytest tests/test_codex_sandbox_probe.py -v -m integration

TODO(this module, follow-up commit): document the authoritative-run notice
and the manual reporting checklist here.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING

import pytest

from cw.codex_review import _run_codex_role
from tests.test_codex_contract_live import (
    _RecordingCodexRunner,
    _scratch,
    _seed_repo,
    live_base,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Re-export so pytest resolves the `live_base` fixture from this module too
# (fixtures imported into a test module's namespace are discovered by name).
__all__ = ["live_base"]

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

_CODEX_LIVE = os.environ.get("INTEGRATION_CODEX_LIVE", "").strip() not in ("", "0")

_SENTINEL_FILENAME = "sandbox-probe-sentinel.txt"


def _sandbox_probe_prompt(sentinel_token: str) -> str:
    """A probe-local prompt instructing codex to read from its own ``cwd``.

    Deliberately does NOT reuse ``cw.codex_review._context._OUTPUT_INSTRUCTIONS``
    -- that text explicitly forbids filesystem access, the opposite of what
    this probe needs. Full prompt text lands in a follow-up commit.
    """
    return (
        "# Reviewer Role: Sandbox Probe\n\n"
        f"Read `{_SENTINEL_FILENAME}` from your cwd and report token "
        f"{sentinel_token!r} if found."
    )


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexSandboxReadOnlyProbe:
    """Record-only: can ``--sandbox read-only`` codex read a file in its own cwd?"""

    def test_probe_worktree_read_access(
        self,
        make_git_repo: Callable[..., Path],
        live_base: Callable[[], Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        base = live_base()
        sentinel_token = f"SENTINEL-{uuid.uuid4().hex}"
        repo = _seed_repo(
            make_git_repo,
            base,
            "sandbox-probe",
            filename=_SENTINEL_FILENAME,
            content=sentinel_token + "\n",
        )
        runner = _RecordingCodexRunner()
        doc, failure = _run_codex_role(
            runner=runner,
            worktree=repo,
            role="Sandbox Probe",
            prompt=_sandbox_probe_prompt(sentinel_token),
            model=None,
            timeout_seconds=120,
            scratch_dir=_scratch(base),
            session_id="sandbox-probe-suite",
        )
        # Structural contract ONLY -- the run completed and the document
        # parsed. Never assert on whether codex could read the file.
        assert failure is None
        assert doc is not None
        runner.assert_clean()
