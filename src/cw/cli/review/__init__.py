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
- ``attestation`` — the ``cw review attestation check`` group (#3040, RFC
  0017 II1): the fail-closed verifier of the head-pinned review sign-off
  attestation, backed by ``cw.review_approval``; exit 0/1/2.
- ``check_approval`` — the ``cw review check-approval`` command (#2773):
  finalize Step 4.0's verdict, an adapter from cw-context and HEAD onto the
  shared ``cw.review_approval`` predicate the dispatch claim gate also calls.
- ``confirm`` — the ``cw review confirm`` command (#2787): one independent
  codex review round for converge-to-clean mode, with its own exit-code
  contract (0 verdict, 3 backend down, 4 round failed on a healthy backend).
- ``constrain`` — the ``cw review constrain`` command (#2964): records a
  binding operator constraint on the dev-queue row, which the codex review
  prompt and fix loop read on every tracker, and prints a mirror comment.
  Append-only (retraction is tracked in #2968).
- ``converge`` — the ``cw review converge`` group (#2787) and its ledger
  subcommands (``config``, ``init``, ``suppress``, ``record-round``,
  ``confirm-failed``, ``status``, ``history-comment``, and ``diff-base``, which
  pins the review diff base, #2800), plus the shared
  ledger/git helpers. ``converge_tip`` holds ``verify-tip`` and ``finalize``
  (the strict, fetching tip check; the tolerated-merge predicate they share
  with ``init`` and ``status`` lives in ``cw.review_convergence._merge_tolerance``)
  and ``operator_settle`` holds ``operator-settle``; both import the group from
  ``converge``. ``converge_delta`` holds ``delta-confirm`` (#2980), the
  cw-recorded ``delta`` round over only the conflict resolution of a
  default-branch merge.
- ``_settle_store`` — ``settle``'s tracker-aware row write (#2701): record
  the ledger on the dev-queue row, degrade to mirror-only on a GitHub-tracked
  client when the row cannot be recorded, fail on every other tracker. No
  command of its own, so it needs no registration import below.
- ``decided`` — the read-only ``cw review decided`` view (#2984): the row's
  constraint entries, grouped by provenance, as the Claude-native reviewers'
  ``## Decided Constraints`` prompt block.
- ``dispositions`` — the read-only ``cw review dispositions`` ledger
  inspection view (#2232). Its own submodule rather than a fifth command in
  ``commands``: everything there parses an operator-supplied payload and
  writes something, while this one only reads the dev-queue row.
- ``engine`` — the ``cw review engine validate`` command (#3036): strict check
  of an engine's JSON summary, read-only, exit 0/1/2.
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
    attestation,
    check_approval,
    commands,
    confirm,
    consolidate,
    constrain,
    converge,
    converge_delta,
    converge_tip,
    decided,
    dispositions,
    engine,
    history,
    operator_settle,
    voided,
    workspace,
)
from cw.cli.review._group import _build_captured_diff

__all__ = ["_build_captured_diff"]
