"""Shared constants for the ``cw.review_finding_dispositions`` package.

:data:`_LOGGER_NAME`, the pinned logger name every submodule of this package
logs under, and the verdict and match vocabulary that both the claim matcher
(``match``) and the suppression backstop (``suppress``) read. Imports nothing:
the root of the package's layering. Split out of the flat
``review_finding_dispositions.py`` (#2498).
"""

from __future__ import annotations

# Pinned logger name for every record this package emits. Deliberately a
# literal, not ``__name__``: the pre-split ``review_finding_dispositions.py``
# logged under this fixed name, and ``caplog`` filters, record-name assertions
# and operator log routing key on it, so it must not change with the package
# split (#2498).
_LOGGER_NAME = "cw.review_finding_dispositions"

_MUST_FIX = "MUST_FIX"
#: ``AcceptedFinding.disposition``'s post-consolidate default — "nothing has
#: decided anything about this finding yet". The claim tier below refuses to
#: re-stamp anything else, so a void pass's ``"rejected"`` survives untouched.
_FIXED = "fixed"

#: Which tier produced a match, carried onto the event payload and the log so
#: an audit can tell an exact-identity suppression from a fuzzy one.
_MATCH_EXACT = "exact"
_MATCH_CLAIM = "claim"
