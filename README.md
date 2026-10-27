# Codex Quota Watchdog (`cqw`)

A small, local, **independent** watchdog for OpenAI Codex on a ChatGPT plan:

1. polls your real 5-hour / weekly usage through the official `codex app-server` (`account/rateLimits/read`),
2. **stops Codex cleanly** when remaining quota drops below your threshold,
3. **sleeps until the official `resetsAt`** (+ a grace period) — not a hard-coded "5 hours",
4. confirms the quota is genuinely back, then **resumes the exact same thread** with
   `codex exec resume <thread UUID>`.

It runs outside Codex, so it keeps working when a Codex session dies. It is **fail-closed**:
whenever it is unsure about the quota, it waits instead of restarting.

> **What it is not.** It cannot bypass or extend your usage limits — it only stops earlier and restarts
> once OpenAI's own servers say ordinary usage is allowed again. It never spends banked reset credits,
> never switches to API billing, never uses `--last`, and never types into a Codex window.

Design notes and the comparison with the existing tools it learned from: [docs/DESIGN.md](docs/DESIGN.md).

---

## Requirements

* Python **3.11+** (stdlib only, no dependencies)
* Codex CLI logged in with your ChatGPT account (`codex login`), or the Codex desktop app's bundled CLI
* Windows 10/11 (primary target), macOS or Linux

## Install

```powershell
git clone https://github.com/cc100053/task-relay.git codex-quota-watchdog
cd codex-quota-watchdog
pip install .          # gives you the `cqw` command
# or run without installing:  python -m codex_quota_watchdog <command>
```

## Quick start

```powershell
cqw doctor             # checks codex, reads quota, lists the processes it WOULD stop
cqw status             # live quota, next reset, watchdog state
cqw init-config        # writes a commented config.toml (path printed)
cqw watch              # foreground, DRY RUN: logs decisions, touches nothing
cqw watch --arm        # really stop / wait / resume
```

Start in dry-run, check `cqw logs` after a limit event, then arm it (`armed = true` in config, or `--arm`).

### Ways to use it

**A. Guard whatever Codex you run (`cqw watch`)**
Use Codex as usual (TUI, `codex exec`, IDE). When quota runs low, the watchdog stops the Codex CLI
processes, waits for the reset, and resumes the thread **if it knows the exact UUID**: from a
`codex exec resume <UUID>` / `codex resume <UUID>` command line it stopped, or from `thread_id`
in config. Otherwise it stops and waits, then parks in `needs_user` rather than guessing.

**B. Let the watchdog own a long unattended job (`cqw run`)** — the most reliable mode
```powershell
cqw run --arm --cd C:\work\repo -- "Migrate the GL posting module to the new API and keep tests green"
```
It starts `codex exec --json …`, captures the thread UUID from the `thread.started` event, and
guards it: stop near the limit → wait for reset → `codex exec resume <UUID> "<resume prompt>"` →
repeat until the job exits successfully.

**C. Recover one desktop chat (`desktop-queue`)**

