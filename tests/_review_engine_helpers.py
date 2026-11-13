"""Shared builders for the review engine summary tests (#3036).

Imported by ``test_review_engine_summary.py`` (the model),
``test_review_engine_summary_io.py`` (the bounded loader) and
``test_cli_review_engine.py`` (``cw review engine validate``). This module has
no ``test_`` prefix, so pytest does not collect it (the
``tests/_cli_review_helpers.py`` convention).

``make_summary`` derives ``counts`` and ``verdict`` from its findings so a
valid summary is the default; any keyword override replaces the derived value,
which is how a test forces a counts mismatch or a PASS with a gating finding.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from click.testing import CliRunner, Result

from cw.cli import main
from cw.dev_queue import add_ticket
from cw.models import HOOK_CONTEXT_RELATIVE_PATH, QueueItemStatus, Stage, TicketTask
from cw.models.review_engine_summary import REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_engine.materialize import engine_paths
from cw.review_findings import consolidate_verdict
from tests._cli_review_helpers import _seed_review_row
from tests._shared.git_repos import add_bare_origin, commit_tracked_file, git_in
from tests._shared.review_builders import _make_diff, _make_reviewer_doc
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.review_findings import ReviewVerdict

SHA = "a" * 40

_GATING = frozenset({"MUST_FIX", "SHOULD_FIX"})
_COUNT_KEYS = ("must_fix", "should_fix", "debt", "nit", "principle")


def make_finding(**overrides: object) -> dict[str, object]:
    """One valid finding; *overrides* replace individual keys."""
    finding: dict[str, object] = {
        "fingerprint": "fp-000001",
        "severity": "NIT",
        "rule_id": "style.naming",
        "file": "src/a.py",
        "line": 3,
        "summary": "Name does not follow the module convention.",
        "evidence": "def Foo(): ...",
        "consequence": None,
        "suggested_fix": None,
    }
    finding.update(overrides)
    return finding


def _tally(findings: list[dict[str, object]]) -> dict[str, int]:
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    for finding in findings:
        severity = finding.get("severity")
        if isinstance(severity, str) and severity.lower() in counts:
            counts[severity.lower()] += 1
    return counts


def make_summary(
    findings: list[dict[str, object]] | None = None, **overrides: object
) -> dict[str, object]:
    """A valid summary over *findings*; *overrides* replace derived keys."""
    rows = [] if findings is None else findings
    gating = any(finding.get("severity") in _GATING for finding in rows)
    summary: dict[str, object] = {
        "schema_version": REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION,
        "head_sha": SHA,
        "verdict": "FAIL" if gating else "PASS",
        "coverage": "COMPLETE",
        "risk": "LOW",
        "risk_notes": [],
        "counts": _tally(rows),
        "findings": rows,
        "acceptance": [],
        "gate_signals": [],
    }
    summary.update(overrides)
    return summary


_CW_FILLED_KEYS = ("schema_version", "head_sha", "counts")


def make_aggregation_output(
    findings: list[dict[str, object]] | None = None, **overrides: object
) -> dict[str, object]:
    """What a codex aggregation turn emits (#3216): a valid summary minus cw's keys.

    The :func:`make_summary` over *findings* without ``schema_version``,
    ``head_sha`` and ``counts``, the three fields cw fills itself. *overrides*
    apply after the removal, so a test can force an invalid output (an extra
    ``counts`` key, a PASS with a gating finding).
    """
    output = make_summary(findings)
    for key in _CW_FILLED_KEYS:
        del output[key]
    output.update(overrides)
    return output


def summary_bytes(summary: dict[str, object]) -> bytes:
    """*summary* as compact UTF-8 JSON."""
    return json.dumps(summary, separators=(",", ":")).encode("utf-8")


def write_summary(
    directory: Path,
    summary: dict[str, object] | bytes,
    name: str = "summary.json",
) -> Path:
    """Write *summary* (a dict, or raw bytes) under *directory*; return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    raw = summary if isinstance(summary, bytes) else summary_bytes(summary)
    path = directory / name
    path.write_bytes(raw)
    return path


def pad_to(raw: bytes, size: int) -> bytes:
    """Pad *raw* with trailing JSON whitespace to exactly *size* bytes."""
    if len(raw) > size:
        msg = f"raw is {len(raw)} bytes, already over {size}"
        raise ValueError(msg)
    return raw + b" " * (size - len(raw))


# ---------------------------------------------------------------------------
# I1 (#3039): a real git repo carrying an engine skill, a stub engine and the
# CLI wrapper. Composed from the shared ``tests/conftest.py`` helpers.
# ---------------------------------------------------------------------------

ENGINE_TICKET = "ENG-1"
ENGINE_CLIENT = "test-client"
ENGINE_SKILL_TEXT = "# repo-review\n\nReview the diff. Write the summary file.\n"
ENGINE_RUBRIC_TEXT = "rubric: be strict\n"
_COMMAND_LAYOUT_DIR = ".claude/commands"


@dataclass(frozen=True)
class EngineRepo:
    """A worktree on ``dev/1`` with an engine skill committed on ``origin/main``."""

    worktree: Path
    head: str
    diff_base: str
    ticket: str
    client: str


def invoke(*args: str, input_text: str | None = None) -> Result:
    """``cw <args>`` through Click's runner (the full argv after ``cw``)."""
    return CliRunner().invoke(main, list(args), input=input_text)


def engine_context_payload(
    *,
    skill: str | None,
    guarded: tuple[str, ...] = (),
    default_branch: str = "main",
    ticket: str = ENGINE_TICKET,
    client: str = ENGINE_CLIENT,
) -> dict[str, object]:
    """The ``.claude/cw-context.json`` object a Claude-native clean lane gets."""
    review: dict[str, object] = {"convergence": "clean"}
    if skill is not None:
        review["engine"] = {"skill": skill, "guarded_paths": list(guarded)}
    return {
        "schema_version": 19,
        "session_id": "abc",
        "headless": False,
        "ticket_id": ticket,
        "client": client,
        "review": review,
        "world_state_snapshot": {"origin_main_branch": default_branch},
    }


def write_engine_context(worktree: Path, payload: dict[str, object]) -> Path:
    """Write *payload* as the worktree's cw context file."""
    path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def commit_symlink(repo: Path, link: str, target: str, message: str = "link") -> None:
    """Commit a symlink *link* -> *target* (the target is stored verbatim)."""
    path = repo / link
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    git_in(repo, "add", link)
    git_in(repo, "commit", "-m", message)


def _write_skill_files(repo: Path, layout: str, skill: str) -> list[str]:
    """Write the skill under *layout*; return the repo-relative files written."""
    if layout == _COMMAND_LAYOUT_DIR:
        rel = f"{layout}/{skill}.md"
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(ENGINE_SKILL_TEXT, encoding="utf-8")
        return [rel]
    files = [f"{layout}/{skill}/SKILL.md", f"{layout}/{skill}/refs/rubric.md"]
    texts = [ENGINE_SKILL_TEXT, ENGINE_RUBRIC_TEXT]
    for rel, text in zip(files, texts, strict=True):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return files


def _copy_engine_files(repo: Path, skill_dir: str, source: Path) -> list[str]:
    """Copy the files under *source* into *skill_dir*; return their repo paths."""
    written: list[str] = []
    for file in sorted(path for path in source.rglob("*") if path.is_file()):
        rel = f"{skill_dir}/{file.relative_to(source).as_posix()}"
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.read_bytes())
        written.append(rel)
    return written


