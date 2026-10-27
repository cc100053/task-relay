"""The monitor -> stop -> wait -> confirm -> restart loop.

Fail-closed: every path that is unsure about quota leads to waiting, never to a restart.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from . import desktop, procs, quota, restart
from .appserver import ProbeError
from .config import Config
from .state import DONE, MONITORING, NEEDS_USER, WAITING_RESET, State, StateStore

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, cfg: Config, store: StateStore, client, codex: str, jobs_dir: Path,
                 dry_run: bool, clock=time.time, proc_api=procs):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.codex = codex
        self.jobs_dir = jobs_dir
        self.dry_run = dry_run
        self.clock = clock
        self.procs = proc_api
        self.state: State = store.load()
        self.job: restart.Job | None = None
        self._unknown_streak = 0
        self._healthy_streak = 0
        self._last_parked_note = ""

    # ------------------------------------------------------------------ probe
    def probe(self) -> quota.Snapshot | None:
        now = self.clock()
        try:
            snap = quota.parse_snapshot(self.client.read_rate_limits(), now)
        except (ProbeError, quota.QuotaParseError) as exc:
            self._unknown_streak += 1
            self._healthy_streak = 0
            log.warning("quota unknown (%s); no stop/restart decisions on this read", exc)
            return None
        self._unknown_streak = 0
        self.state.last_check_at = now
        self.state.last_summary = quota.describe(snap)
        return snap

    def _unknown_delay(self) -> float:
        n = max(0, self._unknown_streak - 1)
        return min(self.cfg.unknown_backoff_max_seconds, self.cfg.unknown_backoff_min_seconds * 2 ** n)

    # ------------------------------------------------------------------- loop
    def tick(self) -> float:
        """Run one decision step; return seconds to sleep before the next."""
        phase = self.state.phase
        if phase == WAITING_RESET:
            delay = self._tick_waiting()
        elif phase in (NEEDS_USER, DONE):
            delay = self._tick_parked()
        else:
            delay = self._tick_monitoring()
        self.store.save(self.state)
        return max(1.0, min(delay, self.cfg.max_sleep_chunk_seconds))

    def run_forever(self, once: bool = False) -> None:
        mode = "DRY RUN (nothing will be stopped or started)" if self.dry_run else "ARMED"
        log.info("watchdog started: %s, phase=%s", mode, self.state.phase)
        if self.cfg.restart_mode == "desktop-queue":
            log.info("desktop recovery registered: thread=%s; process stopping disabled", self.cfg.thread_id)
        while True:
            delay = self.tick()
            if once:
                return
            time.sleep(delay)

    # ------------------------------------------------------------- monitoring
    def _tick_monitoring(self) -> float:
        if self.job and self.job.thread_id:
            self.state.thread_id = self.job.thread_id  # persisted, survives a watchdog crash
        exit_code = self.job.poll() if self.job else None
        snap = self.probe()

        if self.job and exit_code is not None and not self.job.stopped_by_watchdog:
            return self._job_exited(exit_code, snap)

        if snap is None:
            return self._unknown_delay()

        if self.cfg.restart_mode == "desktop-queue":
            return self._monitor_desktop(snap)

        reasons = quota.stop_reasons(snap, self.cfg)
        if not reasons:
            return self.cfg.near_limit_poll_seconds if quota.near_limit(snap, self.cfg) else self.cfg.poll_seconds

        targets = self._targets()
        if not targets:
            log.info("quota low (%s) but no Codex process is running", "; ".join(reasons))
            return self.cfg.near_limit_poll_seconds

        log.warning("quota threshold reached: %s", "; ".join(reasons))
        thread_id = self._pick_thread_id(targets)
        if self.job:
            self.job.stopped_by_watchdog = True
        stopped = self.procs.stop_processes(targets, self.cfg, dry_run=self.dry_run)
        if not stopped:
            self._park(NEEDS_USER, "some Codex processes could not be stopped; see log")
            return self.cfg.poll_seconds

        self.state.phase = WAITING_RESET
        self.state.stopped_at = self.clock()
        self.state.stop_reasons = reasons
        self.state.account_id = snap.account_id
        self.state.thread_id = thread_id
        self.state.cwd = self.cfg.restart_cwd or self.state.cwd
        self.state.resume_after = quota.resume_after(snap, self.cfg)
        self.state.note = ""
        self._healthy_streak = 0
        log.info("stopped Codex; thread=%s; will re-check after %s",
                 thread_id or "none", quota.fmt_ts(self.state.resume_after))
        return self._sleep_until_resume()

    def _monitor_desktop(self, snap: quota.Snapshot) -> float:
        try:
            turn = desktop.latest_turn(self.cfg.thread_id)
        except desktop.DesktopError as exc:
            # Nothing is pending yet, so a transient read error just waits for the next poll.
            log.warning("%s; retrying next poll", exc)
            return self.cfg.poll_seconds
        if self.state.desktop_registered_thread_id != self.cfg.thread_id:
            self.state.desktop_registered_thread_id = self.cfg.thread_id
            self.state.desktop_baseline_turn_id = turn.turn_id
            if turn.quota_limited and not self.cfg.desktop_recover_existing_failure:
                log.warning("desktop chat already ended on a quota failure when registered (turn=%s); "
                            "not recovering it (set desktop_recover_existing_failure = true to allow)",
                            turn.turn_id)
        if not turn.quota_limited or turn.turn_id == self.state.desktop_attempted_turn_id:
            return self.cfg.poll_seconds
        if turn.turn_id == self.state.desktop_baseline_turn_id and not self.cfg.desktop_recover_existing_failure:
            return self.cfg.poll_seconds
        self.state.phase = WAITING_RESET
        self.state.thread_id = turn.thread_id
        self.state.desktop_turn_id = turn.turn_id
        self.state.cwd = turn.cwd
        self.state.stopped_at = self.clock()
        self.state.account_id = snap.account_id
        self.state.stop_reasons = ["desktop turn failed: usageLimitExceeded"]
        self.state.resume_after = (quota.resume_after(snap, self.cfg) if quota.stop_reasons(snap, self.cfg)
                                   else self.clock() + self.cfg.grace_seconds)
        self._healthy_streak = 0
        log.info("desktop quota failure registered: thread=%s turn=%s; waiting for quota recovery",
                 turn.thread_id, turn.turn_id)
        return self._sleep_until_resume()

    def _targets(self) -> list:
        all_procs = self.procs.list_processes()
        exclude = {self.job.pid} if self.job else set()
        targets = self.procs.find_targets(self.cfg, procs=all_procs, exclude=exclude)
        if self.job and self.job.poll() is None:
            targets += [p for p in all_procs if p.pid == self.job.pid]
        return targets

    def _job_exited(self, code: int, snap: quota.Snapshot | None) -> float:
        job, self.job = self.job, None
        log.info("managed job exited with code %s (log: %s)", code, job.log_path)
        if code == 0:
            log.info("managed job finished successfully; nothing to resume, still monitoring")
            self.state.thread_id = None
            return self.cfg.poll_seconds
        if snap is not None and not quota.stop_reasons(snap, self.cfg):
            self._park(NEEDS_USER, f"managed job failed (exit {code}) while quota was available")
            return self.cfg.poll_seconds
        # Most likely a usage-limit stop inside Codex, or quota unknown: wait, fail-closed.
        self.state.phase = WAITING_RESET
        self.state.stopped_at = self.clock()
        self.state.stop_reasons = [f"managed job exited with code {code}"]
        self.state.account_id = snap.account_id if snap else self.state.account_id
        self.state.thread_id = job.thread_id or self.state.thread_id
        self.state.resume_after = quota.resume_after(snap, self.cfg) if snap else None
        self._healthy_streak = 0
        return self._sleep_until_resume()

    def _pick_thread_id(self, targets) -> str | None:
        if self.job and self.job.thread_id:
            return self.job.thread_id
        ids = self.procs.thread_ids_from(targets)
        if len(ids) == 1:
            return ids[0]
        if len(ids) > 1:
            log.warning("several thread ids in stopped processes (%s); not guessing", ", ".join(ids))
            return None
        return self.cfg.thread_id or self.state.thread_id or None

    # ---------------------------------------------------------------- waiting
    def _sleep_until_resume(self) -> float:
        if self.state.resume_after is None:
            return self.cfg.unknown_backoff_min_seconds
        return max(1.0, self.state.resume_after - self.clock())

    def _tick_waiting(self) -> float:
        now = self.clock()
        if self.state.resume_after and now < self.state.resume_after:
            return self.state.resume_after - now  # no need to poll before the official reset

        snap = self.probe()
        if snap is None:
            return self._unknown_delay()

        blockers = quota.restart_blockers(snap, self.cfg, now)
        if blockers:
            self._healthy_streak = 0
            later = quota.resume_after(snap, self.cfg)
            if later and later > now:
                self.state.resume_after = later
                log.info("not ready (%s); next check after %s", "; ".join(blockers), quota.fmt_ts(later))
                return later - now
            log.info("not ready (%s); polling", "; ".join(blockers))
            return self.cfg.poll_seconds

        if self.state.account_id and snap.account_id and snap.account_id != self.state.account_id:
            self._park(NEEDS_USER, "Codex account changed since the stop; not restarting")
            return self.cfg.poll_seconds

        self._healthy_streak += 1
        if self._healthy_streak < self.cfg.confirm_reads:
            log.info("quota looks available (%d/%d confirmations)", self._healthy_streak, self.cfg.confirm_reads)
            return self.cfg.confirm_interval_seconds
        return self._restart(now)

    def _restart(self, now: float) -> float:
        self._healthy_streak = 0
        if self.cfg.restart_mode == "none":
            log.info("quota available again; restart_mode=none, so nothing is started")
            self._back_to_monitoring()
            return self.cfg.poll_seconds
        if self.state.restarts_last_day(now) >= self.cfg.max_restarts_per_day:
            self._park(NEEDS_USER, f"max_restarts_per_day ({self.cfg.max_restarts_per_day}) reached")
            return self.cfg.poll_seconds
        if self.cfg.restart_mode == "desktop-queue":
            return self._restart_desktop(now)
        try:
            args = restart.build_restart_args(self.codex, self.cfg, self.state.thread_id, self.state.cwd)
        except ValueError as exc:
            self._park(NEEDS_USER, str(exc))
            return self.cfg.poll_seconds
        if self.dry_run:
            log.warning("[dry-run] would restart: %s", " ".join(args))
        else:
            try:
                self.job = restart.restart(args, self.state.cwd, self.jobs_dir, self.state.thread_id)
            except (OSError, ValueError) as exc:
                self._park(NEEDS_USER, f"restart failed to launch: {exc}")
                return self.cfg.poll_seconds
            self.state.managed_pid = self.job.pid
        self.state.restart_history.append(now)
        self._back_to_monitoring()
        return self.cfg.poll_seconds

    def _restart_desktop(self, now: float) -> float:
        if self.state.thread_id != self.cfg.thread_id or not self.state.desktop_turn_id:
            self._park(NEEDS_USER, "desktop recovery registration changed or no failed turn was recorded")
            return self.cfg.poll_seconds
        if self.state.desktop_attempted_turn_id == self.state.desktop_turn_id:
            self._park(NEEDS_USER, "desktop delivery was already attempted for this turn and is never "
                                   "resent automatically; inspect the chat and continue it by hand")
            return self.cfg.poll_seconds
        try:
            turn = desktop.latest_turn(self.cfg.thread_id)
        except desktop.DesktopError as exc:
            # Nothing was sent yet: keep waiting and re-confirm quota before the next attempt.
            log.warning("%s; delivery not attempted, retrying next poll", exc)
            return self.cfg.poll_seconds
        if turn.turn_id != self.state.desktop_turn_id or not turn.quota_limited:
            log.info("desktop chat advanced or no longer has a quota failure; cancelling recovery")
            self._back_to_monitoring()
            return self.cfg.poll_seconds
        # Persist before sending: a crash or ambiguous timeout must never duplicate delivery.
        self.state.desktop_attempted_turn_id = turn.turn_id
        self.state.restart_history.append(now)
        self.store.save(self.state)
        if self.dry_run:
            log.warning("[dry-run] would queue desktop continuation: thread=%s turn=%s", turn.thread_id, turn.turn_id)
        else:
            try:
                desktop.queue_resume(self.codex, turn.thread_id, self.cfg.resume_prompt,
                                     self.cfg.probe_timeout_seconds)
            except (desktop.DesktopError, ValueError) as exc:
                self._park(NEEDS_USER, str(exc))
                return self.cfg.poll_seconds
            log.info("desktop continuation queued: thread=%s turn=%s", turn.thread_id, turn.turn_id)
        self._back_to_monitoring()
        return self.cfg.poll_seconds

    def _back_to_monitoring(self) -> None:
        self.state.phase = MONITORING
        self.state.resume_after = None
        self.state.stop_reasons = []
        self.state.note = ""

    # ----------------------------------------------------------------- parked
    def _park(self, phase: str, note: str) -> None:
        self.state.phase = phase
        self.state.note = note
        self._last_parked_note = note
        if phase == NEEDS_USER:
            log.error("NEEDS_USER: %s (run `cqw clear` after checking)", note)
        else:
            log.info("%s: %s", phase.upper(), note)

    def _tick_parked(self) -> float:
        """Parked: never restart anything, but keep stopping Codex if quota runs low."""
        if self.state.note != self._last_parked_note:
            log.info("parked in %s: %s", self.state.phase, self.state.note)
            self._last_parked_note = self.state.note
        snap = self.probe()
        if snap is None:
            return self._unknown_delay()
        reasons = quota.stop_reasons(snap, self.cfg)
        if reasons and self.cfg.restart_mode != "desktop-queue":
            targets = self._targets()
            if targets:
                log.warning("parked, but quota threshold reached (%s); stopping Codex", "; ".join(reasons))
                if self.job:
                    self.job.stopped_by_watchdog = True
                self.procs.stop_processes(targets, self.cfg, dry_run=self.dry_run)
        return self.cfg.poll_seconds

    # ---------------------------------------------------------------- managed
    def start_job(self, prompt: str, cwd: str | None) -> None:
        if self.dry_run:
            log.warning("[dry-run] would start: codex exec --json %r in %s", prompt[:60], cwd)
            return
        self.job = restart.start_new_job(self.codex, self.cfg, prompt, cwd, self.jobs_dir)
        self.state.phase = MONITORING
        self.state.cwd = cwd
        self.state.thread_id = None
        self.state.managed_pid = self.job.pid
        self.store.save(self.state)
