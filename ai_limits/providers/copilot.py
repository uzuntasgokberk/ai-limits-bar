"""GitHub Copilot: the monthly AI-credit budget (or premium requests on the legacy plan).

Source: `gh api copilot_internal/user`, the endpoint Copilot CLI reads too. The GitHub CLI owns the token;
this module never sees it. Hidden when gh is missing, not signed in, or the account has no Copilot.
A 403 can also be a rate limit, so it keeps the cached values visible (Unavailable) instead of hiding them.
"""
import re

from ai_limits.common import NotConfigured, Unavailable, find_tool, loads, result, row, run, to_epoch


def read(settings):
    gh = find_tool("gh")
    if not gh:
        raise NotConfigured("gh not installed")
    code, out, err = run([gh, "api", "copilot_internal/user"], timeout=15)
    if code != 0:
        # gh ends an API error with "(HTTP 403)"; the message before it can hold other digits (user or request ids).
        found = re.search(r"\bHTTP (\d{3})\b", err)
        status = found.group(1) if found else next((c for c in ("401", "403", "404") if c in err), "")
        if status == "403":
            raise Unavailable("GitHub refused the request")
        if status in ("401", "404") or any(s in err for s in ("auth login", "not logged", "Not Found")):
            raise NotConfigured("no Copilot access through gh")
        raise RuntimeError("gh api failed")
    return parse(loads(out))


def parse(d):
    pi = (d.get("quota_snapshots") or {}).get("premium_interactions") or {}
    if not pi:
        raise NotConfigured("no Copilot quota")
    credits = bool(d.get("token_based_billing") or pi.get("token_based_billing"))
    label = "AI credits" if credits else "Premium requests"
    resets = to_epoch(d.get("quota_reset_date_utc") or d.get("quota_reset_date")) or to_epoch(pi.get("quota_reset_at"))
    if pi.get("unlimited"):
        return result("GITHUB COPILOT", rows=[row(label, value="unlimited", resets=resets)])
    total = pi.get("entitlement")
    left = pi.get("remaining", pi.get("quota_remaining"))
    used = pi.get("credits_used") if credits else None
    if used is None and total is not None and left is not None:
        used = total - left
    percent = None
    if pi.get("percent_remaining") is not None:
        percent = max(0.0, 100.0 - float(pi["percent_remaining"]))
    elif total:
        percent = 100.0 * (used or 0) / total
    value = f"{used:,.0f} / {total:,.0f}" if total is not None and used is not None else None
    details = [f"Overage: {pi['overage_count']:,.0f}"] if pi.get("overage_count") else []
    return result("GITHUB COPILOT", rows=[row(label, percent=percent, value=value, resets=resets)], details=details)
