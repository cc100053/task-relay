def rate_limits(now, five_used=10.0, week_used=10.0, allowed=True, include_allowed=True,
                five_reset=3600, week_reset=3 * 86400, account="acct-1", reached=None):
    result = {
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": five_used, "windowDurationMins": 300,
                        "resetsAt": int(now + five_reset)},
            "secondary": {"usedPercent": week_used, "windowDurationMins": 10080,
                          "resetsAt": int(now + week_reset)},
            "rateLimitReachedType": reached,
            "planType": "plus",
        },
        "rateLimitsByLimitId": None,
        "rateLimitResetCredits": {"availableCount": 1, "credits": None},
        "accountId": account,
    }
    if include_allowed:
        result["ordinaryUsageAllowed"] = allowed
    return result
