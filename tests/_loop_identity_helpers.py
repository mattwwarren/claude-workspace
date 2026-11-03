"""Fake dispatch-loop lock records for the loop code-identity tests (#2729).

The dispatch loop records its code identity in the singleton lock file
(``cw.config.dispatch_loop_lock_file()``, redirected per test by the autouse
``tmp_config_dir`` fixture). These helpers write that record directly so a test
can stage NO_LOOP / MATCH / STALE / IDENTITY_UNKNOWN without running a loop.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from cw.config import dispatch_loop_lock_file
from cw.dispatch_loop_identity import CodeIdentity
from cw.local_runner import read_process_start_time_ns

if TYPE_CHECKING:
    import pytest

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
VERSION = "1.85.1"
FAKE_CMD = "cw dev-queue serve"


def write_loop_record(
    *,
    pid: int | None = None,
    version: str | None = VERSION,
    commit: str | None = COMMIT_A,
    with_identity: bool = True,
    proc_start_ns: int | None = None,
) -> None:
    """Write a fake lock-file record for a loop with this identity.

    *pid* defaults to the test process's own pid, and *proc_start_ns* to that
    pid's real start time, so the fake loop reads as live. *with_identity*
    False writes the pre-#2729 ``{"pid", "cmd"}`` shape.
    """
    holder = os.getpid() if pid is None else pid
    record: dict[str, object] = {"pid": holder, "cmd": FAKE_CMD}
    if with_identity:
        start = (
            proc_start_ns
            if proc_start_ns is not None
            else read_process_start_time_ns(holder)
        )
        record.update({"proc_start_ns": start, "version": version, "commit": commit})
    path = dispatch_loop_lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def patch_checkout_identity(
    monkeypatch: pytest.MonkeyPatch,
    version: str | None = VERSION,
    commit: str | None = COMMIT_A,
) -> list[CodeIdentity]:
    """Make ``checkout_code_identity()`` report *version*/*commit*.

    Returns the list each call's result is appended to, so a test can assert
    the checkout was (or was not) consulted.
    """
    calls: list[CodeIdentity] = []

    def _fake() -> CodeIdentity:
        identity = CodeIdentity(version=version, commit=commit)
        calls.append(identity)
        return identity

    monkeypatch.setattr("cw.dispatch_loop_identity.checkout_code_identity", _fake)
    return calls
