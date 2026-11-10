# PR channel: wire contract, identity and coverage

The PR channel is the push path that wakes a Claude session when a pull request
changes. A GitHub Actions workflow (`.github/workflows/pr-events.yml`) posts to
`POST /pr-event` on `cw pr-channel serve`. The server appends one durable record
per accepted post and fans it out over SSE to subscribed sessions.

This page covers three ticket surfaces. Each has its own section below.

- **#3171**: the cross-channel envelope and durable replay. See
  [`event-channel-wire-contract.md`](event-channel-wire-contract.md).
- **#3172**: the ack and cursor semantics. See
  [`event-channel-wire-contract.md`](event-channel-wire-contract.md) and
  [`dispatch-runbook.md`](dispatch-runbook.md) §10.5.
- **#3173**: the normalized `message` (schema v1), event identity and the
  coverage matrix. This is the subject of the rest of this page.

## Transport

- The supported transport is SSE `notifications/message`, with `params.data` set to
  the durable record `{notification_type, message, title, offset}`.
- The subscription contract is `GET /sse?client_id=<id>`, replay of records with
  `offset >= cursor`, and `POST /ack`.
- This is a cw-specific use of `notifications/message`. It is not the MCP
  `events/list` or `events/subscribe` API, and nothing here claims to be.

## Envelope (unchanged by #3173)

The durable envelope keeps exactly four keys: `notification_type`, `message`,
`title` and `offset`. #3173 adds no envelope key. Every new field lives inside
`message`.

- `notification_type` is always `cw-pr-event`.
- `title` is a human hint, `PR #<n>: <event type with spaces>`. It omits `repo`
  and is not an identity. Consumers read `message`.
- `offset` is transport position only. See the identity table below.

## Message (schema v1)

`message` is a JSON object carried as a string. Parse it. Do not string-match
it, because the byte layout (key order, separators) is not part of the contract.

```json pr-channel-wire-example
{
  "schema_version": 1,
  "repo": "acme/widgets",
  "pr_number": 42,
  "event_type": "merged",
  "kind": "transition",
  "payload": {},
  "source_id": null,
  "event_id": null,
  "head_sha": null,
  "event_time": null,
  "received_at": "2026-11-09T12:00:00.000+00:00"
}
```

The example above is the frozen-clock output for the merged workflow body. A
guard test in `tests/test_pr_channel_event_model.py` keeps this block in step
with `_build_notification`.

### Fields

- `schema_version` (int, `>= 1` for v1). The registered schema is `pr-channel-event`
  (`cw schema show pr-channel-event`). Additive keys do not bump it.
- `repo`, `pr_number`, `event_type`: as sent. `repo` is preserved as sent, including
  case. Identity folds case (see below).
- `event_type`: one of `ci_failed`, `review_received`, `mergeable`, `merged`,
  `review_requested`.
- `kind`: `transition` or `hint`. Server-derived, never sender-claimed. See below.
- `payload`: an object. Its keys are per `event_type` and are not closed in v1.
- `source_id`: a producer's stable name for the event. Null when unknown.
- `event_id`: `sha256` of the canonical identity, or null when `source_id` is null.
- `head_sha`: full lowercase 40- or 64-hex SHA, or null.
- `event_time`: UTC ISO-8601 with milliseconds, or null.
- `received_at`: server wall clock at acceptance, UTC ISO-8601 with milliseconds.
  Never an event time, never used for identity or cross-server ordering.

### Versioning rules

- Additive keys do not bump `schema_version`.
- A missing `schema_version` is legacy v0: the four pre-#3173 keys only. Read the
  new fields as null.
- Consumers ignore unknown keys.
- Consumers must not enforce `additionalProperties: false` or a closed
  `event_type` enum. An unknown `event_type` is a recovery-path signal, not a
  parse failure to swallow.

### `kind`

- `transition`: the trigger payload itself reports the change, and the server
  applies it through the existing overlay (or watched-PR registration).
  `transition` is **not** proof of the PR's present aggregate state.
  `review_received.review_decision` is the single submitted review's state, not
  GitHub's aggregate `reviewDecision`. `ci_failed` reflects one workflow run,
  which may be stale or superseded.