def copy_engine_fixture(
    tmp_path: Path,
    source: Path,
    *,
    name: str = "engine",
    **manifest_overrides: object,
) -> Path:
    """Copy an engine fixture and optionally override its manifest fields."""
    copy = tmp_path / name
    shutil.copytree(source, copy)
    if manifest_overrides:
        manifest = copy / "cw-engine.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload.update(manifest_overrides)
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    return copy


def manifest_directory_engine(
    tmp_path: Path, source: Path, *, name: str = "engine-files"
) -> Path:
    """A copy of *source* whose ``cw-engine.json`` is a directory, not a file.

    Every file except the manifest is copied; ``cw-engine.json/marker`` makes
    git commit a directory at the manifest path (#3445).
    """
    engine = tmp_path / name
    for path in source.rglob("*"):
        if path.is_file() and path.name != "cw-engine.json":
            target = engine / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    marker = engine / "cw-engine.json" / "marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("not a manifest", encoding="utf-8")
    return engine


def make_engine_repo(
    make_git_repo: Callable[..., Path],
    *,
    layout: str = ".claude/skills",
    skill: str = "repo-review",
    symlink_to: str | None = None,
    guarded: tuple[str, ...] = (),
    default_branch: str = "main",
    name: str = "engine-repo",
    engine_files: Path | None = None,
) -> EngineRepo:
    """A repo whose *default_branch* carries the engine skill, checked out on dev/1.

    ``layout`` is the directory the probe path lives under (``.claude/skills``,
    ``.agents/skills`` or ``.claude/commands``). With *symlink_to* the real
    files live under that directory and *layout* is a committed symlink to it.
    ``origin/<default>`` and ``origin/dev/1`` exist after the call. The worktree
    carries one code commit (``src/a.py``) beyond the default branch, plus an
    untracked, git-excluded context file and ``.cw`` directory.

    *engine_files* is a directory whose files (for example the
    ``tests/fixtures/review_engine_stub`` engine) are copied into the skill
    directory on the default branch before the commit. It needs a directory
    layout, and a file it carries replaces a same-named skill file.
    """
    repo = make_git_repo(name)
    if default_branch != "main":
        git_in(repo, "branch", "-m", default_branch)
    real_layout = symlink_to or layout
    files = _write_skill_files(repo, real_layout, skill)
    if engine_files is not None:
        if real_layout == _COMMAND_LAYOUT_DIR:
            msg = "engine_files needs a directory layout, not .claude/commands"
            raise ValueError(msg)
        copied = _copy_engine_files(repo, f"{real_layout}/{skill}", engine_files)
        files.extend(rel for rel in copied if rel not in files)
    for rel in files:
        git_in(repo, "add", rel)
    git_in(repo, "commit", "-m", "add engine skill")
    if symlink_to is not None:
        relative = os.path.relpath(symlink_to, start=PurePosixPath(layout).parent)
        commit_symlink(repo, layout, relative, "link skills dir")
    git_in(repo, "checkout", "-b", "dev/1")
    commit_tracked_file(repo, "src/a.py", "x = 1\n")
    add_bare_origin(repo)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".cw/\n.claude/cw-context.json\n", encoding="utf-8")
    write_engine_context(
        repo,
        engine_context_payload(
            skill=skill, guarded=guarded, default_branch=default_branch
        ),
    )
    return EngineRepo(
        worktree=repo,
        head=git_in(repo, "rev-parse", "HEAD"),
        diff_base=git_in(repo, "rev-parse", default_branch),
        ticket=ENGINE_TICKET,
        client=ENGINE_CLIENT,
    )


