"""Pure text helpers for the synthesized PR repair report (#2944).

Everything ``cw.pr_feedback`` copies out of a PR (CI log text, check names,
conflicted file paths) is untrusted. These helpers make it inert before it is
persisted: terminal control sequences are stripped, single-line fields have
line breakers and other control characters escaped, log tails are cut on a
UTF-8 character boundary, and every untrusted payload is quoted between
SHA-tagged BEGIN/END markers with each content line prefixed ``"| "`` so no
content line can ever read as an unprefixed end marker.

Neither ``cw._text._bounded`` (a fixed 4000-character cap) nor
``cw.native_daemon``'s SGR-only ANSI pattern fits a byte-bounded, any-CSI
strip, so they are not reused here; secret redaction is ``cw._text.redact``,
applied by the caller.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Final

from cw.models.review_feedback import MAX_REVIEW_REPORT_BYTES

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Bytes left unused under :data:`MAX_REVIEW_REPORT_BYTES`, so the synthesized
#: report never sits exactly at the model's limit.
REPORT_HEADROOM_BYTES: Final = 2 * 1024
#: The synthesized report's total UTF-8 byte budget.
REPORT_BUDGET_BYTES: Final = MAX_REVIEW_REPORT_BYTES - REPORT_HEADROOM_BYTES

#: The prefix on every content line inside an untrusted block.
QUOTE_PREFIX: Final = "| "

# ESC (or the literal two-character ``^[`` that ``gh run view --log-failed``
# writes; see tests/fixtures/gh_run_log_failed_job.txt) followed by a CSI
# sequence, an OSC sequence ended by BEL or ST, or a two-byte escape.
_ESCAPE: Final = r"(?:\x1b|\^\[)"
_TERMINAL_SEQUENCE: Final = re.compile(
    _ESCAPE + r"(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\|\^\[\\)|[@-Z\\-_])"
)
_KEPT_CONTROLS: Final = frozenset({"\t", "\n", "\r"})
_BYTE_ESCAPE_LIMIT: Final = 0xFF
_UTF8_CONTINUATION_MASK: Final = 0xC0
_UTF8_CONTINUATION_BITS: Final = 0x80
_BOM: Final = "\ufeff"
_ESCAPED_CATEGORIES: Final = frozenset({"Cc", "Zl", "Zp"})


def payload_string(payload: dict[str, object], key: str) -> str:
    """Return a string field from an untyped external payload, or ``""``."""
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def untrusted_begin(sha: str) -> str:
    """The BEGIN marker for evidence about commit *sha*."""
    return f"<<<CW-UNTRUSTED-BEGIN sha={sha}>>>"


def untrusted_end(sha: str) -> str:
    """The END marker for evidence about commit *sha*."""
    return f"<<<CW-UNTRUSTED-END sha={sha}>>>"


def utf8_len(text: str) -> int:
    """*text*'s UTF-8 byte length."""
    return len(text.encode("utf-8"))


def strip_terminal_controls(text: str) -> str:
    """Drop terminal escape sequences, BOMs and controls other than tab/CR/LF."""
    text = _TERMINAL_SEQUENCE.sub("", text).replace(_BOM, "")
    return "".join(
        ch for ch in text if ch in _KEPT_CONTROLS or unicodedata.category(ch) != "Cc"
    )


def _escape_char(ch: str) -> str:
    code = ord(ch)
    return f"\\x{code:02x}" if code <= _BYTE_ESCAPE_LIMIT else f"\\u{code:04x}"


def escape_controls(text: str) -> str:
    """Escape every control or line-breaking character, keeping *text* one line.

    For single-line fields (check names, conflicted paths, urls): a newline,
    CR, NUL, U+0085 NEL or U+2028/U+2029 becomes a visible ``\\xNN`` /
    ``\\uNNNN`` escape instead of starting a new line.
    """
    return "".join(
        _escape_char(ch) if unicodedata.category(ch) in _ESCAPED_CATEGORIES else ch
        for ch in text
    )


def tail_truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep at most *max_bytes* UTF-8 bytes of *text*'s tail; flag a cut.

    The cut never splits a multi-byte character: leading continuation bytes
    of the kept tail are dropped.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    tail = encoded[len(encoded) - max_bytes :]
    start = 0
    while (
        start < len(tail)
        and tail[start] & _UTF8_CONTINUATION_MASK == _UTF8_CONTINUATION_BITS
    ):
        start += 1
    return tail[start:].decode("utf-8"), True


def quote_untrusted_block(sha: str, lines: Iterable[str]) -> str:
    """Quote *lines* as untrusted evidence about commit *sha*.

    Each element is re-split with :meth:`str.splitlines` (CR, LF, VT, FF,
    the file/group/record separators, NEL, LS and PS all break), and every
    resulting line is emitted as ``"| " + line``; an empty line becomes
    ``"| "``. The block ends with a newline.
    """
    quoted = [untrusted_begin(sha)]
    for line in lines:
        quoted.extend(QUOTE_PREFIX + part for part in (line.splitlines() or [""]))
    quoted.append(untrusted_end(sha))
    return "\n".join(quoted) + "\n"
