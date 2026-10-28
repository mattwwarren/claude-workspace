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
import time
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
LONG_RUN_MAX_SECONDS = 2.0


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


def test_custom_home_value_does_not_eat_a_longer_name() -> None:
    assert _redact("/var/lib/dave2/x", home="/var/lib/dave") == "/var/lib/dave2/x"


def test_custom_home_value_keeps_trailing_punctuation() -> None:
    assert _redact("/var/lib/dave.", home="/var/lib/dave") == "<home>."


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


# (raw, whole-string result). The OS username is in pytest's tmp root and in the
# `~user` form; neither is `$HOME`, `/home/<u>` or `/Users/<u>`.
_USERNAME_PATH_SHAPES = (
    (
        "/tmp/pytest-of-matthew/pytest-3/test_x0/a.py",
        "/tmp/pytest-of-<redacted>/pytest-3/test_x0/a.py",
    ),
    ("pytest-of-jane.doe/pytest-0", "pytest-of-<redacted>/pytest-0"),
    ("~matthew/x", "<home>/x"),
    ("~jane.doe/src/cw", "<home>/src/cw"),
    ("~matthew/.cw/wt/abcd1234/dev-1/y", "<worktree>/y"),
    ("~matthew/.claude/projects/-a-b/c.jsonl", TRANSCRIPT_REDACTED),
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    _USERNAME_PATH_SHAPES,
    ids=[raw for raw, _ in _USERNAME_PATH_SHAPES],
)
def test_username_path_shapes_are_redacted_whole(raw: str, expected: str) -> None:
    """A tail the pattern missed would survive; so would a stray username."""
    assert _redact(raw, home="/srv/other") == expected
    assert _redact(f"at ({raw}) end", home="/srv/other") == f"at ({expected}) end"
    assert _redact(expected, home="/srv/other") == expected
    assert "matthew" not in _redact(raw, home="/srv/other")


@pytest.mark.parametrize(
    "text",
    [
        "took ~3 files and ~100ms",
        "~/notes and ~ alone",
        "docs/~matthew/x",
        "no-pytest-of-matthew-here",
        "mypytest-of-matthew/x",
    ],
)
def test_username_path_shapes_leave_lookalikes_alone(text: str) -> None:
    assert _redact(text, home="/srv/other") == text


ENCODED_CWD = "-home-alice--cw-wt-abcd1234-dev-2794"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"see /tmp/claude-1000/{ENCODED_CWD}/tasks/b1.output",
            "see /tmp/claude-<redacted>/<redacted>/tasks/b1.output",
        ),
        (
            "/private/tmp/claude-501/-Users-bob-src/tasks/x.output",
            "/private/tmp/claude-<redacted>/<redacted>/tasks/x.output",
        ),
        ("cat /tmp/claude-1000.", "cat /tmp/claude-<redacted>."),
        ("/tmp/claude-1000/", "/tmp/claude-<redacted>/"),
    ],
)
def test_claude_tmp_dir_hides_the_uid_and_encoded_cwd(text: str, expected: str) -> None:
    """The uid dir and its encoded-cwd child go even when HOME is unknown."""
    assert _redact(text, home="/srv/other") == expected
    assert _redact(text, home=HOME, worktrees=(WORKTREE,)) == expected
    assert _redact(expected, home="/srv/other") == expected


def test_claude_tmp_lookalikes_pass_through() -> None:
    text = "/tmp/claude-abc/x and /tmp/claudette-1000/x and claude-1000/x"
    assert _redact(text, home="/srv/other") == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"cwd {ENCODED_CWD} now", "cwd <worktree> now"),
        (f"{ENCODED_CWD}/x", "<worktree>/x"),
        ("-home-alice-projects-foo", "<home>-projects-foo"),
        ("'-home-alice'", "'<home>'"),
        ("at -srv-wt-7 now", "at <worktree> now"),
    ],
)
def test_encoded_home_and_worktree_names_are_redacted(text: str, expected: str) -> None:
    result = _redact(text, home=HOME, worktrees=("/srv/wt-7",))
    assert result == expected
    assert _redact(result, home=HOME, worktrees=("/srv/wt-7",)) == result


