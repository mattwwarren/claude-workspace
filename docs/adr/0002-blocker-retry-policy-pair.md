# Blocker carries an explicit retry policy

**Status:** Accepted
**Driven by:** #174 (Phase B + Phase E)
**Builds on:** [`docs/headless-contract.md`](../headless-contract.md) §4.2.

## Decision

A `blocker` payload encodes the orchestrator's retry policy in two fields:
`retry_eligible: bool | None` and `retry_delay_seconds: int | None`. Consumers
read the pair to choose between retry-now, retry-after-delay, and
human-required without hard-coding per-`reason` rules.

## Invariant

For any `Blocker` instance produced by `cw.auto_dev_result.parse_stdout`:

1. If `retry_delay_seconds` is non-null then `retry_eligible` is `True`.
2. If `retry_delay_seconds` is non-null then `retry_delay_seconds >= 0`.
3. `retry_eligible=True` with `retry_delay_seconds=None` is legal — it means
   "safe to retry, no specific backoff needed".
4. Both fields default to `None`. A producer that does not commit a retry
   policy leaves them null; consumers MUST treat null as "human required".

## What this means for callers

- **Queue orchestrator** (today: `cw dispatch`; future: SDK orchestrator
  per #117 / #140) reads `blocker.retry_eligible` first, then `retry_delay_seconds`.
  Routes `retry_eligible=False` and `retry_eligible=None` to human escalation;
  routes `retry_eligible=True` to a re-dispatch path that respects the delay.
- **`signal_stop`** (issue #184) inspects `blocker.retry_eligible` inline to
  decide whether to fire a PushNotification to the user (eligible=False) or
  silently queue a retry (eligible=True).
- **`/cw-followup`** (PR #189) branches on `retry_eligible` in the `blocked`
  arm of its dispatch table. The historical fallback — hardcoded "tool_denied
  retries with 2-min delay" — gives way to the producer's hint once the
  producer migrates to v3 emits.

## What this means for producers

- The `/auto-dev` skill (in `global-claude`) MUST populate both fields when
  the v3 emit lands. Recipe per common reason:

  | `reason`              | `retry_eligible` | `retry_delay_seconds` |
  |-----------------------|------------------|-----------------------|
  | `ci_timeout`          | `true`           | `120`                 |
  | `tool_denied`         | `true`           | `120`                 |
  | `network_transient`   | `true`           | `60`                  |
  | `impl_failed`         | `false`          | `null`                |
  | `review_blocked`      | `false`          | `null`                |
  | `agent_block`         | `false`          | `null`                |
  | `plan_unreviewable`   | `false`          | `null`                |

- Producer MAY leave both fields null during a rollout window. Consumers
  treat null as "human required" (the safe default), which never
  regresses behavior.

## Consequences

- One new cross-field invariant lives in the `Blocker` model. Every test
  that constructs a `Blocker` with a non-null delay must also set
  `retry_eligible=True`, or the validator raises. Acceptable: makes the
  policy explicit at the point of construction.
- The retry table in this ADR is advisory, not enforced. If a producer
  emits `retry_eligible=True, retry_delay_seconds=10000` for an
  `impl_failed`, the parser accepts it; the orchestrator will follow it.
  Worth it: the parser's job is structural validation, not policy
  judgment.
- Adding a new retryable failure mode is now an additive producer change,
  not a coordinated table edit across cw + the orchestrator.

## Alternatives considered

- **Single field `retry_policy: enum("now" | "delay" | "human")`.** Rejected
  because the delay value is a producer-side concern that should live next
  to the boolean — keeping the policy and its parameter together makes the
  invariant local to one struct.
- **Inline retry policy in `next_actions`.** Rejected. `next_actions` is a
  string list; encoding a backoff there would either reintroduce string
  parsing or balloon the vocabulary. The structured pair is small and
  obviously additive.
- **Hardcode policy on the consumer side, keyed on `reason`.** Rejected.
  This is what we had before #174 Phase E; the producer is the only
  party that knows whether a particular failure was transient (CI flake)
  or semantic (impl failed twice).

## Referenced by

- #174 (Phase B + Phase E), #184, #189 (PR introducing `/cw-followup`)
