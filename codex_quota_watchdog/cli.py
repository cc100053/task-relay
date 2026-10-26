"""Command line: cqw doctor | status | threads | watch | run | clear | stop | init-config | autostart | logs."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, autostart, procs, quota, threads
from .appserver import AppServerClient, ProbeError, find_codex
from .config import SAMPLE_CONFIG, ConfigError, data_dir, default_config_path, load_config
from .restart import valid_thread_id
from .state import MONITORING, AlreadyRunning, InstanceLock, State, StateStore
from .watchdog import Watchdog

log = logging.getLogger("codex_quota_watchdog")


def paths(dry_run: bool = False) -> dict[str, Path]:
    base = data_dir()
    return {
        "base": base,
        "log": base / "watchdog.log",
        "state": base / ("state.dryrun.json" if dry_run else "state.json"),
        "lock": base / "watchdog.lock",
        "pid": base / "watchdog.pid",
        "jobs": base / "jobs",
    }


def setup_logging(cfg, quiet: bool) -> None:
    p = paths()
    p["base"].mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        p["log"], maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backups, encoding="utf-8")
    handler.setFormatter(fmt)
    log.addHandler(handler)
    if not quiet and sys.stderr:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        log.addHandler(console)
    log.setLevel(cfg.log_level.upper())


def _load(args, **overrides):
    try:
        return load_config(Path(args.config) if args.config else None, overrides)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)


def _probe(cfg) -> tuple[quota.Snapshot | None, str]:
    try:
        codex = find_codex(cfg.codex_path)
        result = AppServerClient(codex, cfg.app_server_args, cfg.probe_timeout_seconds).read_rate_limits()
        return quota.parse_snapshot(result, time.time()), ""
    except (ProbeError, quota.QuotaParseError) as exc:
        return None, str(exc)


# ---------------------------------------------------------------- commands
def cmd_status(args) -> int:
    cfg = _load(args)
    snap, err = _probe(cfg)
    print(f"Codex Quota Watchdog {__version__}")
    if snap:
        print("Quota (live, from codex app-server):")
        for line in quota.describe(snap):
            print("  " + line)
        reasons = quota.stop_reasons(snap, cfg)
        print("  verdict: " + ("STOP - " + "; ".join(reasons) if reasons else "OK"))
        blocking = quota.blocking_windows(snap, cfg)
        if blocking:
            print(f"  next restart check: {quota.fmt_ts(quota.resume_after(snap, cfg))}")
    else:
        print(f"Quota: UNKNOWN ({err}) - the watchdog would neither stop nor restart on this")
    for label, dry in (("state", False), ("dry-run state", True)):
        p = paths(dry)["state"]
        if not p.exists():
            continue
        st = StateStore(p).load()
        print(f"Watchdog {label}: {st.phase}" + (f" - {st.note}" if st.note else ""))
        if st.resume_after:
            print(f"  waiting until: {quota.fmt_ts(st.resume_after)}")
        if st.thread_id:
            print(f"  thread to resume: {st.thread_id}")
        if st.thread_ids:
            print(f"  threads to resume: {', '.join(st.thread_ids)}")
        for entry in st.pending_queue:
            print(f"  queued, waiting for the app to start it: {entry.get('thread_id')} "
                  f"(since {quota.fmt_ts(entry.get('queued_at'))})")
        if st.stop_reasons:
            print(f"  stopped because: {'; '.join(st.stop_reasons)}")
    return 0 if snap else 1


def cmd_threads(args) -> int:
    cfg = _load(args)
    if args.hours:
        cfg.thread_lookback_hours = args.hours
    try:
        client = AppServerClient(find_codex(cfg.codex_path), cfg.app_server_args, cfg.probe_timeout_seconds)
        now = time.time()
        if args.all:
            with client.session() as session:
                rows = threads.recent_threads(session, cfg, now - cfg.thread_lookback_hours * 3600)
                print(f"Threads updated in the last {cfg.thread_lookback_hours:g} h: {len(rows)}")
                for t in rows:
                    turn = threads.latest_turn(session, t["id"])
                    last = f"{turn.status}{' / ' + turn.error_kind if turn.error_kind else ''}" if turn else "no turns"
                    title = " ".join(str(t.get("name") or t.get("preview") or "").split())[:50]
                    print(f"  {t['id']}  {last:<32} {title}")
            return 0
        limited = threads.find_limited(client, cfg, now)
    except ProbeError as exc:
        print(f"could not read threads: {exc}", file=sys.stderr)
        return 1
    print(f"Threads whose last turn stopped on the usage limit (last {cfg.thread_lookback_hours:g} h): "
          f"{len(limited)}")
    for t in limited:
        kind = " [sub-agent]" if t.subagent else ""
        print(f"  {t.thread_id}  ended {quota.fmt_ts(t.completed_at)}{kind}  {t.title}")
        print(f"      cwd: {t.cwd or '?'}")
    if limited:
        print(f"With discover_threads = true these are resumed after the reset (restart_mode = {cfg.restart_mode}).")
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print(f"Codex Quota Watchdog {__version__} on {platform.platform()} / Python {platform.python_version()}")
    cfg_path = Path(args.config) if args.config else default_config_path()
    print(f"[..] config file: {cfg_path} ({'found' if cfg_path.exists() else 'not found, using defaults'})")
    cfg = _load(args)
    print("[ok] config valid")
    print(f"[..] data dir: {data_dir()}")
    try:
        codex = find_codex(cfg.codex_path)
        ver = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=30)
        print(f"[ok] codex: {codex} ({(ver.stdout or ver.stderr).strip()})")
    except (ProbeError, OSError, subprocess.SubprocessError) as exc:
        print(f"[!!] codex: {exc}")
        ok = False
    snap, err = _probe(cfg)
    if snap:
        print("[ok] app-server quota read:")
        for line in quota.describe(snap):
            print("       " + line)
        if not snap.ordinary_field_present:
            print("[..] this Codex does not report ordinaryUsageAllowed; restarts rely on window data")
    else:
        print(f"[!!] app-server quota read failed: {err}")
        ok = False
    try:
        targets = procs.find_targets(cfg)
        print(f"[ok] Codex processes the watchdog would stop now: {len(targets)}")
        for t in targets:
            print(f"       pid {t.pid}: {t.cmdline[:120]}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[!!] process listing failed: {exc}")
        ok = False
    print(f"[..] armed: {cfg.armed} ({'will stop/start processes' if cfg.armed else 'dry run unless --arm'})")
    print(f"[..] restart_mode: {cfg.restart_mode}")
    if cfg.discover_threads:
        try:
            client = AppServerClient(find_codex(cfg.codex_path), cfg.app_server_args, cfg.probe_timeout_seconds)
            limited = threads.find_limited(client, cfg, time.time())
            print(f"[ok] thread discovery works; threads stopped on the limit now: {len(limited)} (`cqw threads`)")
        except ProbeError as exc:
            print(f"[!!] thread discovery failed: {exc}")
            ok = False
    if cfg.restart_mode == "exec-resume" and cfg.thread_id and not valid_thread_id(cfg.thread_id):
        print("[!!] thread_id is not a valid UUID")
        ok = False
    print(f"[..] autostart: {autostart.status()}")
    print("All checks passed." if ok else "Some checks failed.")
    return 0 if ok else 1


def _watch(args, prompt: str | None = None) -> int:
    cfg = _load(args, armed=True if args.arm else None)
    dry_run = args.dry_run or not cfg.armed
    setup_logging(cfg, args.quiet)
    p = paths(dry_run)
    try:
        with InstanceLock(p["lock"]):
            p["pid"].write_text(str(os.getpid()))
            try:
                codex = find_codex(cfg.codex_path)
            except ProbeError as exc:
                log.error("%s", exc)
                return 2
            client = AppServerClient(codex, cfg.app_server_args, cfg.probe_timeout_seconds)
            dog = Watchdog(cfg, StateStore(p["state"]), client, codex, p["jobs"], dry_run)
            if prompt is not None:
                try:
                    dog.start_job(prompt, os.path.abspath(args.cd) if args.cd else os.getcwd())
                except (OSError, ValueError) as exc:
                    log.error("could not start the job: %s", exc)
                    return 2
            try:
                dog.run_forever(once=getattr(args, "once", False))
            except KeyboardInterrupt:
                log.info("watchdog stopped by user (Codex processes left as they are)")
            finally:
                p["pid"].unlink(missing_ok=True)
    except AlreadyRunning as exc:
        print(f"{exc}; use `cqw stop` first", file=sys.stderr)
        return 3
    return 0


def cmd_watch(args) -> int:
    return _watch(args)


def cmd_run(args) -> int:
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("give the task prompt, e.g. cqw run --cd C:\\work\\repo -- \"fix the tests\"", file=sys.stderr)
        return 2
    return _watch(args, prompt=prompt)


def cmd_clear(args) -> int:
    for dry in (False, True):
        store = StateStore(paths(dry)["state"])
        if store.path.exists():
            old = store.load()
            store.save(State(phase=MONITORING, restart_history=old.restart_history,
                             thread_restarts=old.thread_restarts, handled_turns=old.handled_turns))
    print("state reset to monitoring (a running watchdog picks this up after restarting it)")
    return 0


def cmd_stop(args) -> int:
    pid_file = paths()["pid"]
    if not pid_file.exists():
        print("no running watchdog found")
        return 1
    pid = int(pid_file.read_text().strip() or 0)
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True, capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"could not stop pid {pid}: {exc}")
        return 1
    pid_file.unlink(missing_ok=True)
    print(f"stopped watchdog pid {pid} (Codex processes were not touched)")
    return 0


def cmd_init_config(args) -> int:
    path = Path(args.config) if args.config else default_config_path()
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    print(f"wrote {path}")
    return 0


def cmd_autostart(args) -> int:
    if args.action == "status":
        print(autostart.status())
        return 0
    if args.action == "uninstall":
        print(autostart.uninstall())
        return 0
    extra = ["--config", os.path.abspath(args.config)] if args.config else []
    print(autostart.install(extra))
    return 0


def cmd_logs(args) -> int:
    path = paths()["log"]
    if not path.exists():
        print("no log yet")
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.lines:]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cqw", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check codex, quota access, process matching").set_defaults(func=cmd_doctor)
    sub.add_parser("status", help="show live quota, next reset and watchdog state").set_defaults(func=cmd_status)
    t = sub.add_parser("threads", help="list threads whose last turn stopped on the usage limit")
    t.add_argument("--all", action="store_true", help="list every recent thread with its last turn status")
    t.add_argument("--hours", type=float, help="look back this many hours (default: thread_lookback_hours)")
    t.set_defaults(func=cmd_threads)

    def loop_flags(p):
        p.add_argument("--arm", action="store_true", help="really stop/start processes (overrides armed=false)")
        p.add_argument("--dry-run", action="store_true", help="log decisions only, even if armed")
        p.add_argument("--quiet", action="store_true", help="log to file only")

    w = sub.add_parser("watch", help="run the watchdog loop in the foreground")
    loop_flags(w)
    w.add_argument("--once", action="store_true", help="single decision step, then exit")
    w.set_defaults(func=cmd_watch)

    r = sub.add_parser("run", help="start a headless `codex exec` job and guard it")
    loop_flags(r)
    r.add_argument("--cd", help="working directory for the job")
    r.add_argument("prompt", nargs=argparse.REMAINDER, help="task prompt (put it after --)")
    r.set_defaults(func=cmd_run)

    sub.add_parser("clear", help="leave needs_user/done and return to monitoring").set_defaults(func=cmd_clear)
    sub.add_parser("stop", help="stop a running watchdog (Codex is not touched)").set_defaults(func=cmd_stop)

    i = sub.add_parser("init-config", help="write a commented config.toml")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init_config)

    a = sub.add_parser("autostart", help="start the watchdog at logon")
    a.add_argument("action", choices=["install", "uninstall", "status"])
    a.set_defaults(func=cmd_autostart)

    lg = sub.add_parser("logs", help="print the end of the log")
    lg.add_argument("-n", "--lines", type=int, default=50)
    lg.set_defaults(func=cmd_logs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "prompt", None) and args.prompt[:1] == ["--"]:
        args.prompt = args.prompt[1:]
    return args.func(args)