def test_encoded_worktree_value_replaces_with_the_worktree_placeholder() -> None:
    result = _redact(f"x {ENCODED_CWD}", home="/srv/other", worktrees=(WORKTREE,))
    assert result == "x <worktree>"
    assert "alice" not in result


@pytest.mark.parametrize(
    "text", ["-home-alice2-x", "x-home-alice-y", "a.-home-alice", "-srv-wt-70"]
)
def test_encoded_names_leave_lookalikes_alone(text: str) -> None:
    assert _redact(text, home=HOME, worktrees=("/srv/wt-7",)) == text


def test_single_segment_encoded_home_is_not_treated_as_a_path() -> None:
    """HOME=/root encodes to `-root`, which is also a CLI flag."""
    assert _redact("run -root now", home="/root") == "run -root now"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("file:///home/alice/x", "file://<home>/x"),
        ("file:///home/jane.doe/x.py", "file://<home>/x.py"),
        ("see file:///Users/bob/src.", "see file://<home>/src."),
        ("file:///home/alice/.cw/wt/ab12/dev-1/y", "file://<worktree>/y"),
        (
            "file:///home/bob/.claude/projects/-a/b.jsonl",
            f"file://{TRANSCRIPT_REDACTED}",
        ),
        ("file:///var/lib/dave/notes", "file://<home>/notes"),
        ("file:///srv/wt-7/x.py", "file://<worktree>/x.py"),
    ],
)
def test_file_url_home_paths_are_redacted(text: str, expected: str) -> None:
    """`file://` is a path start; the scheme stays and the path part goes."""
    result = _redact(text, home="/var/lib/dave", worktrees=("/srv/wt-7",))
    assert result == expected
    assert _redact(result, home="/var/lib/dave", worktrees=("/srv/wt-7",)) == result


def test_file_url_lookalikes_pass_through() -> None:
    for text in ("file:///srv/other/x", "http://x.io/home/alice/x", "xfile:///home/a"):
        assert _redact(text, home="/srv/none") == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/srv/root/x.py", "/srv/root/x.py"),
        ("a.b/root/x", "a.b/root/x"),
        ("/root/x.py", "<home>/x.py"),
        ("(/root/x)", "(<home>/x)"),
        ("path=/root/x", "path=<home>/x"),
        ("/root/.cw/wt/ab12/dev-1/y", "<worktree>/y"),
        ("/srv/root/.cw/wt/ab12/dev-1/y", "/srv/root/.cw/wt/ab12/dev-1/y"),
        ("/root/.claude/projects/-a/b.jsonl", TRANSCRIPT_REDACTED),
        ("/srv/root/.claude/projects/-a/b", "/srv/root/.claude/projects/-a/b"),
    ],
)
def test_literal_home_only_matches_at_a_path_start(text: str, expected: str) -> None:
    """HOME=/root is the container default; it must not mangle `/srv/root/x`."""
    assert _redact(text, home="/root") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("src/cw/wt-7/x", "src/cw/wt-7/x"),
        ("/srv/wt-7/x", "<worktree>/x"),
        ("see (/srv/wt-7/x)", "see (<worktree>/x)"),
        ("cwd=/srv/wt-7", "cwd=<worktree>"),
    ],
)
def test_explicit_worktree_only_matches_at_a_path_start(
    text: str, expected: str
) -> None:
    assert _redact(text, worktrees=("/srv/wt-7",)) == expected
    assert _redact("src/cw/wt-7/x", worktrees=("/wt-7",)) == "src/cw/wt-7/x"
    assert _redact("/wt-7/x", worktrees=("/wt-7",)) == "<worktree>/x"


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
    """The mirror is exact; the full set is the mirror plus distinct extras.

    Order between the two groups is behaviour, not pinned here: the tail-leak
    tests below fail if a narrow mirrored shape runs before a wider extra one.
    """
    upstream = [p.pattern for p in _text._SECRET_PATTERNS]
    mirrored = [p.pattern for p in _mod._MIRRORED_SECRET_PATTERNS]
    extras = [p.pattern for p in _mod._EXTRA_SECRET_PATTERNS]
    local = [p.pattern for p in _mod._SECRET_PATTERNS]
    assert mirrored == upstream
    assert extras
    assert not set(extras) & set(upstream)
    assert sorted(local) == sorted(upstream + extras)