def engine_finding(
    severity: str,
    *,
    rule_id: str = "style.naming",
    fingerprint: str | None = None,
    file: str | None = "src/a.py",
    **overrides: object,
) -> dict[str, object]:
    """One engine finding at *severity*; the fingerprint defaults to a derived one."""
    if fingerprint is None:
        stem = re.sub(r"[^A-Za-z0-9]", "_", f"{severity}-{rule_id}-{file}")
        fingerprint = f"fp-{stem}"
    fields: dict[str, object] = {
        "severity": severity,
        "rule_id": rule_id,
        "fingerprint": fingerprint,
        "file": file,
        "line": None if file is None else 1,
    }
    fields.update(overrides)
    return make_finding(**fields)


class StubEngine:
    """Stands in for the engine skill: writes (or withholds) the summary file."""

    def __init__(self, repo: EngineRepo) -> None:
        self.repo = repo

    def summary_path(self, round_id: str) -> Path:
        return engine_paths(self.repo.worktree, round_id).summary_path

    def run(
        self,
        round_id: str,
        summary: dict[str, object] | bytes | None = None,
        *,
        mutate: bool = False,
        skip: bool = False,
    ) -> Path:
        """Write *summary* at the round's summary path; optionally dirty the tree."""
        path = self.summary_path(round_id)
        if mutate:
            tracked = self.repo.worktree / "src" / "a.py"
            tracked.write_text(tracked.read_text() + "y = 2\n", encoding="utf-8")
        if skip:
            return path
        body = summary if summary is not None else make_summary(head_sha=self.repo.head)
        if isinstance(body, dict):
            # The placeholder SHA means "the repo's HEAD"; any other value is kept.
            sha = body.get("head_sha")
            body = {**body, "head_sha": self.repo.head if sha in (None, SHA) else sha}
        write_summary(path.parent, body, name=path.name)
        return path


def seed_row(repo: EngineRepo, parked_reason: str | None = None) -> TicketTask:
    """Enqueue the repo's ticket; parked ``BLOCKED_ON_USER`` when a reason is given."""
    if parked_reason is None:
        return _seed_review_row(repo.ticket, repo.client, worktree_path=repo.worktree)
    task = _make_ticket_task(
        ticket_id=repo.ticket,
        client=repo.client,
        stage=Stage.REVIEW,
        status=QueueItemStatus.BLOCKED_ON_USER,
        blocked_reason=parked_reason,
        worktree_path=repo.worktree,
    )
    add_ticket(task)
    return task


def engine_loop_verdict(sha: str, *, also: tuple[str, ...] = ()) -> ReviewVerdict:
    """A clean loop verdict whose ``agents_run`` is the engine role (#3042).

    Built through the real ``consolidate_verdict``. *also* adds further
    reviewer roles beside the engine (the mixed-roster variant).
    """
    documents = [
        _make_reviewer_doc(reviewer_role=role) for role in (ENGINE_ROLE, *also)
    ]
    verdict = consolidate_verdict(documents, _make_diff("x = 1"), sha)
    assert {r.reviewer_role for r in verdict.agents_run} == {ENGINE_ROLE, *also}
    return verdict
