#!/usr/bin/env python3
"""
Review monitor: track PR review threads and nudge authors/reviewers.

Thin entry point. The implementation lives in the sibling package
``review_monitor_lib`` (one submodule per concern); see its ``cli`` module for
the subcommand list.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Installed copies are symlinks (~/.claude/scripts -> global-claude/scripts ->
# this repo), so resolve first: the package and utils/ must come from the same
# checkout as this file, never from a stale or partial copy beside the link.
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from review_monitor_lib.cli import main
except ModuleNotFoundError as exc:
    if (exc.name or "").split(".")[0] != "review_monitor_lib":
        raise
    sys.exit(
        f"review_monitor.py: package 'review_monitor_lib' is missing next to "
        f"{_SCRIPTS_DIR / 'review_monitor.py'} (launched as {__file__}): {exc}"
    )

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

if __name__ == "__main__":
    main()
