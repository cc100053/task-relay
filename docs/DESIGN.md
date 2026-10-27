# Design: Codex Quota Watchdog

Research date: October 2026. Codex protocol facts were checked against
`openai/codex` `main` (`codex-rs/app-server-protocol/src/protocol/v2/account.rs`,
`common.rs`, `codex-rs/cli/src/main.rs`, `codex-rs/exec/src/cli.rs`).

## 1. Research: existing tools

| Tool | Limit detection & reset time | How it resumes | Safety mechanisms | Fit for "independent kill → wait → restart" |
|---|---|---|---|---|
| **songyb111-gachon/codex-auto-resume-windows** | Watches Codex's own records for a classified usage-limit / 429 / 5xx error; waits for the reset time Codex reports | `codex queue` one continuation to the **exact thread UUID**; only if the desktop app has that thread loaded | Fail-closed, never `--last`, no keystrokes, never resends, no telemetry; refuses unclassified errors | Excellent resume safety, Windows-polished. Reacts *after* Codex stops; never kills anything; the "thread must be loaded in the app" limit means no unattended restart after app restart (standard edition). Heavy (tray, dashboard, installer). |
| **feifeigong/codex-auto-resume** | Python daemon polling `account/rateLimits/read` via app-server; scans recent interrupted sessions (36 h lookback) | Same thread (no `--last`); queues a message if Desktop holds the thread | Single instance, once per limit window, only sends after live quota confirms recovery (fixed after a bug that spammed resumes while limited) | Closest in spirit (Python, doctor/status/autostart). **Auto-redeems weekly reset credits by default** — opposite of what we want. Doesn't stop Codex. |
| **raysonmeng/agent-quota-guard** | Hooks inside Claude Code / Codex TUI read official usage endpoints; tiers at 80/90/95/99 % with burn-rate forecast | Checkpoint file + MCP tool that blocks until refresh; optional cron watchdog runs `codex exec resume` | Watchdog dry-run by default (`ARM=1` to enable), sandboxed resume; **fail-open** in-session (silent if it can't read usage) | Great "pause before the cliff" idea. Lives *inside* the agent (hooks don't fire for `codex exec`), fail-open by design, Unix cron/launchd oriented. |
| **flowing-water1/codex-watchdog** | WebSocket proxy between TUI and app-server; sees structured errors; reads `account/rateLimits/read` (`usedPercent`, `windowDurationMins`, `resetsAt`) | Re-activates the **same `/goal`** on the same thread (`thread/goal/set`) after reset + grace (default 10 s) | Only resumes goals still `usageLimited`/`quotaExhausted`; never touches billing/auth failures; loopback only | Best protocol-level design, but it *is* the Codex launcher (proxy) — dies with the session; depends on experimental WebSocket app-server + `/goal`. |
| **awslew/codex-auto-resume-trio** | Scans Desktop session SQLite (read-only); 5 h window only from structured app-server data (`windowDurationMins=300`), else UNKNOWN | `codex exec resume` / app-server on the same session | Default **observe-only** (zero sends until `--execute`), owner lease, unreadable state = conflict (block) | Strong fail-closed rules worth copying. Large surface (3 apps, Node + Python). Doesn't stop Codex. |
| **shangzhimingge/codex-auto-resume** | Codex Skill preflight registers thread+turn+workspace; on-demand daemon polls `account/rateLimits/read` until every exhausted bucket reaches its real reset | `codex exec resume <saved UUID>`, verifies first `thread.started` UUID; Git workspace snapshot check before resuming | No reset credits, no API billing, no guessing, PID+creation-time identity, Job Objects on Windows, transactional installer | Very rigorous but complex (skill + AGENTS.md injection + daemon). Requires the agent to cooperate (preflight). Doesn't stop Codex. |
| **@alanwalk/codex-until** (npm) | Spawns `codex app-server --stdio` per poll, `account/rateLimits/read` with `excludeResetCreditDetails`; strict window validation; rollover detection | **Doesn't resume** — it's a kill switch | Respects `Retry-After`, validates every field, rollover needs ≥ 1 full window advance | Exactly the "stop" half. Kills **all** Codex processes with `taskkill /F /T /IM codex.exe` / SIGKILL (no polite stop, no Desktop/CLI distinction). |

### Takeaways

