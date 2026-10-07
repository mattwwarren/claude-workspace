"""Implementation package for ``.claude/scripts/review_monitor.py`` (#2499).

One submodule per concern; ``review_monitor.py`` is the thin entry point and
imports only :mod:`review_monitor_lib.cli`. Nothing is re-exported here: the
script is run, never imported, so the submodules are imported by name.
"""