This mode reads the registered local desktop chat's latest turn. Only a failed turn
with structured `codexErrorInfo = "usageLimitExceeded"` is eligible. It waits for the
official reset plus grace, confirms healthy quota, and sends one continuation through
`codex queue --thread <UUID> --message ...`. It never stops processes or starts a
headless replacement. Keep the desktop app running with the chat loaded so it can
consume the queued continuation.
The queue command is documented in the [official CLI reference](https://learn.chatgpt.com/docs/developer-commands#codex-queue).

Set these values in your config (use a CLI that supports `codex queue --help`):

```toml
[watchdog]
restart_mode = "desktop-queue"
thread_id = "YOUR-EXACT-DESKTOP-THREAD-UUID"
armed = false   # verify with doctor and dry-run first; true enables delivery
# macOS bundled CLI; adjust for your installation:
codex_path = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
```

Inside a desktop chat's shell, `CODEX_THREAD_ID` is the exact chat UUID. A foreground
watchdog can capture it with `cqw watch --desktop-thread` (or pass an explicit UUID).
For background operation, save that UUID as `thread_id` in the config; the LaunchAgent
does not inherit the current chat's environment. Stop the existing watchdog service
before running a second foreground instance.

`cqw doctor` verifies the queue command and registered chat; `cqw status` shows its
latest turn and recovery state. Active turns, completed work, manual interruptions,
authentication errors and generic HTTP 429 errors never trigger desktop recovery.
If the chat advances before delivery, the pending recovery is cancelled. A delivery
attempt is saved before sending; failures or uncertain timeouts enter `needs_user`
and are never automatically resent, even after a watchdog restart or `cqw clear`.
Inspect the chat and queue before manually retrying a failed delivery.

**D. Follow TaskRelay coordinator handoffs (`relay-queue`)**

Use the [TaskRelay skill](skills/task-relay/SKILL.md) and its
[watchdog registration contract](skills/task-relay/references/watchdog.md). The coordinator
records its exact local UUID and active worker UUIDs in the relay's existing durable state:

```json
{
  "coordinator": {"threadId": "COORDINATOR-UUID", "hostId": "local"},
  "workers": [{"threadId": "WORKER-UUID", "hostId": "local"}],
  "status": "RUNNING",
  "quotaRecoveryId": null,
  "resumeAtUTC": null
}
```

```sh
cqw register-relay --state /absolute/project/.task-relay/state.json --arm
cqw autostart install   # reload the managed daemon once with the new binding
cqw status
# Alternatively, foreground dry-run (stop the background service first):
cqw watch --relay-state /absolute/project/.task-relay/state.json --dry-run
```

The daemon rereads the state on every poll and immediately before delivery. When a coordinator
hands off, atomically update `coordinator` in the same file; no daemon restart or fixed UUID
config update is needed. One installation follows one relay; registration refuses to replace a
different active relay. A missing, malformed or remote registration parks recovery safely.

A structured quota failure in **any registered participant** wakes only the **current coordinator**
after live quota recovers. A coordinator can request recovery before exhaustion by atomically
saving `WAITING_QUOTA`, a new `quotaRecoveryId` UUID for that waiting episode, and `resumeAtUTC`
(ISO 8601 UTC, or null when unknown). Keep that request ID through handoffs and ordinary updates.
The default recovery threshold is **at least 15% remaining in every reported window**
(`relay_min_remaining`); the saved resume time, grace period and healthy-read confirmations also apply.

`HANDOFF_PENDING`, `NEEDS_INPUT`, `PAUSED` and `DONE` suppress recovery. Active or manually
interrupted coordinator turns defer delivery. Attempts are persisted before sending and survive
daemon restarts and `cqw clear`; ambiguous delivery is never automatically resent. The prompt
requires the coordinator to verify ownership and reconcile workers/checkpoints before dispatch.
The daemon never writes the relay state, stops Codex, or resumes workers directly.

Keep the desktop app running and the current coordinator chat loaded. Enqueueing does not prove
execution. The skill binds the service during an authorized relay execution; installing or editing
the skill alone does not start work. Install the `skills/task-relay` folder into your Codex skills
directory to use the updated workflow.

### Run in the background / at logon

```powershell
cqw autostart install     # Windows: Startup folder (no admin). macOS: LaunchAgent. Linux: systemd --user
cqw autostart status
cqw autostart uninstall
cqw stop                  # stop a running watchdog (never touches Codex)
```
The autostarted watchdog runs `cqw watch --quiet` and respects `armed` in your config.

## Commands

| Command | What it does |
|---|---|
| `cqw doctor` | Environment check, live quota read, list of processes that would be stopped, armed state |
| `cqw status` | Live quota per window, verdict, next restart check, persisted watchdog state |
| `cqw watch [--arm] [--dry-run] [--quiet] [--once] [--desktop-thread [UUID] \| --relay-state PATH]` | The watchdog loop; select one desktop chat or a dynamic TaskRelay coordinator |
| `cqw register-relay --state PATH [--arm]` | Validate and persist a TaskRelay binding; reload the managed service once afterwards |
| `cqw run [--arm] [--cd DIR] -- PROMPT` | Start a guarded headless `codex exec` job |
| `cqw clear` | Leave `needs_user` after you have checked things, back to monitoring |
| `cqw stop` | Stop a running watchdog |
| `cqw logs [-n N]` | Tail the log |
| `cqw init-config [--force]` | Write a sample config |
| `cqw autostart install\|uninstall\|status` | Logon autostart |

Global flag: `--config PATH`.

## Configuration

Location (or `--config`, or set `CQW_HOME` to move everything):

* Windows: `%LOCALAPPDATA%\codex-quota-watchdog\config.toml`
* macOS: `~/Library/Application Support/codex-quota-watchdog/config.toml`
* Linux: `~/.local/state/codex-quota-watchdog/config.toml`

```toml
[watchdog]
armed = false                    # dry run until true (or --arm)

five_hour_min_remaining = 3.0    # stop when <= 3% of the 5h window is left
weekly_min_remaining = 1.0       # stop when <= 1% of the weekly window is left
poll_seconds = 120               # normal polling
near_limit_poll_seconds = 30     # polling once within near_limit_margin (10 points) of a threshold
grace_seconds = 120              # wait after the official resetsAt
confirm_reads = 2                # healthy reads in a row before restarting

stop_cli = true                  # codex CLI / TUI / exec / node launcher
stop_desktop = false             # desktop app — true closes your UI too
extra_process_patterns = []      # extra regexes on the command line
terminate_timeout_seconds = 20   # polite stop, then force-kill

restart_mode = "exec-resume"     # exec-resume | command | none | desktop-queue | relay-queue
relay_state_path = ""            # absolute durable TaskRelay JSON path for relay-queue
relay_min_remaining = 15.0        # relay-queue: recover at >= this remaining percentage
exec_extra_args = []             # e.g. ["--sandbox", "workspace-write"] (check `codex exec --help`)
resume_prompt = "The ChatGPT usage window has reset. Continue the previous task ..."
thread_id = ""                   # fallback UUID to resume
restart_cwd = ""
restart_command = []             # for restart_mode = "command"; placeholders {thread_id} {cwd} {prompt} {codex}
max_restarts_per_day = 6

codex_path = ""                  # auto: CODEX_PATH env, PATH, desktop bundle, %APPDATA%\npm\codex.cmd
app_server_args = ["app-server"] # older CLIs: ["app-server", "--stdio"]
```

Files in the data dir: `watchdog.log` (rotating), `state.json` (`state.dryrun.json` for dry runs),
`jobs/*.log` (output of jobs the watchdog started).

## How decisions are made

**Stop** when any of these is true on a *valid* read:
`ordinaryUsageAllowed == false`, `rateLimitReachedType` is set, or a window's remaining % ≤ its threshold.

**Wait** until `max(resetsAt of the windows that tripped) + grace_seconds`. A weekly limit waits for the
weekly reset. Sleeps are chunked (≤ 5 min) so laptop sleep and clock changes are handled.

**Restart** only when all of these hold:

* the quota read is valid and `ordinaryUsageAllowed` is `true`
  (older Codex without that field: every window above threshold, no stale `resetsAt`);
* `confirm_reads` consecutive healthy reads;
* same `accountId` as at stop time;
* an exact thread UUID is known (exec-resume mode);
* fewer than `max_restarts_per_day` restarts in the last 24 h.

Anything else → keep waiting, or park in `needs_user` (logged). A parked watchdog **still stops Codex**
on low quota but never restarts anything until you run `cqw clear`.

## Limitations (honest list)

* **Quota polling is not instantaneous.** Usage can jump between polls; a single large turn can still hit
  the hard limit before the watchdog reacts. Raise thresholds if that matters to you.
* **Stopping Codex interrupts the current turn.** A tool call in flight may be cut off; resumed work should be
  safe to re-run (the default resume prompt asks Codex to re-check the workspace first).
* **Interactive sessions are resumed headless.** A TUI session is restarted as `codex exec resume`, with output in
  `jobs/*.log`, not as a new TUI window. Open the thread in Codex afterwards to see it.
* **Plain TUI sessions have no UUID on their command line**, so in guard mode the watchdog can stop and wait but
  can't resume them unless you set `thread_id`. Use `cqw run`, or start work with `codex resume <UUID>`.
* **Desktop app:** `desktop-queue` recovers one registered local chat without closing the app.
  It currently reads the desktop's `state_5.sqlite` and `thread_history_1.sqlite` read-only;
  missing data or schema changes block recovery. Older chats without projected turn history
  are unsupported. Queue acknowledgement confirms enqueueing, not completion of the resumed work.
  Stopping the whole app remains opt-in (`stop_desktop`); relaunching it needs `restart_mode = "command"`.
  Process detection is by name/path heuristics — always check `cqw doctor` before arming.
* **Windows consoles ignore polite stops**, so after `terminate_timeout_seconds` the CLI is force-killed.
* **`ordinaryUsageAllowed`** and the app-server protocol are OpenAI's and may change; the watchdog treats any
  unexpected shape as "unknown" (no action) rather than guessing. `exec_extra_args` placement may differ by
  Codex version — verify with `codex exec resume --help`.
* **Reset credits are never consumed.** `cqw status` shows how many you have; redeem them yourself in Codex
  if you want to.

## Development

```bash
python -m unittest discover -s tests -t . -v
```

Tests use a fake app-server (`tests/fake_app_server.py`) and a fake clock/process table, so they never
touch your real quota or processes. They cover parsing/validation, every fail-closed path, the full
stop → wait → confirm → resume cycle, process matching (incl. ancestor protection), and the Windows
`.cmd` shim handling.

## License

MIT — see [LICENSE](LICENSE).
