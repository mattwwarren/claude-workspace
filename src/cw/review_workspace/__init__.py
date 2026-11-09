"""Crash-safe bookkeeping for the Claude-native review findings workspace.

The coordinator still chooses reviewer roles and adjudicates their findings.
This module owns only the durable workspace transitions: roster persistence,
per-role returns, reconciliation, round promotion, recovery, and terminal
marking. The JSON layout stays beside the existing ``.cw/review-findings``
directory so existing review consumers keep reading the same documents.

This package was split out of a single ``review_workspace.py`` module (#3037);
the import surface (``from cw.review_workspace import X``) is preserved here via
re-exports. Submodules, each importing its siblings by direct path and never
through this package:

- ``_constants`` — workspace paths, identifier patterns and the role and
  terminal state vocabularies. A leaf: imports from no sibling.
- ``_models`` — ``TransactionPhase``, the ``_Transaction`` manifest model, the
  ``_Marker`` view and ``WorkspaceResult``. Imports from no sibling.
- ``_primitives`` — error construction, identifier and role-slug validation,
  the ``.cw`` directory guard, path classification and JSON object
  read/atomic write. Imports ``_constants``.
- ``_lock`` — ``_workspace_lock``, the exclusive ``"review_workspace"`` lock.
  Imports ``_constants`` and ``_primitives``.
- ``_marker`` — round-marker parse, payload, write, change, reconcile and
  bundle verification. Imports ``_constants``, ``_models`` and ``_primitives``.
- ``_manifest`` — archive transaction manifest paths, construction and
  validated loading. Imports ``_constants``, ``_models`` and ``_primitives``.
- ``_recovery`` — the archive/promotion transaction engine, its crash-recovery
  state machine and ``recover_workspace``. Imports every submodule above.
- ``_rounds`` — the round API: ``initialize_workspace``,
  ``reconcile_workspace``, ``record_document``, ``record_failure``,
  ``record_reprompt`` and ``finish_workspace``. Imports every submodule above.

Besides the nine public names, ``__all__`` re-exports the thirteen private
names ``tests/test_review_workspace.py`` reaches through the package. Tests
that patch a name patch it on the submodule that calls it, not here: a patch
on this package rebinds only the re-export.
"""

from cw.review_workspace._lock import _workspace_lock
from cw.review_workspace._manifest import _new_transaction
from cw.review_workspace._marker import (
    _change_marker,
    _parse_marker,
    _verify_bundle,
    _write_marker,
)
from cw.review_workspace._models import (
    TransactionPhase,
    WorkspaceResult,
    _Transaction,
)
from cw.review_workspace._primitives import _kind, _read_object, _write_object
from cw.review_workspace._recovery import (
    _rename,
    _write_transaction,
    recover_workspace,
)
from cw.review_workspace._rounds import (
    _new_marker,
    finish_workspace,
    initialize_workspace,
    reconcile_workspace,
    record_document,
    record_failure,
    record_reprompt,
)

__all__ = [
    "TransactionPhase",
    "WorkspaceResult",
    "_Transaction",
    "_change_marker",
    "_kind",
    "_new_marker",
    "_new_transaction",
    "_parse_marker",
    "_read_object",
    "_rename",
    "_verify_bundle",
    "_workspace_lock",
    "_write_marker",
    "_write_object",
    "_write_transaction",
    "finish_workspace",
    "initialize_workspace",
    "reconcile_workspace",
    "record_document",
    "record_failure",
    "record_reprompt",
    "recover_workspace",
]
