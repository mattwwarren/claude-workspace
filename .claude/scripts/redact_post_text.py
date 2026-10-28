#!/usr/bin/env python3
# cw-script-version: 1
"""Redact paths and secrets from text bound for a public tracker post (#2794).

Usage (from `/auto-dev-finalize`'s "Finalize review-findings comment" step):
    python3 .claude/scripts/redact_post_text.py \\
        --worktree "$GUARD_ROOT" --worktree "$CTX_WORKTREE" \\
        < raw.txt > redacted.txt

A FINALIZE run whose `/prep-pr` ship-it gate blocked on review findings posts
those findings to the ticket so the regressed IMPL run can read them. The
repository is public, and finding text routinely carries this machine's
worktree paths, home directory and `~/.claude/projects/<encoded>` transcript
directory names (the encoded name embeds the username), so the text is run
through this script before it is split into parts and posted.

Shapes replaced, paths first, then secrets:
    1. each `--worktree <path>` value, longest first   -> `<worktree>`
    2. `(/home/<u>|/Users/<u>|$HOME|~)/.cw/wt/<hash>[/<name>]` -> `<worktree>`
    3. `(/home/<u>|/Users/<u>|$HOME|~)/.claude/projects/<...>`
                                     -> `<home>/.claude/projects/<redacted>`
    4. the `$HOME` value, `/home/<u>`, `/Users/<u>` and `~<u>/` -> `<home>`
    5. the user segment of pytest's tmp root `pytest-of-<u>`  -> `pytest-of-<redacted>`
    6. `/tmp/claude-<uid>/<encoded cwd>` (Claude Code's task output dir)
                                     -> `/tmp/claude-<redacted>/<redacted>`
    7. the encoded cwd name (`/` and `.` made `-`: `-home-<u>--cw-wt-<hash>-<n>`)
       of each `--worktree` value -> `<worktree>`, of `$HOME` -> `<home>`
    8. secret shapes (a superset of `cw._text._SECRET_PATTERNS`) -> `<redacted>`

The `/home/<u>`, `/Users/<u>`, `~<u>` and `~` shapes, and the literal `$HOME` and
`--worktree` values, only match at the start of a path
(start of text, after `file://`, or after a character other than a word
character, `.`, `/` or `-`), so a repo-relative `web/src/home/Hero.tsx` or a URL
path `/Users/guide` is left alone while `file:///home/<u>/x` becomes
`file://<home>/x`. An encoded name matches where a name starts and not before a
word character, so `-home-alice2` is not `-home-alice`.

Redaction replaces spans and never truncates: the output differs from the
input only inside replaced spans, a missing trailing newline stays missing,
and the CLI round-trips CRLF and undecodable bytes unchanged. Over-redaction is
tolerated, as in `cw._text`.

Stdlib-only by design: nothing in `.claude/scripts/` depends on the `cw`
package, so the secret patterns are a copy. They are a SUPERSET of
`cw._text._SECRET_PATTERNS`: `_MIRRORED_SECRET_PATTERNS` copies it (pinned by
`tests/test_redact_post_text.py`), and `_EXTRA_SECRET_PATTERNS` adds the vendor
token, URL-credential, private-key, JWT and keyword-assignment shapes that
`cw._text` does not cover. The extras run first, so a wider shape consumes a
secret's whole run before a narrower mirrored one can leave its tail behind.
`cw._text` redacts logs read by the operator; this text goes to a PUBLIC
tracker, an irreversible sink where a finding that quotes the secret it flags
must not carry it.

Exit codes:
    0  -- redacted text written to stdout
    2  -- usage error (argparse)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import TextIO

WORKTREE_PLACEHOLDER = "<worktree>"
HOME_PLACEHOLDER = "<home>"
REDACTION_PLACEHOLDER = "<redacted>"
TRANSCRIPT_PLACEHOLDER = f"{HOME_PLACEHOLDER}/.claude/projects/{REDACTION_PLACEHOLDER}"

# Undecodable input bytes survive the round trip instead of failing the post.
_ENCODING = "utf-8"
_ERRORS = "surrogateescape"

# One path segment. A trailing "." is never part of it, so a path that ends a
# sentence keeps its full stop. A "+" is a legal username character (`john+doe`).
_SEGMENT = r"[\w+-]+(?:\.[\w+-]+)*"
# A user-home shape starts a path: it is not preceded by a word character, ".",
# "/" or "-", so `src/home/x.tsx` and `https://x.io/Users/guide` are not homes.
# A `file://` scheme is the one exception: `file:///home/<u>/x` is a home path,
# and the scheme stays in the output (`file://<home>/x`). So is a single-letter
# short flag glued to the path (`-I/home/<u>/inc`, `git -C/home/<u>/repo`): the
# flag letter is a word character, but the flag itself starts at a boundary.
_PATH_START = r"(?:(?<![\w./-])|(?<=(?<!\w)file://)|(?<=(?<![\w./-])-[A-Za-z]))"
# `~user/` is a tilde-user home; a bare `~3 files` or `~/x` is not.
_TILDE_USER = rf"~(?=[A-Za-z_]){_SEGMENT}(?=/)"
_USER_HOMES = rf"{_PATH_START}(?:/home/{_SEGMENT}|/Users/{_SEGMENT}|{_TILDE_USER})"
# A replaced value must not be the prefix of a longer name ("wt-7" in "wt-70").
_NOT_MID_NAME = r"(?![\w-])"

# Claude Code names a project directory after its cwd with every "/" and "." made
# a "-": `/home/u/.cw/wt/h/n` is `-home-u--cw-wt-h-n`. That name also appears
# outside `~/.claude/projects/`, e.g. in `/tmp/claude-<uid>/<name>/tasks/<id>.output`.
# An encoded value starts a name (the "-" it begins with must not follow a word
# character, "." or "-") and ends before a word character; a "-" may follow,
# since it continues the encoded path (`-home-u-src`).
_ENCODED_START = r"(?<![\w.-])"
_ENCODED_END = r"(?!\w)"
# An encoded value with fewer interior dashes (`-root` for HOME=/root) is too
# common a token to treat as a path.
_MIN_ENCODED_DASHES = 2

# `<tmp>/claude-<uid>/<encoded cwd>`: the uid and the cwd name both identify the
# user, and they survive even when HOME and `--worktree` were not supplied. The
# `tmp/` stays (it also covers macOS `/private/tmp`); only the two names go.
CLAUDE_TMP_ROOT_PLACEHOLDER = f"claude-{REDACTION_PLACEHOLDER}"
CLAUDE_TMP_PLACEHOLDER = f"{CLAUDE_TMP_ROOT_PLACEHOLDER}/{REDACTION_PLACEHOLDER}"
_CLAUDE_TMP_CWD = re.compile(r"(?<=/tmp/)claude-\d+/[\w-]+")
_CLAUDE_TMP_ROOT = re.compile(r"(?<=/tmp/)claude-\d+(?![\w-])")

# pytest's per-user tmp root (`/tmp/pytest-of-<username>/pytest-3/...`): the
# most common path shape in test-failure findings, and it embeds the OS username.
_PYTEST_USER_DIR = re.compile(rf"((?<![\w-])pytest-of-){_SEGMENT}")

# Mirror of cw._text._SECRET_PATTERNS, same order; see that module for the
# rationale of each shape. A test pins these as equal to the upstream tuple.
_MIRRORED_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"(?<=[=:])[A-Za-z0-9_-]{32,}"),
    re.compile(r"(?i)\b(?:Authorization|token)\s*[:=]\s*\S+"),
)

# An assignment value: a whole quoted string (spaces included), else one token.
# A backslash escapes the next character inside the quotes, so `"ab\"cd"` is one
# value. The two branches of each loop are disjoint (`[^"\\]` never matches a
# backslash), so a quoted value is scanned once, without backtracking.
_ASSIGNED_VALUE = r"""(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\S+)"""
# An identifier around a keyword (`DB_PASSWORD`, `AWS_SECRET_ACCESS_KEY`). The
# prefix starts only where a word run starts, so a run is tried from one offset,
# not from every offset inside it; the suffix is capped. Without the cap, a run
# holding N keyword hits (`passwordpassword...`, no `:`/`=`) scanned to the end
# of the run and backtracked from every hit: O(N^2), seconds at 48 KB. With it,
# each hit costs at most `_IDENT_SUFFIX_MAX` steps. An identifier tail longer
# than the cap is not a real variable name; its value is left in place.
_IDENT_START = r"(?<![\w-])[\w-]*"
_IDENT_SUFFIX_MAX = 128
_IDENT_SUFFIX = rf"[\w-]{{0,{_IDENT_SUFFIX_MAX}}}"
# The credential keywords shared by the `=`/`:` assignment form and the
# space-separated flag form, defined once so the two cannot drift. `token`,
# `authorization` and `cookie` have their own assignment patterns (they take a
# scheme word or a whole header line) and join the keywords only for the flag form.
_CREDENTIAL_KEYWORDS = (
    r"password|passwd|passphrase|secret|credential|bearer|api[_-]?key|[_-]key"
)
_FLAG_KEYWORDS = rf"{_CREDENTIAL_KEYWORDS}|token|authorization|cookie"

# What a clipped key block (no END line) may carry before its body: armor or
# encapsulation headers (`Version: GnuPG v1`, `Proc-Type: 4,ENCRYPTED`,
# `DEK-Info: ...`) and then the blank line RFC 4880 / RFC 1421 put before the
# body. Without it the clipped-block scan stopped at that blank line and left the
# whole body behind. Optional: a body that starts right after the marker keeps the
# plain blank-line stop. Each header is one bounded line, so the loop stays linear.
_CLIPPED_KEY_PREAMBLE = r"(?:\r?\n(?:[A-Za-z][A-Za-z0-9-]*:[^\r\n]*\r?\n)*[ \t]*\r?\n)?"

# Shapes beyond cw._text, for a public and irreversible sink: a review finding
# often quotes the very secret it flags. Over-redaction is tolerated. These run
# BEFORE the mirrored shapes: a mirrored shape is a fixed-width or narrower
# cousin of a wider one here (`sk-` + 20 alphanumerics vs. `sk-` + any run of
# `[A-Za-z0-9_-]`), and letting it match first leaves the wider shape's tail
# behind it (`<redacted>-tail`).
_EXTRA_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # A PEM private key, END line included; a clipped block with no END line
    # runs to the next blank line (left in place) or the end of the text, past
    # the armor-header preamble and its blank line when it has one. The
    # END search stops at the next `-----BEGIN ` marker: unbounded, it scanned to
    # the end of the text from every unterminated marker, O(N^2) in the number of
    # markers (22 s at 493 KB). Bounded, each marker scans only up to the next
    # one, so the whole text is scanned O(1) times; a well-formed block holds no
    # second BEGIN line, so it still matches whole. An armored PGP key closes its
    # marker with ` BLOCK` (`PGP PRIVATE KEY BLOCK`), hence the optional suffix.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
        r"(?:(?:(?!-----BEGIN ).)*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
        r"|" + _CLIPPED_KEY_PREAMBLE + r".*?(?=\r?\n[ \t]*\r?\n|\Z))",
        re.DOTALL,
    ),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Anthropic / OpenAI keys with "-" or "_" inside (sk-ant-api03-..., sk-proj-...).
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"npm_[A-Za-z0-9]{30,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    # AKIA = long-lived access key id, ASIA = temporary (STS) key id.
    re.compile(r"A(?:KIA|SIA)[0-9A-Z]{16}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"lin_api_[A-Za-z0-9]{20,}"),
    # user:password@ inside a URL; the host and the rest of the URL stay.
    re.compile(r"(?<=://)[^\s/@:]+:[^\s/@]+(?=@)"),
    # `Authorization: Basic <b64>` / `Bearer <t>` / `token = "<t>"`: the scheme
    # word AND the credential after it, or the credential would survive.
    re.compile(
        rf"(?i)[\"']?{_IDENT_START}(?:Authorization|token)[\"']?\s*[:=]\s*"
        rf"(?:(?:Basic|Bearer|Token)\s+)?{_ASSIGNED_VALUE}"
    ),
    re.compile(
        rf"(?i)[\"']?{_IDENT_START}(?:{_CREDENTIAL_KEYWORDS})"
        rf"{_IDENT_SUFFIX}[\"']?\s*[:=]\s*{_ASSIGNED_VALUE}"
    ),
    # A space-separated flag: `--password hunter2`, `--token abc`, `-api-key k`. The
    # `=` form is the keyword pattern above. A dash-led word only (prose such as
    # "password reset" is left alone), a value on the SAME line (`[ \t]+`, so a
    # bare flag never takes the next line), and a value that does not look like a long
    # option (`--token --verbose` keeps `--verbose`; a single leading "-" is a
    # credential, since ~1 in 64 urlsafe-base64 tokens starts with one). The identifier suffix cap
    # is shared with the patterns above, so a run of hits stays linear.
    re.compile(
        rf"(?i)(?<![\w-])-{{1,2}}[\w-]*(?:{_FLAG_KEYWORDS})"
        rf"{_IDENT_SUFFIX}[ \t]+(?!--){_ASSIGNED_VALUE}"
    ),
    # `Cookie: a=1; b=2` / `Set-Cookie: sid=...; Path=/`: a header value runs to
    # the end of the line and holds spaces and `;`, so one token would leak the rest.
    re.compile(
        rf"(?i)[\"']?{_IDENT_START}cookie{_IDENT_SUFFIX}[\"']?\s*[:=]\s*"
        r"""(?:"[^"]*"|'[^']*'|[^\r\n]+)"""
    ),
)

