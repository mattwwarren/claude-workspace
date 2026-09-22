"""Leaf text helpers shared by modules that must not import `cw.config` (#1409).

`redact` and `_bounded` originated in :mod:`cw.executor_diagnostics`, which
imports `cw.config`. `cw.native_daemon` needs both helpers too, but
`cw.config` imports `cw._config_migrate`, which imports `cw.native_daemon` —
so a module-level `cw.executor_diagnostics` import from `native_daemon` would
close that loop and break `import cw.config` outright. This module imports
nothing from `cw`, so both call sites can import it at module scope with no
cycle and no deferred-import lint suppression.

`cw.executor_diagnostics` re-exports `redact` and `_bounded` from here so its
existing callers are unaffected.
"""

from __future__ import annotations

import re

# Every bounded excerpt field is capped at this many characters. Matches
# codex_runner.py's stderr[-4000:] and local_runner's _AIDER_LOG_TAIL_CHARS
# conventions so callers never drift onto different caps.
_EXCERPT_LIMIT = 4000

_REDACTION_PLACEHOLDER = "<redacted>"

# Common secret shapes. Conservative and false-positive-tolerant: over-redacting
# a local diagnostics artifact is cheaper than leaking a token. The generic
# high-entropy rule only fires for a 32+ char run immediately preceded by ``=``
# or ``:`` (an assignment/header shape), so ordinary file paths are left alone.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"(?<=[=:])[A-Za-z0-9_-]{32,}"),
    # Closes a gap the generic rule above misses (#1482): its lookbehind
    # requires no space between "="/":" and the secret run, and has no
    # length-floor exemption for a literal "Authorization"/"token" marker,
    # so "Authorization: <token>" (colon-space, no "Bearer") and a short
    # "token=<value>" both pass through unredacted.
    re.compile(r"(?i)\b(?:Authorization|token)\s*[:=]\s*\S+"),
)


def _bounded(text: str) -> str:
    """Return *text* capped at :data:`_EXCERPT_LIMIT`, keeping the tail.

    When *text* exceeds the cap, the newest ``_EXCERPT_LIMIT`` characters are
    kept and a ``...[truncated, N chars omitted]...\\n`` marker is prepended so
    a reader knows the head was dropped (the tail carries the failure's last
    output, which is the diagnostically useful part).
    """
    if len(text) <= _EXCERPT_LIMIT:
        return text
    omitted = len(text) - _EXCERPT_LIMIT
    return f"...[truncated, {omitted} chars omitted]...\n{text[-_EXCERPT_LIMIT:]}"


def redact(text: str) -> str:
    """Replace known secret shapes in *text* with a redaction placeholder."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTION_PLACEHOLDER, text)
    return text
