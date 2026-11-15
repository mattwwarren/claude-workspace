from __future__ import annotations

from pathlib import Path

from cw.cli.review import converge_confirm
from cw.dev_queue import review_engine_reconcile


def test_confirm_uses_the_shared_gating_set() -> None:
    shared = review_engine_reconcile.GATING_SEVERITIES
    assert vars(converge_confirm)["GATING_SEVERITIES"] is shared
    assert shared == frozenset({"MUST_FIX", "SHOULD_FIX"})


def test_confirm_uses_the_shared_should_fix_literal() -> None:
    assert vars(converge_confirm)["SHOULD_FIX"] is review_engine_reconcile.SHOULD_FIX
    assert review_engine_reconcile.SHOULD_FIX == "SHOULD_FIX"


def test_no_private_duplicates_remain() -> None:
    assert not hasattr(converge_confirm, "_GATING")
    assert not hasattr(converge_confirm, "_SHOULD_FIX")


def test_converge_confirm_source_has_no_gating_literal() -> None:
    source = Path(converge_confirm.__file__).read_text(encoding="utf-8")
    assert 'frozenset({"MUST_FIX", "SHOULD_FIX"})' not in source
