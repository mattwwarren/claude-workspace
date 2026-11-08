"""The legal next ``cw`` commands for a dev-queue row as it stands (#2691, #2692).

One ordered rule table, first match wins, mapping a row's state to the
commands an operator (or agent) can run next. ``cw dev-queue show`` renders
it, ``cw dev-queue tasks`` shows its first command in the NEXT column, and
every dev-queue refusal appends it through :func:`refusal_suffix` rather than
re-deriving which verb fits which park.

Pure: no state is loaded and nothing is resolved. The caller passes the
``Session`` the row is bound to -- ``show`` resolves it with
``cw.reconcile._shared.resolve_session_for_task`` and the refusal sites with
``cw.dev_queue.refusal.bound_session`` (both id-only, never raising).
``approve`` itself resolves with ``find_by_name_or_id``, which is id-first, so
the two agree for any row whose ``session_id`` is a ``Session.id``; a legacy
row holding a session *name* resolves to no session here and falls toward
``requeue``, never toward an ``approve`` the verb would refuse. A human-kept
BLOCKED row bound that way is treated as sessionless: rule 0a lists the
operator's labelled ``requeue --stage <stage>``, which is legal. ``approve``
might find the session by name, but ``show`` cannot see it, so it is not
listed (#2760).

Human-kept gates (an operator-signoff park, a finalize hold, a
``scope_hint: large`` approval gate) list only the operator's command:
``approve`` when a bound session is at the gate, otherwise
``requeue --stage <stage>`` -- at the terminal stage it releases the gate and
ships, and when no bound session is at the gate it re-runs the stage. Each is
labelled operator-only and flagged ``operator_only``. A finalize Step 4.0
park (rule 0c, #2767: a FINALIZE ``review_pending_approval`` sentinel stamped
``stage4a_merge_gate``) lists the operator's labelled ``requeue --stage
review`` instead, since its approval is recorded only at REVIEW. On a lane
with no review stage (``TERMINAL_NO_REVIEW``, #2779) that requeue is refused,
so rule 0c lists the operator's ``approve`` (which binds the approval to the
branch head) then ``requeue --stage finalize``, both operator-only; the order
flips once an approval was recorded after a ``missing`` park. A finalize
Step 4.0b stale-tip park (#2911: the same stamp, a ``review_tip_check: stale``
friction token and no ``review_approval_park:`` line) lists the same
``requeue --stage review``, runnable by an agent unless the row is human-kept:
it re-runs review on the current head, and the requeue cannot ship anything
unreviewed because finalize re-checks the tip before it arms auto-merge. When
the stale-tip reason is ``tip_check_fetch_failed`` (#2910: origin could not be
fetched) it lists the plain ``requeue --stage finalize`` instead, with the same
labelling, since no review round can cure a network fault. A
row whose stage is missing from its lane pipeline lists ``show`` plus the
plain ``requeue``. cw never recommends that an agent run a human gate's
command: ``show``, ``tasks`` and refusals carry the label, and the attention
stream withholds it
(:func:`human_gate_attention_command`) on the state-derived
:func:`is_human_gate_command` and resolves the lane and global opt-in as
``show`` does. For the same reason rules 6/6a drop the ``requeue --stage
finalize`` jump past REVIEW for a row that carries a human opt-in. The row
check is partial (lane and global opt-ins are not on the row); ``show`` and
``tasks`` pass the config-resolved opt-in as ``operator_opt_in``.

``pipeline_position`` is another caller-resolved fact, like
``operator_opt_in``: where the row's stage sits in its resolved lane pipeline
(``cw.executor.pipeline_position``). ``approve`` refuses the last stage and a
stage outside the pipeline, so at those positions the table lists what the
verbs accept instead. The default, ``INTERIOR``, is the pre-#2760 output.
``finalize_in_pipeline`` (#2765) is the same kind of fact: whether FINALIZE is
in that pipeline (``cw.executor.finalize_in_pipeline``). When it is not, rules
6/6a list a plain re-run, because ``requeue --stage finalize`` is refused there
and only finalize honors a MUST_FIX override; the default, True, is the
pre-#2765 output.
The rule 0c/0d predicates and purpose text live in ``finalize_park``.

Package split (#2999): the 999-line module is now a pure re-export package
grouped by lifecycle state. Submodules import each other by full path,
never this ``__init__``, in this order (no cycles):

- ``_common`` — shared labels, purposes and command builders (the root).
- ``running`` — the PENDING and RUNNING rules (2, 3).
- ``terminal`` — the red-shipped and terminal-status rules (3a, 4).
- ``parked`` — the human-gate and finalize-park rules (0a-0e) and the
  BLOCKED_ON_USER rules (5-11).
- ``table`` — ``_Rule`` and the single ordered ``_RULES`` table. Rule ids
  interleave across the groups, so the order is kept in one tuple.
- ``engine`` — ``next_commands`` and the refusal / attention helpers.

``finalize_park``, ``park_guidance`` and ``shipped_red`` stay sibling modules
of ``cw.dev_queue``.
"""

from __future__ import annotations

from cw.dev_queue.next_commands._common import (
    _OFF_PIPELINE_STATUSES,
    HUMAN_GATE_CELL_LABEL,
    HUMAN_GATE_LABEL,
    HUMAN_GATE_PURPOSE_LABEL,
    SCOPE_DRIFT_PATHS_PLACEHOLDER,
    dev_queue_command,
    human_gate_settled_notice,
    is_human_kept_gate,
    row_carries_human_opt_in,
)
from cw.dev_queue.next_commands.engine import (
    compact_command,
    human_gate_attention_command,
    is_human_gate_command,
    next_commands,
    refusal_suffix,
)
from cw.dev_queue.next_commands.running import (
    LANE_PAUSED_SKIP_CODE,
    LANE_RESUME_PURPOSE,
)
from cw.dev_queue.next_commands.table import _RULES
from cw.dev_queue.next_commands.terminal import red_recover_command

__all__ = [
    "HUMAN_GATE_CELL_LABEL",
    "HUMAN_GATE_LABEL",
    "HUMAN_GATE_PURPOSE_LABEL",
    "LANE_PAUSED_SKIP_CODE",
    "LANE_RESUME_PURPOSE",
    "SCOPE_DRIFT_PATHS_PLACEHOLDER",
    "_OFF_PIPELINE_STATUSES",
    "_RULES",
    "compact_command",
    "dev_queue_command",
    "human_gate_attention_command",
    "human_gate_settled_notice",
    "is_human_gate_command",
    "is_human_kept_gate",
    "next_commands",
    "red_recover_command",
    "refusal_suffix",
    "row_carries_human_opt_in",
]
