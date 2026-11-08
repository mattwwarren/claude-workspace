"""Shared recipe-key validators and the ticket id / branch charsets.

Used by the ``TicketTask`` field validators and by
``cw.models.orchestrator_config.lane``. Depends only on ``keys``.
"""

from __future__ import annotations

import re

from cw.models.tasks.keys import PARK_ON_ABANDONED_EXIT_KEY


def _validate_gate_recipe_keys(value: dict[str, bool]) -> dict[str, bool]:
    """Fail loud on an unrecognized gate-recipe key (RFC 0009 P4).

    Shared by the ``gate_recipes`` field validators on both
    :class:`TicketTask` and :class:`LaneConfig`. Local literal, not an import
    of cw.reconcile.gate_recipes's RECIPE_* constants — models.py sits below
    cw.reconcile in the import graph (reconcile imports from models, not the
    reverse), so importing them here would be circular. Mirrors
    OrchestratorConfig._validate_concierge_recoveries_keys's fail-loud stance:
    a typo'd key would otherwise silently resolve to the hardcoded default-off
    via resolve_gate_recipe_enabled's plain ``in`` check, leaving the intended
    recipe disabled with zero error at config-load time.
    """
    recognized = {"auto_approve_clean_review", "auto_adopt_clean_plan"}
    unknown = sorted(set(value) - recognized)
    if unknown:
        msg = (
            f"gate_recipes has unrecognized recipe key(s): {unknown}. "
            f"Recognised keys: {sorted(recognized)}."
        )
        raise ValueError(msg)
    return value


def _validate_review_recipe_keys(value: dict[str, bool]) -> dict[str, bool]:
    """Fail loud on an unrecognized review-recipe key (RFC 0010 P3, #1098).

    Shared by the ``review_recipes`` field validators on both
    :class:`TicketTask` and :class:`LaneConfig`. Local literal, not an import
    of cw.reconcile.review_recipes's RECIPE_ADDRESS_REVIEW — models.py sits
    below cw.reconcile in the import graph (reconcile imports from models, not
    the reverse), so importing it here would be circular. Mirrors
    _validate_gate_recipe_keys's fail-loud stance: a typo'd key would otherwise
    silently resolve to the hardcoded default-off via
    resolve_review_recipe_enabled's plain ``in`` check, leaving the intended
    recipe disabled with zero error at config-load time.
    """
    recognized = {
        "address_review",
        "auto_fix_ci",
        "request_reviewer",
        "escalate_merge_block",
        "repair_changelog_conflict",
        "repair_mechanical_conflict",
    }
    unknown = sorted(set(value) - recognized)
    if unknown:
        msg = (
            f"review_recipes has unrecognized recipe key(s): {unknown}. "
            f"Recognised keys: {sorted(recognized)}."
        )
        raise ValueError(msg)
    return value


def _validate_park_on_abandoned_exit_keys(value: dict[str, bool]) -> dict[str, bool]:
    """Fail loud on an unrecognized abandoned-exit-park key (GitHub #2135).

    Shared by the ``park_on_abandoned_exit`` field validators on both
    :class:`TicketTask` and :class:`LaneConfig`, and keyed off
    :data:`PARK_ON_ABANDONED_EXIT_KEY` below — the key's single definition,
    which ``cw.reconcile.abandoned_exit`` imports from here (models sits below
    reconcile in the import graph, so the dependency only runs this way). The
    map holds exactly one recognised key; it is a map rather than a bare bool
    so the per-lane / per-ticket tiers keep the same shape as their
    gate_recipes and review_recipes siblings, and so a second park policy can
    join it without a schema change.
    """
    recognized = {PARK_ON_ABANDONED_EXIT_KEY}
    unknown = sorted(set(value) - recognized)
    if unknown:
        msg = (
            f"park_on_abandoned_exit has unrecognized key(s): {unknown}. "
            f"Recognised keys: {sorted(recognized)}."
        )
        raise ValueError(msg)
    return value


# ticket_id feeds into the default branch name <prefix>/<ticket_id>
# (cw.models.resolve_feature_branch), raw `gh` argv slots, and gh API URL path
# segments (cw.gh.branches). Sibling to _SAFE_CLIENT_NAME / _SAFE_BRANCH_NAME in
# cw.config (duplicated here, not imported, to avoid a models<->config import
# cycle -- config.py already imports from cw.models). No '/': ticket_id is a
# path *component*, the
# branch prefix supplies the separator. No '..': blocks the gh-api
# path-segment-confusion case in _fetch_branch_exists_on_origin. See #1129.
#
# '#' IS permitted: `repo#N` is a real tracker id shape in production use, and
# #1129's original charset outlawed it, which bricked load_dev_queue() for every
# client the moment one such row hit disk. '#' is legal in a git ref name and
# inert in argv (subprocess takes a list — no shell). Its one real hazard is
# acting as a URL fragment inside a `gh api` path, and that is handled by
# percent-encoding at the sink (cw.gh._fetch_branch_exists_on_origin) rather
# than by outlawing an id the tracker already assigned.
_SAFE_TICKET_ID = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9][a-zA-Z0-9._#-]*$")

# TicketTask.branch (v49, #2948): the row's explicit git branch. Same charset
# as cw.config._SAFE_BRANCH_NAME plus the no-'..' rule above, duplicated for the
# same import-cycle reason. Stricter than git (no '#', '@', '+'), which is safe
# only because nothing on disk carries the field yet: a writer must validate a
# branch name before persisting it, or one bad row would brick
# load_dev_queue() for every client (the #1129 failure mode above).
_SAFE_TICKET_BRANCH = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9][a-zA-Z0-9/_.-]*$")
