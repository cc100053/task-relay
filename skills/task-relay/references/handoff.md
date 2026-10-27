# Worker callbacks and fresh-session succession

Use this for the default execution contract in [SKILL.md](../SKILL.md), or a narrower arrangement explicitly requested by the human. Bind the human execution request and standing-preference source to this project, goal and participants in the specification; it does not grant permission to message unrelated chats or publish/deploy work.

## Dispatch a worker chat

1. Discover current tools. Use `list_projects` before `create_thread` for repository work; use the returned project ID. Follow the human's requested environment and model, otherwise retain tool defaults. Put the actual authorized checkout in the prompt. Do not request a new worktree unless authorized.
2. Save a pending dispatch ID before creation. Send a compact batch assignment with the state/specification paths, baseline, scope, acceptance criteria, artifact location, stop condition, both retrievable human authorization references, and any pending human decisions with their dependent work. Apply the unattended-operation defaults in SKILL.md. Include the coordinator ID/host, assign the worker role, and require a scoped result callback. Keep full predecessor history out of the initial prompt. Confirm the returned `threadId` before messaging it; a setup `clientThreadId` is not a usable thread ID.
3. Persist the actual worker ID/host and dispatch ID, including its entry in the watchdog's `workers` list when bound. Remove that entry when the batch is accepted and the worker is released. If creation/delivery has an ambiguous outcome, reconcile existing chats before retrying. Attach created chats using the app's required created-thread directive.

The coordinator evaluates outcomes and chooses corrections or the next batch. Workers execute their assignment, checkpoint and report; they do not independently extend the scope or open another implementation batch.

## Return results to the coordinator

The worker first verifies direct human authorization to message the coordinator, either from trusted context or by reading the referenced human execution request and standing-preference message once. The short execution request plus that configured contract is sufficient; another approval sentence is unnecessary. A coordinator's instruction to report back is insufficient on its own. If that evidence is unavailable, save the result for coordinator retrieval and explain that the callback was not sent.

Before returning, save a durable result record containing task/batch/result IDs, the baseline and resulting commit or patch, validation/report paths, original visual evidence when relevant, limitations, and whether all writers and owned services are released. Use a unique result ID and keep delivery status in the worker's own record.

Read the current coordinator ID from shared state immediately before sending. Use `send_message_to_thread` for an authorized compact callback, such as:

> REVIEW_READY — task [id], batch [id], result [id]. Checkpoint [commit], report [absolute path], ownership released [yes/no]. Please validate this result and decide the next authorized action.

Keep the callback to identifiers, checkpoint, a brief outcome/check summary, evidence paths, limitations and ownership release. The coordinator retrieves the detailed result as needed; keep full logs and debugging transcripts in the worker's files. Report outstanding human acceptance separately from technical checks.

Only mark delivery sent after confirmed success. A success means the message was sent, not that review finished. If quota is low, finish the safe checkpoint and callback using the remaining headroom. If delivery fails, leave the result pending for recovery; avoid repeatedly sending it. A queued callback received during `WAITING_QUOTA` must not start another batch until the quota gate passes.

The coordinator deduplicates callbacks by result ID, verifies the actual evidence and ownership release, and reviews once. It then records acceptance/correction and checks quota before dispatching. Worker completion alone is not acceptance.

## Replace the coordinator

Use a fresh coordinator at a milestone when accumulated context materially obstructs decisions; the default execution contract already covers this scoped succession. Make the transition at a safe batch boundary with no active writer. Use a new chat with the specification, current state, authorization references, unresolved results and next action, rather than forking full history.

1. Save the compact checkpoint and `HANDOFF_PENDING` intent. Create one authorized successor with state/specification paths, the human authorization source and any pending human decisions; its initial prompt must check ownership and refrain from dispatching until activated.
2. Record the returned successor ID/host. After the predecessor has released dispatch authority, atomically transfer the active coordinator ID and revision, including the canonical `coordinator.threadId`/`hostId` registration in [watchdog.md](watchdog.md). Keep `HANDOFF_PENDING` until activation is reconciled, so recovery cannot race the transfer. Only the coordinator named in state may dispatch.
3. Send the authorized successor an activation message referencing that revision. If activation delivery is uncertain, reconcile its status instead of creating another successor. The predecessor exits the relay and does not continue an independent dispatch loop.
4. The successor verifies state, checkpoint, ownership and any pending results, verifies its exact UUID matches `coordinator.threadId`, and atomically leaves `HANDOFF_PENDING` for the appropriate active status before continuing the saved next action. A bound local watchdog follows this same state path automatically; verify the reported target with `cqw status` without rebinding or restarting on each handoff. Reconcile any other relay-owned recovery schedule through supported tools. If recovery cannot target the successor, disclose that gap before claiming automatic succession.

Creating a successor does not replenish shared quota. Check quota before creating it and preserve enough headroom to complete activation. If transfer cannot safely finish, checkpoint and arrange recovery first.
