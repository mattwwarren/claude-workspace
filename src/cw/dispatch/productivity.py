"""Re-export shim for the claim-evidence classifier, now ``cw.claim_evidence``.

#2613 moved the module verbatim to the top-level ``cw.claim_evidence`` leaf so
``cw.reconcile`` can import it at module scope (importing anything under
``cw.dispatch`` runs the package ``__init__``, which imports ``cw.reconcile``).
This path keeps ``from cw.dispatch.productivity import X`` working; every name
here is the ``cw.claim_evidence`` object itself.
"""

from __future__ import annotations

from cw.claim_evidence import ClaimEvidence, extract_claim_evidence, is_unproductive

__all__ = ["ClaimEvidence", "extract_claim_evidence", "is_unproductive"]
