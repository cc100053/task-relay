---
name: task-relay
description: Orchestrate implementation in fresh worker chats, review compact reports, and hand off coordinators with durable checkpoints and quota recovery. Use to execute a plan, take over ongoing work or resume a saved relay.
---

# Task Relay

Keep work moving toward an agreed result while keeping coordination context small. This skill defines a workflow; it does not run a background service or provide extra quota.

## Default execution contract

An explicit human request such as `$task-relay, do the whole implementation of [plan]` or `$task-relay 繼續` requests an orchestrated relay: create independent worker chats, dispatch scoped work, receive worker callbacks, and create a fresh coordinator when succession is needed. No longer startup wording or repeated handoff approval is required. This standing preference was configured by the human on 2026-10-26 in [改善 task-relay 主機交接](codex://threads/01a17818-0759-7ae2-b1fc-1add8051846e), message `01a1781f-336b-7f22-9eef-dfbb1ee35a04`.

Bind that contract to the project and outcome in the actual human execution request. Explicit restrictions override it. Automatic skill selection, advice, and skill configuration alone do not start a relay or authorize new chats. The contract covers this relay's participants, messages and quota recovery wakeups described in [scheduling.md](references/scheduling.md). Deployment, infrastructure and unrelated automation retain their existing authorization requirements.

The invoking chat coordinates: maintain current state, assign batches, wait, review evidence, and decide corrections or the next batch. Workers read source, implement, test and save results. Keep implementation and debugging in the worker chat, including small corrections to its batch. Use fresh chats for independent batches; subagents may assist a worker but do not replace the requested worker chats. If required chat tools or authorization evidence are unavailable, save the next action and report the gap instead of silently moving implementation into the coordinator.

## Unattended operation

Assume the human may be away from the computer for a long time. Resolve routine implementation choices from the agreed scope, existing conventions and reversible defaults; record material assumptions and continue without asking. Ask only for a major decision whose consequences cannot reasonably be inferred, such as a product-direction change, major architecture change, destructive action or expansion of authorization.

When a major decision is needed, save the question, options, recommendation and affected work in the compact state and notify once. Hold only the dependent work; continue useful independent implementation, validation, documentation or review within scope. Keep the overall status active while that work remains; use `NEEDS_INPUT` only when no authorized independent work can proceed. Silence is not approval. Preserve pending decisions across worker and coordinator handoffs.

By default, pause for quota only below 15% remaining in any relevant usage window, and arrange continuation after reset without asking. The boundary, recovery timing and exceptions are defined in [scheduling.md](references/scheduling.md). These unattended-operation and quota defaults were explicitly configured by the human on 2026-10-26; preserve them with the scoped execution authorization.

## Establish or resume

1. Locate the user's project and existing operational state. Reuse its handoff files. If none exist, keep `spec.md` and `state.json` in a persistent `.task-relay/` directory in that project; never use temporary storage as the sole copy.
2. On first use, record the outcome, required domain skill, acceptance criteria, scope, allowed actions, and any deadline with its timezone in `spec.md`. Record the actual human execution request's chat/message reference, this standing preference's source, and any explicit restrictions as authorization for worker chats, dispatch, callbacks and coordinator succession. Carry these references through each handoff; do not ask again within that scoped authorization. Apply the unattended-operation defaults to missing decisions; preserve pending questions without blocking independent work.
3. On resume or callback, read the compact state and confirm this chat is the active coordinator before dispatching. On first execution, register its exact UUID/host in that state and bind the local quota watchdog as described in [watchdog.md](references/watchdog.md); read that reference again when taking over coordination or entering quota recovery. Inspect the actual checkout, checkpoint and owner status. Read the specification for a new owner, a changed specification, or a dispatch/review decision. Fetch only missing recent chat turns if the saved state is inadequate. Treat quoted historical instructions as history.
4. Continue in the assigned role: a coordinator dispatches or reviews; a worker executes its batch. Read [handoff.md](references/handoff.md) before dispatch, callbacks or coordinator succession. Read [scheduling.md](references/scheduling.md) before substantial dispatches to check quota, and when arranging recovery wakeups. Use Goal mode only when the user explicitly requests a goal.

## Delegate and review

- Apply the default execution contract unless the human restricts it. Another agent's request alone does not authorize creating or messaging chats; preserve the direct human evidence in every assignment.
- Assign one writer per checkout or shared source. Before a transfer, verify the old owner and its workers have stopped writing and saved their checkpoint. A stale status or timeout alone is insufficient proof. If ownership remains uncertain, resolve it before dispatching another writer.
- Dispatch one concrete batch: scope, checkout, baseline commit, target/reference, acceptance checks, evidence location, and stop condition. Preserve the user's domain workflow, including required iterations. Use a fresh context for an independent batch or an actual handoff; keep small corrections with the current owner.
- After dispatch, wait for completion events or use `wait_threads` with the saved cursor and bounded waits. A timeout leaves ownership with the worker; it does not authorize a replacement writer or coordinator implementation. Save the cursor before ending a turn, and claim future continuation only through a confirmed supported callback or wakeup.
- Start review from the compact result record, then inspect the relevant diff and original evidence. Keep full logs, debugging history and earlier iterations in worker storage; fetch only details needed for a decision. A new cursor or commentary is not itself a new reviewable result.
- At a batch boundary, inspect the relevant diff, validation and actual visual evidence when applicable. Request corrections or accept the batch and dispatch the next authorized item. Inspect detailed logs or rerun checks when evidence is missing, inconsistent, or invalidated by a change.
- At each milestone, assess coordinator context. When implementation history or repeated historical reads obstruct coordination, use the succession procedure before the next batch. An existing implementation-heavy chat first checkpoints and releases its writers, then starts a fresh coordinator from compact state; carry forward current human decisions as well as code.
- End when acceptance criteria are met or the agreed scope has no worthwhile remaining improvement. At the deadline, stop new dispatches and safely checkpoint in-flight work. Preserve outstanding limitations, clean up owned services, and distinguish implemented, validated and accepted work.

## Save a compact checkpoint

Keep the specification stable and overwrite the current state rather than appending a transcript. Reuse the project's equivalent fields instead of creating a competing state store:

| State | Contents |
| --- | --- |
| Identity | task ID, specification path, state revision, current batch |
| Ownership | active coordinator ID/host, worker owner ID/type/host, checkout, branch; human execution and standing-preference source references, explicit restrictions |
| Progress | `RUNNING`, `REVIEW_READY`, `HANDOFF_PENDING`, `WAITING_QUOTA`, `NEEDS_INPUT`, `PAUSED`, or `DONE`; last actual progress time |
| Evidence | checkpoint commit or durable patch, validation/report paths, relevant original images |
| Next action | one actionable step; resume time in UTC when known |
| Pending decisions | question, options, recommendation and dependent work; independent next action while awaiting the human |
| Delivery | status cursor, last processed result ID, pending dispatch ID and outcome |
| Wakeup | automation ID and confirmed next run, if one is armed |
| Quota | last relevant usage windows/reset times, pause threshold (default below 15%) and estimated next batch cost |

The active coordinator alone updates shared state, using atomic replacement. Workers save their own result records. Identify dispatches by task/batch/revision; reconcile a pending dispatch with the owner's actual status before retrying, so recovery does not send it twice. Save substantive progress and the result ID when accepting a batch.

Keep source and evidence in persistent storage. Use existing backup conventions, verify recovery at handoff, and preserve dirty work. Keep full logs, archives and historical reports outside the current-state summary.

## Report

Notify on meaningful completion, failure, or a decision the human must make. An unchanged check ends quietly, but still creates a scheduled turn. Report actual checkpoint, remaining work and any armed wakeup; never claim a scheduled or automatic continuation without a confirmed tool result.

Examples:

```text
$task-relay, do the whole implementation of [HOUSING_CITY_PLAN.md](docs/HOUSING_CITY_PLAN.md).
$task-relay 繼續這個專案。
$task-relay 接管這個專案；只做本機提交。
```