# Wider shapes first, then the cw._text mirror (see above).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    *_EXTRA_SECRET_PATTERNS,
    *_MIRRORED_SECRET_PATTERNS,
)


def _normalize_path_value(value: str) -> str:
    """Return *value* without trailing slashes, or "" when nothing is left.

    An empty value must never reach a replacement: replacing "" inserts the
    placeholder between every character of the text.
    """
    return value.strip().rstrip("/")


def _home_prefixes(home: str) -> str:
    """The alternation of every home-directory spelling, *home* included."""
    alternatives = [_USER_HOMES, rf"{_PATH_START}~"]
    if home:
        alternatives.append(rf"{_PATH_START}{re.escape(home)}")
    return "|".join(alternatives)


def _encoded_forms(value: str) -> list[str]:
    """Claude Code's directory-name encodings of *value*, longest first.

    "/" and "." become "-"; the other spelling, every non-alphanumeric becomes
    "-", differs only for names holding "_" or similar, and both are replaced.
    """
    forms = {re.sub(r"[/.]", "-", value), re.sub(r"[^A-Za-z0-9]", "-", value)}
    kept = [f for f in forms if f.count("-") >= _MIN_ENCODED_DASHES]
    return sorted(kept, key=len, reverse=True)


def _redact_encoded(text: str, *, home: str, worktrees: Sequence[str]) -> str:
    """Replace the encoded-cwd spelling of each worktree and of *home*."""
    for worktree in sorted(worktrees, key=len, reverse=True):
        for encoded in _encoded_forms(worktree):
            text = re.sub(
                _ENCODED_START + re.escape(encoded) + _ENCODED_END,
                WORKTREE_PLACEHOLDER,
                text,
            )
    for encoded in _encoded_forms(home):
        # A worktree under HOME that `--worktree` did not name: swallow its
        # whole `--cw-wt-<hash>-<name>` tail, not just the HOME prefix.
        text = re.sub(
            _ENCODED_START + re.escape(encoded) + r"--cw-wt-[\w-]+",
            WORKTREE_PLACEHOLDER,
            text,
        )
        text = re.sub(
            _ENCODED_START + re.escape(encoded) + _ENCODED_END,
            HOME_PLACEHOLDER,
            text,
        )
    return text


