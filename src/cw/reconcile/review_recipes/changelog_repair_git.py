"""Git and script steps of the ``repair_changelog_conflict`` recipe (#2771).

Post-lock only: every function here runs a subprocess, so none may run under
``sessions_lock`` or ``dev_queue_lock``.
"""

from __future__ import annotations

import hashlib

_INDEX_LINE_PREFIX = "index "
_HUNK_HEADER_PREFIX = "@@"


def _diff_fingerprint(text: str) -> str:
    """A line-number-independent sha256 of a ``git diff -U0`` text.

    ``git patch-id`` semantics without stdin (``run_git`` offers none): drop
    ``index`` lines, rewrite every ``@@ -a,b +c,d @@ ...`` hunk header to
    ``@@``, keep the file headers and the ``+``/``-`` lines.
    """
    kept = [
        _HUNK_HEADER_PREFIX if line.startswith(_HUNK_HEADER_PREFIX) else line
        for line in text.splitlines()
        if not line.startswith(_INDEX_LINE_PREFIX)
    ]
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()
