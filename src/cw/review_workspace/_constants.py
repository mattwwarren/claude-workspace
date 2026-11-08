"""Workspace paths, identifier patterns and state vocabularies.

A leaf: imports from no sibling.
"""

from __future__ import annotations

import re
from pathlib import Path

_ACTIVE_DIR = Path(".cw/review-findings")
_ACTIVE_MARKER = Path(".cw/review-findings-round.json")
_ARCHIVE_ROOT = Path(".cw/review-findings-archive")
_STAGING_ROOT = Path(".cw/review-findings-staging")
_LOCK_PATH = Path(".cw/.review-findings.lock")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ROLE_STATES = frozenset({"pending", "completed", "failed"})
_TERMINAL_STATES = frozenset({"completed", "failed", "blocked", "terminal"})
