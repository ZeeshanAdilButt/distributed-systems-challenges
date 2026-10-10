# jiffy-flaky

A fake messaging provider (a cut-down Twilio or SendGrid) that misbehaves on purpose, and
keeps a log of what it really did.

Point the system under test at it, run your workload, then audit the provider's log. The
log is written only by the provider, so it is an independent witness: it shows whether
each action produced exactly one message, whatever the system under test believes.

The provider honours one dedupe mechanism, the `Idempotency-Key` header. It ignores any
epoch or fencing token. It uses only the Python standard library.

## Quick start

```sh
jiffy-flaky serve --port 8080 --log run.jsonl --webhook-url http://127.0.0.1:9000/hooks
# jiffy-flaky listening on http://127.0.0.1:8080

curl -s -X POST http://127.0.0.1:8080/v1/messages \
  -H 'Idempotency-Key: order-17' \
  -d '{"to": "+15550100", "body": "Your order shipped", "metadata": {"action_id": "17"}}'

jiffy-flaky audit run.jsonl --expect-ids ids.txt   # exit 0 when every id has one effect
```

From Python:

```python
from jiffy_flaky import FlakyConfig, FlakyProvider, audit

with FlakyProvider(FlakyConfig(send_timeout_rate=0.2, seed=7)) as provider:
    run_workload(provider.url)
    provider.wait_idle()
    report = audit(provider.records(), expected=action_ids)
assert report.ok(), report.summary()
```

## Configuration

Every field of `FlakyConfig` can be set as a `serve` flag (`--send-timeout-rate 0.2`), as
an environment variable (`FLAKY_SEND_TIMEOUT_RATE=0.2`), or in code. Flags win over
environment variables, which win over defaults.

| Field | Default | Meaning |
|---|---|---|
| `host`, `port` | `127.0.0.1`, `0` | Where to listen. Port 0 picks a free port. |
| `webhook_url` | none | Where status webhooks go. No webhooks when unset. |
| `log_path` | none | JSONL audit log (`--log`). Records are always kept in memory too. |
| `seed` | `0` | Seed for every fault decision. |
| `send_latency` | `0.02` | Seconds of normal processing per send. |
| `send_timeout_rate` | `0.05` | Chance that a send times out. |
| `lookup_timeout_rate` | `0.05` | Chance that a lookup or get times out. |
| `timeout_mode` | `mixed` | `lost_request`, `lost_response`, or `mixed` (50/50). |
| `hang_seconds` | `5.0` | How long a timed-out request holds the connection before its 504. |
| `duplicate_webhook_rate` | `0.02` | Chance that a webhook event is delivered twice. |
| `reorder_webhook_rate` | `0.10` | Chance that a message's earlier events arrive after its final one. |
| `late_failure_rate` | `0.01` | Chance that a message fails after it was sent. |
| `sent_after`, `final_after`, `late_failure_after` | `0.1`, `0.3`, `1.5` | Seconds after creation for each status. |
| `idempotency_ttl` | `3600` | How long a key replays its message. After that it creates a new one. |
| `lookup_lag` | `0` | Seconds a new message stays invisible to lookups by key. |
| `webhook_timeout` | `2.0` | Per-attempt timeout. |
| `webhook_max_attempts` | `60` | Attempts before giving up on an event. |
| `webhook_backoff_max` | `2.0` | Retry backoff cap. The backoff starts at 0.1s and doubles. |
| `fsync` | `true` | fsync the log (`--no-fsync` to turn off). See below. |

Fault decisions hash the seed with the idempotency key and how many times that key has
been seen, so the same seed and the same requests give the same faults, whatever order
requests from different keys arrive in.

## HTTP API

| Request | Answer |
|---|---|
| `POST /v1/messages` with `Idempotency-Key` (1 to 255 chars) and `{"to", "body", "metadata"?}` | `201` new message, or `200` the existing one for that key (`"replayed": true`). `400 missing_idempotency_key` or `400 invalid_body`. |
| `GET /v1/messages?idempotency_key=K` | `200 {"data": [message, ...]}`, oldest first, empty when none. |
| `GET /v1/messages/{id}` | `200` message, or `404 not_found`. |
| `GET /_health` | `200 {"ok": true}` |
| `GET /_stats` | `200` integer counters. |

