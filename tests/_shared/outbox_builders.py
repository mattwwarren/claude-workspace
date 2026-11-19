"""Shared builders for the outbox drain tests (#3706, RFC 0019 O2).

Plain functions and one class, no fixtures, so nothing is registered in
``tests/conftest.py``. The five drain test files import their entry, claim and
client builders from here and define no private copy.

``GhThread`` is the stateful ``gh`` the drain needs: :class:`FakeGhCli` scripts
statically, but the drain lists a ticket's comments, posts the missing parts,
then reads each one back from a listing that must now contain it. ``GhThread``
owns one :class:`FakeGhCli` (so ``records`` and ``lock_snapshots`` still work)
and re-scripts its ``issue view`` and ``issue comment`` replies through the
public :meth:`FakeGhCli.reply` before delegating every call to it.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from cw import outbox
from cw.outbox import EnqueueResult, OutboxEntry, OutboxRole
from tests._doctor_helpers import make_client
from tests._shared.bindings_builders import bindings_github_cli
from tests._tool_fakes import GH_UNAUTHENTICATED, FakeGhCli, GhCall

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest

    from cw.models import ClientConfig, LaunchMarker
    from cw.models.bindings import Bindings

CLIENT = "client-a"
_FIXTURE = Path(__file__).parent.parent / "fixtures" / "gh_issue_comments_2633.json"
_URL_BASE = "https://github.com/OWNER/REPO/issues/{ticket}#issuecomment-{n}"

# What one ``gh issue comment`` call does in a GhThread:
# - ``ok``: exit 0, prints a URL, the comment lands in the listing as sent.
# - ``refused``: exit 1, nothing lands.
# - ``timeout``: ``subprocess.TimeoutExpired`` (``post_issue_comment`` -> None).
# - ``no_url``: exit 0 with no URL printed; the comment lands.
# - ``mismatch``: exit 0 with a URL; the listing holds different text.
# - ``missing``: exit 0 with a URL; the comment never appears in the listing.
CommentOutcome = Literal["ok", "refused", "timeout", "no_url", "mismatch", "missing"]


def enqueue(
    client: str = CLIENT,
    *,
    role: OutboxRole = OutboxRole.TICKETS,
    ticket: str = "T-1",
    marker: str = "m1",
    producer: str = "codex-review",
    parts: Sequence[str] = ("body",),
    redacted: bool = False,
    operator_authored: bool = False,
) -> EnqueueResult:
    """Enqueue one entry with test defaults."""
    return outbox.enqueue(
        client,
        role=role,
        ticket=ticket,
        marker=marker,
        producer=producer,
        parts=parts,
        redacted=redacted,
        operator_authored=operator_authored,
    )


def claim_as(
    monkeypatch: pytest.MonkeyPatch,
    marker: LaunchMarker | None,
    client: str = CLIENT,
    **kwargs: Any,
) -> list[OutboxEntry]:
    """Claim as *marker*: ``current_launch_marker`` is patched where used."""
    with monkeypatch.context() as patch:
        patch.setattr("cw.outbox._store.current_launch_marker", lambda: marker)
        return outbox.claim(client, **kwargs)


def client_config(tmp_path: Path, *, bindings: Bindings | None = None) -> ClientConfig:
    """A client bound to GitHub through ``gh`` unless *bindings* says otherwise."""
    return make_client(
        name=CLIENT,
        workspace_path=tmp_path,
        bindings=bindings_github_cli() if bindings is None else bindings,
    )


def observed_comment(url: str, body: str) -> dict[str, object]:
    """An observed ``gh issue view --json comments`` item, ``url``/``body`` replaced."""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    entry = dict(data["comments"][0])
    entry["url"] = url
    entry["body"] = body
    return entry


class GhThread:
    """A per-ticket comment thread behind a :class:`FakeGhCli`.

    ``install`` patches ``subprocess.run`` (what ``cw.gh._sp.run`` resolves to).
    Successive ``gh issue comment`` calls on a ticket take their outcome from
    the queue set with :meth:`script_comments` (``ok`` once it is empty).
    :meth:`unreadable` makes a ticket's ``gh issue view`` exit non-zero.
    """

    def __init__(self) -> None:
        self.gh = FakeGhCli(default=GH_UNAUTHENTICATED)
        self.threads: dict[str, list[dict[str, object]]] = defaultdict(list)
        self._outcomes: dict[str, deque[CommentOutcome]] = defaultdict(deque)
        self._unreadable: set[str] = set()
        self._count = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> GhThread:
        monkeypatch.setattr("subprocess.run", self)
        return self

    def seed(self, ticket: str, body: str) -> None:
        """Put a comment with *body* on *ticket* before the drain runs."""
        self._count += 1
        url = _URL_BASE.format(ticket=ticket, n=self._count)
        self.threads[ticket].append(observed_comment(url, body))

    def script_comments(self, ticket: str, *outcomes: CommentOutcome) -> None:
        self._outcomes[ticket].extend(outcomes)

    def unreadable(self, ticket: str, *, readable: bool = False) -> None:
        if readable:
            self._unreadable.discard(ticket)
        else:
            self._unreadable.add(ticket)

    @property
    def records(self) -> list[GhCall]:
        return self.gh.records

    def comment_records(self, ticket: str | None = None) -> list[GhCall]:
        """Every ``gh issue comment`` launch, optionally for one ticket."""
        return [
            r
            for r in self.gh.records
            if r.argv[1:3] == ["issue", "comment"]
            and (ticket is None or r.argv[3] == ticket)
        ]

    def bodies(self, ticket: str) -> list[str]:
        return [str(c["body"]) for c in self.threads[ticket]]

    def _script_view(self, ticket: str) -> None:
        if ticket in self._unreadable:
            self.gh.reply("issue", "view", ticket, returncode=1, stderr="boom")
            return
        payload = json.dumps({"comments": self.threads[ticket]})
        self.gh.reply("issue", "view", ticket, stdout=payload)

    def _script_comment(self, ticket: str, stdin: bytes | None) -> None:
        queue = self._outcomes[ticket]
        outcome: CommentOutcome = queue.popleft() if queue else "ok"
        if outcome == "refused":
            self.gh.reply("issue", "comment", ticket, returncode=1, stderr="no")
            return
        if outcome == "timeout":
            exc = subprocess.TimeoutExpired(["gh"], 1)
            self.gh.reply("issue", "comment", ticket, raises=exc)
            return
        self._count += 1
        url = _URL_BASE.format(ticket=ticket, n=self._count)
        sent = (stdin or b"").decode("utf-8")
        if outcome != "missing":
            stored = "tampered" if outcome == "mismatch" else sent
            self.threads[ticket].append(observed_comment(url, stored))
        printed = "" if outcome == "no_url" else f"{url}\n"
        self.gh.reply("issue", "comment", ticket, stdout=printed)

    def __call__(
        self, argv: Sequence[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[object]:
        if list(argv[:3]) == ["gh", "issue", "view"]:
            self._script_view(str(argv[3]))
        elif list(argv[:3]) == ["gh", "issue", "comment"]:
            stdin = kwargs.get("input")
            self._script_comment(
                str(argv[3]), stdin if isinstance(stdin, bytes) else None
            )
        return self.gh(argv, *args, **kwargs)
