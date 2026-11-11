"""End-to-end engine round scenarios through the real CLI chain (#3039).

Drives ``workspace init`` -> ``engine materialize`` -> stub engine -> ``engine
record`` -> ``workspace reconcile`` -> ``consolidate`` -> ``converge suppress``
-> ``adjudicate`` -> ``converge record-round``. No Skill runs: ``StubEngine``
writes the summary file the skill would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.review_engine.constants import ENGINE_ROLE
from tests._review_engine_helpers import (
    EngineRepo,
    StubEngine,
    engine_context_payload,
    engine_finding,
    invoke,
    make_engine_repo,
    make_summary,
    seed_row,
    write_engine_context,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

    from cw.models import ClientConfig

BACKEND = "claude-native"
SKILL_FILE = ".claude/skills/repo-review/SKILL.md"
PARKED = "review_engine_modified"
OPERATOR = "mattwwarren"


class Flow:
    """One ticket's engine rounds against one worktree."""

    def __init__(self, repo: EngineRepo) -> None:
        self.repo = repo
        self.worktree = str(repo.worktree)
        self.stub = StubEngine(repo)
        self.rounds = 0
        self.init_result: dict[str, Any] = {}

    # -- plumbing ---------------------------------------------------------

    def cli(self, *args: str, payload: object | None = None) -> Result:
        text = None if payload is None else json.dumps(payload)
        return invoke(*args, input_text=text)

    def ok(self, *args: str, payload: object | None = None) -> dict[str, Any]:
        result = self.cli(*args, payload=payload)
        assert result.exit_code == 0, f"{args}: {result.output}"
        body: dict[str, Any] = json.loads(result.stdout)
        return body

    @property
    def round_id(self) -> str:
        return f"r{self.rounds}"

    # -- steps ------------------------------------------------------------

    def converge_init(self) -> dict[str, Any]:
        return self.ok(
            "review",
            "converge",
            "init",
            "--ticket-id",
            self.repo.ticket,
            "--fork-point",
            self.repo.diff_base,
            "--reviewed-sha",
            self.repo.head,
        )

    def start_round(self) -> None:
        """Open the next workspace round with exactly the engine role."""
        if self.rounds:
            self.ok(
                "review", "workspace", "finish", "--status", "completed",
                "--worktree", self.worktree,
            )  # fmt: skip
        self.rounds += 1
        self.init_result = self.ok(
            "review",
            "workspace",
            "init",
            "--ticket",
            self.repo.ticket,
            "--round-id",
            self.round_id,
            "--role",
            ENGINE_ROLE,
            "--worktree",
            self.worktree,
        )

    def materialize(self) -> Result:
        return self.cli(
            "review",
            "engine",
            "materialize",
            "--round-id",
            self.round_id,
            "--expect-head",
            self.repo.head,
            "--worktree",
            self.worktree,
        )

    def record(self) -> Result:
        return self.cli(
            "review",
            "engine",
            "record",
            "--round-id",
            self.round_id,
            "--expect-head",
            self.repo.head,
            "--worktree",
            self.worktree,
        )

    def record_engine(self, summary: dict[str, object] | bytes | None) -> Result:
        """init -> materialize -> stub engine -> record."""
        self.start_round()
        assert self.materialize().exit_code == 0
        self.stub.run(self.round_id, summary)
        return self.record()

    def consolidate(self) -> dict[str, Any]:
        self.ok("review", "workspace", "reconcile", "--worktree", self.worktree)
        diff = git_in(self.repo.worktree, "diff", f"{self.repo.diff_base}...HEAD")
        return self.ok(
            "review",
            "consolidate",
            "-",
            "--documents-from",
            str(self.repo.worktree / ".cw" / "review-findings"),
            "--no-base-check",
            "--worktree",
            self.worktree,
            payload={"diff": diff + "\n", "reviewed_sha": self.repo.head},
        )

    def round_to_verdict(self, summary: dict[str, object]) -> dict[str, Any]:
        result = self.record_engine(summary)
        assert result.exit_code == 0, result.output
        return self.consolidate()

    def suppress(self, verdict: dict[str, Any]) -> dict[str, Any]:
        return self.ok(
            "review", "converge", "suppress", "-", payload={"verdict": verdict}
        )

    def adjudicate(
        self, verdict: dict[str, Any], adjudications: list[dict[str, Any]]
    ) -> Result:
        return self.cli(
            "review",
            "adjudicate",
            "-",
            payload={"verdict": verdict, "adjudications": adjudications},
        )

    def record_round(self, verdict: dict[str, Any]) -> dict[str, Any]:
        return self.ok(
            "review",
            "converge",
            "record-round",
            "-",
            payload={
                "kind": "loop",
                "backend": BACKEND,
                "reviewed_sha": self.repo.head,
                "roster": [ENGINE_ROLE],
                "verdict": verdict,
            },
        )

    def full_round(
        self, summary: dict[str, object], decisions: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run a whole round; *decisions* maps fingerprint -> adjudication outcome.

        Returns ``(suppress_body, round_report)``.
        """
        verdict = self.round_to_verdict(summary)
        suppressed = self.suppress(verdict)
        rows = list(suppressed["adjudications"])
        stamped = {row["fingerprint"] for row in rows}
        for finding in verdict["accepted"]:
            fingerprint = finding["finding"]["fingerprint"]
            if fingerprint in stamped or fingerprint not in decisions:
                continue
            rows.append(adjudication(finding["finding"], decisions[fingerprint]))
        result = self.adjudicate(suppressed["verdict"], rows)
        assert result.exit_code == 0, result.output
        report = self.record_round(json.loads(result.stdout))
        return suppressed, report


def adjudication(
    finding: dict[str, Any], outcome: str, *, non_deferrable: bool = False
) -> dict[str, Any]:
    """The adjudication row deciding *finding* (an engine finding dump)."""
    row: dict[str, Any] = {
        "severity": finding["severity"],
        "file": finding["file"],
        "outcome": outcome,
        "rationale": f"decided {outcome}",
        "fingerprint": finding["fingerprint"],
    }
    if non_deferrable:
        row["non_deferrable"] = True
    return row


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo)


@pytest.fixture(autouse=True)
def _operator_in_worktree(
    repo: EngineRepo,
    monkeypatch: pytest.MonkeyPatch,
    sample_client_config: ClientConfig,
) -> None:
    """Run from the worktree (its context says ``headless: false``) as the operator."""
    monkeypatch.chdir(repo.worktree)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: OPERATOR)
    monkeypatch.setattr(
        "cw.dev_queue.review_engine_allow.get_client",
        lambda _name: sample_client_config,
    )


@pytest.fixture
def flow(repo: EngineRepo) -> Flow:
    seed_row(repo)
    flow = Flow(repo)
    flow.converge_init()
    return flow


def _must_fix(fingerprint: str = "fp-must-0001", **kw: object) -> dict[str, object]:
    return engine_finding("MUST_FIX", fingerprint=fingerprint, **kw)


def _should_fix(fingerprint: str = "fp-should-001", **kw: object) -> dict[str, object]:
    return engine_finding("SHOULD_FIX", fingerprint=fingerprint, **kw)


# ---------------------------------------------------------------------------
# Happy paths and failed rounds
# ---------------------------------------------------------------------------


def test_pass_round_flows_to_record_round(flow: Flow) -> None:
    verdict = flow.round_to_verdict(make_summary())

    assert verdict["blocking"] is False
    assert verdict["accepted"] == []
    suppressed = flow.suppress(verdict)
    assert suppressed["suppressed"] == 0
    result = flow.adjudicate(suppressed["verdict"], [])
    assert result.exit_code == 0, result.output
    report = flow.record_round(json.loads(result.stdout))
    assert report["new_accepted"] == 0
    assert report["decision"] != "stop"


def test_fail_round_blocks_until_adjudicated(flow: Flow) -> None:
    summary = make_summary([_must_fix()])

    verdict = flow.round_to_verdict(summary)

    assert verdict["blocking"] is True
    assert len(verdict["must_fix"]) == 1
    _suppressed, report = flow.full_round(summary, {"fp-must-0001": "fix"})
    assert report["decision"] == "continue"
    assert report["new_accepted"] == 1


def test_built_in_roster_never_runs(flow: Flow) -> None:
    verdict = flow.round_to_verdict(make_summary([_should_fix()]))

    assert flow.init_result["pending_roles"] == [ENGINE_ROLE]
    assert [a["reviewer_role"] for a in verdict["agents_run"]] == [ENGINE_ROLE]
    assert verdict["review"]["agents_run"] == 1
    marker = json.loads(
        (flow.repo.worktree / ".cw/review-findings-round.json").read_text()
    )
    assert [r["role"] for r in marker["SPAWNED_ROLES"]] == [ENGINE_ROLE]


def test_incomplete_coverage_fails_the_round(flow: Flow) -> None:
    result = flow.record_engine(make_summary(coverage="PARTIAL"))

    assert result.exit_code == 3
    assert json.loads(result.stdout)["reason"] == "engine_coverage_incomplete"
    reconciled = flow.ok(
        "review", "workspace", "reconcile", "--worktree", flow.worktree
    )
    assert [f["reason"] for f in reconciled["failed_reviewers"]] == [
        "engine_coverage_incomplete"
    ]
    refused = flow.cli(
        "review", "workspace", "finish", "--status", "completed",
        "--worktree", flow.worktree,
    )  # fmt: skip
    assert refused.exit_code == 1
    failed = flow.cli(
        "review", "workspace", "finish", "--status", "failed",
        "--worktree", flow.worktree,
    )  # fmt: skip
    assert failed.exit_code == 0, failed.output


def test_invalid_summary_fails_the_round(flow: Flow) -> None:
    result = flow.record_engine(b"{not json")

    assert result.exit_code == 3
    body = json.loads(result.stdout)
    assert body["reason"] == "engine_summary_invalid"
    assert "not_json" in body["friction"]


def test_head_mismatch_fails_the_round(flow: Flow) -> None:
    result = flow.record_engine(make_summary(head_sha="b" * 40))

    assert result.exit_code == 3
    assert json.loads(result.stdout)["reason"] == "engine_head_mismatch"


def test_mutated_tree_fails_the_round(flow: Flow) -> None:
    flow.start_round()
    assert flow.materialize().exit_code == 0
    flow.stub.run(flow.round_id, make_summary(), mutate=True)

    result = flow.record()

    assert result.exit_code == 3
    assert json.loads(result.stdout)["reason"] == "engine_worktree_mutated"


# ---------------------------------------------------------------------------
# Preflight park, allow and guarded paths
# ---------------------------------------------------------------------------


def _preflight(repo: EngineRepo) -> Result:
    return invoke(
        "review",
        "engine",
        "preflight",
        "--diff-base",
        repo.diff_base,
        "--worktree",
        str(repo.worktree),
    )


def test_engine_modified_branch_parks_with_touched_paths(repo: EngineRepo) -> None:
    commit_tracked_file(repo.worktree, SKILL_FILE, "weakened\n")

    result = _preflight(repo)

    assert result.exit_code == 3
    body = json.loads(result.stdout)
    assert body["decision"] == "park"
    assert body["touched"] == [SKILL_FILE]


def test_allowed_head_runs_and_materializes_default_branch_copy(
    repo: EngineRepo,
) -> None:
    commit_tracked_file(repo.worktree, SKILL_FILE, "weakened\n")
    head = git_in(repo.worktree, "rev-parse", "HEAD")
    seed_row(repo, PARKED)
    assert _preflight(repo).exit_code == 3

    allowed = invoke(
        "review", "engine", "allow", "--ticket", repo.ticket, "--head-sha", head,
        "--reason", "operator read the skill edit", "-c", repo.client,
    )  # fmt: skip
    assert allowed.exit_code == 0, allowed.output

    result = _preflight(repo)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["reason"] == "allowed_head"
    moved = EngineRepo(repo.worktree, head, repo.diff_base, repo.ticket, repo.client)
    flow = Flow(moved)
    flow.start_round()
    materialized = flow.materialize()
    assert materialized.exit_code == 0, materialized.output
    skill_md = json.loads(materialized.stdout)["skill_md"]
    assert "weakened" not in Path(skill_md).read_text(encoding="utf-8")


def test_guarded_path_parks(make_git_repo: Callable[..., Path]) -> None:
    repo = make_engine_repo(make_git_repo, guarded=("policy/**",), name="guarded-repo")
    commit_tracked_file(repo.worktree, "policy/rules.yaml", "k: v\n")

    result = _preflight(repo)

    assert result.exit_code == 3
    assert json.loads(result.stdout)["touched"] == ["policy/rules.yaml"]


# ---------------------------------------------------------------------------
# Anchors, deferral and escalation
# ---------------------------------------------------------------------------


def test_anchors_outside_the_diff_are_kept_never_rejected(flow: Flow) -> None:
    findings = [
        _should_fix("fp-gone-0001", file="src/not_in_diff.py"),
        _should_fix("fp-line-0001", line=999),
        engine_finding("DEBT", fingerprint="fp-nofile-01", file=None),
    ]

    verdict = flow.round_to_verdict(make_summary(findings))

    assert verdict["rejected"] == []
    assert verdict["rejected_count"] == 0
    assert verdict["review"]["rejected_count"] == 0
    degraded = {
        f["finding"]["fingerprint"]: f["finding"]["anchor_degraded"]
        for f in verdict["accepted"]
    }
    assert degraded["fp-gone-0001"] is True
    assert degraded["fp-line-0001"] is True


def test_deferred_finding_reraised_next_round_is_resurfaced_not_new(
    flow: Flow,
) -> None:
    first = make_summary([_should_fix("fp-defer-001"), _must_fix("fp-fix-00001")])
    _s, report = flow.full_round(
        first, {"fp-defer-001": "defer", "fp-fix-00001": "fix"}
    )
    assert report["decision"] == "continue"

    second = make_summary([_should_fix("fp-defer-001")])
    suppressed, report = flow.full_round(second, {})

    assert suppressed["suppressed"] == 1
    assert suppressed["adjudications"][0]["outcome"] == "defer"
    stamped = suppressed["verdict"]["accepted"][0]
    assert stamped["disposition"] == "deferred"
    assert report["resurfaced_settled"] == 1
    assert report["new_accepted"] == 0
    assert report["decision"] != "stop"


def test_escalation_to_must_fix_is_new_and_cannot_be_deferred(flow: Flow) -> None:
    first = make_summary([_should_fix("fp-esc-00001"), _must_fix("fp-fix-00001")])
    flow.full_round(first, {"fp-esc-00001": "defer", "fp-fix-00001": "fix"})

    escalated = make_summary([_must_fix("fp-esc-00001")])
    verdict = flow.round_to_verdict(escalated)
    suppressed = flow.suppress(verdict)

    assert suppressed["suppressed"] == 0
    finding = verdict["accepted"][0]["finding"]
    refused = flow.adjudicate(suppressed["verdict"], [adjudication(finding, "defer")])
    assert refused.exit_code == 1
    assert "engine_must_fix_defer_requires_operator" in refused.output
    assert "fp-esc-00001" in refused.output
    assert "src/a.py" in refused.output

    accepted = flow.adjudicate(
        suppressed["verdict"], [adjudication(finding, "operator_action")]
    )
    assert accepted.exit_code == 0, accepted.output
    report = flow.record_round(json.loads(accepted.stdout))
    assert report["decision"] == "stop_operator_action"
    status = flow.ok("review", "converge", "status")
    assert status["next_action"] == "stopped"
    assert status["stop"]["reason"] == "operator_action"


def test_operator_defer_accepts_the_must_fix_reraise(flow: Flow) -> None:
    first = make_summary([_should_fix("fp-op-000001"), _must_fix("fp-fix-00001")])
    flow.full_round(first, {"fp-op-000001": "defer", "fp-fix-00001": "fix"})
    escalated = make_summary([_must_fix("fp-op-000001", summary="escalated issue")])
    verdict = flow.round_to_verdict(escalated)
    suppressed = flow.suppress(verdict)
    finding = verdict["accepted"][0]["finding"]
    parked = flow.adjudicate(
        suppressed["verdict"], [adjudication(finding, "operator_action")]
    )
    flow.record_round(json.loads(parked.stdout))

    settled = flow.cli(
        "review", "converge", "operator-settle", "-",
        "--ticket-id", flow.repo.ticket, "-c", flow.repo.client,
        "--reason", "operator accepts this MUST_FIX", "--outcome", "defer",
        payload={
            "entries": [
                {
                    "severity": "MUST_FIX",
                    "file": finding["file"],
                    "summary": finding["summary"],
                    "fingerprint": finding["fingerprint"],
                }
            ]
        },
    )  # fmt: skip
    assert settled.exit_code == 0, settled.output
    released = flow.converge_init()
    assert released["status"] == "in_progress"

    suppressed, report = flow.full_round(escalated, {})

    assert suppressed["suppressed"] == 1
    assert suppressed["adjudications"][0]["outcome"] == "defer"
    assert report["resurfaced_settled"] == 1
    assert report["new_accepted"] == 0
    assert report["decision"] != "stop_operator_action"


def test_reraise_at_lower_severity_after_operator_defer_stays_deferred(
    flow: Flow,
) -> None:
    must = make_summary([_must_fix("fp-low-000001")])
    verdict = flow.round_to_verdict(must)
    suppressed = flow.suppress(verdict)
    finding = verdict["accepted"][0]["finding"]
    parked = flow.adjudicate(
        suppressed["verdict"], [adjudication(finding, "operator_action")]
    )
    flow.record_round(json.loads(parked.stdout))
    settled = flow.cli(
        "review", "converge", "operator-settle", "-",
        "--ticket-id", flow.repo.ticket, "-c", flow.repo.client,
        "--reason", "accept", "--outcome", "defer",
        payload={
            "entries": [
                {
                    "severity": "MUST_FIX",
                    "file": finding["file"],
                    "summary": finding["summary"],
                    "fingerprint": finding["fingerprint"],
                }
            ]
        },
    )  # fmt: skip
    assert settled.exit_code == 0, settled.output
    flow.converge_init()

    lower = make_summary([_should_fix("fp-low-000001")])
    suppressed, report = flow.full_round(lower, {})

    assert suppressed["suppressed"] == 1
    assert report["resurfaced_settled"] == 1
    assert report["new_accepted"] == 0


def test_reject_entry_for_an_engine_finding_exits_1(flow: Flow) -> None:
    verdict = flow.round_to_verdict(make_summary([_should_fix("fp-rej-000001")]))
    suppressed = flow.suppress(verdict)
    finding = verdict["accepted"][0]["finding"]

    result = flow.adjudicate(suppressed["verdict"], [adjudication(finding, "reject")])

    assert result.exit_code == 1
    assert "engine_reject_forbidden" in result.output
    assert "fp-rej-000001" in result.output
    assert "src/a.py" in result.output


def test_coverage_finding_cannot_be_deferred(flow: Flow) -> None:
    summary = make_summary(
        [
            engine_finding(
                "SHOULD_FIX", rule_id="coverage.tests", fingerprint="fp-cov-00001"
            )
        ]
    )
    verdict = flow.round_to_verdict(summary)
    suppressed = flow.suppress(verdict)
    finding = verdict["accepted"][0]["finding"]

    result = flow.adjudicate(suppressed["verdict"], [adjudication(finding, "defer")])

    assert result.exit_code == 1
    assert "engine_non_deferrable" in result.output


# ---------------------------------------------------------------------------
# Other lanes are unchanged
# ---------------------------------------------------------------------------


def _config(*extra: str) -> dict[str, Any]:
    result = invoke("review", "converge", "config", *extra)
    assert result.exit_code == 0, result.output
    body: dict[str, Any] = json.loads(result.stdout)
    return body


@pytest.mark.parametrize("backend", ["opencode", "local"])
def test_other_lanes_print_no_engine(repo: EngineRepo, backend: str) -> None:
    body = _config("--loop-backend", backend)

    assert body["engine"] is None
    assert body["engine_error"] is None


def test_codex_lane_reports_the_engine(repo: EngineRepo) -> None:
    """#3216: a codex lane runs the engine, so converge config reports it."""
    body = _config("--loop-backend", "codex")

    assert body["engine"] == "repo-review"
    assert body["engine_error"] is None


def test_claude_native_lane_reports_the_engine(repo: EngineRepo) -> None:
    body = _config("--loop-backend", BACKEND)

    assert body["engine"] == "repo-review"
    assert body["engine_error"] is None


def test_malformed_engine_is_reported_never_dropped(repo: EngineRepo) -> None:
    payload = engine_context_payload(skill="repo-review")
    payload["review"] = {"convergence": "clean", "engine": {"skill": 7}}
    write_engine_context(repo.worktree, payload)

    body = _config("--loop-backend", BACKEND)

    assert body["engine"] is None
    assert body["engine_error"]


def test_context_without_engine_keeps_the_builtin_shape(repo: EngineRepo) -> None:
    write_engine_context(repo.worktree, engine_context_payload(skill=None))

    body = _config("--loop-backend", BACKEND)

    assert body["engine"] is None
    assert body["convergence"] == "clean"


def test_builtin_workspace_init_accepts_the_full_roster(tmp_path: Path) -> None:
    roles = [
        "Code Quality Reviewer",
        "Test Reviewer",
        "Architecture Reviewer",
        "Product Manager Reviewer",
    ]
    worktree = tmp_path / "builtin"
    worktree.mkdir()
    args = ["review", "workspace", "init", "--ticket", "T-9", "--round-id", "r1"]
    for role in roles:
        args += ["--role", role]

    result = invoke(*args, "--worktree", str(worktree))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["pending_roles"] == roles
    marker = json.loads((worktree / ".cw/review-findings-round.json").read_text())
    assert [entry["role"] for entry in marker["SPAWNED_ROLES"]] == roles
