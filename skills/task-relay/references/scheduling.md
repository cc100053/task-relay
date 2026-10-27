# Wakeups, quota and recovery

Read this before a substantial dispatch and when the relay needs delayed continuation or fault recovery.

## Select a supported mechanism

For a local relay with Codex Quota Watchdog available, bind its state and active coordinator using [watchdog.md](watchdog.md). This is the preferred quota recovery mechanism: it survives exhausted model quota and follows coordinator succession without a scheduled model turn. Use one recovery mechanism per relay; retire a redundant relay-owned quota automation after confirming watchdog registration and service health. Preserve unrelated schedules.

When the local watchdog is unavailable, discover the current app tools and use the app's automation tool for an authorized wakeup; inspect existing automation configuration first and update the matching schedule instead of creating a duplicate. Keep a thread heartbeat as the default unless the human requests independent scheduled runs.

The human's configured default authorizes creating, updating and retiring this relay's quota recovery wakeup so work can continue after reset without another question. Bind it to the actual execution request, saved state and active coordinator. Respect an explicitly paused schedule; enable it only when the human authorizes resuming it. This authorization excludes unrelated monitors, perpetual automation and messages outside the task. A waiting coordinator normally uses the active wait/callback path.

## React to state

| State | Action |
| --- | --- |
| `RUNNING` | Within an active execution, wait for a result with its cursor. Use an authorized low-frequency watchdog only for recovery; a suggested initial cadence is 1–2 hours, adjusted to the batch duration. |
| `REVIEW_READY` | Review once per result ID, then continue the agreed work. |
| `HANDOFF_PENDING` | Reconcile the saved successor and activation status before creating or messaging another successor. |
| `WAITING_QUOTA` | Save progress while quota remains. Use reported usage limits and reset timestamps; account quota is shared by coordinator and workers. Set the earliest next check after the relevant blocking window resets, with a small margin such as 2–5 minutes. Recheck limits at that time. |
| `NEEDS_INPUT` | All remaining authorized work depends on a saved human decision. Notify once and hold dependent dispatches; if independent work becomes available, continue it under an active status. |
| `DONE` | Remove or disable the relay's own wakeups and record completion. Preserve unrelated schedules. |

## Apply the quota threshold

Check limits before each substantial dispatch and at safe batch checkpoints. Prefer `rateLimitsByLimitId` for the buckets that apply to the planned models; use the legacy view only when necessary. Remaining percent is `100 - usedPercent`; a missing window or reset time is unknown, not free capacity. Account quota is shared across coordinator, workers and other chats.

Unless the human overrides it, the quota pause threshold is strictly below 15% remaining in any window relevant to the planned work. Exactly 15% is not below the threshold.

- At 15% or more in every relevant known window, continue useful work. Use observed batch costs to choose smaller bounded batches and more frequent safe checkpoints as capacity falls; with no history, start small. Estimates guide batch size rather than imposing an additional reserve or minimum-start gate above 15%.
- Below 15% in a relevant window, stop new substantial work. Use the remaining capacity for a safe checkpoint, required result delivery, ownership/service cleanup and recovery scheduling; enter `WAITING_QUOTA`. An actual provider limit or explicit human restriction can also require a stop.
- When the local watchdog is bound, atomically save `WAITING_QUOTA`, a new `quotaRecoveryId` UUID for this waiting episode, and `resumeAtUTC` when known, following [watchdog.md](watchdog.md). Preserve that ID through ordinary state updates and coordinator succession until recovery is reconciled. A registered worker's structured quota failure also triggers coordinator recovery if the coordinator cannot save this transition.
- Set `resumeAtUTC` after the latest reported reset among the windows below the threshold or actually blocking work, plus a small margin such as 2–5 minutes. Automatically create or update this relay's supported recovery wakeup under the standing authorization; no further confirmation is needed. Preserve the authorized next action and any pending human decisions.
- At recovery, reread limits and resume when every relevant known window has at least 15% remaining and no actual limit blocks work. Otherwise move the wakeup to the next blocking reset. A five-hour reset may leave a weekly or model-specific limit blocking work.
- Missing usage data does not establish low quota. Retry the supported usage check at the next safe checkpoint and keep batches small while work is available. If an actual limit blocks work and reset data is unavailable, save the uncertainty and use a supported low-frequency recovery check; report any scheduling gap without inventing a reset time.

Quota jumps and use by other chats can still exhaust the account despite these checks. Do not consume earned resets, buy credits, or change models to evade limits without the human's authorization.

An agent already blocked by quota cannot reliably change its own schedule. Bind and verify local recovery before dispatch, or arrange a confirmed scheduler wakeup before exhaustion. Record the watchdog state/config paths and service verification, or the actual next run and automation ID, in the state. If the fallback scheduler has no supported one-time mechanism, use a supported temporary schedule whose first run is after the reset and remove it after recovery; the standing authorization covers this fallback. Disclose its actual cadence. If no suitable mechanism is available, state that recovery is not armed and give the manual resume instruction.

Do not implement a long sleep, a recurring retry storm, or a claim that Goal mode bypasses quota. Waiting for a known reset should not keep generating 20-minute attempts.

## Keep each scheduled invocation small

Use a short, durable prompt with the actual absolute state path, for example:

> Use $task-relay to inspect the saved state at [absolute state path]. If waiting for quota before resumeAtUTC, exit quietly. Otherwise inspect only new owner results with the saved cursor. Load the specification only for a decision; continue only the authorized next action. Save state and notify only on meaningful completion, failure, or required input. Stop this relay's wakeups when done.

Use the scheduler's notification settings; do not invent heartbeat XML in user-facing output. Suppressing notifications does not suppress scheduled turns or erase context. A fresh chat per poll also creates unnecessary runs.

For recovery after a missed wakeup or a stalled batch, reconcile state with the actual owner, processes and saved work before reassigning. A process may still be working even if chat status appears idle.

Local recovery requires the machine and app to remain available, with the active coordinator chat loaded to consume queued messages. Authorized worker messages can request coordinator review directly; this is an explicit callback. A worker that has already exhausted quota may be unable to send it; the bound watchdog can detect that worker's structured quota failure from local records. The watchdog polls without model calls and queues one recovery attempt after confirmed quota recovery; enqueueing does not prove execution or completion.
