"""Codex fix-loop adapter for CodexExecutor's REVIEW stage (#1392).

Wraps :func:`cw.codex_review.run_review` in a bounded fix loop: after an
initial (cycle 0) review pass that surfaces blocking MUST_FIX findings, cw runs
up to ``_MAX_FIX_CYCLES`` cycles of ``codex exec --sandbox workspace-write``
fix invocations, committing each cycle's real changes and re-running the
per-role review pass over the delta to see which findings cleared.
``CodexExecutor.spawn()``'s Step 3 delegates to :func:`run_review_with_fix_loop`
instead of ``run_review``.

This package was split out of a single ``codex_fix_loop.py`` module plus its
three flat ``codex_fix_loop_*`` siblings (#2370); the import surface
(``from cw.codex_fix_loop import X``) is preserved here via re-exports.
Internal cross-references use the direct submodule path, never this package.
A test that patches a name one of these submodules imported from elsewhere
must target the submodule that looks it up at call time
(``cw.codex_fix_loop._driver.run_review``), not this package. Submodules:

- ``_driver`` — :func:`run_review_with_fix_loop`, the per-cycle re-review, the
  wall-clock budget helpers, and the per-cycle exit decision.
- ``commit`` — one cycle's fix invocation, out-of-scope sensitive-path check,
  and commit (argv/prompt builders included).
- ``baseline`` — the per-cycle clean-start baseline and the measurement of
  what a cycle itself changed (#2633).
- ``fence`` — the fix-cycle scope fence (#2485) and revert guard (#2492),
  checked on the cycle's measured changes before it is committed.
- ``growth`` — the in-file growth budget and its lock, state-file and
  path-constant detectors (#2633).
- ``constraints`` — the operator's binding constraints from the resolutions
  comment: prompt section and forbidden-token check (#2633).
- ``hook_failure`` — a rejected commit hook parked as ``codex_fix_hook_failed``
  with capped, redacted output (#2633).
- ``posted_text`` — the one redact-and-cap helper for text a guard posts.
- ``park`` — the terminal park/clean-exit builders and the terminal
  ``Review`` reconstruction.
- ``snapshot`` — per-cycle ``ReviewVerdict`` snapshot persist/finalize and the
  ``friction_highlights`` pointer (#1763).
- ``convergence`` — the delta-aware admission gate and cross-cycle
  open-finding tracker (#1837).
- ``divergence`` — the loop-wide divergence guard (#2394).
- ``push`` — push-and-verify for fix-cycle commits (#2354).
"""

from __future__ import annotations

from cw.codex_fix_loop._driver import (
    _FIX_CYCLE_FLOOR_SECONDS,
    _MAX_FIX_CYCLES,
    run_review_with_fix_loop,
)
from cw.codex_fix_loop.commit import (
    _build_fix_codex_argv,
    _build_fix_prompt,
    _commit_fix_cycle,
)

__all__ = [
    "_FIX_CYCLE_FLOOR_SECONDS",
    "_MAX_FIX_CYCLES",
    "_build_fix_codex_argv",
    "_build_fix_prompt",
    "_commit_fix_cycle",
    "run_review_with_fix_loop",
]
