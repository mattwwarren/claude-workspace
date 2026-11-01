"""Rendering of a consolidated verdict into a GitHub-issue-comment body.

:func:`render_verdict_comment` picks one of four headlines and then appends
every per-concern note section unconditionally, each written to the same
empty-list-returns-``[]`` shape so a pass with nothing to say about a concern
produces no bytes for it. Nothing here reads or influences the disposition
table — the sections report what the verdict already decided. The one section
that is not purely a report is the settle-payload section (#2210), the
producer half of the cross-round adjudication ledger's first real write path.

**Everything a model wrote is escaped on its way in** (#2210 round 4): every
interpolation of model-authored text goes through ``_safe``, because the
comment is read back on the next round by the disposition ledger's parser.
The full contract, and its one lossless exception (the settle payload), is
documented on ``_common``.

Package split (#2808) of the historical flat ``_render.py``, which had grown
past the ~1000-line ceiling in CLAUDE.md. One submodule per concern:

- ``_common`` — the ``_safe`` escaping alias and ``_truncate``.
- ``_findings`` — the accepted-findings sections and their annotations.
- ``_notes`` — the clean headline and the history/coverage/capability/
  agent-spec/delta/debt notes.
- ``_rejected`` — the mechanically-rejected and discarded-findings sections.
- ``_dispositions`` — the refused and stale disposition-record sections.
- ``_settle`` — the bounded settle-payload section.
- ``core`` — :func:`render_verdict_comment`, which orders all of the above.

Names imported anywhere are re-exported here so ``from
cw.codex_review._verdict._render import X`` keeps working. A test that
patches one of them must patch it on the submodule that DEFINES it — a
bare-name lookup inside a submodule reads that submodule's own globals, not
this package's separate binding. ``_SETTLE_SECTION_BUDGET_CHARS`` is
deliberately NOT re-exported, so a patch aimed at this package fails loudly
instead of silently doing nothing.
"""

from __future__ import annotations

from cw.codex_review._verdict._render._dispositions import (
    _REFUSED_DISPOSITION_HEADING,
    _REFUSED_DISPOSITION_NOTE,
    _REFUSED_MAX_ROWS,
    _STALE_DISPOSITION_HEADING,
    _STALE_DISPOSITION_NOTE,
    _STALE_MAX_ROWS,
)
from cw.codex_review._verdict._render._findings import (
    _CONFIDENCE_ANNOTATION,
    _render_findings,
)
from cw.codex_review._verdict._render._rejected import _render_rejected_finding_text
from cw.codex_review._verdict._render._settle import (
    _SETTLE_COMPACT_SUMMARY_MAX,
    _SETTLE_MAX_COMPACT_ROWS,
    _SETTLE_MAX_PAYLOADS,
    _SETTLE_OVERFLOW_NOTE,
)
from cw.codex_review._verdict._render.core import render_verdict_comment

__all__ = [
    "_CONFIDENCE_ANNOTATION",
    "_REFUSED_DISPOSITION_HEADING",
    "_REFUSED_DISPOSITION_NOTE",
    "_REFUSED_MAX_ROWS",
    "_SETTLE_COMPACT_SUMMARY_MAX",
    "_SETTLE_MAX_COMPACT_ROWS",
    "_SETTLE_MAX_PAYLOADS",
    "_SETTLE_OVERFLOW_NOTE",
    "_STALE_DISPOSITION_HEADING",
    "_STALE_DISPOSITION_NOTE",
    "_STALE_MAX_ROWS",
    "_render_findings",
    "_render_rejected_finding_text",
    "render_verdict_comment",
]
