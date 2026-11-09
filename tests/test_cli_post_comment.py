"""Tests for the ``cw post-comment`` command (#2810).

The one redact-and-post seam for agent-authored tracker comments. Client and
tracker resolution run for real; ``cw.tracker_post``'s ``post_issue_comment``,
``fetch_issue_comments`` and ``time.sleep`` bindings and the command's
``fetch_repo_visibility`` binding are stubbed, so no ``gh`` runs. Every JSON
assertion follows the result contract in ``.claude/commands/auto-dev.md``'s
"Tracker post seam (#2810)" section: one object, eight keys, always present.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.cli.post_comment import exit_code_for
from cw.config import events_dir, history_dir
from cw.gh import AGENT_COMMENT_MARKER
from cw.tracker_post import PostReason, PostStatus
from tests import _git_sandbox
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._tool_fakes import FakePostCommentGh
from tests.conftest import _make_agent_repo

URL_BASE = "https://github.com/OWNER/REPO/issues/7#issuecomment-"
RESULT_KEYS = {
    "status",
    "reason",
    "ticket",
    "repo_visibility",
    "redacted",
    "parts_total",
    "posted",
    "out_files",
}
EXIT_POSTED = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3
SECRET = "ghp_" + "x" * 36
_FAILING_PART = 2
HEADER = "## Blocking Review Findings"
SUBHEADING = "### Finalize ship-it gate findings"

# Every (status, reason) pair the contract defines, with its exit code.
CONTRACT_TABLE: list[tuple[str, str | None, int]] = [
    ("posted", None, EXIT_POSTED),
    ("emitted", None, EXIT_POSTED),
    *[
        ("refused", reason, EXIT_REFUSED)
        for reason in (
            "body_unreadable",
            "empty_body",
            "tracker_not_github",
            "visibility_undetermined",
            "single_part_too_long",
            "post_failed",
        )
    ],
    *[
        ("partial", reason, EXIT_PARTIAL)
        for reason in (
            "post_failed",
            "post_outcome_unknown",
            "url_missing",
            "readback_mismatch",
            "readback_missing",
        )
    ],
]


@pytest.fixture(autouse=True)
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from a non-git dir with no cw-context.json unless a test adds one."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> FakePostCommentGh:
    fake = FakePostCommentGh(URL_BASE)
    monkeypatch.setattr("cw.tracker_post.post_issue_comment", fake.post)
    monkeypatch.setattr("cw.tracker_post.fetch_issue_comments", fake.fetch)
    monkeypatch.setattr("cw.tracker_post.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "cw.cli.post_comment.fetch_repo_visibility", fake.fetch_visibility
    )
    return fake


@pytest.fixture
def acme_root(tmp_config_dir: Path, tmp_path: Path) -> Path:
    """A registered GitHub-tracked client ``acme``."""
    root = _make_agent_repo(tmp_path / "acme", tracker="github-issues")
    write_clients_yaml(ClientSpec("acme", workspace_path=root))
    return root


@pytest.fixture
def linear_root(tmp_config_dir: Path, tmp_path: Path) -> Path:
    """A registered client ``lin`` whose tracker is positively Linear."""
    root = _make_agent_repo(tmp_path / "lin", tracker="linear")
    write_clients_yaml(ClientSpec("lin", workspace_path=root))
    return root


def _body(tmp_path: Path, text: str | bytes, name: str = "body.txt") -> Path:
    path = tmp_path / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_bytes(text.encode("utf-8"))
    return path


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["post-comment", *args])


def _json(result: Result) -> dict[str, object]:
    """Parse stdout as exactly one JSON object with the eight contract keys."""
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    assert set(data) == RESULT_KEYS
    assert isinstance(data["ticket"], str)
    assert isinstance(data["redacted"], bool)
    assert isinstance(data["parts_total"], int)
    assert isinstance(data["posted"], list)
    assert data["status"] in {"posted", "emitted", "refused", "partial"}
    assert (data["reason"] is None) == (data["status"] in {"posted", "emitted"})
    assert (data["out_files"] is None) == (data["status"] != "emitted")
    return data


class TestPosted:
    def test_posts_redacted_wrapped_parts_and_reports_them(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        body = _body(tmp_path, f"gate: x\nsee {acme_root}/src/a.py and {SECRET}\n")

        result = _invoke(
            "#7",
            "-c",
            "acme",
            "--body-file",
            str(body),
            "--header",
            HEADER,
            "--subheading",
            SUBHEADING,
            "--fence",
        )

        assert result.exit_code == EXIT_POSTED, result.output
        data = _json(result)
        assert data == {
            "status": "posted",
            "reason": None,
            "ticket": "#7",
            "repo_visibility": "PUBLIC",
            "redacted": True,
            "parts_total": 1,
            "posted": [{"index": 1, "url": f"{URL_BASE}1"}],
            "out_files": None,
        }
        ticket_id, posted, cwd = fake_gh.posts[0]
        assert ticket_id == "7"
        assert cwd == acme_root
        assert fake_gh.visibility_cwds == [acme_root]
        assert fake_gh.fetch_cwds == [acme_root]
        assert posted.startswith(f"{HEADER}\n\n{SUBHEADING}\n\n```\n")
        assert "<worktree>/src/a.py" in posted
        assert str(acme_root) not in posted
        assert SECRET not in posted

    @pytest.mark.parametrize("visibility", ["PRIVATE", "INTERNAL"])
    def test_non_public_repos_post_redacted_and_echo_the_visibility(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        visibility: str,
    ) -> None:
        fake_gh.visibility = visibility
        body = _body(tmp_path, f"see {acme_root}/src/a.py and {SECRET}\n")

        result = _invoke("7", "-c", "acme", "--body-file", str(body))

        assert result.exit_code == EXIT_POSTED, result.output
        data = _json(result)
        assert data["repo_visibility"] == visibility
        assert data["redacted"] is True
        posted = fake_gh.posts[0][1]
        assert "<worktree>/src/a.py" in posted
        assert str(acme_root) not in posted
        assert SECRET not in posted

    def test_clean_text_is_not_flagged_redacted(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, "ok\n")))
        assert result.exit_code == EXIT_POSTED, result.output
        assert _json(result)["redacted"] is False
        assert fake_gh.posts[0][1] == "ok\n"

    def test_long_body_splits_into_labelled_parts(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        text = ("finding line\n" * 5000) * 2  # 130,000 chars
        result = _invoke(
            "7",
            "-c",
            "acme",
            "--body-file",
            str(_body(tmp_path, text)),
            "--header",
            HEADER,
        )
        data = _json(result)
        assert result.exit_code == EXIT_POSTED, result.output
        assert data["parts_total"] == len(fake_gh.posts)
        assert len(fake_gh.posts) > 1
        assert "(part 1 of " in fake_gh.posts[0][1]

    def test_marker_is_not_doubled(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        body = _body(tmp_path, f"text\n\n{AGENT_COMMENT_MARKER}\n")
        result = _invoke("7", "-c", "acme", "--body-file", str(body))
        assert result.exit_code == EXIT_POSTED, result.output
        assert fake_gh.posts[0][1] == "text"
        assert fake_gh.thread[0]["body"] == f"text\n\n{AGENT_COMMENT_MARKER}"

    def test_worktree_anchors_are_redacted(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        run_dir: Path,
    ) -> None:
        ctx_wt = "/srv/build/ctx-worktree"
        flag_wt = "/opt/flag-worktree"
        (run_dir / ".claude").mkdir()
        (run_dir / ".claude" / "cw-context.json").write_text(
            json.dumps({"client": "acme", "worktree_path": ctx_wt}), encoding="utf-8"
        )
        body = _body(tmp_path, f"{ctx_wt}/a.py {flag_wt}/b.py\n")

        result = _invoke("7", "--body-file", str(body), "--worktree", flag_wt)

        assert result.exit_code == EXIT_POSTED, result.output
        assert fake_gh.posts[0][1] == "<worktree>/a.py <worktree>/b.py\n"

    def test_git_toplevel_is_an_anchor(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "toplevel-repo"
        repo.mkdir()
        _git_sandbox.run("init", "-q", str(repo), text=False)
        monkeypatch.chdir(repo)
        body = _body(tmp_path, f"{repo.resolve()}/x.py\n")

        result = _invoke("7", "-c", "acme", "--body-file", str(body))

        assert result.exit_code == EXIT_POSTED, result.output
        assert fake_gh.posts[0][1] == "<worktree>/x.py\n"

    def test_git_that_cannot_run_skips_the_toplevel_anchor(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _no_git(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr("cw.cli.post_comment.run_git", _no_git)
        body = _body(tmp_path, f"{acme_root}/a.py /srv/other/b.py\n")

        result = _invoke("7", "-c", "acme", "--body-file", str(body))

        assert result.exit_code == EXIT_POSTED, result.output
        assert fake_gh.posts[0][1] == "<worktree>/a.py /srv/other/b.py\n"

    def test_home_comes_from_the_environment(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A service account's HOME outside /home and /Users is still redacted."""
        monkeypatch.setenv("HOME", "/srv/svc-user")
        body = _body(tmp_path, "see /srv/svc-user/x.py\n")

        result = _invoke("7", "-c", "acme", "--body-file", str(body))

        assert result.exit_code == EXIT_POSTED, result.output
        posted = fake_gh.posts[0][1]
        assert "<home>/x.py" in posted
        assert "/srv/svc-user" not in posted

    def test_client_repo_path_is_an_anchor(
        self,
        tmp_config_dir: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
    ) -> None:
        """repo_path differing from workspace_path is redacted like the workspace."""
        workspace = _make_agent_repo(tmp_path / "ws", tracker="github-issues")
        repo = tmp_path / "separate-repo"
        repo.mkdir()
        write_clients_yaml(
            ClientSpec("split", workspace_path=workspace, repo_path=repo)
        )
        body = _body(tmp_path, f"see {repo}/src/a.py\n")

        result = _invoke("7", "-c", "split", "--body-file", str(body))

        assert result.exit_code == EXIT_POSTED, result.output
        assert fake_gh.posts[0][1] == "see <worktree>/src/a.py\n"


