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

Authoritative-run notice: the authoritative answer to this ticket's question
is a MANUAL, LOCAL run under snap-confined codex (``which codex`` ->
``/snap/bin/codex``). A run under any npm-installed codex (e.g. hypothetically
added to a workflow later) characterizes a different sandbox stack and does
NOT answer this ticket's question. This module is never invoked by
``nightly-codex.yml`` -- that workflow's pytest step names
``tests/test_codex_contract_live.py`` explicitly, a different file -- so no
nightly run ever exercises this probe.

Manual reporting checklist: after a local run, a human posts a comment on
#1548 and #1545 stating:

1. Install method -- ``snap`` vs ``npm``, determined via ``which codex``.
2. ``codex --version`` output.
3. Whether the sentinel token was returned -- read the
   ``codex_sandbox_probe ...`` log line this test emits (``sentinel_returned``
   field).

This module's test never performs that posting itself -- it is a manual,
out-of-band human step (Comment 3's binding resolution), not something this
test or any script automates. Merging this ticket does NOT itself unblock
#1548 -- the premise stays open until a human runs the probe and reports.
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
    -- that text explicitly forbids filesystem access ("Evaluate the diff
    strictly from the material inlined above -- do not rely on filesystem
    access"), which is the opposite of what this probe needs to instruct.
    The ``ReviewerFindingsDocument`` JSON contract is still targeted (and
    mechanically enforced by ``--output-schema``, independent of prompt
    wording), so the response still round-trips through the same schema as
    every other ``_run_codex_role`` caller.
    """
    return "\n\n".join(
        [
            "# Reviewer Role: Sandbox Probe",
            (
                "Your current working directory is a git repository. Look for "
                f"a file named `{_SENTINEL_FILENAME}` in your working directory "
                "and read its contents directly from disk -- do NOT rely on "
                "any content inlined into this prompt, because none has been "
                "provided."
            ),
            (
                "If you can read the file: emit exactly one finding with "
                "severity 'NIT', file set to the file's name, summary "
                "'sandbox read access confirmed', consequence 'none', "
                "suggested_fix 'none', confidence 'HIGH', and evidence set to "
                "the exact file contents you read (the sentinel token). Set "
                "status to 'ok'."
            ),
            (
                "If you cannot read the file (permission denied, sandbox "
                "blocked, file not found, or any other filesystem-access "
                "failure): emit zero findings, set status to 'degraded', and "
                "set detail to a short explanation of what happened."
            ),
            "Respond ONLY with the required JSON document -- no prose.",
        ]
    )


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexSandboxReadOnlyProbe:
    """Record-only: can ``--sandbox read-only`` codex read a file in its own cwd?

    Structural contract only (run completed, document parsed) is asserted.
    The empirical direction of the answer -- whether the sentinel token came
    back -- is logged, never asserted. See module docstring for the manual
    reporting checklist.
    """

    def test_probe_worktree_read_access(
        self,
        make_git_repo: Callable[..., Path],
        live_base: Callable[[], Path],
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

        evidence = doc.findings[0].evidence if doc.findings else None
        sentinel_returned = evidence is not None and sentinel_token in evidence
        _log.info(
            "codex_sandbox_probe status=%s findings=%d sentinel_returned=%s detail=%r",
            doc.status,
            len(doc.findings),
            sentinel_returned,
            doc.detail,
        )
        assert doc.status in ("ok", "degraded", "failed")
        assert isinstance(doc.findings, list)
        assert isinstance(sentinel_returned, bool)
        runner.assert_clean()
