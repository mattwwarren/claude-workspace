# cw Code Cleanup Audit (Issue #6)

Audit of dead code candidates identified after the orchestrator subsystem landed.

## Candidates

| Symbol | File | Status | Rationale |
|--------|------|--------|-----------|
| `SessionPurpose.EXPLORE` | `models.py` | **DELETE** | Not in `DEFAULT_AUTO_PURPOSES`; no session creation path uses it; only appears in `prompts.py` (dead prompt entry) and tests. No functional session was ever of this type in the normal flow. |
| `PURPOSE_PROMPTS["explore"]` | `prompts.py` | **DELETE** | Companion to `SessionPurpose.EXPLORE`; unreachable once the enum variant is removed. |
| `HandoffReason` | `models.py` | **DELETE** | Defined but never imported or referenced outside its definition. Zero callers. Replaced conceptually by the `/handoff` skill. |
| `SessionOrigin.DELEGATE` | `models.py` | **DELETE** | Defined but never used; no callsite sets origin to DELEGATE. |
| `SessionOrigin.DAEMON` | `models.py` | **DELETE** | Defined but never used; no callsite sets origin to DAEMON. |
| `SessionPurpose.IDEA` | `models.py` | **KEEP** | In `DEFAULT_AUTO_PURPOSES`; heavily referenced across session, queue, tests. |
| `wrapper.py` / `cw run-claude` / `cw pane-exited` | `wrapper.py`, `cli.py` | **DELETED** | Deleted in #242 — ADR-0000 Phase C complete, native daemon is the only path. |
| Zellij refs in `session.py`, `cli.py` | `session.py`, `cli.py` | **NOTE (pending #4)** | `session.py` and `cli.py` have extensive Zellij calls. These will become dead once issue #4 replaces `zellij.py`. Do NOT delete now — tracked in #4. |
| `SessionOrigin.USER` | `models.py` | **KEEP** | Default value for `Session.origin`; the field is live. |

## Evidence

### SessionPurpose.EXPLORE
```
grep 'SessionPurpose.EXPLORE' -> only in:
  tests/test_models.py (enum value assertion, independence test)
  tests/test_queue.py (by_purpose returns empty for unknown purpose)
  src/cw/prompts.py (dead prompt string)
  src/cw/models.py (enum definition)
```
Not in `DEFAULT_AUTO_PURPOSES`. No session creation code references it.

### HandoffReason
```
grep 'HandoffReason' -> only in:
  src/cw/models.py:33 (class definition)
```
Never imported, never used.

### SessionOrigin.DELEGATE / SessionOrigin.DAEMON
```
grep 'SessionOrigin' -> only in:
  src/cw/models.py:41-44 (class definition)
  src/cw/models.py:127 (Session.origin default = SessionOrigin.USER)
```
DELEGATE and DAEMON variants are never referenced anywhere.

### wrapper.py / run-claude / pane-exited

Deleted in #242 — all references to `run_claude_wrapper`, `signal_idle`, `cw run-claude`,
and `cw pane-exited` removed from `cli.py` and `session.py`. Native daemon (`claude --bg`)
is now the only session spawn path.

## Changes Applied

1. Removed `SessionPurpose.EXPLORE` from `models.py`
2. Removed `PURPOSE_PROMPTS["explore"]` entry from `prompts.py`
3. Removed `HandoffReason` class from `models.py`
4. Removed `SessionOrigin.DELEGATE` and `SessionOrigin.DAEMON` from `models.py`
5. Updated tests to remove references to deleted symbols