class TestRefusals:
    def _assert_refused(
        self, result: Result, fake_gh: FakePostCommentGh, reason: str
    ) -> dict[str, object]:
        assert result.exit_code == EXIT_REFUSED, result.output
        data = _json(result)
        assert data["status"] == "refused"
        assert data["reason"] == reason
        assert data["posted"] == []
        assert data["out_files"] is None
        assert fake_gh.posts == []
        assert reason in result.stderr
        return data

    def test_linear_client_without_emit_only(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke("7", "-c", "lin", "--body-file", str(_body(tmp_path, "x")))
        data = self._assert_refused(result, fake_gh, "tracker_not_github")
        assert data["repo_visibility"] is None
        assert data["parts_total"] == 0
        assert fake_gh.visibility_calls == 0

    def test_visibility_undetermined(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        fake_gh.visibility = None
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, "x")))
        data = self._assert_refused(result, fake_gh, "visibility_undetermined")
        assert data["repo_visibility"] is None
        assert data["parts_total"] == 0
        assert data["redacted"] is False

    @pytest.mark.parametrize("text", ["", "   \n\t\r\n"])
    def test_empty_body(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path, text: str
    ) -> None:
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, text)))
        data = self._assert_refused(result, fake_gh, "empty_body")
        assert data["repo_visibility"] is None
        assert data["parts_total"] == 0

    def test_missing_body_file(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke("7", "-c", "acme", "--body-file", str(tmp_path / "nope.txt"))
        data = self._assert_refused(result, fake_gh, "body_unreadable")
        assert data["parts_total"] == 0

    def test_non_utf8_body_file(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        body = _body(tmp_path, b"bad \xff bytes")
        result = _invoke("7", "-c", "acme", "--body-file", str(body))
        self._assert_refused(result, fake_gh, "body_unreadable")

    def test_single_part_too_long(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        body = _body(tmp_path, "z" * 61_000)
        result = _invoke(
            "7", "-c", "acme", "--body-file", str(body), "--parts", "single"
        )
        data = self._assert_refused(result, fake_gh, "single_part_too_long")
        assert isinstance(data["parts_total"], int)
        assert data["parts_total"] > 1
        assert data["repo_visibility"] == "PUBLIC"

    def test_first_post_failure_is_refused(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        fake_gh.results = [subprocess.CompletedProcess(["gh"], 1, b"", b"boom")]
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, "x")))
        assert result.exit_code == EXIT_REFUSED, result.output
        data = _json(result)
        assert (data["status"], data["reason"]) == ("refused", "post_failed")
        assert data["parts_total"] == 1
        assert data["posted"] == []

    def test_empty_body_wins_over_tracker_not_github(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke("7", "-c", "lin", "--body-file", str(_body(tmp_path, " ")))
        self._assert_refused(result, fake_gh, "empty_body")


class TestPartial:
    def test_comment_never_visible_is_readback_missing(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        url = f"{URL_BASE}1"
        # gh returns a URL, but the comment never shows up in the thread.
        fake_gh.results = [subprocess.CompletedProcess(["gh"], 0, url.encode(), b"")]
        text = "finding line\n" * 10_000  # 130,000 chars: 3 parts
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, text)))
        assert result.exit_code == EXIT_PARTIAL, result.output
        data = _json(result)
        assert (data["status"], data["reason"]) == ("partial", "readback_missing")
        assert data["posted"] == [{"index": 1, "url": url}]
        assert data["parts_total"] == len(text) // 59_000 + 1

    def test_partial_failure_exit_3_lists_posted_part(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []
        real_post = fake_gh.post

        def _post(
            ticket_id: str, body: str, *, cwd: Path | None = None
        ) -> subprocess.CompletedProcess[bytes] | None:
            calls.append(body)
            if len(calls) == _FAILING_PART:
                return subprocess.CompletedProcess(["gh"], 1, b"", b"boom")
            return real_post(ticket_id, body, cwd=cwd)

        monkeypatch.setattr("cw.tracker_post.post_issue_comment", _post)
        text = "finding line\n" * 10_000
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, text)))

        assert result.exit_code == EXIT_PARTIAL, result.output
        data = _json(result)
        assert (data["status"], data["reason"]) == ("partial", "post_failed")
        assert data["posted"] == [{"index": 1, "url": f"{URL_BASE}1"}]
        assert data["parts_total"] == len("finding line\n" * 10_000) // 59_000 + 1
        assert "post_failed" in result.stderr

    def test_readback_mismatch_is_partial(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        fake_gh.tamper = True
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, "x")))
        assert result.exit_code == EXIT_PARTIAL, result.output
        data = _json(result)
        assert data["reason"] == "readback_mismatch"
        assert data["posted"] == [{"index": 1, "url": f"{URL_BASE}1"}]


