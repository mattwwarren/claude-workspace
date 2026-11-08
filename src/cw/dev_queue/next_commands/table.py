"""``_Rule`` and the single ordered ``_RULES`` table: first match wins.

The rule ids interleave across the lifecycle submodules (0a/0b outrank
2-4), so the order lives in one tuple rather than per-group tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.dev_queue.finalize_park import (
    is_approval_park,
    is_stale_tip_park,
)
from cw.dev_queue.next_commands._common import (
    _anything,
    _generic_commands,
)
from cw.dev_queue.next_commands.parked import (
    _at_approval_gate,
    _attempt_capped,
    _attempt_capped_commands,
    _drain_commands,
    _drain_held,
    _finalize_approval_park_commands,
    _finalize_approval_park_review_less_commands,
    _finalize_stale_tip_park_commands,
    _gate_commands,
    _gate_terminal_commands,
    _human_gate,
    _human_gate_commands,
    _human_gate_off_gate,
    _human_gate_off_gate_commands,
    _human_gate_off_gate_terminal_commands,
    _human_gate_terminal_commands,
    _must_fix,
    _must_fix_commands,
    _must_fix_held_commands,
    _must_fix_no_finalize_commands,
    _must_fix_overridden,
    _must_fix_overridden_commands,
    _must_fix_overridden_held_commands,
    _review_approval_gate_parked,
    _salvage_commands,
    _salvage_parked,
    _scope_drift,
    _scope_drift_commands,
    _sessionless,
    _sessionless_commands,
)
from cw.dev_queue.next_commands.running import (
    _pending,
    _pending_commands,
    _running,
    _running_commands,
)
from cw.dev_queue.next_commands.terminal import (
    _red_shipped,
    _red_shipped_commands,
    _terminal,
    _terminal_commands,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import Session, TicketTask
    from cw.models.ticket_view import NextCommand


class _Rule(NamedTuple):
    matches: Callable[[TicketTask, Session | None], bool]
    commands: Callable[[TicketTask], list[NextCommand]]
    # Used instead of ``commands`` when the row carries a human opt-in (R5).
    opted_in_commands: Callable[[TicketTask], list[NextCommand]] | None = None
    # Used instead of ``commands`` for a BLOCKED row at the terminal stage of
    # its lane pipeline, where ``approve`` refuses (#2760).
    terminal_commands: Callable[[TicketTask], list[NextCommand]] | None = None
    # Used instead of ``commands`` at every on-pipeline position when the
    # commands depend on the bound session's sentinel (rule 0d, #2911).
    session_commands: (
        Callable[[TicketTask, Session | None], list[NextCommand]] | None
    ) = None
    # Used instead of ``terminal_commands`` for a BLOCKED row at the last stage
    # of a lane with no review stage, where ``requeue --stage review`` is
    # refused too (rule 0c, #2779).
    no_review_commands: (
        Callable[[TicketTask, Session | None], list[NextCommand]] | None
    ) = None
    # Used instead of ``commands`` when FINALIZE is not in the lane pipeline
    # (rules 6/6a, #2765); checked after ``opted_in_commands``.
    no_finalize_commands: Callable[[TicketTask], list[NextCommand]] | None = None


# First match wins. Rule 0d (#2911) runs first: a finalize Step 4.0b
# stale-tip park is released through REVIEW by a requeue an agent may run
# (labelled operator-only for a human-kept row), and its sentinel shares rule
# 0c's stamp, so it must be taken before 0c. Rule 0c (#2767) is next: a
# finalize Step 4.0 park is released only through REVIEW, whatever the row's
# opt-in, so it outranks the human-gate rules' `requeue --stage finalize`.
# Rules 0a/0b then cover every human-kept gate:
# 0a is a BLOCKED one ``approve`` cannot take (no bound session at the gate),
# 0b every other one. Rules 5-11 are BLOCKED_ON_USER only; 2-4 cover the other
# statuses. Scope-drift and must-fix approvals never consult the session, so
# only the finalize-park rules (0d/0c), the plain-approve rules (9/10) and
# 0a/0b depend on it. Rule 6b
# (attempt_cap_blocked, #2695) runs before 9 so a capped row bound to a gate
# session still points at `requeue --reset-attempts`; plain requeue would
# re-park it. Rule 8 runs before 9 so an availability hold is released by
# requeue/drain, and DRAIN_DISPOSITIONS deliberately excludes
# branch_behind_main (#1823), which falls to the generic requeue. Rules
# 0c/0a/0b/9 carry ``terminal_commands`` (#2760), and rule 0d's
# ``session_commands`` (``requeue --stage review``) are legal there too; rules
# 5-8 outrank 9 and are unchanged at the terminal stage. Only rule 0c carries
# ``no_review_commands`` (#2779): on a lane with no review stage it lists the
# operator's approve then ``requeue --stage finalize``; every other rule keeps
# its ``terminal_commands`` there (rule 0d is unchanged, #3014).
# Rules 6/6a alone carry ``no_finalize_commands`` (#2765): with FINALIZE absent
# from the lane pipeline they list a plain re-run, after the opt-in check (a
# held row never listed a finalize requeue).
_RULES: tuple[_Rule, ...] = (
    _Rule(
        is_stale_tip_park,
        # Never reached: session_commands covers every on-pipeline position.
        _generic_commands,
        session_commands=_finalize_stale_tip_park_commands,
    ),  # 0d
    _Rule(
        is_approval_park,
        _finalize_approval_park_commands,
        terminal_commands=_finalize_approval_park_commands,
        no_review_commands=_finalize_approval_park_review_less_commands,
    ),  # 0c
    _Rule(
        _review_approval_gate_parked,
        _finalize_approval_park_commands,
        terminal_commands=_finalize_approval_park_commands,
    ),  # 0e
    _Rule(
        _human_gate_off_gate,
        _human_gate_off_gate_commands,
        terminal_commands=_human_gate_off_gate_terminal_commands,
    ),  # 0a
    _Rule(
        _human_gate,
        _human_gate_commands,
        terminal_commands=_human_gate_terminal_commands,
    ),  # 0b
    _Rule(_pending, _pending_commands),  # 2
    _Rule(_running, _running_commands),  # 3
    _Rule(_red_shipped, _red_shipped_commands),  # 3a
    _Rule(_terminal, _terminal_commands),  # 4
    _Rule(_scope_drift, _scope_drift_commands),  # 5
    _Rule(
        _must_fix_overridden,
        _must_fix_overridden_commands,
        _must_fix_overridden_held_commands,
        no_finalize_commands=_must_fix_no_finalize_commands,
    ),  # 6a
    _Rule(
        _must_fix,
        _must_fix_commands,
        _must_fix_held_commands,
        no_finalize_commands=_must_fix_no_finalize_commands,
    ),  # 6
    _Rule(_attempt_capped, _attempt_capped_commands),  # 6b
    _Rule(_salvage_parked, _salvage_commands),  # 7
    _Rule(_drain_held, _drain_commands),  # 8
    _Rule(
        _at_approval_gate, _gate_commands, terminal_commands=_gate_terminal_commands
    ),  # 9
    _Rule(_sessionless, _sessionless_commands),  # 10
    _Rule(_anything, _generic_commands),  # 11
)
