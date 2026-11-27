"""Claude (Pro/Max subscription): 5-hour, weekly and model-specific weekly limits (e.g. Weekly Opus).

Source: the endpoint behind Claude Code's `/usage` screen, `GET /api/oauth/usage`. The OAuth access token is
read from Claude Code's Keychain item ("Claude Code-credentials") and only travels in the request header. It is
NEVER refreshed: a second client refreshing could sign Claude Code out. Claude Code refreshes it itself
whenever it runs.

Fallback when the token is expired: today's token counts from `~/.claude/stats-cache.json` (no percentages).
"""
import os
import time
from datetime import date

from ai_limits.common import (SECURITY, NotConfigured, SessionExpired, https_json, loads, result, row, run, shape,
                              shape_key, short_number, to_epoch)

URL = "https://api.anthropic.com/api/oauth/usage"
KIND = {"session": "5-hour", "weekly_all": "Weekly"}
LEGACY = {"five_hour": "5-hour", "seven_day": "Weekly", "seven_day_opus": "Weekly Opus",
          "seven_day_sonnet": "Weekly Sonnet", "seven_day_fable": "Weekly Fable"}
# The row's machine-readable window (common.row): only the account's 5-hour and weekly limits, not model-specific ones.
WINDOW = {"session": "5h", "weekly_all": "weekly", "five_hour": "5h", "seven_day": "weekly"}


def _token():
    code, out, _ = run([SECURITY, "find-generic-password", "-s", "Claude Code-credentials", "-w"], timeout=5)
    if code == 44:  # item not found: Claude Code is not signed in on this Mac
        raise NotConfigured("no Claude Code credentials")
    if code != 0:
        raise RuntimeError("Keychain item could not be read")
    entry = (loads(out) or {}).get("claudeAiOauth") or {}
    token, expires = entry.get("accessToken"), entry.get("expiresAt")
    if not token:
        raise NotConfigured("no token")
    if expires and float(expires) / 1000 < time.time():
        raise SessionExpired("expired")
    return token


def _model_name(limit):
    scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
    model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
    name = model.get("display_name") or model.get("id") or scope.get("surface")
    return str(name).replace("Claude ", "")[:20] if name else None


def rows_from_limits(limits):
    """Current shape (Nov 2026): limits = [{kind: session|weekly_all|weekly_scoped, percent, resets_at,
    scope: {model: {display_name}}}]. weekly_scoped is a model-specific weekly limit (e.g. Opus); it is only
    present for accounts that have one, so nothing is shown otherwise."""
    rows = []
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict) or limit.get("percent") is None:
            continue
        kind = limit.get("kind")
        if kind in KIND:
            label = KIND[kind]
        elif kind == "weekly_scoped":
            label = f"Weekly {_model_name(limit) or 'model'}"
        else:
            label = str(kind or "Limit").replace("_", " ").capitalize()
        rows.append(row(label, percent=float(limit["percent"]), resets=to_epoch(limit.get("resets_at")),
                        window=WINDOW.get(kind) if isinstance(kind, str) else None))
    return rows


def _legacy_rows(d):
    """Older shape: five_hour / seven_day / seven_day_<model> = {utilization, resets_at}."""
    rows = []
    for key, value in d.items():
        if isinstance(value, dict) and value.get("utilization") is not None:
            label = LEGACY.get(key) or key.replace("seven_day_", "Weekly ").replace("_", " ").capitalize()[:24]
            rows.append(row(label, percent=float(value["utilization"]), resets=to_epoch(value.get("resets_at")),
                            window=WINDOW.get(key)))
    order = {"5-hour": 0, "Weekly": 1}
    rows.sort(key=lambda x: order.get(x["label"], 2))
    return rows


def breakdown(data):
    """Where this week's usage came from: Claude Code, chats, Cowork, other (percent of the weekly limit)."""
    items = data.get("rows") if isinstance(data, dict) else None
    parts = []
    for x in items if isinstance(items, list) else []:
        if isinstance(x, dict) and x.get("percent_of_limit") is not None:
            parts.append(f"{str(x.get('display_name') or x.get('key') or '?')[:20]} {x['percent_of_limit']}%")
    return ["This week by surface (% of limit): " + ", ".join(parts)] if parts else []


def shares(items, limits):
    """A model-specific limit's share of the weekly limit (e.g. Opus: 40% of the weekly limit)."""
    out = []
    for s in items if isinstance(items, list) else []:
        if not isinstance(s, dict) or s.get("allowance_percent_of_weekly") is None:
            continue
        i = s.get("limit_index")
        limit = limits[i] if isinstance(limits, list) and isinstance(i, int) and 0 <= i < len(limits) else {}
        name = _model_name(limit) if isinstance(limit, dict) else None
        out.append(f"{name or 'Model'} share: {s['allowance_percent_of_weekly']}% of the weekly limit, "
                   f"{s.get('used_percent_of_weekly', '?')}% used")
    return out


def read(settings):
    try:
        token = _token()
        d = https_json(URL, {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
        del token
    except SessionExpired:
        r = _fallback()
        r["status"] = "expired"
        r["note"] = "Session expired: open Claude Code once"
        return r

    rows = rows_from_limits(d.get("limits")) or _legacy_rows(d)
    if not rows:
        raise RuntimeError("expected fields missing")
    details = breakdown(d.get("seven_day_breakdown")) + shares(d.get("weekly_scoped_shares"), d.get("limits"))
    extra = d.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        details.append("Extra usage is on")
    r = result("CLAUDE", rows=rows, details=details)
    if settings.get("debug_shapes"):
        r["debug"] = [debug_line(k, v) for k, v in d.items() if v is not None]
    return r


def debug_line(key, value):
    """One top-level field of the response for `debug_shapes`: field names and value types only."""
    return f"{shape_key(key)}={shape(value)}"[:900]


def _fallback():
    """Local summary without percentages: tokens used today, per model."""
    details = []
    try:
        with open(os.path.expanduser("~/.claude/stats-cache.json"), encoding="utf-8") as f:
            d = loads(f.read())
        today = date.today().isoformat()
        day = next((x for x in d.get("dailyModelTokens", []) if x.get("date") == today), None)
        if day:
            for model, n in sorted((day.get("tokensByModel") or {}).items(), key=lambda x: -x[1]):
                details.append(f"Today {model.replace('claude-', '')}: {short_number(n)} tokens")
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return result("CLAUDE", status="unknown", details=details or ["No local record for today"])