class TestEmitOnly:
    def test_writes_marked_parts_and_posts_nothing(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "out"
        text = f"see {linear_root}/a.py\n" + "finding line\n" * 10_000
        result = _invoke(
            "LIN-1",
            "-c",
            "lin",
            "--body-file",
            str(_body(tmp_path, text)),
            "--header",
            HEADER,
            "--emit-only",
            "--out-dir",
            str(out_dir),
        )

        assert result.exit_code == EXIT_POSTED, result.output
        data = _json(result)
        assert data["status"] == "emitted"
        assert data["reason"] is None
        assert data["repo_visibility"] is None
        assert data["posted"] == []
        assert data["redacted"] is True
        out_files = data["out_files"]
        assert isinstance(out_files, list)
        assert len(out_files) == data["parts_total"]
        assert len(out_files) > 1
        for index, name in enumerate(out_files, start=1):
            path = Path(name)
            assert path.is_absolute()
            assert path.name == f"part-{index}.md"
            content = path.read_text(encoding="utf-8")
            assert content.endswith(f"\n\n{AGENT_COMMENT_MARKER}")
            assert content.startswith(HEADER)
            assert str(linear_root) not in content
        assert fake_gh.posts == []
        assert fake_gh.visibility_calls == 0

    def test_single_part_too_long_is_refused_without_a_visibility_lookup(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "out"
        result = _invoke(
            "LIN-1",
            "-c",
            "lin",
            "--body-file",
            str(_body(tmp_path, "z" * 61_000)),
            "--parts",
            "single",
            "--emit-only",
            "--out-dir",
            str(out_dir),
        )
        assert result.exit_code == EXIT_REFUSED, result.output
        data = _json(result)
        assert data["reason"] == "single_part_too_long"
        assert data["repo_visibility"] is None
        assert data["out_files"] is None
        assert not out_dir.exists()
        assert fake_gh.visibility_calls == 0

    def test_refused_emit_only_has_null_out_files(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke(
            "LIN-1",
            "-c",
            "lin",
            "--body-file",
            str(_body(tmp_path, "")),
            "--emit-only",
            "--out-dir",
            str(tmp_path / "out"),
        )
        assert result.exit_code == EXIT_REFUSED, result.output
        data = _json(result)
        assert data["reason"] == "empty_body"
        assert data["out_files"] is None

    def test_emit_only_requires_out_dir(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke(
            "LIN-1",
            "-c",
            "lin",
            "--body-file",
            str(_body(tmp_path, "x")),
            "--emit-only",
        )
        assert result.exit_code == EXIT_USAGE
        assert result.stdout == ""

    def test_out_dir_requires_emit_only(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke(
            "LIN-1",
            "-c",
            "lin",
            "--body-file",
            str(_body(tmp_path, "x")),
            "--out-dir",
            str(tmp_path),
        )
        assert result.exit_code == EXIT_USAGE
        assert result.stdout == ""

    def test_consume_removes_the_body_after_emitting(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "out"
        body = _body(tmp_path, "finding\n")

        result = _invoke(
            "LIN-1",
            "-c",
            "lin",
            "--body-file",
            str(body),
            "--emit-only",
            "--out-dir",
            str(out_dir),
            "--consume",
        )

        assert result.exit_code == EXIT_POSTED, result.output
        data = _json(result)
        assert data["status"] == "emitted"
        out_files = data["out_files"]
        assert isinstance(out_files, list)
        assert out_files
        assert all(Path(name).is_file() for name in out_files)
        assert not body.exists()

    def test_consume_removes_the_body_when_the_out_dir_is_unwritable(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        """The raw file is consumed on every path after a successful read."""
        # click rejects an existing file as --out-dir before the body is read
        # (usage error), so nest the path under a file: it passes option
        # validation and fails at mkdir, after the body was read and consumed.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("a regular file", encoding="utf-8")
        out_dir = blocker / "out"
        body = _body(tmp_path, "finding\n")

        result = _invoke(
            "LIN-1",
            "-c",
            "lin",
            "--body-file",
            str(body),
            "--emit-only",
            "--out-dir",
            str(out_dir),
            "--consume",
        )

        assert result.exit_code != EXIT_POSTED
        assert result.exception is not None
        assert result.stdout == ""
        assert not body.exists()


class TestConsume:
    def test_removed_on_success(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        body = _body(tmp_path, "x")
        result = _invoke("7", "-c", "acme", "--body-file", str(body), "--consume")
        assert result.exit_code == EXIT_POSTED, result.output
        assert not body.exists()

    def test_removed_on_refusal(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        fake_gh.visibility = None
        body = _body(tmp_path, "x")
        result = _invoke("7", "-c", "acme", "--body-file", str(body), "--consume")
        assert result.exit_code == EXIT_REFUSED, result.output
        assert not body.exists()

    def test_removed_on_partial_failure(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        fake_gh.results = [None]
        body = _body(tmp_path, "x")
        result = _invoke("7", "-c", "acme", "--body-file", str(body), "--consume")
        assert result.exit_code == EXIT_PARTIAL, result.output
        assert _json(result)["reason"] == "post_outcome_unknown"
        assert not body.exists()

    def test_retained_when_never_read(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        body = _body(tmp_path, b"\xff\xfe")
        result = _invoke("7", "-c", "acme", "--body-file", str(body), "--consume")
        assert result.exit_code == EXIT_REFUSED, result.output
        assert body.exists()

    def test_kept_without_the_flag(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        body = _body(tmp_path, "x")
        _invoke("7", "-c", "acme", "--body-file", str(body))
        assert body.exists()

    def test_unlink_failure_warns_and_continues(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _deny(_self: Path, *, missing_ok: bool = False) -> None:
            del missing_ok
            msg = "denied"
            raise PermissionError(msg)

        monkeypatch.setattr(Path, "unlink", _deny)
        body = _body(tmp_path, "x")
        result = _invoke("7", "-c", "acme", "--body-file", str(body), "--consume")
        assert result.exit_code == EXIT_POSTED, result.output
        assert "could not delete" in result.stderr


class TestTicketValidation:
    """A non-numeric TICKET on a GitHub tracker bypasses the visibility check."""

    def _assert_usage_error(
        self, result: Result, fake_gh: FakePostCommentGh, body: Path, ticket: str
    ) -> None:
        assert result.exit_code == EXIT_USAGE, result.output
        assert result.stdout == ""
        assert "GitHub issue number" in result.stderr
        assert ticket in result.stderr
        assert fake_gh.posts == []
        assert fake_gh.visibility_calls == 0
        assert body.exists()

    def test_issue_url_ticket_is_a_usage_error_and_keeps_the_body(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        ticket = "https://github.com/other/repo/issues/5"
        body = _body(tmp_path, "x")
        result = _invoke("-c", "acme", "--body-file", str(body), "--consume", ticket)
        self._assert_usage_error(result, fake_gh, body, ticket)

    def test_flag_shaped_ticket_is_a_usage_error_and_keeps_the_body(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        ticket = "--repo=x/y"
        body = _body(tmp_path, "x")
        result = _invoke(
            "-c", "acme", "--body-file", str(body), "--consume", "--", ticket
        )
        self._assert_usage_error(result, fake_gh, body, ticket)

    @pytest.mark.parametrize(("ticket", "gh_arg"), [("12", "12"), ("#12", "12")])
    def test_numeric_tickets_still_post(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        ticket: str,
        gh_arg: str,
    ) -> None:
        result = _invoke(ticket, "-c", "acme", "--body-file", str(_body(tmp_path, "x")))
        assert result.exit_code == EXIT_POSTED, result.output
        assert fake_gh.posts[0][0] == gh_arg

    def test_linear_without_emit_only_is_still_refused_not_a_usage_error(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        body = _body(tmp_path, "x")
        result = _invoke("LIN-1", "-c", "lin", "--body-file", str(body))
        assert result.exit_code == EXIT_REFUSED, result.output
        assert _json(result)["reason"] == "tracker_not_github"

    def test_linear_emit_only_accepts_a_non_numeric_ticket(
        self, linear_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke(
            "LIN-1",
            "-c",
            "lin",
            "--body-file",
            str(_body(tmp_path, "x")),
            "--emit-only",
            "--out-dir",
            str(tmp_path / "out"),
        )
        assert result.exit_code == EXIT_POSTED, result.output
        assert _json(result)["status"] == "emitted"


class TestClientResolution:
    def test_client_falls_back_to_cw_context(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        run_dir: Path,
    ) -> None:
        (run_dir / ".claude").mkdir()
        (run_dir / ".claude" / "cw-context.json").write_text(
            json.dumps({"client": "acme"}), encoding="utf-8"
        )
        result = _invoke("7", "--body-file", str(_body(tmp_path, "x")))
        assert result.exit_code == EXIT_POSTED, result.output
        assert fake_gh.posts[0][2] == acme_root

    def test_no_client_anywhere_is_a_usage_error_with_no_json(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke("7", "--body-file", str(_body(tmp_path, "x")))
        assert result.exit_code == EXIT_USAGE
        assert result.stdout == ""
        assert fake_gh.posts == []

    def test_unknown_client_is_a_usage_error_with_no_json(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        result = _invoke("7", "-c", "ghost", "--body-file", str(_body(tmp_path, "x")))
        assert result.exit_code == EXIT_USAGE
        assert result.stdout == ""
        assert "ghost" in result.stderr


class TestFailClosed:
    def test_redactor_failure_posts_nothing_and_prints_no_json(
        self,
        acme_root: Path,
        fake_gh: FakePostCommentGh,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(_text: str, *, home: str, worktrees: object) -> str:
            del home, worktrees
            msg = "redactor broke"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.tracker_post.redact_post_text", _boom)
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, "x")))
        assert result.exit_code == EXIT_REFUSED
        assert isinstance(result.exception, RuntimeError)
        assert result.stdout == ""
        assert fake_gh.posts == []

    def test_no_audit_event_and_only_json_on_stdout(
        self, acme_root: Path, fake_gh: FakePostCommentGh, tmp_path: Path
    ) -> None:
        def _snapshot() -> dict[str, int]:
            """Every file under the isolated events and history dirs, with size."""
            return {
                str(path): path.stat().st_size
                for root in (events_dir(), history_dir())
                for path in root.rglob("*")
            }

        before = _snapshot()
        result = _invoke("7", "-c", "acme", "--body-file", str(_body(tmp_path, "x")))
        assert result.exit_code == EXIT_POSTED, result.output
        assert _snapshot() == before
        assert result.stdout.count("\n") == 1
        _json(result)


class TestContractTable:
    @pytest.mark.parametrize(("status", "reason", "code"), CONTRACT_TABLE)
    def test_every_pair_maps_to_its_exit_code(
        self, status: str, reason: str | None, code: int
    ) -> None:
        assert exit_code_for(status) == code
        assert (reason is None) == (status in {"posted", "emitted"})
        if reason is not None:
            assert reason in {r.value for r in PostReason}

    def test_table_covers_every_enum_value(self) -> None:
        statuses = {status for status, _r, _c in CONTRACT_TABLE}
        reasons = {reason for _s, reason, _c in CONTRACT_TABLE if reason}
        assert statuses == {s.value for s in PostStatus} | {"emitted"}
        assert reasons == {r.value for r in PostReason}
