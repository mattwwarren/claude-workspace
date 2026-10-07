#!/usr/bin/env python3
"""
Review monitor: track PR review threads and nudge authors/reviewers.

Subcommands (to be added in subsequent tasks):
  register  — Start monitoring a PR
  drop      — Stop monitoring a PR
  complete  — Mark a PR as done
  status    — Show current monitor state
  check     — Run one monitoring cycle (resolve threads, detect deferrals, nudge)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_monitor_lib.cli import main

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHub / git helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-discover, auto-fix tracking, channel-bump
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    main()
