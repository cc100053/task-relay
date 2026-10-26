"""The monitor -> stop -> wait -> confirm -> restart loop.

Fail-closed: every path that is unsure about quota leads to waiting, never to a restart.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from . import procs, quota, restart, threads
from .appserver import ProbeError
from .config import Config
from .state import DONE, MONITORING, NEEDS_USER, WAITING_RESET, State, StateStore

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, cfg: Config, store: StateStore, client, codex: str, jobs_dir: Path,
                 dry_run: bool, clock=time.time, proc_api=procs, sleep=time.sleep):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.codex = codex
        self.jobs_dir = jobs_dir
        self.dry_run = dry_run
        self.clock = clock
        self.procs = proc_api
        self.sleep = sleep
        self.state: State = store.load()
        self.job: restart.Job | None = None
        self.resumed_jobs: list[restart.Job] = []  # discovery-mode restarts, not guarded as `cqw run`
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

        reasons = quota.stop_reasons(snap, self.cfg)
        if not reasons:
            if self.state.pending_queue and self._check_pending_queue():
                return self.cfg.poll_seconds
            return self.cfg.near_limit_poll_seconds if quota.near_limit(snap, self.cfg) else self.cfg.poll_seconds

        targets = self._targets()
        if not targets:
            if self.cfg.discover_threads:
                log.warning("quota threshold reached: %s; no Codex process to stop, Codex will stop "
                            "itself at the limit", "; ".join(reasons))
                return self._enter_waiting(snap, reasons, None, [])
            log.info("quota low (%s) but no Codex process is running", "; ".join(reasons))
            return self.cfg.near_limit_poll_seconds

        log.warning("quota threshold reached: %s", "; ".join(reasons))
        if self.cfg.discover_threads:
            thread_id, thread_ids = None, self._all_thread_ids(targets)
        else:
            thread_id, thread_ids = self._pick_thread_id(targets), []
        if self.job:
            self.job.stopped_by_watchdog = True
        stopped = self.procs.stop_processes(targets, self.cfg, dry_run=self.dry_run)
        if not stopped:
            self._park(NEEDS_USER, "some Codex processes could not be stopped; see log")
            return self.cfg.poll_seconds
        return self._enter_waiting(snap, reasons, thread_id, thread_ids)

    def _enter_waiting(self, snap: quota.Snapshot, reasons: list[str], thread_id: str | None,
                       thread_ids: list[str]) -> float:
        self.state.phase = WAITING_RESET
        self.state.stopped_at = self.clock()
        self.state.stop_reasons = reasons
        self.state.account_id = snap.account_id
        self.state.thread_id = thread_id
        self.state.thread_ids = thread_ids
        self.state.cwd = self.cfg.restart_cwd or self.state.cwd
        self.state.resume_after = quota.resume_after(snap, self.cfg)
        self.state.note = ""
        self._healthy_streak = 0
        shown = ", ".join(thread_ids) if thread_ids else thread_id or "none"
        log.info("waiting for the reset; thread(s)=%s; will re-check after %s",
                 shown, quota.fmt_ts(self.state.resume_after))
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

    def _all_thread_ids(self, targets) -> list[str]:
        """Discovery mode: every exact thread id known for the stopped processes (no guessing)."""
        ids = [self.job.thread_id] if self.job and self.job.thread_id else []
        ids += [t for t in self.procs.thread_ids_from(targets) if t not in ids]
        if not ids and self.cfg.thread_id:
            ids.append(self.cfg.thread_id)
        return ids

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
        if self.cfg.discover_threads:
            return self._resume_threads(now)
        if self.state.restarts_last_day(now) >= self.cfg.max_restarts_per_day:
            self._park(NEEDS_USER, f"max_restarts_per_day ({self.cfg.max_restarts_per_day}) reached")
            return self.cfg.poll_seconds
        try:
            args = restart.build_restart_args(self.codex, self.cfg, self.state.thread_id, self.state.cwd)
        except ValueError as exc:
            self._park(NEEDS_USER, str(exc))
            return self.cfg.poll_seconds
        if self.dry_run:
            log.warning("[dry-run] would restart: %s", " ".join(args))
        elif self.cfg.restart_mode == "queue":
            ok, detail = restart.queue(args)
            if not ok:
                self._park(NEEDS_USER, f"codex queue failed for {self.state.thread_id}: {detail}")
                return self.cfg.poll_seconds
            self.state.pending_queue.append({"thread_id": self.state.thread_id, "queued_at": now})
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

    def _resume_threads(self, now: float) -> float:
        """Discovery mode: resume every thread that stopped on the limit, each by its exact id."""
        try:
            limited = threads.find_limited(self.client, self.cfg, now)
        except ProbeError as exc:
            log.warning("could not list threads (%s); not resuming yet", exc)
            return self.cfg.poll_seconds

        candidates: dict[str, tuple[str | None, str | None]] = {}
        for t in limited:
            if t.turn_id in self.state.handled_turns:
                continue
            candidates[t.thread_id] = (t.turn_id, t.cwd)
            log.info("thread %s stopped on the usage limit (%s)", t.thread_id, t.title or "untitled")
        for tid in [*self.state.thread_ids, *([self.state.thread_id] if self.state.thread_id else [])]:
            if restart.valid_thread_id(tid) and tid not in candidates:
                candidates[tid] = (None, self.state.cwd)  # a process the watchdog stopped itself

        if not candidates:
            log.info("quota available again; no thread stopped on the usage limit, nothing to resume")
            self._back_to_monitoring()
            return self.cfg.poll_seconds

        failures, skipped = [], []
        resumed = 0
        for tid, (turn_id, cwd) in candidates.items():
            if resumed >= self.cfg.max_threads_per_resume:
                skipped.append(tid)
                continue
            if self.state.restarts_last_day(now, tid) >= self.cfg.max_restarts_per_day:
                failures.append(f"{tid}: max_restarts_per_day reached")
                continue
            if resumed:
                self.sleep(self.cfg.resume_stagger_seconds)
            error = self._resume_one(tid, cwd or self.cfg.restart_cwd or None, now)
            if error:
                failures.append(f"{tid}: {error}")
                continue
            resumed += 1
            if not self.dry_run:
                self.state.thread_restarts.setdefault(tid, []).append(now)
                if turn_id:
                    self.state.handled_turns.append(turn_id)

        if resumed and not self.dry_run:
            self.state.restart_history.append(now)
        log.info("resumed %d thread(s)%s", resumed, " (dry run)" if self.dry_run else "")
        if skipped:
            failures.append(f"not resumed (max_threads_per_resume={self.cfg.max_threads_per_resume}): "
                            + ", ".join(skipped))
        if failures:
            self._park(NEEDS_USER, "; ".join(failures))
        else:
            self._back_to_monitoring()
        return self.cfg.poll_seconds

    def _resume_one(self, tid: str, cwd: str | None, now: float) -> str | None:
        """Resume one thread; return an error text, or None on success."""
        try:
            args = restart.build_restart_args(self.codex, self.cfg, tid, cwd)
        except ValueError as exc:
            return str(exc)
        if self.dry_run:
            log.warning("[dry-run] would resume %s: %s", tid, " ".join(args))
            return None
        if self.cfg.restart_mode == "queue":
            ok, detail = restart.queue(args)
            if not ok:
                return f"codex queue failed: {detail}"
            self.state.pending_queue.append({"thread_id": tid, "queued_at": now})
            return None
        try:
            self.resumed_jobs.append(restart.restart(args, cwd, self.jobs_dir, tid))
        except (OSError, ValueError) as exc:
            return f"restart failed to launch: {exc}"
        self.resumed_jobs = [j for j in self.resumed_jobs if j.poll() is None]
        return None

    def _check_pending_queue(self) -> bool:
        """Queued messages are sent by the desktop app. Park if one never started. True = parked."""
        now = self.clock()
        due = [e for e in self.state.pending_queue
               if now - e.get("queued_at", now) >= self.cfg.queue_start_timeout_seconds]
        not_started = []
        for entry in due:
            started = threads.started_since(self.client, entry["thread_id"], entry["queued_at"])
            if started is None:
                continue  # unreadable: check again next time
            self.state.pending_queue.remove(entry)
            if started:
                log.info("queued message started on thread %s", entry["thread_id"])
            else:
                not_started.append(entry["thread_id"])
        if not_started:
            self._park(NEEDS_USER, "queued message not started on " + ", ".join(not_started)
                       + "; open these threads in the Codex app (it sends queued messages), "
                         "or use restart_mode = \"exec-resume\"")
            return True
        return False

    def _back_to_monitoring(self) -> None:
        self.state.phase = MONITORING
        self.state.resume_after = None
        self.state.stop_reasons = []
        self.state.thread_ids = []
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
        if reasons:
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
