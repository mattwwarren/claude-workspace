"""Pure tests for ``cw.scope_grants`` (#3772): exact paths, verification, ledger.

The ancestry probe is a fake dict, so these run with no git. The real-git
acceptance fixture lives in ``tests/test_scope_grants_git.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from cw.models.client import ReviewConfig, ReviewEngineConfig
from cw.models.scope_grant import ScopeGrant
from cw.scope_grants import (
    MAX_GRANT_PATH_LEN,
    MAX_GRANT_PATHS,
    MAX_SCOPE_GRANTS,
    GrantContext,
    append_scope_grant,
    exact_paths_error,
    parse_drift_extra_files,
    review_policy_fingerprint,
    verify_scope_grants,
)

_T0 = datetime(2026, 11, 1, tzinfo=UTC)
_ROW_CREATED = datetime(2026, 11, 2, tzinfo=UTC)
_H1 = "1" * 40
_H2 = "2" * 40
_H3 = "3" * 40
_POLICY = "policy-a"


def _grant(
    head: str = _H1,
    paths: tuple[str, ...] = ("src/a.py", "src/b.py"),
    *,
    minutes: int = 0,
    repo: str | None = "acme/widgets",
    pr_number: int | None = None,
    policy: str = _POLICY,
    plan: str | None = "plan-x",
) -> ScopeGrant:
    return ScopeGrant(
        client="acme",
        ticket_id="T-1",
        row_created_at=_ROW_CREATED,
        repo=repo,
        pr_number=pr_number,
        head=head,
        policy=policy,
        plan_fingerprint=plan,
        paths=list(paths),
        actor="op",
        recorded_at=_T0 + timedelta(minutes=minutes),
    )


def _ctx(
    *,
    repo: str | None = "acme/widgets",
    pr_number: int | None = None,
    policy: str = _POLICY,
    plan: str | None = "plan-x",
) -> GrantContext:
    return GrantContext(
        client="acme",
        ticket_id="T-1",
        row_created_at=_ROW_CREATED,
        repo=repo,
        pr_number=pr_number,
        policy=policy,
        plan_fingerprint=plan,
    )


def _ancestry(
    edges: dict[tuple[str, str], bool | None],
) -> Callable[[str, str], bool | None]:
    """Fake ``is_ancestor``: unknown pairs answer ``False``."""

    def _is_ancestor(old: str, new: str) -> bool | None:
        return edges.get((old, new), False)

    return _is_ancestor


# --- exact paths -----------------------------------------------------------


class TestExactPaths:
    def test_plain_repo_relative_paths_are_accepted(self) -> None:
        assert exact_paths_error(["src/a.py", "app/[id]/page.tsx"]) is None

    def test_empty_list_is_refused(self) -> None:
        assert exact_paths_error([]) == "no paths given"

    def test_each_rule_refuses_with_its_reason(self) -> None:
        cases = {
            "": "empty path",
            "x" * (MAX_GRANT_PATH_LEN + 1): "over-long path",
            "a\x00b": "control character in path",
            "a\nb": "control character in path",
            "a\x7fb": "control character in path",
            "a\\b": "backslash in path",
            "/etc/passwd": "absolute path",
            "src/*.py": "glob character in path",
            "src/?.py": "glob character in path",
            "src/dir/": "trailing slash in path",
            "src//a.py": "empty segment in path",
            "src/../a.py": "dot segment in path",
            "./a.py": "dot segment in path",
        }
        for path, reason in cases.items():
            assert exact_paths_error([path]) == reason, path

    def test_count_cap(self) -> None:
        many = [f"f{i}.py" for i in range(MAX_GRANT_PATHS + 1)]
        assert exact_paths_error(many) == (
            f"too many paths ({MAX_GRANT_PATHS + 1}; the cap is {MAX_GRANT_PATHS})"
        )


# --- review policy fingerprint ---------------------------------------------


class TestReviewPolicyFingerprint:
    def test_stable_for_equal_config(self) -> None:
        a = ReviewConfig(convergence="clean")
        b = ReviewConfig(convergence="clean")
        assert review_policy_fingerprint(a) == review_policy_fingerprint(b)

    def test_changes_with_convergence(self) -> None:
        assert review_policy_fingerprint(
            ReviewConfig(convergence="clean")
        ) != review_policy_fingerprint(ReviewConfig(convergence="blocking"))

    def test_changes_with_engine_skill_and_guarded_paths(self) -> None:
        base = ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(skill="review-x", guarded_paths=["a/*.py"]),
        )
        other_skill = ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(skill="review-y", guarded_paths=["a/*.py"]),
        )
        other_guard = ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(skill="review-x", guarded_paths=["b/*.py"]),
        )
        fp = review_policy_fingerprint(base)
        assert fp != review_policy_fingerprint(other_skill)
        assert fp != review_policy_fingerprint(other_guard)


# --- ledger append ----------------------------------------------------------


class TestAppendScopeGrant:
    def test_same_head_and_paths_replaces_older_entry(self) -> None:
        first = _grant(minutes=1)
        again = _grant(minutes=2)
        kept = append_scope_grant([first], again)
        assert kept == [again]

    def test_evicts_oldest_beyond_cap(self) -> None:
        grants: list[ScopeGrant] = []
        for i in range(MAX_SCOPE_GRANTS + 2):
            grants = append_scope_grant(grants, _grant(paths=(f"f{i}.py",), minutes=i))
        assert len(grants) == MAX_SCOPE_GRANTS
        assert grants[0].paths == ["f2.py"]
        assert grants[-1].paths == [f"f{MAX_SCOPE_GRANTS + 1}.py"]


# --- verification ----------------------------------------------------------


class TestVerifyScopeGrants:
    def test_single_grant_on_head_is_carried(self) -> None:
        ancestry = _ancestry({(_H1, "HEAD"): True})
        carried = verify_scope_grants([_grant()], _ctx(), is_ancestor=ancestry)
        assert carried.paths == ["src/a.py", "src/b.py"]
        assert carried.head == _H1
        assert carried.rejected == []

    def test_no_grants_yields_nothing(self) -> None:
        carried = verify_scope_grants([], _ctx(), is_ancestor=_ancestry({}))
        assert carried.head is None
        assert carried.paths == []

    def test_row_identity_mismatch_rejects(self) -> None:
        other = _grant().model_copy(update={"ticket_id": "T-9"})
        carried = verify_scope_grants(
            [other], _ctx(), is_ancestor=_ancestry({(_H1, "HEAD"): True})
        )
        assert carried.rejected == ["row identity differs"]
        assert carried.head is None

    def test_repo_unbound_rejects(self) -> None:
        carried = verify_scope_grants(
            [_grant(repo=None)], _ctx(), is_ancestor=_ancestry({(_H1, "HEAD"): True})
        )
        assert carried.rejected == ["repository unbound"]

    def test_repo_unresolved_at_carry_rejects(self) -> None:
        carried = verify_scope_grants(
            [_grant()],
            _ctx(repo=None),
            is_ancestor=_ancestry({(_H1, "HEAD"): True}),
        )
        assert carried.rejected == ["repository unbound"]

    def test_repo_differs_case_insensitively_matches(self) -> None:
        carried = verify_scope_grants(
            [_grant(repo="Acme/Widgets")],
            _ctx(repo="acme/widgets"),
            is_ancestor=_ancestry({(_H1, "HEAD"): True}),
        )
        assert carried.head == _H1

    def test_repo_differs_rejects(self) -> None:
        carried = verify_scope_grants(
            [_grant(repo="acme/other")],
            _ctx(),
            is_ancestor=_ancestry({(_H1, "HEAD"): True}),
        )
        assert carried.rejected == ["repository differs"]

    def test_pr_both_known_and_differing_rejects(self) -> None:
        carried = verify_scope_grants(
            [_grant(pr_number=7)],
            _ctx(pr_number=8),
            is_ancestor=_ancestry({(_H1, "HEAD"): True}),
        )
        assert carried.rejected == ["pull request differs"]

    def test_pr_unknown_on_either_side_is_not_a_mismatch(self) -> None:
        ancestry = _ancestry({(_H1, "HEAD"): True})
        unknown_grant = verify_scope_grants(
            [_grant(pr_number=None)], _ctx(pr_number=8), is_ancestor=ancestry
        )
        unknown_row = verify_scope_grants(
            [_grant(pr_number=7)], _ctx(pr_number=None), is_ancestor=ancestry
        )
        assert unknown_grant.head == _H1
        assert unknown_row.head == _H1

    def test_policy_change_rejects(self) -> None:
        carried = verify_scope_grants(
            [_grant(policy="old")],
            _ctx(policy="new"),
            is_ancestor=_ancestry({(_H1, "HEAD"): True}),
        )
        assert carried.rejected == ["review policy changed"]

    def test_plan_fingerprint_changes_reject_including_none_edges(self) -> None:
        ancestry = _ancestry({(_H1, "HEAD"): True})
        cases = [
            (_grant(plan="X"), _ctx(plan="Y")),
            (_grant(plan=None), _ctx(plan="X")),
            (_grant(plan="X"), _ctx(plan=None)),
        ]
        for grant, ctx in cases:
            carried = verify_scope_grants([grant], ctx, is_ancestor=ancestry)
            assert carried.rejected == ["plan of record changed"]

    def test_plan_none_equals_none_binds(self) -> None:
        carried = verify_scope_grants(
            [_grant(plan=None)],
            _ctx(plan=None),
            is_ancestor=_ancestry({(_H1, "HEAD"): True}),
        )
        assert carried.head == _H1

    def test_non_exact_stored_paths_reject(self) -> None:
        carried = verify_scope_grants(
            [_grant(paths=("src/*.py",))],
            _ctx(),
            is_ancestor=_ancestry({(_H1, "HEAD"): True}),
        )
        assert carried.rejected == ["grant paths are not exact"]

    def test_malformed_head_rejects_without_probing(self) -> None:
        carried = verify_scope_grants(
            [_grant(head="not-a-sha")], _ctx(), is_ancestor=_ancestry({})
        )
        assert carried.rejected == ["head is not a full commit id"]

    def test_head_not_ancestor_of_head_rejects(self) -> None:
        carried = verify_scope_grants(
            [_grant()], _ctx(), is_ancestor=_ancestry({(_H1, "HEAD"): False})
        )
        assert carried.rejected == ["head is not an ancestor of HEAD"]

    def test_unverifiable_ancestry_fails_closed(self) -> None:
        carried = verify_scope_grants(
            [_grant()], _ctx(), is_ancestor=_ancestry({(_H1, "HEAD"): None})
        )
        assert carried.rejected == ["ancestry unverifiable"]
        assert carried.head is None

    def test_older_grant_on_anchor_ancestry_is_unioned(self) -> None:
        ancestry = _ancestry(
            {
                (_H2, "HEAD"): True,
                (_H1, "HEAD"): True,
                (_H1, _H2): True,
            }
        )
        older = _grant(_H1, ("src/a.py",), minutes=1)
        newer = _grant(_H2, ("src/c.py",), minutes=2)
        carried = verify_scope_grants([older, newer], _ctx(), is_ancestor=ancestry)
        assert carried.head == _H2
        assert carried.paths == ["src/a.py", "src/c.py"]

    def test_older_grant_diverging_from_anchor_is_dropped(self) -> None:
        ancestry = _ancestry(
            {
                (_H2, "HEAD"): True,
                (_H1, "HEAD"): True,
                (_H1, _H2): False,
            }
        )
        older = _grant(_H1, ("src/a.py",), minutes=1)
        newer = _grant(_H2, ("src/c.py",), minutes=2)
        carried = verify_scope_grants([older, newer], _ctx(), is_ancestor=ancestry)
        assert carried.head == _H2
        assert carried.paths == ["src/c.py"]
        assert carried.rejected == ["head diverges from the anchor"]

    def test_newer_grant_not_on_head_is_dropped_and_older_becomes_anchor(self) -> None:
        ancestry = _ancestry({(_H1, "HEAD"): True, (_H3, "HEAD"): False})
        older = _grant(_H1, ("src/a.py",), minutes=1)
        newer = _grant(_H3, ("src/z.py",), minutes=2)
        carried = verify_scope_grants([older, newer], _ctx(), is_ancestor=ancestry)
        assert carried.head == _H1
        assert carried.paths == ["src/a.py"]
        assert carried.rejected == ["head is not an ancestor of HEAD"]


# --- parse_drift_extra_files -----------------------------------------------


class TestParseDriftExtraFiles:
    def test_reads_cw_park_format(self) -> None:
        details = "plan_scope_drift\nextra_files:\n- src/c.py\n- src/d.py\n"
        assert parse_drift_extra_files(details) == ["src/c.py", "src/d.py"]

    def test_garbage_yields_empty(self) -> None:
        assert parse_drift_extra_files("nothing here") == []

    def test_stops_at_first_non_item_line(self) -> None:
        details = "extra_files:\n- src/c.py\nother: x\n- src/d.py\n"
        assert parse_drift_extra_files(details) == ["src/c.py"]

    def test_caps_entries(self) -> None:
        items = "\n".join(f"- f{i}.py" for i in range(MAX_GRANT_PATHS + 5))
        result = parse_drift_extra_files(f"extra_files:\n{items}")
        assert len(result) == MAX_GRANT_PATHS
