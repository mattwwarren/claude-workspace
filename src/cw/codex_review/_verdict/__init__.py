"""Verdict synthesis and review-comment rendering for the codex-review package.

Consolidates the per-role reviewer documents into a single verdict, maps it to
a typed :class:`AutoDevResult` (blocked/CODEX_* on empty, MUST_FIX, or partial
rosters; stage_complete otherwise), and renders the consolidated verdict into a
GitHub-issue-comment markdown body. Consumed by ``core`` (result synthesis).

Package split (#2043) of the historical flat module, which had grown past the
~1000-line ceiling in CLAUDE.md. Three submodules, named for the two coequal
responsibilities the module docstring has always claimed plus the health
signals both consult:

- ``_health`` — roster health derivation and the failure predicates.
- ``_render`` — the consolidated verdict's markdown body.
- ``_synthesis`` — the disposition table and the blocked-result constructor.

Names are re-exported here so ``from cw.codex_review._verdict import X`` keeps
working. A test that patches one of them must patch it on the submodule that
DEFINES it — a bare-name lookup inside a submodule reads that submodule's own
globals, not this shim's separate binding.
"""

from __future__ import annotations

from cw.codex_review._verdict._health import _format_failures_detail
from cw.codex_review._verdict._render import (
    _CONFIDENCE_ANNOTATION,
    _render_findings,
    render_verdict_comment,
)
from cw.codex_review._verdict._synthesis import (
    make_codex_blocked,
    synthesize_codex_review_result,
)

__all__ = [
    "_CONFIDENCE_ANNOTATION",
    "_format_failures_detail",
    "_render_findings",
    "make_codex_blocked",
    "render_verdict_comment",
    "synthesize_codex_review_result",
]
