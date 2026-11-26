"""Post-turn stage verification: the deterministic checks cw runs on a turn's output.

Introduced by RFC 0019 H3.

Harness-neutral: no module here imports a codex runner, argv, profile or driver
module. The codex packages import this package, never the reverse. Consumers
import the submodule they need directly; this ``__init__`` re-exports nothing
(eager re-exports would re-create the import cycles this layout avoids).

Submodules:

- ``baseline``: cycle baseline capture and the touched-path diff.
- ``fence``: the fix-cycle fence breaches and ``scope_violations``.
- ``finalize_*``: FINALIZE gates, CI evidence, merge gate, PR probes, arm and result.
- ``growth``: the cumulative growth budget.
- ``impl_block``, ``impl_feedback``, ``impl_gates``: the IMPL park, feedback and gates.
- ``impl_operator_directions``: the operator resolutions and constraints an IMPL
  run receives (#3761), shared by the claude-native key and the codex prompt.
- ``impl_regress_findings``: the REVIEW-regress findings rule (#3694), shared by
  every consumer.
- ``impl_regress_ledger``: the clean-mode ledger verdict reader the regress rule
  falls back to (#3862).
- ``impl_merge_adopt``: adopting an unconcluded regress merge and the guards
  that conclude it (#3529).
- ``impl_resume``: the IMPL re-entry classification and the four dispositions
  (#3529).
- ``impl_staleness``: the IMPL staleness check and the regress signal (#3529).
- ``impl_type_gate``: IMPL gate 4's declaration, baseline, ``tests.prepare``,
  gate-worktree, mypy-attribution and debt-record pieces (#3531).
- ``plan_gates``: PLAN gate set B and the PLAN status vocabulary.
- ``posted_text``: redaction and capping of text posted to the tracker.
- ``push``: push-and-verify-head.
"""