def redact_paths(text: str, *, home: str, worktrees: Sequence[str]) -> str:
    """Replace worktree, transcript-dir and home paths in *text* with placeholders."""
    usable_home = _normalize_path_value(home)
    usable_worktrees = {_normalize_path_value(w) for w in worktrees} - {""}
    text = _CLAUDE_TMP_CWD.sub(CLAUDE_TMP_PLACEHOLDER, text)
    text = _CLAUDE_TMP_ROOT.sub(CLAUDE_TMP_ROOT_PLACEHOLDER, text)
    for worktree in sorted(usable_worktrees, key=len, reverse=True):
        text = re.sub(
            _PATH_START + re.escape(worktree) + _NOT_MID_NAME,
            WORKTREE_PLACEHOLDER,
            text,
        )
    prefixes = _home_prefixes(usable_home)
    text = re.sub(
        rf"(?:{prefixes})/\.cw/wt/{_SEGMENT}(?:/{_SEGMENT})?",
        WORKTREE_PLACEHOLDER,
        text,
    )
    text = re.sub(
        rf"(?:{prefixes})/\.claude/projects/{_SEGMENT}(?:/{_SEGMENT})*",
        TRANSCRIPT_PLACEHOLDER,
        text,
    )
    text = _redact_encoded(text, home=usable_home, worktrees=sorted(usable_worktrees))
    if usable_home:
        text = re.sub(
            _PATH_START + re.escape(usable_home) + _NOT_MID_NAME,
            HOME_PLACEHOLDER,
            text,
        )
    text = re.sub(rf"(?:{_USER_HOMES}){_NOT_MID_NAME}", HOME_PLACEHOLDER, text)
    return _PYTEST_USER_DIR.sub(rf"\1{REDACTION_PLACEHOLDER}", text)


