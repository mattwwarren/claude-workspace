"""The ``cw review`` CLI group (GitHub #1154, RFC 0011 S2; #1241).

Package split (#2048) of the historical flat ``cw.cli.review`` module, which had
grown to 951 lines, into focused submodules:

- ``_group`` — the ``review`` click group object plus the payload helpers
  (``_build_captured_diff``, ``_parse_payload_or_exit``) every command shares.
- ``_diff_integrity`` — the placeholder / duplicate-hunk / ``git diff``-match
  guards, and the ``--base``/``--no-base-check`` alternatives pair that gates
  them.
- ``consolidate`` — the ``cw review consolidate`` command and its
  ``--documents-from`` loading helpers.
- ``commands`` — ``register``, ``adjudicate``, ``settle`` (#2210, plus
  #2232's ``REVERSED`` rollback outcome), and ``verify-fixes``, plus the
  ``check-voided`` request/response envelopes.
- ``confirm`` — the ``cw review confirm`` command (#2787): one independent
  codex review round for converge-to-clean mode, with its own exit-code
  contract (0 verdict, 3 backend down, 4 round failed on a healthy backend).
- ``converge`` — the ``cw review converge`` group (#2787) and its ledger
  subcommands (``config``, ``init``, ``suppress``, ``record-round``,
  ``confirm-failed``, ``status``, ``history-comment``, and ``diff-base``, which
  pins the review diff base, #2800), plus the shared
  ledger/git helpers. ``converge_tip`` holds ``verify-tip`` and ``finalize``
  (the strict, fetching tip check) and ``operator_settle`` holds
  ``operator-settle``; both import the group from ``converge``.
- ``_settle_store`` — ``settle``'s tracker-aware row write (#2701): record
  the ledger on the dev-queue row, degrade to mirror-only on a GitHub-tracked
  client when the row cannot be recorded, fail on every other tracker. No
  command of its own, so it needs no registration import below.
- ``dispositions`` — the read-only ``cw review dispositions`` ledger
  inspection view (#2232). Its own submodule rather than a fifth command in
  ``commands``: everything there parses an operator-supplied payload and
  writes something, while this one only reads the dev-queue row.
- ``history`` — the read-only ``cw review history`` view (#2790): one ticket's
  review replayed round by round from the converge ledger, its sentinel and the
  row's disposition records (``cw.review_history``).
- ``voided`` — the ``cw review check-voided`` command (#1814) and its
  unmatched-new-entry check (#2319). Split out of ``commands`` once that check
  would have pushed it past the module-size ceiling.

The per-command behavioral prose the flat module's docstring carried now lives
on the submodule that owns that command. Importing the command submodules below
is what registers the commands on the ``main`` group, exactly as the flat
module's import-time decoration did.
"""

from __future__ import annotations

from cw.cli.review import (  # noqa: F401  (command registration side effects)
    _diff_integrity,
    commands,
    confirm,
    consolidate,
    converge,
    converge_tip,
    dispositions,
    history,
    operator_settle,
    voided,
)
from cw.cli.review._group import _build_captured_diff

__all__ = ["_build_captured_diff"]