A message looks like this. `events` lists only the status events that have happened.

```json
{"id": "msg_000001", "idempotency_key": "order-17", "to": "+15550100",
 "body": "Your order shipped", "metadata": {"action_id": "17"}, "status": "sent",
 "created_at": 1760000000.0,
 "events": [{"status": "queued", "sequence": 1, "at": 1760000000.0},
            {"status": "sent", "sequence": 2, "at": 1760000000.1}]}
```

Statuses: `queued` (1) at creation, `sent` (2) at `sent_after`, then `delivered` (3) at
`final_after`, or `failed` (3) at `late_failure_after` for a late failure.

### Timeouts

A timed-out request waits `hang_seconds`, then answers `504 {"error": "gateway_timeout"}`.

- `lost_request`: nothing was created. Retrying is safe.
- `lost_response`: the send was processed (created or replayed) and only the answer was
  lost. The client cannot tell this apart from `lost_request`.

## Webhooks

Each status event is POSTed to `webhook_url` when it happens:

```json
{"event_id": "evt_msg_000001_2", "type": "message.status", "message_id": "msg_000001",
 "idempotency_key": "order-17", "status": "sent", "sequence": 2,
 "occurred_at": 1760000000.1, "metadata": {"action_id": "17"}}
```

A 2xx answer counts as delivered. Anything else, a refused connection or a timeout is
retried with backoff up to `webhook_max_attempts`, each attempt on a new connection, so a
receiver can be restarted mid-run. Injected faults: a duplicate is the same `event_id`
again 50 to 300 ms after the first delivery; a reordered message has its `queued` and
`sent` events held until delivery of its final event has succeeded or given up.

## Audit log

One JSON object per line, in the order things happened. Every record has `kind` and `at`
(unix time written). With `fsync` on, `effect` and `request` records are on disk before
the HTTP answer is sent; concurrent writers share one fsync. `status` and `webhook`
records are flushed at once and fsynced with the next batch, so slow disks do not delay
status changes.

| kind | Fields |
|---|---|
| `request` | `op` (`send`, `lookup`, `get`), `idempotency_key`?, `message_id`?, `outcome`, `received_at`, `created`? (for `timeout_lost_response`) |
| `effect` | `message_id`, `idempotency_key`, `metadata`, `to`. Exactly one per created message. |
| `status` | `message_id`, `idempotency_key`, `status`, `sequence` |
| `webhook` | `event_id`, `message_id`, `status`, `sequence`, `attempt`, `result` (`ok`, `error`), `code`?, `duplicate`, `reordered` |
| `webhook_gave_up` | `event_id`, `message_id`, `attempts` |

Request outcomes: `created`, `replayed`, `timeout_lost_request`, `timeout_lost_response`,
`lookup_hit`, `lookup_miss`, `lookup_timeout`, `bad_request`, `not_found`.

## The audit

`audit(records, group_by="action_id", expected=None)` groups `effect` records by a
metadata field and returns an `AuditReport`:

- `duplicates`: groups with more than one effect.
- `missing`: expected ids with no effect. `unexpected`: effects for ids not expected.
- `final_status`: the last status of each group's latest message.
- `sends_without_lookup`: keys sent again without a `lookup_miss` since the previous send.
- `faults`: injected timeouts, duplicate webhooks, reordered messages, late failures.

`report.ok()` is true when there are no duplicates, no missing and no unexpected ids.
`report.summary()` gives a short text version and `report.to_json()` a dict.
`jiffy-flaky audit LOG [--group-by FIELD] [--expect-ids FILE] [--json]` prints the same
report and exits 0 when it is ok, 1 when it is not, and 2 when the log cannot be read.

## Test helpers

`jiffy_flaky.testing` has `FlakyClient` (a small client for the API above) and
`WebhookRecorder` (a receiver that records events and can fail its next n requests).

## Limitations

- State lives in memory. A restarted provider starts again at `msg_000001`, so use a fresh
  log file for each provider process.
- HTTP/1.0 only: every request uses its own connection.
- Webhooks are sent by 8 threads. A receiver that hangs slows delivery down.