# (name, raw shape, whole-string result of redacting it, the span one extra pattern
# must fullmatch). Each shape is assembled from parts so no literal reads as a
# hard-coded credential. The exact result is what catches a pattern that covers
# only a prefix of its shape.
_EXTRA_SHAPES = (
    ("sk-ant", "sk-" + "ant-api03-" + "Ab1_" * 8, "<redacted>", None),
    ("sk-live", "sk_" + "live_" + "a1B2c3D4e5F6", "<redacted>", None),
    ("sk-test", "sk_" + "test_" + "a1B2c3D4e5F6", "<redacted>", None),
    ("github_pat", "github_pat_" + "11AB" * 6, "<redacted>", None),
    ("gh-oauth", "gho_" + "q" * 24, "<redacted>", None),
    ("gh-pat", "ghp_" + "w" * 36, "<redacted>", None),
    ("glpat", "glpat-" + "aB3dE5gH7j" * 2, "<redacted>", None),
    ("npm", "npm_" + "aB3dE5gH7j" * 3, "<redacted>", None),
    ("google", "AIza" + "Sy" + "a1B2c3D4e" * 3 + "Z" * 6, "<redacted>", None),
    ("aws-id", "AKIA" + "IOSFODNN7EXAMPLE", "<redacted>", None),
    ("aws-sts-id", "ASIA" + "IOSFODNN7EXAMPLE", "<redacted>", None),
    ("slack", "xoxb-" + "1234567890-abcdef", "<redacted>", None),
    ("linear", "lin_api_" + "k" * 24, "<redacted>", None),
    (
        "url-userinfo",
        "https://deploy:hunter2@git.example.com/org/repo",
        "https://<redacted>@git.example.com/org/repo",
        "deploy:hunter2",
    ),
    (
        "pem",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----",
        "<redacted>",
        None,
    ),
    (
        "jwt",
        "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + ".sig" + "nature12345",
        "<redacted>",
        None,
    ),
    ("authorization-basic", "Authorization: Basic dXNlcjpwYXNz", "<redacted>", None),
    ("authorization-bearer", "Authorization: Bearer abc.def", "<redacted>", None),
    ("authorization-token", "Authorization: Token abc123", "<redacted>", None),
    ("keyword", "password: hunter2", "<redacted>", None),
    ("keyword-env", "DB_PASSWORD=hunter2", "<redacted>", None),
    ("keyword-pg", "PGPASSWORD=hunter2", "<redacted>", None),
    ("keyword-secret-key", "SECRET_KEY=hunter2", "<redacted>", None),
    ("keyword-api-key", "OPENAI_API_KEY=hunter2", "<redacted>", None),
    ("keyword-aws", "AWS_SECRET_ACCESS_KEY=hunter2/abc", "<redacted>", None),
    ("keyword-env-token", "GITHUB_TOKEN=hunter2", "<redacted>", None),
    ("keyword-key-suffix", "STRIPE_KEY=hunter2", "<redacted>", None),
    ("keyword-private-key", "PRIVATE_KEY=hunter2", "<redacted>", None),
    ("keyword-access-key", "ACCESS_KEY=hunter2", "<redacted>", None),
    ("keyword-dash-key", "deploy-key: hunter2", "<redacted>", None),
    ("cookie-quoted", 'cookie = "sid=abc123 theme=hunter2"', "<redacted>", None),
    ("json-cookie", '{"cookie": "sid=abc123"}', "{<redacted>}", None),
    ("json-api-key", '{"api_key": "hunter2hunter2"}', "{<redacted>}", None),
    ("json-password", '{"password": "hunter2"}', "{<redacted>}", None),
    ("json-token", '{"token": "hunter2"}', "{<redacted>}", None),
    (
        "quoted-multi-word",
        'password = "correct horse hunter2 staple"',
        "<redacted>",
        None,
    ),
    ("single-quoted-multi-word", "secret = 'hunter2 two'", "<redacted>", None),
    ("keyword-passphrase", "passphrase=hunter2", "<redacted>", None),
    ("keyword-env-passphrase", "GPG_PASSPHRASE=hunter2", "<redacted>", None),
    ("keyword-credential", "credential: hunter2", "<redacted>", None),
    ("keyword-bearer", "bearer=hunter2", "<redacted>", None),
    ("flag-passphrase-space", "--passphrase hunter2", "<redacted>", None),
    ("flag-password-space", "--password hunter2", "<redacted>", None),
    ("flag-token-space", "--token abc123", "<redacted>", None),
    ("flag-password-equals", "--password=hunter2", "<redacted>", None),
    (
        "flag-api-key-space-quoted",
        '--api-key "correct horse hunter2"',
        "<redacted>",
        None,
    ),
)
_LEAKED_FRAGMENTS = (
    "hunter2",
    "horse",
    "Ab1_",
    "11AB",
    "qqqq",
    "IOSFODNN7",
    "1234567890",
    "kkkk",
    "MIIEow",
    "hbGciOi",
    "dXNlcjpwYXNz",
    "abc123",
    "nature12345",
)