- `hint`: wake-up only. Reread the PR before acting. A hint never mutates cw
  state. It is reserved: no v1 producer emits one, and a server guard keeps any
  future `hint` out of the state path.

All five v1 types are `transition`, because each keeps its current state mutation.

## Event identity

`event_id` is the only identity in the message. It is computed as:

    sha256(canonical_json({schema_version, repo.casefold(), pr_number, event_type, source_id}))

- Canonical JSON: sorted keys, separators `,` and `:`, ASCII.
- It uses no offset, clock, body hash or counter.
- It is null whenever `source_id` is null. It never falls back to a body hash.
- It is not stable across a `schema_version` bump, by design.
- `head_sha` and `event_time` are not part of it.

### Per-field stability

Four axes: (a) workflow re-run, (b) relay replay of an identical body, (c) server
restart, (d) repo-slug case change. "N/A in v1" means the workflow sends null.

- `schema_version`: stable on all four.
- `repo`: stable on (a), (b), (c). On (d) it is echoed as sent, so it differs.
  Identity ignores that difference through `casefold`.
- `pr_number`: stable on (b), (c), (d). On (a) it is unknown for `workflow_run`
  events, because the number comes from `gh pr list --head <branch>` at run time.
- `event_type`: stable on (b), (c), (d). On (a) it is stable for the same trigger.
- `kind`: a pure function of `event_type`. Stable on all four.
- `payload`: stable on (b), (c), (d). On (a) not guaranteed.
- `source_id`: null on all four in v1. Caller-supplied values are verbatim and
  case-sensitive. Behaviour on (a) is undefined by this contract (see the
  follow-up below).
- `event_id`: null in v1 for workflow events. Otherwise stable on (b), (c), (d).
  On (a) it depends on `source_id` composition.
- `head_sha`: null in v1 from the workflow. When supplied it is stable on (b), (c),
  (d). Not part of `event_id`.
- `event_time`: null in v1 from the workflow. When supplied, normalized to UTC.
- `received_at`: not stable on (a) or (b), because each acceptance stamps the
  clock. Stable on (c), because replay returns the stored line.
- `offset`: not stable on (a) or (b). Stable on (c). Transport position only.
- `title`: derived from `event_type` and `pr_number`. Not an identity.

### What is null in v1, and why

The workflow posts `source_id: null`, `head_sha: null` and `event_time: null`
explicitly. Nothing it reads supplies these:

- `run_id` and `run_attempt` are not in any captured fixture, so they are not
  emitted. Building `source_id` from the already-consumed context (event name,
  action, PR number, review state) would collide for legitimately distinct
  events, for example two `review_requested` events for one reviewer.
- Consequence: **in production, `event_id` is null for every workflow-originated
  event until the capture follow-up lands.** Server derivation is complete and
  tested with caller-supplied `source_id` values.

Follow-up (draft; to be filed as its own ticket): capture real `github.event`
payloads per trigger, commit the redacted captures under `tests/fixtures/`, then
set a real `source_id`, `head_sha` and `event_time`. The `source_id` decision
includes whether `run_attempt` belongs in it, because that decides whether a
workflow re-run is deduplicated. That change is additive and needs no schema bump.

## Inbound request

`POST /pr-event` body: `repo`, `pr_number`, `event_type`, `payload`, plus optional
`source_id`, `head_sha`, `event_time`.

- The inbound model ignores unknown keys (`extra="ignore"`). Version skew is not a 400.
- `event_type` must be one of the five values. Otherwise the response is `400`:

  ```json
  {"error": "event_type must be one of [...]", "accepted_event_types": ["ci_failed", "mergeable", "merged", "review_received", "review_requested"]}
  ```

- Other malformed bodies return `400 {"error": "<message>"}`, unchanged.
- The optional identity fields never cause a 400. A wrong-type or malformed value
  is dropped to null with a `pr-event <field> dropped (<reason>)` warning.