def redact_secrets(text: str) -> str:
    """Replace the `cw._text.redact` secret shapes, plus the extras, in *text*."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTION_PLACEHOLDER, text)
    return text


def redact_post_text(text: str, *, home: str, worktrees: Sequence[str]) -> str:
    """Redact paths, then secrets. Never truncates."""
    return redact_secrets(redact_paths(text, home=home, worktrees=worktrees))


def _resolve_home(environ: Mapping[str, str]) -> str:
    """`$HOME` when set (an empty value skips the home shapes), else `~`."""
    home = environ.get("HOME")
    if home is not None:
        return home
    try:
        return str(Path("~").expanduser())
    except RuntimeError:
        return ""


def _read_stdin(stdin: TextIO | None) -> str:
    if stdin is not None:
        return stdin.read()
    return sys.stdin.buffer.read().decode(_ENCODING, _ERRORS)


def _write_stdout(stdout: TextIO | None, text: str) -> None:
    if stdout is not None:
        stdout.write(text)
        return
    sys.stdout.buffer.write(text.encode(_ENCODING, _ERRORS))
    sys.stdout.buffer.flush()


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Redact stdin to stdout. Return 0; argparse exits 2 on a usage error."""
    parser = argparse.ArgumentParser(
        description="Redact worktree/home paths and secrets from a tracker post.",
    )
    parser.add_argument(
        "--worktree",
        action="append",
        default=[],
        help="a worktree path to replace with <worktree> (repeatable)",
    )
    args = parser.parse_args(argv)
    worktrees: list[str] = args.worktree
    env: Mapping[str, str] = environ if environ is not None else os.environ
    text = _read_stdin(stdin)
    _write_stdout(
        stdout, redact_post_text(text, home=_resolve_home(env), worktrees=worktrees)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