@pytest.mark.parametrize(
    ("name", "raw", "expected", "_span"),
    _EXTRA_SHAPES,
    ids=[shape[0] for shape in _EXTRA_SHAPES],
)
def test_extra_secret_shapes_are_redacted_whole(
    name: str, raw: str, expected: str, _span: str | None
) -> None:
    """Exact output: a pattern covering only a prefix leaves a tail and fails."""
    assert _redact(raw) == expected, name
    assert _redact(f"quoted: {raw} end") == f"quoted: {expected} end", name
    for fragment in _LEAKED_FRAGMENTS:
        assert fragment not in _redact(raw), (name, fragment)


def test_every_extra_pattern_covers_a_whole_shape() -> None:
    """A pattern must cover a whole shape, not just a prefix of it."""
    for pattern in _mod._EXTRA_SECRET_PATTERNS:
        covered = [
            match.group(0) == (span if span is not None else raw)
            for _, raw, _, span in _EXTRA_SHAPES
            if (match := pattern.search(raw))
        ]
        assert any(covered), pattern.pattern


def test_sk_proj_dash_key_is_redacted() -> None:
    raw = "sk-" + "proj-" + "Zy9-" * 8
    assert _redact(f"key {raw}") == "key <redacted>"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sk-" + "A" * 24 + "-tailtailtail", "<redacted>"),
        ("x sk-" + "A" * 24 + "-tailtailtail y", "x <redacted> y"),
        ("ghp_" + "x" * 36 + "LEAKED", "<redacted>"),
        ("see ghp_" + "x" * 36 + "LEAKED.", "see <redacted>."),
    ],
)
def test_wide_shapes_win_over_the_narrow_mirrored_ones(
    text: str, expected: str
) -> None:
    """No secret tail survives a narrower mirrored match."""
    assert _redact(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "curl -H 'Authorization: Basic dXNlcjpwYXNz' url",
            "curl -H <redacted> url",
        ),
        ("Authorization: Token abc123 trailing", "<redacted> trailing"),
        ('token = "two words" after', "<redacted> after"),
        ("export DB_PASSWORD=hunter2 && run", "export <redacted> && run"),
        ("PGPASSWORD=hunter2 psql", "<redacted> psql"),
        ('{"token": "abcd1234efgh5678", "ok": 1}', '{<redacted>, "ok": 1}'),
        ("password = 'correct horse' next", "<redacted> next"),
    ],
)
def test_credential_assignments_are_consumed_whole(text: str, expected: str) -> None:
    assert _redact(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabcd\n\nnext paragraph",
            "<redacted>\n\nnext paragraph",
        ),
        (
            "-----BEGIN RSA PRIVATE KEY-----\r\nMIIEow\r\nabcd\r\n\r\nnext",
            "<redacted>\r\n\r\nnext",
        ),
        (
            "before\n-----BEGIN PRIVATE KEY-----\nMII\n-----END PRIVATE KEY-----\n"
            "after",
            "before\n<redacted>\nafter",
        ),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabcd", "<redacted>"),
        ("key: -----BEGIN PRIVATE KEY-----\nMII", "key: <redacted>"),
        # An armored PGP secret key closes its markers with ` BLOCK`.
        (
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nlQOYBF\n=abcd\n"
            "-----END PGP PRIVATE KEY BLOCK-----\ntail",
            "<redacted>\ntail",
        ),
        (
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBFabcd\n\nnext paragraph",
            "<redacted>\n\nnext paragraph",
        ),
        ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBFabcd", "<redacted>"),
    ],
)
def test_pem_block_ends_at_its_end_line_or_the_next_blank_line(
    text: str, expected: str
) -> None:
    assert _redact(text) == expected
    assert _redact(expected) == expected


