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
- ``plan_gates``: PLAN gate set B and the PLAN status vocabulary.
- ``posted_text``: redaction and capping of text posted to the tracker.
- ``push``: push-and-verify-head.
"""