* **Quota source:** everyone serious converged on `codex app-server` (stdio JSON-RPC) →
  `initialize` → `initialized` → `account/rateLimits/read`. It returns
  `rateLimits.{primary,secondary}.{usedPercent, windowDurationMins, resetsAt}` and, on current
  Codex, a top-level **`ordinaryUsageAllowed`** whose doc comment reads: *"Null means unavailable;
  clients must not infer recovery from percentages or reset times."* That is the cleanest
  fail-closed signal available, and none of the reviewed tools use it yet.
* **Resume identity:** exact thread UUID only. `codex exec resume <SESSION_ID> <prompt>` is the
  official headless path; `--last` is a guess and is rejected by every careful tool.
* **Nobody combines** an *independent* process that (a) kills Codex before the hard cut, (b) waits
  on the official reset, and (c) restarts the exact thread. codex-until does (a), the auto-resume
  tools do (c). This project does all three, in one small stdlib-only Python package.

## 2. Implementation plan

### Architecture
A standalone Python CLI/daemon (`cqw`), stdlib only, Python 3.11+. One foreground loop
(`cqw watch`) that can be autostarted at logon (Startup folder on Windows, LaunchAgent on macOS,
`systemd --user` on Linux). No tray/GUI: status is `cqw status` + a log file. Nothing is injected
into Codex (no hooks, no AGENTS.md edits, no proxy), so Codex upgrades can't break the session.

```
            +---------------- cqw watch (independent process) ----------------+
            |                                                                  |
  poll ---> | spawn `codex app-server` (stdio) -> account/rateLimits/read      |
            |   -> parse/validate -> decision                                  |
            |                                                                  |
            |  MONITORING --threshold--> stop Codex --> WAITING_RESET          |
            |       ^                                     | sleep to resetsAt  |
            |       |                                     | + grace, re-probe  |
            |       +-- codex exec resume <UUID> <-- N healthy confirmations   |
            |                                                                  |
            |  NEEDS_USER: never restarts; still stops Codex on low quota      |
            +------------------------------------------------------------------+
```

### Quota query & parsing
* Spawn per probe (no long-lived connection to break), 30 s timeout, kill the whole process tree after.
* Omit parameters for `initialized` and `account/rateLimits/read`: installed CLI 0.137.0
  requires a unit quota request; the desktop CLI also accepts omitted optional parameters.
* Prefer `rateLimitsByLimitId.codex`, fall back to `rateLimits`.
* Window kind from `windowDurationMins` (300 = 5 h, 10080 = weekly), never from text.
* Strict validation: numeric `usedPercent ≥ 0`, positive integer duration, `resetsAt` in seconds and
  ≤ 8 days ahead. Anything else → **unknown**.

### Process management
* Enumerate with `Get-CimInstance Win32_Process` (Windows) / `ps -A -o pid,ppid,lstart,args` (POSIX).
  Read POSIX executable names separately with `ps -A -o pid,comm` so spaces in paths
  cannot turn a helper into a CLI match.
* Categories: `cli` (native `codex`/`codex-<triple>`, `node … @openai/codex`), `desktop`
  (path under `Codex.app`, `ChatGPT.app`, `Codex Computer Use.app` or
  `Programs\OpenAI\Codex`) — **off by default**, custom regexes.
* Never touch itself, its ancestors (a shell inside Codex) or its own probe.
* Stop roots politely (`taskkill /T` / SIGTERM), wait `terminate_timeout_seconds`, then force-kill
  only processes whose PID **and start time** still match (no PID-reuse kills).

### Waiting
* `resume_after = max(resetsAt of the windows that tripped) + grace_seconds` (default 120 s).
  Weekly exhaustion therefore waits for the weekly reset, not 5 h.
* Sleep in ≤ 5 min chunks against wall-clock time, so laptop sleep/hibernate is handled.
* No probing before `resume_after`; after it, re-probe; if the server shows a later reset, move the
  target; if unknown, exponential backoff 60 s → 15 min.

### Restart
* `exec-resume` (default): `codex exec [exec_extra_args] resume <UUID> <resume_prompt>`. The UUID
  comes from (1) the job the watchdog started (`thread.started` event of `codex exec --json`),
  (2) a single `resume <UUID>` in the stopped processes' command lines, or (3) `thread_id` in
  config. Zero or several candidates → `needs_user`. Never `--last`.
* `command`: user-defined argv with `{thread_id}`, `{cwd}`, `{prompt}`, `{codex}` placeholders
  (e.g. relaunch the desktop app or a script).
* `none`: stop + wait only.
* Restarted work runs headless with output in `jobs/*.log`, and is itself guarded.