# A header value runs to the end of its line (it holds spaces and `;`), so these
# cannot sit in `_EXTRA_SHAPES`, whose rows are re-checked mid-sentence.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Cookie: session=abc123", "<redacted>"),
        ("Cookie: sid=abc123; theme=hunter2; csrf=horse", "<redacted>"),
        ("Set-Cookie: sid=abc123; Path=/; HttpOnly", "<redacted>"),
        (
            "> Cookie: session=abc123\nnext line stays\n",
            "> <redacted>\nnext line stays\n",
        ),
        ("Cookie: sid=abc123\r\nHost: x", "<redacted>\r\nHost: x"),
    ],
)
def test_cookie_header_is_redacted_to_the_end_of_its_line(
    text: str, expected: str
) -> None:
    result = _redact(text)
    assert result == expected
    assert _redact(result) == result
    for fragment in ("abc123", "hunter2", "horse"):
        assert fragment not in result


@pytest.mark.parametrize("keyword", ["password", "_key", "cookie"])
def test_long_keyword_run_redacts_in_bounded_time(keyword: str) -> None:
    """A run of keyword hits with no `:`/`=` must not go quadratic.

    Every hit used to scan to the end of the run and backtrack, so N hits cost
    O(N^2): 4-8 s at 48 KB, 6-11 s at this size. Capped, the same input takes
    well under 0.1 s. The bound is an absolute wall-clock limit, not an n-vs-2n
    ratio: a ratio of two sub-0.1 s timings swings on a loaded runner, while a
    2 s ceiling sits ~20x above the capped time and 3-5x below the quadratic
    one, so only an algorithmic regression crosses it. No freezegun: it would
    freeze the clock.
    """
    text = keyword * (LONG_TARGET_CHARS // len(keyword))
    start = time.perf_counter()
    result = _redact(text)
    elapsed = time.perf_counter() - start
    assert result == text
    assert elapsed < LONG_RUN_MAX_SECONDS, elapsed


_FLAG_NAMES = (
    "password",
    "passwd",
    "passphrase",
    "credential",
    "bearer",
    "secret",
    "client-secret",
    "api-key",
    "api_key",
    "apikey",
    "access-key",
    "token",
    "auth-token",
    "authorization",
)


@pytest.mark.parametrize("name", _FLAG_NAMES)
@pytest.mark.parametrize("dashes", ["--", "-"])
@pytest.mark.parametrize("separator", [" ", "=", "  ", "\t"])
def test_secret_flag_value_is_redacted_in_every_separator_form(
    name: str, dashes: str, separator: str
) -> None:
    """`--password VALUE` and `--password=VALUE` both drop the value."""
    text = f"run tool {dashes}{name}{separator}hunter2 --verbose"
    result = _redact(text)
    assert "hunter2" not in result, result
    assert result == "run tool <redacted> --verbose"
    assert _redact(result) == result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('login --password "correct horse" now', "login <redacted> now"),
        ("login --password 'correct horse' now", "login <redacted> now"),
        ("x --token abc123\nnext line stays", "x <redacted>\nnext line stays"),
        ("x --token abc123\r\nnext", "x <redacted>\r\nnext"),
        ("--DB-PASSWORD hunter2", "<redacted>"),
        ("--password hunter2", "<redacted>"),
        # A cookie header value runs to the end of its line (see the cookie
        # pattern), so the flag form takes one value and no more.
        ("curl --cookie sid=abc123 --verbose", "curl <redacted> --verbose"),
    ],
)
def test_secret_flag_value_boundaries(text: str, expected: str) -> None:
    assert _redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "pass --password --verbose next",
        "pass --token",
        "pass --token\nnext line",
        "the password reset flow needs a token before login",
        "set the api key in the settings page",
        "use --no-verify and --keep-going",
        "pass the test and check the credentials page",
        "pass --passphrase",
        "pass --passphrase --verbose next",
        "the bearer of the token passes through",
    ],
)
def test_secret_flag_form_leaves_prose_and_valueless_flags_alone(text: str) -> None:
    """Only a dash-led flag word followed by a value on the same line is redacted."""
    assert _redact(text) == text


