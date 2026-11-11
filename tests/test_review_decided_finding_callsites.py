"""One test per ``decided_finding`` / ``decide_verdict`` call-site class (#3216, R20).

``decided_finding`` gained an engine-sourced branch: a gating engine finding on
a never-deferrable rule (``coverage.*`` / ``acceptance.*``) is a ``fix``. Every
caller of ``decide_verdict`` therefore needs a recorded decision about whether
it can receive such a finding. The classes below pin each call site, and
:class:`TestCallSiteDriftGuard` fails when a new call site lands without a
classification here.

Call-site classes (post-change set, MUST_FIX 4):

- confirm, both lanes: ``prepare_engine_confirm`` (claude-native) and
  ``decide_confirm_verdict`` (codex): unchanged, a ``coverage.*`` /
  ``acceptance.*`` finding still ends ``operator_actionable`` whatever its source;
- loop: ``codex_loop_rounds._round_findings``: intentionally changed, an engine
  ``coverage.*`` SHOULD_FIX is a recorded ``fix``, not a ``defer``;
- flag: ``converge._apply_defer_should_fix`` (``--defer-should-fix``):
  intentionally changed, the same engine finding stays gating;
- delta: ``converge_delta._record``: unreachable for an engine finding, the
  delta verdict is built by built-in reviewers, so a built-in look-alike is
  unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cli.review.converge import _apply_defer_should_fix
from cw.cli.review.converge_confirm import prepare_engine_confirm
from cw.cli.review.converge_delta import delta_defer_rationale
from cw.codex_clean_confirm import CONFIRM_DEFER_RATIONALE, LOOP_DEFER_RATIONALE
from cw.codex_fix_loop.rounds import CycleRound
from cw.codex_loop_rounds import _round_findings
from cw.review_convergence import (
    CONFIRM_ENGINE_DEFER_RATIONALE,
    ConvergenceLedger,
    RoundInput,
    decide_confirm_verdict,
    decide_verdict,
    decided_finding,
    from_verdict,
)
from cw.review_findings import AcceptedFinding, ReviewVerdict
from tests._codex_review_helpers import _loop_verdict
from tests._review_convergence_helpers import (
    DEFAULT_SHA,
    engine_confirm_payload,
    engine_confirm_verdict,
    engine_pass_ledger,
    record,
)
from tests.conftest import _make_finding

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.review_findings import Severity

_SRC = Path(__file__).resolve().parent.parent / "src" / "cw"
_COVERAGE = "coverage.untested-branch"
_ACCEPTANCE = "acceptance.ac-1"
_NEVER_DEFERRABLE = (_COVERAGE, _ACCEPTANCE)
_PLAIN = "style.naming"
_SHOULD_FIX_SUMMARY = "plain engine finding"
_RULE_SUMMARY = "never deferrable finding"


def _with_source(verdict: ReviewVerdict, source: str) -> ReviewVerdict:
    """*verdict* with every accepted finding restamped to *source*.

    An engine finding carries a fingerprint, as the aggregation turn stamps one.
    """
    accepted = [
        item.model_copy(
            update={
                "finding": item.finding.model_copy(
                    update={"source": source, "fingerprint": f"fp-{index}"}
                    if source == "engine"
                    else {"source": source}
                )
            }
        )
        for index, item in enumerate(verdict.accepted)
    ]
    return verdict.model_copy(update={"accepted": accepted})


def _verdict(
    source: str,
    *,
    should_fix: tuple[str, ...] = (),
    must_fix: tuple[str, ...] = (),
) -> ReviewVerdict:
    """A verdict whose findings are named by their rule (``summary`` is the rule)."""
    base = engine_confirm_verdict(
        must_fix=[(f"mf {rule}", rule) for rule in must_fix],
        should_fix=[(f"sf {rule}", rule) for rule in should_fix],
    )
    return _with_source(base, source)


def _outcomes(verdict: ReviewVerdict) -> list[str]:
    findings, _unread = from_verdict(verdict)
    return [finding.outcome for finding in findings]


def _claude_native_confirm(
    verdict: ReviewVerdict, ledger: ConvergenceLedger
) -> ReviewVerdict:
    payload = prepare_engine_confirm(
        RoundInput.model_validate(engine_confirm_payload(verdict)), ledger
    )
    assert payload.verdict is not None
    return payload.verdict


def _codex_confirm(verdict: ReviewVerdict, ledger: ConvergenceLedger) -> ReviewVerdict:
    return decide_confirm_verdict(verdict, ledger, CONFIRM_DEFER_RATIONALE)


_LANES: dict[str, Callable[[ReviewVerdict, ConvergenceLedger], ReviewVerdict]] = {
    "claude-native": _claude_native_confirm,
    "codex": _codex_confirm,
}


@pytest.fixture(params=sorted(_LANES))
def decide_confirm(
    request: pytest.FixtureRequest,
) -> Callable[[ReviewVerdict, ConvergenceLedger], ReviewVerdict]:
    lane: str = request.param
    return _LANES[lane]


@pytest.fixture(params=["builtin", "engine"])
def source(request: pytest.FixtureRequest) -> str:
    chosen: str = request.param
    return chosen


class TestConfirmPath:
    """Both lanes decide a confirm round after an engine pass identically."""

    @pytest.mark.parametrize("rule", _NEVER_DEFERRABLE)
    def test_a_never_deferrable_should_fix_parks_whatever_its_source(
        self,
        decide_confirm: Callable[[ReviewVerdict, ConvergenceLedger], ReviewVerdict],
        source: str,
        rule: str,
    ) -> None:
        ledger = engine_pass_ledger()
        decided = decide_confirm(_verdict(source, should_fix=(rule,)), ledger)

        (item,) = decided.accepted
        assert item.disposition == "operator_actionable"
        assert _outcomes(decided) == ["operator_action"]
        _, report = record(
            ledger, *from_verdict(decided)[0], sha=DEFAULT_SHA, kind="confirm"
        )
        assert report.decision == "stop_confirm_must_fix"

    @pytest.mark.parametrize("rule", _NEVER_DEFERRABLE)
    def test_a_never_deferrable_must_fix_parks_whatever_its_source(
        self,
        decide_confirm: Callable[[ReviewVerdict, ConvergenceLedger], ReviewVerdict],
        source: str,
        rule: str,
    ) -> None:
        decided = decide_confirm(
            _verdict(source, must_fix=(rule,)), engine_pass_ledger()
        )

        assert _outcomes(decided) == ["operator_action"]

    def test_a_plain_should_fix_is_still_deferred(
        self,
        decide_confirm: Callable[[ReviewVerdict, ConvergenceLedger], ReviewVerdict],
        source: str,
    ) -> None:
        decided = decide_confirm(
            _verdict(source, should_fix=(_PLAIN,)), engine_pass_ledger()
        )

        (item,) = decided.accepted
        assert item.disposition == "deferred"
        assert item.disposition_detail == CONFIRM_ENGINE_DEFER_RATIONALE
        assert _outcomes(decided) == ["defer"]

    def test_engine_and_builtin_sources_record_the_same_outcomes(
        self,
        decide_confirm: Callable[[ReviewVerdict, ConvergenceLedger], ReviewVerdict],
    ) -> None:
        def _outcomes_for(source: str) -> list[str]:
            verdict = _verdict(
                source, should_fix=(_COVERAGE, _ACCEPTANCE, _PLAIN), must_fix=(_PLAIN,)
            )
            return _outcomes(decide_confirm(verdict, engine_pass_ledger()))

        assert _outcomes_for("engine") == _outcomes_for("builtin")

    def test_a_confirm_without_an_engine_pass_keeps_the_default_decision(self) -> None:
        """No engine pass before it: ``decide_verdict`` with the lane's rationale."""
        ledger = engine_pass_ledger(engine=False)
        verdict = _verdict("builtin", should_fix=(_COVERAGE, _PLAIN))

        decided = decide_confirm_verdict(verdict, ledger, CONFIRM_DEFER_RATIONALE)

        assert decided == decide_verdict(verdict, CONFIRM_DEFER_RATIONALE)
        assert {item.disposition_detail for item in decided.accepted} == {
            CONFIRM_DEFER_RATIONALE
        }
        assert _outcomes(decided) == ["defer", "defer"]