### Desktop-chat recovery
* `restart_mode = "desktop-queue"` opts one exact configured `thread_id` into recovery.
  `watch --desktop-thread [UUID]` can capture the executor's `CODEX_THREAD_ID`; it never
  chooses the most recent chat or scans unrelated chats.
* Read `threads` metadata from `state_5.sqlite` and the latest `thread_turns` entry from
  `thread_history_1.sqlite`, both in SQLite read-only mode. Require a non-archived local
  `Codex Desktop` originator and structured `failed` / `usageLimitExceeded` evidence.
  Missing or incompatible schemas fail closed. Do not write Codex databases.
* Desktop mode never calls process stopping or headless restart. Reuse reset waiting,
  healthy confirmations, account-change protection and restart caps from the CLI loop.
* Re-read the latest turn immediately before sending. If the user continued or completed
  the chat, cancel recovery. Persist the attempted failed-turn UUID before calling
  `codex queue --thread <UUID> --message <resume_prompt>`, so crashes and ambiguous delivery
  failures cannot automatically send twice. `clear` preserves this delivery marker.
* Keep the desktop app and target chat loaded to consume queued messages. A successful
  queue command acknowledges enqueueing, not execution; never fall back to `exec resume`.

### TaskRelay recovery
* `relay-queue` binds one absolute relay state path, with the schema defined in
  [the skill's registration contract](../skills/task-relay/references/watchdog.md).
  `register-relay` validates the local coordinator/queue and atomically updates only recovery
  config settings. Reload the managed daemon once after binding; ownership handoffs update state only.
* The relay owns its state. The watchdog reads `coordinator`, registered `workers`, `status`,
  `quotaRecoveryId` and `resumeAtUTC`; it writes only its own recovery state. Missing or malformed
  ownership and non-local participants fail closed. A second active relay cannot replace the binding.
* A saved `WAITING_QUOTA` episode or a registered participant's structured quota failure triggers
  recovery. After reset/grace and live confirmations, every reported window must have at least
  `relay_min_remaining` (default 15%) remaining. Exactly 15% permits recovery.
* Reread ownership immediately before queueing only the current coordinator. Defer for active or
  interrupted turns; suppress delivery during handoff, input waits, pause and completion. The prompt
  verifies ownership again on execution and asks the coordinator to reconcile workers before dispatch.
* Persist request/failure identities before queueing, shared across coordinator succession and daemon
  restarts; `clear` preserves them. A timeout parks recovery instead of automatically resending. This
  provides at-most-once attempts, not guaranteed delivery. Desktop availability constraints still apply.

### Configuration
TOML file (`cqw init-config`), every key optional, validated (unknown keys are errors).
Key knobs: `armed`, thresholds per window, `grace_seconds`, `confirm_reads`, `poll_seconds`,
`stop_cli`/`stop_desktop`, `restart_mode`, `exec_extra_args`, `max_restarts_per_day`.

### Logging & status
Rotating log file in the per-user data dir; `cqw status` (live quota + state), `cqw doctor`
(environment, quota read, *which processes would be stopped*), `cqw logs`.

### Fail-closed rules
1. Unknown quota (spawn error, timeout, JSON-RPC error, invalid fields) → no stop, no restart.
2. Restart requires `ordinaryUsageAllowed == true` when the field exists; `null` blocks.
   (Older Codex without the field: every window above threshold and none stale.)
3. Restart requires `confirm_reads` (default 2) consecutive healthy reads.
4. Account id changed since the stop → `needs_user`.
5. No exact thread UUID → `needs_user` (no guessing).
6. `max_restarts_per_day` cap → `needs_user`.
7. Unreadable state file → `needs_user`.
8. Managed job exits non-zero while quota is fine → `needs_user` (it's a real failure, not a limit).
9. Not armed → dry-run: logs every decision, touches nothing. Separate dry-run state file.
10. Single instance via OS file lock.
11. Reset credits are **never** consumed (only the available count is displayed).

### Platform notes
* **Windows (primary):** hidden child windows (`CREATE_NO_WINDOW`), Startup-folder autostart
  without admin, `codex.cmd` npm shim resolved to `node codex.js` so prompts never pass through
  cmd.exe parsing (and refused if they would). Console processes ignore polite `taskkill`, so
  the force step after the timeout is normal there.
* **macOS:** LaunchAgent with captured `PATH`; Desktop app detected under `Codex.app`.
* **Linux:** `systemd --user` unit; for headless servers enable lingering if needed.
