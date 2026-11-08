"""Crash-safe bookkeeping for the Claude-native review findings workspace.

The coordinator still chooses reviewer roles and adjudicates their findings.
This module owns only the durable workspace transitions: roster persistence,
per-role returns, reconciliation, round promotion, recovery, and terminal
marking. The JSON layout stays beside the existing ``.cw/review-findings``
directory so existing review consumers keep reading the same documents.
"""

from __future__ import annotations

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
    "recover_workspace",
]