def _should_fix(
    summary: str, *, source: str, rule_id: str | None, severity: Severity = "SHOULD_FIX"
) -> AcceptedFinding:
    extra: dict[str, object] = {"source": source}
    if source == "engine":
        extra["fingerprint"] = f"fp-{summary.replace(' ', '-')}"
    return AcceptedFinding(
        finding=_make_finding(
            severity=severity,
            summary=summary,
            file="src/cw/producer.py",
            rule_id=rule_id,
            **extra,
        ),
        reviewers=["Code Quality Reviewer"],
    )


class TestLoopPath:
    """``codex_loop_rounds._round_findings``: the engine loop round."""

    @staticmethod
    def _findings(*accepted: AcceptedFinding) -> dict[str, tuple[str, str]]:
        cycle = CycleRound(0, _loop_verdict(*accepted, reviewed_sha="sha"), {})
        return {
            finding.summary: (finding.outcome, finding.rationale)
            for finding in _round_findings(cycle, frozenset())
        }

    @pytest.mark.parametrize("rule", _NEVER_DEFERRABLE)
    def test_an_engine_never_deferrable_should_fix_is_recorded_as_a_fix(
        self, rule: str
    ) -> None:
        recorded = self._findings(
            _should_fix("engine rule", source="engine", rule_id=rule)
        )

        assert recorded["engine rule"][0] == "fix"

    def test_a_plain_engine_should_fix_still_defers_with_the_loop_rationale(
        self,
    ) -> None:
        recorded = self._findings(
            _should_fix(_SHOULD_FIX_SUMMARY, source="engine", rule_id=_PLAIN)
        )

        assert recorded[_SHOULD_FIX_SUMMARY] == ("defer", LOOP_DEFER_RATIONALE)

    def test_a_builtin_look_alike_rule_still_defers(self) -> None:
        recorded = self._findings(
            _should_fix("builtin rule", source="builtin", rule_id=_COVERAGE)
        )

        assert recorded["builtin rule"] == ("defer", LOOP_DEFER_RATIONALE)