@pytest.mark.parametrize(
    "keyword", ["--password", "--token", "--api-key", "-key", "--passphrase"]
)
def test_long_flag_keyword_run_redacts_in_bounded_time(keyword: str) -> None:
    """The flag form shares the identifier cap: a run of hits must stay linear."""
    text = keyword * (LONG_TARGET_CHARS // len(keyword))
    start = time.perf_counter()
    result = _redact(text)
    elapsed = time.perf_counter() - start
    assert result == text
    assert elapsed < LONG_RUN_MAX_SECONDS, elapsed


_PEM_BEGIN = "-----BEGIN PRIVATE KEY-----"
_PEM_END = "-----END PRIVATE KEY-----"
_PGP_BEGIN = "-----BEGIN PGP PRIVATE KEY BLOCK-----"
# The old pattern scanned from every BEGIN marker to the end of the text looking
# for an END line, so N unterminated markers cost O(N^2): 0.24 s at 49 KB, 22 s at
# 493 KB. The marker inputs are sized so the quadratic form crosses the bound.
_PEM_MARKER_CHARS = 420_000
_PEM_ADVERSARIAL = {
    "markers-blank-line-separated": (_PEM_BEGIN + "\n\n") * (_PEM_MARKER_CHARS // 28),
    "markers-newline-separated": (_PEM_BEGIN + "\n") * (_PEM_MARKER_CHARS // 27),
    "markers-same-line": (_PEM_BEGIN + " ") * (_PEM_MARKER_CHARS // 27),
    "pgp-markers-blank-line-separated": (_PGP_BEGIN + "\n\n")
    * (_PEM_MARKER_CHARS // 40),
    "pgp-markers-same-line": (_PGP_BEGIN + " ") * (_PEM_MARKER_CHARS // 39),
    "unterminated-with-long-body": _PEM_BEGIN + "\n" + "A" * LONG_TARGET_CHARS * 4,
    "markers-then-long-body": (_PEM_BEGIN + "\n\n") * 200
    + _PEM_BEGIN
    + "\n"
    + "B" * LONG_TARGET_CHARS,
}


@pytest.mark.parametrize("name", sorted(_PEM_ADVERSARIAL))
def test_pem_marker_runs_redact_in_bounded_time(name: str) -> None:
    """Repeated or unterminated PEM markers must not go quadratic.

    Absolute wall-clock ceiling for the same reason as the keyword-run test.
    """
    text = _PEM_ADVERSARIAL[name]
    start = time.perf_counter()
    result = _redact(text)
    elapsed = time.perf_counter() - start
    assert elapsed < LONG_RUN_MAX_SECONDS, elapsed
    assert _PEM_BEGIN not in result
    assert _PGP_BEGIN not in result
    assert "AAAA" not in result
    assert "BBBB" not in result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A well-formed block is consumed whole, END line included, even with a
        # blank line inside its body.
        (f"{_PEM_BEGIN}\nMIIEow\n\nabcd\n{_PEM_END}\ntail", "<redacted>\ntail"),
        # The END line closes the block it belongs to; the second block is its own.
        (
            f"{_PEM_BEGIN}\nAAAA\n{_PEM_END}\nkeep\n{_PEM_BEGIN}\nBBBB\n{_PEM_END}",
            "<redacted>\nkeep\n<redacted>",
        ),
        # An unterminated block followed by a complete one: the first runs to its
        # blank line, the second is still consumed whole.
        (
            f"{_PEM_BEGIN}\nAAAA\n\n{_PEM_BEGIN}\nBBBB\n{_PEM_END}",
            "<redacted>\n\n<redacted>",
        ),
    ],
)
def test_pem_pattern_still_consumes_well_formed_blocks(
    text: str, expected: str
) -> None:
    assert _redact(text) == expected


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
        "sort the monkey: banana and hotkey = f2",
        "bake a cookie jar without a colon",
    ],
)
def test_benign_text_is_untouched_by_the_extra_shapes(text: str) -> None:
    assert _redact(text) == text


def test_extra_shapes_are_idempotent() -> None:
    text = " ".join(raw for _, raw, _, _ in _EXTRA_SHAPES)
    once = _redact(text)
    assert _redact(once) == once
    for _, raw, expected, _ in _EXTRA_SHAPES:
        assert _redact(expected) == expected, raw


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
