"""Tests for ``cw review engine validate`` (#3036), through Click's ``CliRunner``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import click
import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.cli.review.engine import MAX_VALIDATE_ERROR_LINES
from cw.dev_queue import find_ticket_row
from cw.events import inbox_path
from cw.models import QueueItemStatus
from cw.models.review_engine_summary import (
    MAX_ENGINE_EVIDENCE_LENGTH,
    MAX_ENGINE_SUMMARY_BYTES,
)
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_engine.preflight import PreflightResult
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context
from tests._review_engine_helpers import (
    EngineRepo,
    StubEngine,
    copy_engine_fixture,
    engine_context_payload,
    engine_finding,
    make_engine_repo,
    make_finding,
    make_summary,
    pad_to,
    seed_row,
    summary_bytes,
    write_engine_context,
    write_summary,
)
from tests._review_engine_helpers import invoke as invoke_cw
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.workflow_text import _norm

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import ClientConfig

_EVIDENCE = "SENTINEL-EVIDENCE-41aa"
_SUMMARY = "SENTINEL-SUMMARY-77bb"


def _invoke(*args: str, stdin: str | None = None) -> Result:
    return CliRunner().invoke(
        main, ["review", "engine", "validate", *args], input=stdin
    )


def _stderr_lines(result: Result) -> list[str]:
    return result.stderr.splitlines()


def _bad_findings(count: int) -> dict[str, object]:
    return make_summary(
        [make_finding(fingerprint=f"fp-{i:06d}", file="") for i in range(count)]
    )


def test_valid_summary(tmp_path: Path) -> None:
    path = write_summary(tmp_path, make_summary([make_finding()]))

    result = _invoke(str(path))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "review engine summary valid: schema_version=1 verdict=PASS coverage=COMPLETE"
    ]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("build", "line_prefix"),
    [
        (lambda tmp: tmp / "absent.json", "unreadable: -: "),
        (lambda tmp: tmp, "unreadable: -: "),
        (
            lambda tmp: write_summary(
                tmp,
                pad_to(summary_bytes(make_summary()), MAX_ENGINE_SUMMARY_BYTES + 1),
            ),
            "oversized: -: ",
        ),
        (lambda tmp: write_summary(tmp, b"\xff\xfe{}"), "not_utf8: -: "),
        (lambda tmp: write_summary(tmp, b"{nope"), "not_json: -: "),
        (lambda tmp: write_summary(tmp, b"[1, 2]"), "not_object: -: "),
        (
            lambda tmp: write_summary(tmp, make_summary(schema_version=2)),
            "unsupported_schema_version: schema_version: ",
        ),
        (
            lambda tmp: write_summary(tmp, make_summary([make_finding(file="N/A")])),
            "schema_invalid: findings.0.file: ",
        ),
        (
            lambda tmp: write_summary(tmp, make_summary(verdict="FAIL", bogus=True)),
            "schema_invalid: bogus: ",
        ),
    ],
    ids=[
        "missing",
        "directory",
        "oversized",
        "not_utf8",
        "not_json",
        "not_object",
        "unsupported_schema_version",
        "schema_invalid",
        "schema_invalid-extra",
    ],
)
def test_rejection_reasons(tmp_path: Path, build: object, line_prefix: str) -> None:
    assert callable(build)
    path = build(tmp_path)

    result = _invoke(str(path))

    assert result.exit_code == 1
    assert result.stdout == ""
    lines = _stderr_lines(result)
    assert len(lines) == 1
    assert lines[0].startswith(line_prefix)


def test_inconsistent_duplicate_fingerprint(tmp_path: Path) -> None:
    path = write_summary(tmp_path, make_summary([make_finding(), make_finding()]))

    result = _invoke(str(path))

    assert result.exit_code == 1
    assert _stderr_lines(result) == [
        "inconsistent: findings.1.fingerprint: duplicate fingerprint 'fp-000001'"
    ]


def test_never_echoes_evidence_or_summary(tmp_path: Path) -> None:
    finding = make_finding(evidence=_EVIDENCE, summary=_SUMMARY, file="")
    path = write_summary(tmp_path, make_summary([finding]))

    result = _invoke(str(path))

    assert result.exit_code == 1
    assert _EVIDENCE not in result.output
    assert _SUMMARY not in result.output
    assert _EVIDENCE not in result.stderr
    assert _SUMMARY not in result.stderr


def test_never_echoes_oversized_evidence(tmp_path: Path) -> None:
    evidence = _EVIDENCE * (MAX_ENGINE_EVIDENCE_LENGTH // len(_EVIDENCE) + 1)
    finding = make_finding(evidence=evidence, summary=_SUMMARY)
    path = write_summary(tmp_path, make_summary([finding]))

    result = _invoke(str(path))

    assert result.exit_code == 1
    assert _stderr_lines(result)[0].startswith("schema_invalid: findings.0.evidence: ")
    assert _EVIDENCE not in result.stderr
    assert _SUMMARY not in result.stderr


@pytest.mark.parametrize(
    ("count", "trailer"),
    [
        (25, "... 5 more line(s) omitted"),
        (21, "... 1 more line(s) omitted"),
        (20, None),
    ],
)
def test_error_lines_truncated(tmp_path: Path, count: int, trailer: str | None) -> None:
    path = write_summary(tmp_path, _bad_findings(count))

    result = _invoke(str(path))

    assert result.exit_code == 1
    lines = _stderr_lines(result)
    issue_lines = [line for line in lines if line.startswith("schema_invalid: ")]
    assert len(issue_lines) == MAX_VALIDATE_ERROR_LINES == 20
    if trailer is None:
        assert len(lines) == MAX_VALIDATE_ERROR_LINES
    else:
        assert lines[-1] == trailer
        assert len(lines) == MAX_VALIDATE_ERROR_LINES + 1


def test_dash_is_not_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = _invoke("-", stdin=summary_bytes(make_summary()).decode())

    assert result.exit_code == 1
    assert _stderr_lines(result)[0].startswith("unreadable: -: ")


@pytest.mark.parametrize(
    "args",
    [("--json", "x.json"), (), ("a.json", "b.json")],
    ids=["json-flag", "no-file", "two-files"],
)
def test_usage_errors_exit_two(args: tuple[str, ...]) -> None:
    result = _invoke(*args)

    assert result.exit_code == 2


def test_emits_no_event_and_writes_nothing(tmp_path: Path) -> None:
    work = tmp_path / "engine-work"
    good = write_summary(work, make_summary())
    bad = write_summary(work, make_summary(bogus=1), name="bad.json")
    good_bytes, bad_bytes = good.read_bytes(), bad.read_bytes()
    before = sorted(work.rglob("*"))
    assert not inbox_path().exists()

    assert _invoke(str(good)).exit_code == 0
    assert _invoke(str(bad)).exit_code == 1

    assert sorted(work.rglob("*")) == before
    assert not inbox_path().exists()
    assert good.read_bytes() == good_bytes
    assert bad.read_bytes() == bad_bytes


def test_engine_group_help() -> None:
    result = CliRunner().invoke(main, ["review", "engine", "--help"])

    assert result.exit_code == 0
    help_text = _norm(result.output)
    assert "Validate and inspect repo review engine summaries (RFC 0017)." in help_text
    assert "validate" in help_text


def test_validate_help() -> None:
    result = CliRunner().invoke(main, ["review", "engine", "validate", "--help"])

    assert result.exit_code == 0
    assert (
        "Validate an engine summary JSON file against the cw-owned schema. "
        "Exits 0 when valid, 1 on any rejection."
    ) in _norm(result.output)


def test_review_help_lists_engine() -> None:
    result = CliRunner().invoke(main, ["review", "--help"])

    assert result.exit_code == 0
    assert "engine" in result.output


# ---------------------------------------------------------------------------
# preflight / materialize / record / allow (#3039)
# ---------------------------------------------------------------------------

_ROUND = "r1"
_SKILL_FILE = ".claude/skills/repo-review/SKILL.md"
_PARKED = "review_engine_modified"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo)


def _preflight(repo: EngineRepo, *extra: str) -> Result:
    return invoke_cw(
        "review",
        "engine",
        "preflight",
        "--diff-base",
        repo.diff_base,
        "--worktree",
        str(repo.worktree),
        *extra,
    )


def _init_round(repo: EngineRepo) -> None:
    result = invoke_cw(
        "review",
        "workspace",
        "init",
        "--ticket",
        repo.ticket,
        "--round-id",
        _ROUND,
        "--role",
        ENGINE_ROLE,
        "--worktree",
        str(repo.worktree),
    )
    assert result.exit_code == 0, result.output


def _materialize(repo: EngineRepo, *extra: str, head: str | None = None) -> Result:
    return invoke_cw(
        "review",
        "engine",
        "materialize",
        "--round-id",
        _ROUND,
        "--expect-head",
        head or repo.head,
        "--worktree",
        str(repo.worktree),
        *extra,
    )


def _record(repo: EngineRepo, *extra: str) -> Result:
    return invoke_cw(
        "review",
        "engine",
        "record",
        "--round-id",
        _ROUND,
        "--expect-head",
        repo.head,
        "--worktree",
        str(repo.worktree),
        *extra,
    )


def test_preflight_exit_0_run_prints_result_json(repo: EngineRepo) -> None:
    result = _preflight(repo)

    assert result.exit_code == 0, result.output
    parsed = PreflightResult.model_validate_json(result.stdout)
    assert (parsed.decision, parsed.touched) == ("run", [])


def test_preflight_exit_3_park_lists_touched(repo: EngineRepo) -> None:
    commit_tracked_file(repo.worktree, _SKILL_FILE, "edited\n")

    result = _preflight(repo)

    assert result.exit_code == 3, result.output
    parsed = PreflightResult.model_validate_json(result.stdout)
    assert parsed.decision == "park"
    assert parsed.touched == [_SKILL_FILE]


def test_preflight_exit_1_without_engine_in_context(repo: EngineRepo) -> None:
    write_engine_context(repo.worktree, engine_context_payload(skill=None))

    result = _preflight(repo)

    assert result.exit_code == 1
    assert "no review.engine" in result.output


def test_preflight_exit_1_when_default_branch_unresolved(repo: EngineRepo) -> None:
    result = _preflight(repo, "--default-branch", "no-such-branch")

    assert result.exit_code == 1
    assert "does not resolve" in result.output


def test_preflight_reads_an_absolute_context_path(
    repo: EngineRepo, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "ctx.json"
    elsewhere.write_text(
        json.dumps(engine_context_payload(skill="ghost")), encoding="utf-8"
    )

    result = _preflight(repo, "--context", str(elsewhere))

    assert result.exit_code == 1
    assert "ghost" in result.output


def test_materialize_exit_0_prints_paths(repo: EngineRepo) -> None:
    _init_round(repo)

    result = _materialize(repo)

    assert result.exit_code == 0, result.output
    assert Path(json.loads(result.stdout)["skill_md"]).is_file()


def test_materialize_exit_1_on_head_mismatch(repo: EngineRepo) -> None:
    _init_round(repo)

    assert _materialize(repo, head="d" * 40).exit_code == 1


def test_materialize_exit_3_when_skill_unresolvable(repo: EngineRepo) -> None:
    _init_round(repo)
    write_engine_context(repo.worktree, engine_context_payload(skill="ghost"))

    result = _materialize(repo)

    assert result.exit_code == 3
    assert json.loads(result.stdout)["cause"] == "skill_not_found"


def test_record_exit_0_recorded(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(_ROUND, make_summary([engine_finding("NIT")]))

    result = _record(repo)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == "recorded"


def test_record_exit_3_failed_round_recorded(repo: EngineRepo) -> None:
    _init_round(repo)
    StubEngine(repo).run(_ROUND, skip=True)

    result = _record(repo)

    assert result.exit_code == 3
    assert json.loads(result.stdout)["reason"] == "engine_no_summary"


def test_record_exit_1_without_workspace(repo: EngineRepo) -> None:
    StubEngine(repo).run(_ROUND, make_summary())

    assert _record(repo).exit_code == 1


@pytest.fixture
def operator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_client_config: ClientConfig,
) -> None:
    _enter_operator_machine(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "cw.dev_queue.review_engine_allow.get_client",
        lambda _name: sample_client_config,
    )


def _allow(repo: EngineRepo, head: str | None = None, *extra: str) -> Result:
    return invoke_cw(
        "review",
        "engine",
        "allow",
        "--ticket",
        repo.ticket,
        "--head-sha",
        head or repo.head,
        "--reason",
        "reviewed the skill edit",
        "-c",
        repo.client,
        *extra,
    )


@pytest.mark.usefixtures("operator")
def test_allow_exit_0_records_and_prints_json(repo: EngineRepo) -> None:
    seed_row(repo, _PARKED)

    result = _allow(repo)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ticket_id": repo.ticket,
        "client": repo.client,
        "head_sha": repo.head,
        "actor": "mattwwarren",
    }


@pytest.mark.usefixtures("operator")
def test_allow_accepts_the_long_client_flag(repo: EngineRepo) -> None:
    seed_row(repo, _PARKED)

    result = invoke_cw(
        "review",
        "engine",
        "allow",
        "--ticket",
        repo.ticket,
        "--head-sha",
        repo.head,
        "--reason",
        "ok",
        "--client",
        repo.client,
    )

    assert result.exit_code == 0, result.output


@pytest.mark.usefixtures("operator")
def test_allow_exit_1_when_row_not_parked(repo: EngineRepo) -> None:
    seed_row(repo)

    result = _allow(repo)

    assert result.exit_code == 1
    assert "review_engine_modified" in result.output


@pytest.mark.usefixtures("operator")
def test_allow_exit_1_on_short_head(repo: EngineRepo) -> None:
    seed_row(repo, _PARKED)

    result = _allow(repo, repo.head[:12])

    assert result.exit_code == 1
    assert "40-hex" in result.output


def test_allow_refused_inside_dispatch_worker(
    repo: EngineRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seed_row(repo, _PARKED)
    _write_session_context(tmp_path, headless=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TMPDIR", raising=False)

    result = _allow(repo)

    assert result.exit_code == 1
    assert "operator-only" in result.output


@pytest.mark.usefixtures("operator")
def test_allowed_head_then_preflight_runs(repo: EngineRepo) -> None:
    commit_tracked_file(repo.worktree, _SKILL_FILE, "edited\n")
    head = git_in(repo.worktree, "rev-parse", "HEAD")
    seed_row(repo, _PARKED)
    assert _preflight(repo).exit_code == 3

    assert _allow(repo, head).exit_code == 0

    result = _preflight(repo)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["reason"] == "allowed_head"
    row = find_ticket_row(repo.ticket, repo.client)
    assert row is not None
    assert row.status is QueueItemStatus.BLOCKED_ON_USER  # kept until requeued
    assert [a.head_sha for a in row.review_engine_allows] == [head]


_ENGINE_COMMANDS = {"validate", "preflight", "materialize", "record", "allow"}
_ENGINE_REFERENCE = re.compile(r"cw review engine ([a-z][a-z-]*)")
_REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "cw"


def test_help_text_every_engine_reference_resolves() -> None:
    sources = [
        *(_REPO_SRC / "review_engine").glob("*.py"),
        _REPO_SRC / "cli" / "review" / "engine.py",
        _REPO_SRC / "dev_queue" / "review_engine_allow.py",
        _REPO_SRC / "dev_queue" / "park_guidance.py",
    ]
    found = {
        match
        for source in sources
        for match in _ENGINE_REFERENCE.findall(source.read_text(encoding="utf-8"))
    }

    assert found
    assert found <= _ENGINE_COMMANDS


def test_engine_group_lists_every_command() -> None:
    result = CliRunner().invoke(main, ["review", "engine", "--help"])

    assert result.exit_code == 0
    for command in _ENGINE_COMMANDS:
        assert command in result.output


def test_every_client_option_also_accepts_short_c() -> None:
    engine_group = main.get_command(click.Context(main), "review")
    assert isinstance(engine_group, click.Group)
    engine = engine_group.get_command(click.Context(engine_group), "engine")
    assert isinstance(engine, click.Group)
    for command in engine.commands.values():
        for param in command.params:
            if isinstance(param, click.Option) and "--client" in param.opts:
                assert "-c" in param.opts, command.name


# ---------------------------------------------------------------------------
# validate --kind (#3216): the summary default and the manifest kind
# ---------------------------------------------------------------------------

_STUB_ENGINE = Path(__file__).parent / "fixtures" / "review_engine_stub"
_MANIFEST_OK = "review engine manifest valid: manifest_version=1 backends=codex roles=2"


def _stub_copy(tmp_path: Path, **manifest_overrides: object) -> Path:
    """A copy of the stub engine directory; returns its manifest path."""
    return (
        copy_engine_fixture(tmp_path, _STUB_ENGINE, **manifest_overrides)
        / "cw-engine.json"
    )


def test_kind_summary_equals_the_default(tmp_path: Path) -> None:
    good = write_summary(tmp_path, make_summary([make_finding()]))
    bad = write_summary(tmp_path, _bad_findings(3), name="bad.json")

    for path in (good, bad):
        default = _invoke(str(path))
        explicit = _invoke("--kind", "summary", str(path))

        assert explicit.exit_code == default.exit_code
        assert explicit.stdout == default.stdout
        assert explicit.stderr == default.stderr


def test_kind_manifest_success_line(tmp_path: Path) -> None:
    result = _invoke("--kind", "manifest", str(_stub_copy(tmp_path)))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [_MANIFEST_OK]
    assert result.stderr == ""


def test_kind_manifest_resolves_refs_relative_to_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _stub_copy(tmp_path)
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = _invoke("--kind", "manifest", str(manifest))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [_MANIFEST_OK]


def test_kind_manifest_ref_failure_names_the_field(tmp_path: Path) -> None:
    manifest = _stub_copy(tmp_path)
    (manifest.parent / "agents" / "quality.md").unlink()

    result = _invoke("--kind", "manifest", str(manifest))

    assert result.exit_code == 1
    assert result.stdout == ""
    lines = _stderr_lines(result)
    assert len(lines) == 1
    assert lines[0].startswith("path_invalid: roles.1.agent_spec: ")


@pytest.mark.parametrize(
    ("build", "line_prefix"),
    [
        (lambda tmp: tmp / "absent.json", "unreadable: -: "),
        (
            lambda tmp: _stub_copy(tmp, manifest_version=2),
            "unsupported_manifest_version: manifest_version: ",
        ),
        (
            lambda tmp: _stub_copy(tmp, backends=["opencode"]),
            "schema_invalid: backends.0: ",
        ),
        (
            lambda tmp: _stub_copy(tmp, schema_version=1),
            "schema_invalid: schema_version: ",
        ),
    ],
    ids=["unreadable", "version", "backends", "unknown-key"],
)
def test_kind_manifest_rejection_lines(
    tmp_path: Path, build: object, line_prefix: str
) -> None:
    assert callable(build)
    path = build(tmp_path)

    result = _invoke("--kind", "manifest", str(path))

    assert result.exit_code == 1
    assert result.stdout == ""
    lines = _stderr_lines(result)
    assert len(lines) == 1
    assert lines[0].startswith(line_prefix)


@pytest.mark.parametrize(
    ("count", "trailer"),
    [
        (25, "... 5 more line(s) omitted"),
        (21, "... 1 more line(s) omitted"),
        (20, None),
    ],
)
def test_kind_manifest_error_lines_truncated(
    tmp_path: Path, count: int, trailer: str | None
) -> None:
    # Eight invalid role ids is the roles cap; the rest are invalid backends.
    roles = [{"id": f"BAD{i}", "agent_spec": "a.md"} for i in range(8)]
    manifest = _stub_copy(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["roles"] = roles
    payload["backends"] = ["x"] * (count - 8)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = _invoke("--kind", "manifest", str(manifest))

    assert result.exit_code == 1
    lines = _stderr_lines(result)
    issue_lines = [line for line in lines if line.startswith("schema_invalid: ")]
    assert len(issue_lines) == MAX_VALIDATE_ERROR_LINES
    if trailer is None:
        assert len(lines) == MAX_VALIDATE_ERROR_LINES
    else:
        assert lines[-1] == trailer
        assert len(lines) == MAX_VALIDATE_ERROR_LINES + 1


def test_kind_manifest_never_echoes_input_values(tmp_path: Path) -> None:
    manifest = _stub_copy(tmp_path, backends=["SENTINEL-BACKEND-31cc"])

    result = _invoke("--kind", "manifest", str(manifest))

    assert result.exit_code == 1
    assert "SENTINEL-BACKEND-31cc" not in result.stderr


def test_unknown_kind_exits_two(tmp_path: Path) -> None:
    result = _invoke("--kind", "bogus", str(tmp_path / "x.json"))

    assert result.exit_code == 2


def test_kind_manifest_emits_no_event_and_writes_nothing(tmp_path: Path) -> None:
    manifest = _stub_copy(tmp_path)
    before = sorted(manifest.parent.rglob("*"))
    content = manifest.read_bytes()

    result = _invoke("--kind", "manifest", str(manifest))

    assert result.exit_code == 0
    assert sorted(manifest.parent.rglob("*")) == before
    assert manifest.read_bytes() == content
    assert not inbox_path().exists()


def test_validate_help_documents_kind() -> None:
    result = CliRunner().invoke(main, ["review", "engine", "validate", "--help"])

    assert result.exit_code == 0
    text = _norm(result.output)
    assert "--kind" in text
    assert "summary" in text
    assert "manifest" in text
