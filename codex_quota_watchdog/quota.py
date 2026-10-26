"""Parse `account/rateLimits/read` results and make fail-closed decisions on them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Config

FIVE_HOURS_MINS = 300
WEEK_MINS = 7 * 24 * 60
# A resetsAt further away than this is not a plausible 5h/weekly window.
MAX_RESET_HORIZON_SECONDS = 8 * 24 * 3600
# Field name in the official protocol; absent on older Codex builds.
ORDINARY_FIELD = "ordinaryUsageAllowed"


class QuotaParseError(ValueError):
    pass


@dataclass
class Window:
    slot: str               # "primary" / "secondary"
    used_percent: float
    window_mins: int | None
    resets_at: int | None   # unix seconds

    @property
    def kind(self) -> str:
        if self.window_mins == FIVE_HOURS_MINS:
            return "5h"
        if self.window_mins == WEEK_MINS:
            return "weekly"
        return f"{self.window_mins}m" if self.window_mins else "unknown"

    @property
    def remaining(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass
class Snapshot:
    taken_at: float
    windows: list[Window]
    account_id: str | None = None
    # True/False from the backend, None when the backend says it is unavailable.
    ordinary_usage_allowed: bool | None = None
    # False on older Codex builds that do not send the field at all.
    ordinary_field_present: bool = False
    reached_type: str | None = None
    reset_credits_available: int | None = None
    plan_type: str | None = None
    raw: dict = field(default_factory=dict, repr=False)


def parse_snapshot(result: dict, now: float) -> Snapshot:
    """Validate the response strictly; anything odd raises QuotaParseError (= unknown)."""
    if not isinstance(result, dict):
        raise QuotaParseError("result is not an object")
    by_id = result.get("rateLimitsByLimitId")
    limits = None
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        limits = by_id["codex"]
    elif isinstance(result.get("rateLimits"), dict):
        limits = result["rateLimits"]
    if limits is None:
        raise QuotaParseError("no rateLimits in response")

    windows = []
    for slot in ("primary", "secondary"):
        raw = limits.get(slot)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise QuotaParseError(f"{slot} window is not an object")
        used = raw.get("usedPercent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or used < 0:
            raise QuotaParseError(f"{slot}.usedPercent invalid: {used!r}")
        mins = raw.get("windowDurationMins")
        if mins is not None and (isinstance(mins, bool) or not isinstance(mins, int) or mins <= 0):
            raise QuotaParseError(f"{slot}.windowDurationMins invalid: {mins!r}")
        resets = raw.get("resetsAt")
        if resets is not None:
            if isinstance(resets, bool) or not isinstance(resets, (int, float)):
                raise QuotaParseError(f"{slot}.resetsAt invalid: {resets!r}")
            resets = int(resets)
            # Milliseconds or garbage would put the reset decades away.
            if resets > now + MAX_RESET_HORIZON_SECONDS:
                raise QuotaParseError(f"{slot}.resetsAt implausibly far: {resets}")
        windows.append(Window(slot, min(float(used), 100.0), mins, resets))
    if not windows:
        raise QuotaParseError("no quota windows returned")

    present = ORDINARY_FIELD in result
    allowed = result.get(ORDINARY_FIELD)
    if allowed is not None and not isinstance(allowed, bool):
        raise QuotaParseError(f"{ORDINARY_FIELD} invalid: {allowed!r}")

    credits = result.get("rateLimitResetCredits")
    available = credits.get("availableCount") if isinstance(credits, dict) else None

    return Snapshot(
        taken_at=now,
        windows=windows,
        account_id=result.get("accountId") if isinstance(result.get("accountId"), str) else None,
        ordinary_usage_allowed=allowed,
        ordinary_field_present=present,
        reached_type=limits.get("rateLimitReachedType") or None,
        reset_credits_available=available if isinstance(available, int) else None,
        plan_type=limits.get("planType") if isinstance(limits.get("planType"), str) else None,
        raw=result,
    )


def blocking_windows(snap: Snapshot, cfg: Config) -> list[Window]:
    return [w for w in snap.windows if w.remaining <= cfg.threshold_for(w.kind)]


def stop_reasons(snap: Snapshot, cfg: Config) -> list[str]:
    """Why Codex should be stopped now. Empty list = keep running."""
    reasons = []
    if snap.ordinary_usage_allowed is False:
        reasons.append("backend reports ordinary usage is not allowed")
    if snap.reached_type:
        reasons.append(f"rate limit reached ({snap.reached_type})")
    for w in blocking_windows(snap, cfg):
        reasons.append(
            f"{w.kind} window: {w.remaining:.1f}% left <= threshold {cfg.threshold_for(w.kind):g}%"
        )
    return reasons


def resume_after(snap: Snapshot, cfg: Config) -> float | None:
    """Earliest time worth re-checking for a restart, or None if the reset time is unknown."""
    blockers = blocking_windows(snap, cfg)
    if not blockers:
        # Stopped for a reason without a window (e.g. ordinaryUsageAllowed=false only).
        # Use the furthest known reset so we never restart before every window has turned over.
        blockers = [w for w in snap.windows if w.resets_at]
        if not blockers:
            return None
    if any(w.resets_at is None for w in blockers):
        return None
    return max(w.resets_at for w in blockers) + cfg.grace_seconds


def restart_blockers(snap: Snapshot, cfg: Config, now: float) -> list[str]:
    """Reasons a restart is NOT safe yet. Empty list = quota looks genuinely available."""
    problems = []
    if snap.ordinary_field_present:
        if snap.ordinary_usage_allowed is None:
            # The protocol says clients must not infer recovery when this is null.
            problems.append("backend could not confirm ordinary usage (ordinaryUsageAllowed=null)")
        elif snap.ordinary_usage_allowed is False:
            problems.append("backend reports ordinary usage is not allowed")
    if snap.reached_type:
        problems.append(f"rate limit still reached ({snap.reached_type})")
    for w in snap.windows:
        if w.remaining <= cfg.threshold_for(w.kind):
            problems.append(f"{w.kind} window still at {w.remaining:.1f}% left")
        if w.resets_at is not None and w.resets_at <= now:
            problems.append(f"{w.kind} window data is stale (resetsAt already passed)")
    return problems


def near_limit(snap: Snapshot, cfg: Config) -> bool:
    return any(w.remaining <= cfg.threshold_for(w.kind) + cfg.near_limit_margin for w in snap.windows)


def fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "unknown"
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def describe(snap: Snapshot) -> list[str]:
    lines = []
    for w in snap.windows:
        lines.append(
            f"{w.kind:>7} ({w.slot}): {w.used_percent:5.1f}% used, {w.remaining:5.1f}% left, "
            f"resets {fmt_ts(w.resets_at)}"
        )
    if snap.ordinary_field_present:
        lines.append(f"ordinaryUsageAllowed: {snap.ordinary_usage_allowed}")
    else:
        lines.append("ordinaryUsageAllowed: (not reported by this Codex version)")
    if snap.reached_type:
        lines.append(f"rateLimitReachedType: {snap.reached_type}")
    if snap.plan_type:
        lines.append(f"plan: {snap.plan_type}")
    if snap.reset_credits_available is not None:
        lines.append(f"reset credits available: {snap.reset_credits_available} (never used automatically)")
    return lines
