# Register local coordinator recovery

Apply this only to an authorized relay execution. Advice or skill configuration does not start a relay. Workers execute their assigned batch and save results; only the active coordinator maintains shared registration.

## Keep one durable identity record

Use the relay's existing state file. For the watchdog binding, give these fields the following exact names; preserve the relay's other fields and use `coordinator` as the authoritative ownership record rather than maintaining a second coordinator ID:

```json
{
  "coordinator": {"threadId": "EXACT-COORDINATOR-UUID", "hostId": "local"},
  "workers": [{"threadId": "EXACT-ACTIVE-WORKER-UUID", "hostId": "local"}],
  "status": "RUNNING",
  "quotaRecoveryId": null,
  "resumeAtUTC": null
}
```

Use `workers: []` before dispatch. Keep active or unaccepted workers registered; remove accepted, released workers. This lets the watchdog detect worker quota failures while a coordinator waits. Use the exact `threadId` returned by chat tools, or `CODEX_THREAD_ID` from that chat's shell for its own identity. A project ID, turn ID, pending `clientThreadId`, or worker's environment is not the coordinator UUID. Local binding requires every registered participant to have `hostId: "local"`; use the scheduler fallback for remote/cloud participants.

The coordinator atomically replaces shared state before first dispatch and at ownership transitions. A worker never registers itself as coordinator. During succession, transfer this record with ownership and leave `HANDOFF_PENDING` only when activation is reconciled. Keep the same absolute state path; the watchdog rereads it, so a new coordinator UUID takes effect without changing service config.

## Bind before first dispatch

Locate `cqw` or the installed `python -m codex_quota_watchdog` runtime. The configured local checkout is `/Users/fatboy/task-relay`, normally using `/Users/fatboy/task-relay/.venv/bin/python -m codex_quota_watchdog`; verify availability instead of assuming another machine has this path.

1. Save the verified coordinator record and worker list in the durable state.
2. Inspect existing `cqw status`, its config and service. A single watchdog installation follows one relay at a time. Reuse a binding to this state; do not replace a different active relay or enable an explicitly paused monitor without human authorization. A prior fixed-chat recovery binding can be replaced for this authorized local relay. Retain existing Codex path and unrelated settings.
3. For a new binding, run `cqw register-relay --state /absolute/project/.task-relay/state.json --arm`. This validates the state, coordinator and queue interface and atomically saves the config. It does not start the service. Set `relay_min_remaining` to a human-overridden quota threshold when necessary; its default is 15, with exactly 15% permitting continuation.
4. Run `cqw autostart install` to reload the existing managed service with the binding. This requires the user's normal system write permissions. If a foreground watchdog owns the lock, reconcile and stop that watchdog before starting the service. Verify service health and `cqw status` reports `relay-queue`, armed delivery, this state path and the exact active coordinator UUID. Save those verification details in shared state. If recovery is parked in `needs_user`, inspect the error and pending queue before using `cqw clear` and reloading; clear preserves attempted-delivery markers.

Registration and service verification must succeed before claiming recovery is armed. If unavailable, save the gap and use the supported scheduling fallback. During a normal handoff only update shared ownership, then verify the new UUID with `cqw status`; preserve the binding and daemon.

## Request and reconcile recovery

Before quota exhaustion, checkpoint and atomically save `status: "WAITING_QUOTA"`, a freshly generated `quotaRecoveryId` UUID, and an ISO 8601 UTC `resumeAtUTC` after the blocking resets plus margin, or null if unknown. Reuse this ID throughout the same waiting episode and a coordinator transfer. The watchdog also detects `usageLimitExceeded` in the latest turn of a registered coordinator or worker, even when status is still `RUNNING` or `REVIEW_READY`.

Recovery waits for the saved time and live quota confirmation in all reported windows. It queues to the current coordinator, including when the failure happened in a worker. It defers while that coordinator has an active or manually interrupted turn. `HANDOFF_PENDING`, `NEEDS_INPUT`, `PAUSED` and `DONE` suppress delivery; atomically save the appropriate status when holding or finishing work. Completed worker work alone does not trigger a recovery.

The queued coordinator first verifies ownership and authorization, rechecks quota, reconciles actual workers/checkpoints/results, then continues the saved next action. Atomically leave `WAITING_QUOTA` and clear `resumeAtUTC` when reconciliation succeeds. Preserve the prior recovery ID for audit; use a new one only for a new waiting episode. Queue delivery is attempted once per recovery request/failure, with markers persisted before sending. An uncertain delivery needs inspection, not a new ID to force an automatic retry.

The local service cannot start a closed desktop app or load an unloaded successor chat. Verify the successor is available to consume its queue; if this cannot be established, report the limitation and retain the supported scheduler fallback. A successful `cqw status` checks registration and live quota, not that a queued turn ran.
