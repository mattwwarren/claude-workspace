"""The ``cw review`` CLI group (GitHub #1154, RFC 0011 S2; #1241).

Package split (#2048) of the historical flat ``cw.cli.review`` module, which had
grown to 951 lines. Four focused modules:

- ``_group`` — the ``review`` click group object plus the payload helpers
  (``_build_captured_diff``, ``_parse_payload_or_exit``) every command shares.
- ``_diff_integrity`` — the placeholder / duplicate-hunk / ``git diff``-match
  guards, and the ``--base``/``--no-base-check`` alternatives pair that gates
  them.
- ``consolidate`` — the ``cw review consolidate`` command and its
  ``--documents-from`` loading helpers.
- ``commands`` — ``register``, ``adjudicate``, ``check-voided``, and
  ``verify-fixes``.

The per-command behavioral prose the flat module's docstring carried now lives
on the submodule that owns that command. Importing the command submodules below
is what registers the commands on the ``main`` group, exactly as the flat
module's import-time decoration did.
"""

from __future__ import annotations

from cw.cli.review import (  # noqa: F401  (command registration side effects)
    _diff_integrity,
    commands,
    consolidate,
)
from cw.cli.review._group import _build_captured_diff

__all__ = ["_build_captured_diff"]