class TestDeferShouldFixFlag:
    """``--defer-should-fix`` over a loop-round verdict (``converge.py``)."""

    @staticmethod
    def _decided(verdict: ReviewVerdict) -> dict[str, str]:
        payload = RoundInput.model_validate(
            {**engine_confirm_payload(verdict), "kind": "loop"}
        )
        decided = _apply_defer_should_fix(payload).verdict
        assert decided is not None
        return {item.finding.summary: item.disposition for item in decided.accepted}

    @pytest.mark.parametrize("rule", _NEVER_DEFERRABLE)
    def test_an_engine_never_deferrable_should_fix_stays_gating(
        self, rule: str
    ) -> None:
        decided = self._decided(_verdict("engine", should_fix=(rule,)))

        assert decided == {f"sf {rule}": "fixed"}

    def test_builtin_and_plain_engine_should_fix_defer_as_before(self) -> None:
        builtin = self._decided(_verdict("builtin", should_fix=(_COVERAGE,)))
        plain = self._decided(_verdict("engine", should_fix=(_PLAIN,)))

        assert builtin == {f"sf {_COVERAGE}": "deferred"}
        assert plain == {f"sf {_PLAIN}": "deferred"}


class TestDeltaPath:
    """``converge_delta._record``: only built-in findings can reach it."""

    @pytest.mark.parametrize("rule", _NEVER_DEFERRABLE)
    def test_a_builtin_look_alike_rule_is_unchanged(self, rule: str) -> None:
        verdict = _verdict("builtin", should_fix=(rule,))
        rationale = delta_defer_rationale("claude-native")

        decided = decide_verdict(verdict, rationale)

        (item,) = decided.accepted
        assert item.finding.source == "builtin"
        assert item.disposition == "deferred"
        assert item.disposition_detail == rationale
        assert decided_finding(verdict.accepted[0], rationale) == item


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------

_TRACKED = frozenset({"decided_finding", "decide_verdict", "decide_confirm_verdict"})

#: The post-change call-site set: file (relative to ``src/cw``) -> the tracked
#: functions it calls, with the class the test above pins it under.
_CLASSIFIED: dict[str, frozenset[str]] = {
    # decide_verdict itself maps decided_finding over the accepted findings.
    "review_convergence/_findings.py": frozenset({"decided_finding"}),
    # confirm class: decide_confirm_verdict calls decide_verdict (TestConfirmPath).
    "review_convergence/_confirm_route.py": frozenset({"decide_verdict"}),
    # confirm class, claude-native thin wrapper (TestConfirmPath).
    "cli/review/converge_confirm.py": frozenset({"decide_confirm_verdict"}),
    # confirm class, codex confirm round (TestConfirmPath).
    "codex_clean_confirm.py": frozenset({"decide_confirm_verdict"}),
    # loop class (TestLoopPath).
    "codex_loop_rounds.py": frozenset({"decided_finding"}),
    # flag class (TestDeferShouldFixFlag).
    "cli/review/converge.py": frozenset({"decide_verdict"}),
    # delta class, unreachable for engine findings (TestDeltaPath).
    "cli/review/converge_delta.py": frozenset({"decide_verdict"}),
}


def _called_tracked_names(path: Path) -> frozenset[str]:
    """The tracked functions *path* actually calls.

    An AST walk, so a docstring or comment mention (``codex_confirm_feedback.py``,
    ``codex_engine_confirm.py``), an import or re-export
    (``review_convergence/__init__.py``) and a ``def`` are not call sites. Both
    ``name(...)`` and ``module.name(...)`` calls count.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if called in _TRACKED:
            names.add(called)
    return frozenset(names)


class TestCallSiteDriftGuard:
    def test_every_call_site_under_src_is_classified(self) -> None:
        found = {
            path.relative_to(_SRC).as_posix(): names
            for path in sorted(_SRC.rglob("*.py"))
            if (names := _called_tracked_names(path))
        }

        assert found == _CLASSIFIED, (
            "a decided_finding/decide_verdict/decide_confirm_verdict call site "
            "was added or removed: classify it in this file (can it receive an "
            "engine-sourced finding? what outcome?) and add a test for its class"
        )

    def test_the_scan_excludes_docstring_and_comment_mentions(self) -> None:
        """Pins the guard's scope: these files name a tracked function in prose only."""
        for relative in (
            "codex_confirm_feedback.py",
            "codex_engine_confirm.py",
            "review_convergence/__init__.py",
        ):
            text = (_SRC / relative).read_text(encoding="utf-8")
            assert any(name in text for name in _TRACKED), relative
            assert _called_tracked_names(_SRC / relative) == frozenset(), relative