- Success returns `200 {"status": "ok"}`, or the `review_requested` registration
  body for that type.

### Recovery when a workflow sends an unknown type

Adding a type is not how a new trigger is supported. An unknown `event_type` is
rejected. The observable recovery is: the server warning log, the failed
Actions step (`curl -sf` exits 22), and the matrix below. The operator fixes the
skew by deploying the server before the workflow.

## Coverage matrix

Producers:

- **W**: `.github/workflows/pr-events.yml`. It is the only POSTer to `/pr-event`,
  so it is the only broadcaster on this channel.
- **P**: the poll producer (`fetch_pr_view` plus `_diff_transitions`). It feeds
  `inbox.jsonl` only. It never feeds the PR channel.
- **R**: reconciliation-owned. Only author-wide reconciliation covers it.

| Change | W | P | Owner |
| --- | --- | --- | --- |
| PR open | none (`pull_request` types exclude `opened`) | observes `OPEN`, emits nothing | R |
| PR reopen | none | cannot see it for tracked tasks (terminal states excluded) | R |
| Close without merge | receives, skips (`merged != true`) | stores `CLOSED`, emits nothing | R |
| Merge | `merged` (transition) | emits `pr.merged` to inbox | W + P |
| Draft / ready | none | reads `isDraft`, no event | R |
| Head SHA change (push) | none (no `synchronize`) | none (`headRefOid` excluded from hydration, #2943 R2b) | R |
| Base change | none | none | R |
| Review submitted | `review_received` (single review's state, not aggregate) | emits `pr.review_received` on aggregate change; COMMENTED reviews bypass dedupe on the push side | W + P |
| Review edit / dismissal | none (`types: [submitted]` only) | sees a dismissal only if aggregate changes | R |
| Review requested | `review_requested` (also registers the watched PR) | counts `reviewRequests` only; withdrawal not observed | W; R for withdrawal |
| Review and issue comments | none | reads for attention only, no event | R |
| Thread resolution | none | none | R |
| Check pending / running | none (`workflow_run` `completed` only) | records pending count, no event | R |
| Check failure | `ci_failed` for a workflow named `CI` on a branch with one open PR | emits `pr.ci_failed` on passing-to-not-passing. Disagrees on `cancelled`: W fires, P treats it as ok | W + P |
| Check recovery | skips success (`pr-events.yml`) | emits nothing; the push overlay never sets `ci_ok=True` | R |
| Mergeable state | none | emits `pr.mergeable` with the camelCase `mergeStateStatus` passthrough | P only |
| Labels | none | none | R |
| Auto-merge | none | read only for the finalize head read | R |
| PR discovery outside tracked tasks or watched PRs | broadcasts any PR in a repo carrying the workflow, five types only | hydrates tracked tasks and watched PRs only; no author-wide listing | R |

Two notes the matrix depends on:

- `mergeable` splits names. The poll producer's inbox event uses the camelCase
  `mergeStateStatus` passthrough. The channel's `mergeable` type, if an external
  poster ever sends one, is read by the overlay as snake_case `merge_state_status`.
- Author-wide reconciliation is independent and required wherever the matrix says
  R, or where discovery is incomplete. An empty queue or channel never proves
  complete coverage.

## Section: #3171 (envelope and replay)

See [`event-channel-wire-contract.md`](event-channel-wire-contract.md) for the
cross-channel envelope, the durable file and the replay rules. The PR channel
uses them unchanged.

## Section: #3172 (ack and cursor)

Acks are validated and monotonic. An ack beyond the next-offset frontier is a
`400` with state unchanged. A duplicate or regressing ack is a `200` no-op that
returns the effective cursor. See `dispatch-runbook.md` §10.5.

## Section: #3173 (this page)

Covered above: the v1 message, `kind`, `event_id`, the null-in-v1 identity fields,
the inbound tolerance rules, the recovery path and the coverage matrix.

A producer that sends a field this contract does not know yet is not a bug. Add
it as a new key under the same `schema_version`, or bump the version only for a
breaking change to an existing key.
