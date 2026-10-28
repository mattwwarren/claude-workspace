"""Unit tests for ``.claude/scripts/redact_post_text.py`` (#2794).

The finalize stage posts ``/prep-pr`` ship-it gate findings to a public tracker
before regressing to IMPL. Finding text routinely carries worktree paths, home
directories and transcript-dir names, so the session pipes it through this
stdlib-only redactor first. Two properties are load-bearing: every path and
secret shape is replaced, and nothing else is removed, so a finding is never
truncated on its way to the regressed IMPL run.

Loaded in-process via ``load_guard_script_module`` so the ``.claude/scripts``
coverage floor counts the script; the CLI is also exercised as a subprocess.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from cw import _text
from tests.conftest import load_guard_script_module, run_guard_script_cli

SCRIPT = (
    Path(__file__).resolve().parents[1] / ".claude" / "scripts" / "redact_post_text.py"
)
_mod = load_guard_script_module(SCRIPT, "redact_post_text")

HOME = "/home/alice"
WORKTREE = "/home/alice/.cw/wt/abcd1234/dev-2794"
TRANSCRIPT = (
    "/home/alice/.claude/projects/-home-alice--cw-wt-abcd1234-dev-2794/abc.jsonl"
)
TRANSCRIPT_REDACTED = "<home>/.claude/projects/<redacted>"

# Secret shapes, assembled so no literal reads as a hard-coded credential.
SK_SHAPE = "sk-" + "A1b2C3d4" * 3
GHP_SHAPE = "ghp_" + "x" * 36
BEARER_SHAPE = "Bearer abc.def"
AUTH_SHAPE = "Authorization: xyz"

LONG_TARGET_CHARS = 70_000


def _redact(text: str, *, home: str = HOME, worktrees: tuple[str, ...] = ()) -> str:
    result = _mod.redact_post_text(text, home=home, worktrees=list(worktrees))
    assert isinstance(result, str)
    return result


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_worktree_path_keeps_its_remainder() -> None:
    assert _redact(f"{WORKTREE}/src/cw/x.py") == "<worktree>/src/cw/x.py"


def test_bare_worktree_path_becomes_placeholder() -> None:
    assert _redact(f"in {WORKTREE} now") == "in <worktree> now"


def test_explicit_worktree_anywhere_longest_first() -> None:
    text = "see /srv/build/wt-7/a.py and /srv/build/other"
    result = _redact(text, worktrees=("/srv/build", "/srv/build/wt-7"))
    assert result == "see <worktree>/a.py and <worktree>/other"


def test_explicit_worktree_does_not_eat_a_longer_name() -> None:
    result = _redact("/srv/wt-7 and /srv/wt-70", worktrees=("/srv/wt-7",))
    assert result == "<worktree> and /srv/wt-70"


def test_explicit_worktree_trailing_slash_is_normalized() -> None:
    assert _redact("/srv/wt/x", worktrees=("/srv/wt/",)) == "<worktree>/x"


def test_home_directories_become_placeholder() -> None:
    assert _redact("/home/alice/projects/foo") == "<home>/projects/foo"
    assert _redact("/home/bob/projects/foo") == "<home>/projects/foo"
    assert _redact("/Users/carol/src") == "<home>/src"


def test_custom_home_value_becomes_placeholder() -> None:
    assert _redact("/var/lib/dave/notes", home="/var/lib/dave") == "<home>/notes"


def test_tilde_worktree_shape_becomes_placeholder() -> None:
    assert _redact("~/.cw/wt/abcd1234/dev-2794/src") == "<worktree>/src"


def test_users_worktree_shape_becomes_placeholder() -> None:
    assert _redact("/Users/erin/.cw/wt/ff00/auto-dev-12.") == "<worktree>."


def test_transcript_dir_encoded_segment_is_redacted() -> None:
    assert _redact(TRANSCRIPT) == TRANSCRIPT_REDACTED
    assert _redact("~/.claude/projects/-home-x/y.jsonl") == TRANSCRIPT_REDACTED
    assert (
        _redact("/var/lib/dave/.claude/projects/-a-b/c.jsonl", home="/var/lib/dave")
        == TRANSCRIPT_REDACTED
    )


@pytest.mark.parametrize(
    "text",
    [
        "web/src/home/Hero.tsx:12",
        "src/home/x.tsx",
        "app/Users/y.py:3",
        "https://x.io/Users/guide",
        "see ../home/alice/x",
        "a-/home/alice/x",
        "docs/~/x",
    ],
)
def test_mid_path_home_lookalikes_pass_through(text: str) -> None:
    """F2: the home shapes only match where a path starts."""
    assert _redact(text, home="/srv/other") == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/home/alice/x", "<home>/x"),
        (" /home/alice/x", " <home>/x"),
        ('"/home/alice/x"', '"<home>/x"'),
        ("path=/home/alice/x", "path=<home>/x"),
        ("(/Users/bob/x)", "(<home>/x)"),
        ("a:/home/alice/x,/Users/bob/y", "a:<home>/x,<home>/y"),
        ("[~/.cw/wt/ab12/dev-1/x]", "[<worktree>/x]"),
        (
            "'/home/alice/.claude/projects/-a/b.jsonl'",
            "'<home>/.claude/projects/<redacted>'",
        ),
    ],
)
def test_home_shapes_at_a_path_start_still_redact(text: str, expected: str) -> None:
    assert _redact(text) == expected


def test_dotted_username_is_one_home() -> None:
    assert _redact("/home/jane.doe/x") == "<home>/x"


def test_home_value_with_regex_metacharacters_is_escaped() -> None:
    home = "/srv/a.b+c"
    assert _redact("/srv/a.b+c/x and /srv/axbbc/y", home=home) == (
        "<home>/x and /srv/axbbc/y"
    )


# ---------------------------------------------------------------------------
# Secrets, and parity with cw._text.redact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", [SK_SHAPE, GHP_SHAPE, BEARER_SHAPE, AUTH_SHAPE])
def test_secret_shapes_are_redacted(secret: str) -> None:
    result = _redact(f"found {secret} here")
    assert secret not in result
    assert "<redacted>" in result


_PARITY_FIXTURES = (
    f"key {SK_SHAPE} end",
    f"gh {GHP_SHAPE}",
    f"curl -H '{BEARER_SHAPE}'",
    AUTH_SHAPE,
    "token=short",
    "value=" + "Z" * 40,
    "nothing secret at all",
)


@pytest.mark.parametrize("text", _PARITY_FIXTURES)
def test_secret_redaction_matches_cw_text_redact(text: str) -> None:
    assert _mod.redact_secrets(text) == _text.redact(text)


def test_secret_patterns_are_a_superset_of_cw_text() -> None:
    """The cw._text shapes lead, in order; the extras come after them."""
    upstream = [p.pattern for p in _text._SECRET_PATTERNS]
    local = [p.pattern for p in _mod._SECRET_PATTERNS]
    assert local[: len(upstream)] == upstream
    assert len(local) > len(upstream)
    assert [p.pattern for p in _mod._EXTRA_SECRET_PATTERNS] == local[len(upstream) :]


# One (name, shape) per entry of _EXTRA_SECRET_PATTERNS, in the same order, each
# assembled from parts so no literal reads as a hard-coded credential.
_EXTRA_SHAPES = (
    ("sk-ant", "sk-" + "ant-api03-" + "Ab1_" * 8),
    ("github_pat", "github_pat_" + "11AB" * 6),
    ("gh-token", "gho_" + "q" * 24),
    ("aws-id", "AKIA" + "IOSFODNN7EXAMPLE"),
    ("slack", "xoxb-" + "1234567890-abcdef"),
    ("linear", "lin_api_" + "k" * 24),
    ("url-userinfo", "https://deploy:hunter2@git.example.com/org/repo"),
    (
        "pem",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----",
    ),
    (
        "jwt",
        "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + ".sig" + "nature12345",
    ),
    ("keyword", "password: hunter2"),
)
_LEAKED_FRAGMENTS = (
    "hunter2",
    "Ab1_",
    "11AB",
    "qqqq",
    "IOSFODNN7",
    "1234567890",
    "kkkk",
    "MIIEow",
    "hbGciOi",
)


@pytest.mark.parametrize(
    ("name", "raw"), _EXTRA_SHAPES, ids=[n for n, _ in _EXTRA_SHAPES]
)
def test_extra_secret_shapes_are_redacted(name: str, raw: str) -> None:
    result = _redact(f"quoted: {raw} end")
    assert "<redacted>" in result, name
    for fragment in _LEAKED_FRAGMENTS:
        assert fragment not in result, (name, fragment)


def test_sk_proj_dash_key_is_redacted() -> None:
    raw = "sk-" + "proj-" + "Zy9-" * 8
    assert _redact(f"key {raw}") == "key <redacted>"


def test_extra_patterns_align_with_the_shapes() -> None:
    """Kill-check: dropping any extra pattern shifts the pairing and fails here."""
    patterns = _mod._EXTRA_SECRET_PATTERNS
    assert len(patterns) == len(_EXTRA_SHAPES)
    for pattern, (name, raw) in zip(patterns, _EXTRA_SHAPES, strict=True):
        assert pattern.search(raw), name


def test_url_userinfo_keeps_the_host() -> None:
    result = _redact("clone https://deploy:hunter2@git.example.com/org/repo now")
    assert result == "clone https://<redacted>@git.example.com/org/repo now"


@pytest.mark.parametrize(
    "text",
    [
        "pip install sk-learn and ask-me",
        "ghx_short and gho_tiny",
        "https://git.example.com:8443/org/repo",
        "ssh://git@github.com/org/repo",
        "eyJ.only.two",
        "AKIA-not-an-id",
        "the secret sauce",
    ],
)
def test_benign_text_is_untouched_by_the_extra_shapes(text: str) -> None:
    assert _redact(text) == text


def test_extra_shapes_are_idempotent() -> None:
    text = " ".join(raw for _, raw in _EXTRA_SHAPES)
    once = _redact(text)
    assert _redact(once) == once


# ---------------------------------------------------------------------------
# Never truncates, idempotent, byte-identical when there is nothing to redact
# ---------------------------------------------------------------------------


def test_long_input_is_never_truncated() -> None:
    head = f"MUST_FIX at {WORKTREE}/src/cw/x.py\n"
    line = "MUST_FIX finding detail, quoted verbatim from the ship-it gate.\n"
    body = line * (LONG_TARGET_CHARS // len(line) + 1)
    text = head + body + "TAIL-MARKER"
    assert len(text) > LONG_TARGET_CHARS

    result = _redact(text)

    assert len(result) == len(text) - len(WORKTREE) + len("<worktree>")
    assert result.endswith(body + "TAIL-MARKER")
    assert "[truncated" not in result


def test_redaction_is_idempotent() -> None:
    text = f"{WORKTREE}/a {TRANSCRIPT} /home/bob/x {SK_SHAPE} {AUTH_SHAPE}"
    once = _redact(text)
    assert _redact(once) == once


@pytest.mark.parametrize(
    "text", ["plain text, no trailing newline", "a\r\nb\n", "", "\n\n"]
)
def test_text_without_paths_or_secrets_is_byte_identical(text: str) -> None:
    assert _redact(text) == text


@pytest.mark.parametrize("empty", ["", "   ", "/"])
def test_empty_values_insert_no_placeholder(empty: str) -> None:
    """F3: an empty value must never reach ``str.replace``."""
    assert _redact("abc", home=empty, worktrees=(empty,)) == "abc"
    assert _redact("abc /home/alice/x", home=empty, worktrees=(empty,)) == (
        "abc <home>/x"
    )


def test_redact_paths_leaves_secrets_alone() -> None:
    result = _mod.redact_paths(f"/home/alice/a {SK_SHAPE}", home=HOME, worktrees=[])
    assert result == f"<home>/a {SK_SHAPE}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(
    stdin_text: str,
    argv: list[str],
    environ: dict[str, str] | None = None,
) -> tuple[int, str]:
    stdout = io.StringIO()
    code = _mod.main(
        argv,
        stdin=io.StringIO(stdin_text),
        stdout=stdout,
        environ={"HOME": HOME} if environ is None else environ,
    )
    assert isinstance(code, int)
    return code, stdout.getvalue()


def test_main_redacts_stdin_to_stdout() -> None:
    code, out = _main(
        f"{WORKTREE}/a\n/srv/w/b\n/home/alice/c",
        ["--worktree", WORKTREE, "--worktree", "/srv/w"],
    )
    assert code == 0
    assert out == "<worktree>/a\n<worktree>/b\n<home>/c"


def test_main_empty_worktree_flag_is_ignored_beside_a_real_one() -> None:
    code, out = _main(
        f"{WORKTREE}/a abc",
        ["--worktree", "", "--worktree", WORKTREE],
    )
    assert code == 0
    assert out == "<worktree>/a abc"


def test_main_empty_stdin_gives_empty_stdout() -> None:
    assert _main("", []) == (0, "")


def test_main_empty_home_skips_only_the_home_value() -> None:
    code, out = _main("/home/alice/x /opt/y", [], environ={"HOME": ""})
    assert code == 0
    assert out == "<home>/x /opt/y"


def test_main_unset_home_falls_back_to_tilde_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/opt/fallback-home")
    code, out = _main("/opt/fallback-home/f", [], environ={})
    assert code == 0
    assert out == "<home>/f"


def test_main_unresolvable_home_keeps_the_user_home_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_home(_self: Path) -> Path:
        message = "Could not determine home directory."
        raise RuntimeError(message)

    monkeypatch.setattr(Path, "expanduser", _no_home)
    code, out = _main("/home/alice/x abc", [], environ={})
    assert code == 0
    assert out == "<home>/x abc"


def test_main_unknown_flag_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _main("", ["--bogus"])
    assert excinfo.value.code == 2


def test_main_default_streams_round_trip_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real stdin/stdout path keeps CRLF and undecodable bytes as-is."""
    raw_in = io.BytesIO(b"x\r\n/home/alice/a \xff")
    raw_out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(raw_in, encoding="utf-8"))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw_out, encoding="utf-8"))
    monkeypatch.setenv("HOME", HOME)

    assert _mod.main([]) == 0

    assert raw_out.getvalue() == b"x\r\n<home>/a \xff"


def test_cli_subprocess_reads_stdin_and_writes_stdout() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--worktree", WORKTREE],
        input=f"{WORKTREE}/a\r\n{SK_SHAPE}".encode(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == b"<worktree>/a\r\n<redacted>"


def test_cli_subprocess_unknown_flag_exits_2() -> None:
    result = run_guard_script_cli(SCRIPT, ["--bogus"])
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_script_carries_the_version_marker_on_line_2() -> None:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env python3"
    assert lines[1] == "# cw-script-version: 1"
