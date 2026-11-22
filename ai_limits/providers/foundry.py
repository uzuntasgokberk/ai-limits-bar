"""Microsoft Foundry (Azure): spend today, this week, this month and all time; per-item details.

Identity: the Azure CLI session (`az login`); no keys. Two sources:
- Token metrics (Azure Monitor on `resource`; input, output, cache read, cache write 5 min / 1 h; UTC days).
  Estimated $ = tokens x the per-deployment list prices in config.json. Up to date within minutes. Azure keeps
  these metrics for 93 days, so monthly token totals per deployment are also kept in the history file.
  (`FoundryModelEstimatedCost` is empty for Anthropic models, so it is not used.)
- Invoices (Cost Management for `resource_group`, grouped by meter). Actual amounts, but Claude models are
  billed through the Azure Marketplace and arrive 1-2 days late. Claude meters do not name the model.
Cost Management throttles frequent queries (HTTP 429) and past months don't change, so the invoice and token
history is refreshed at most every `invoice_refresh_hours` (15 minutes on "Refresh now"), not again within 15
minutes of a failed attempt, and kept in `foundry-history.json` in the cache folder (0600; per-item and
per-deployment amounts and token totals only).

Totals: each period = max(token estimate, invoiced amount). All time = invoiced months before this one + this
month's total. The menu never shows the subscription id, tenant, resource or resource-group name; deployment
names and invoice item (model and service) names appear only in Details, and in the cache. Disabled by default:
set it up in config.json.
"""
import contextlib
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

from ai_limits.common import (NotConfigured, SessionExpired, find_tool, read_json, result, row, run, short_number,
                              write_private_json)

METRICS = ["InputTokens", "OutputTokens", "cacheReadInputTokens", "ephemeral5mInputTokens", "ephemeral1hInputTokens"]
# Price key -> metric. InputTokens excludes cache reads and writes (Anthropic convention).
PRICE_FIELD = {"InputTokens": "input", "OutputTokens": "output", "cacheReadInputTokens": "cache_read",
               "ephemeral5mInputTokens": "cache_write_5m", "ephemeral1hInputTokens": "cache_write_1h"}
AZURE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._()-]{0,89}")
HISTORY_FILE = "foundry-history.json"
PERIOD_KEY = re.compile(r"\d{4}-\d{2}(-\d{2})?")
METER_STOP = {"inp", "outp", "opt", "cd", "wr", "shortco", "glbl", "gl", "std", "1m", "tokens", "unit", "cached",
              "batch", "regnl", "dz", "rgnl"}
MAX_ITEMS = 14
FAILURE_PAUSE = 900  # seconds without a new history attempt after a failed one
LATEST_EPOCH = 4102444800  # 2100-01-01


class Throttled(Exception):
    """Cost Management answered 429; keep using the saved history."""


def valid_name(x):
    """az replaces '@file' arguments with the file's content, so names must be plain."""
    return x if isinstance(x, str) and AZURE_NAME.fullmatch(x) else None


def valid_budget(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) and x > 0 else None


def _az(az, argv, timeout=40):
    code, out, err = run([az, *argv, "-o", "json"], timeout=timeout)
    if code != 0:
        if "az login" in err or "AADSTS" in err or "expired" in err.lower():
            raise SessionExpired("az session")
        if "429" in err or "Too Many Requests" in err:
            raise Throttled()
        raise RuntimeError("az failed")
    return json.loads(out) if out.strip() else None


# ---- periods --------------------------------------------------------------------------------------------

def period_starts(now):
    """First UTC day ('YYYY-MM-DD') of today, this week (Monday) and this month."""
    return {"today": now.strftime("%Y-%m-%d"),
            "week": (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d"),
            "month": now.strftime("%Y-%m-01")}


def sum_days(days, first_day):
    """Add up {'YYYY-MM-DD': {metric: n}} (or {'YYYY-MM-DD': amount}) on or after first_day."""
    total = {}
    for day, value in days.items():
        if day >= first_day:
            if isinstance(value, dict):
                for k, v in value.items():
                    total[k] = total.get(k, 0) + v
            else:
                total["$"] = total.get("$", 0) + value
    return total


# ---- invoices -------------------------------------------------------------------------------------------

def meter_item(meter, subcategory):
    """Readable item name from a Cost Management meter: the model for Azure OpenAI meters (e.g. 'GPT-9 demo'
    from '9 demo ... 1M Tokens' in an '... GPT5' subcategory, 'demo-listen' from 'demo-listen-aud-...'),
    'Claude (<where it is hosted>)' for Claude meters (they don't name the model), otherwise the service
    (the subcategory, e.g. 'Example Service')."""
    meter, sub = str(meter or ""), str(subcategory or "")
    if sub.startswith("Claude in Microsoft Foundry"):
        hosted = re.search(r"\((.*?)\)", sub)
        return f"Claude ({hosted.group(1)})" if hosted else "Claude"
    if sub.startswith("Azure OpenAI") and meter:
        name = re.sub(r"-(aud|txt|img)-.*$", "", meter)
        words = []
        for w in name.split():
            if w.lower() in METER_STOP:
                break
            words.append(w)
        name = " ".join(words) or meter
        name = re.sub(r"-(regnl|glbl|batch)$", "", name, flags=re.I)
        if sub.endswith("GPT5") and name[:1].isdigit():
            name = f"GPT-{name}"
        return name[:40]
    return (sub or meter or "Other")[:40]


def _cost_query(az, subscription, group, start, end, granularity):
    """[(period 'YYYY-MM' or 'YYYY-MM-DD', item, amount)] and the currency, for one window (at most a year)."""
    body = {"type": "ActualCost", "timeframe": "Custom",
            "timePeriod": {"from": start.strftime("%Y-%m-%dT00:00:00Z"), "to": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "dataset": {"granularity": granularity, "aggregation": {"t": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": "Meter"},
                                     {"type": "Dimension", "name": "MeterSubCategory"}],
                        "filter": {"dimensions": {"name": "ResourceGroupName", "operator": "In", "values": [group]}}}}
    d = _az(az, ["rest", "--method", "post", "--url",
                 f"https://management.azure.com/subscriptions/{subscription}/providers/"
                 "Microsoft.CostManagement/query?api-version=2023-11-01", "--body", json.dumps(body)])
    columns = [c["name"] for c in d["properties"]["columns"]]
    date_col = next((c for c in ("BillingMonth", "UsageDate", "UsageMonth") if c in columns), None)
    out, currency = [], "USD"
    for r in d["properties"]["rows"]:
        raw = str(r[columns.index(date_col)]) if date_col else ""
        if raw.isdigit() and len(raw) == 8:
            key = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        else:
            key = raw[:10] if granularity == "Daily" else raw[:7]
        if not PERIOD_KEY.fullmatch(key):
            continue
        item = meter_item(r[columns.index("Meter")] if "Meter" in columns else "",
                          r[columns.index("MeterSubCategory")] if "MeterSubCategory" in columns else "")
        out.append((key, item, float(r[columns.index("Cost")])))
        if "Currency" in columns:
            currency = r[columns.index("Currency")]
    return out, currency


def _invoices(az, subscription, group, now):
    """{'monthly': {item: {'YYYY-MM': $}}, 'daily': {item: {'YYYY-MM-DD': $}}, 'currency'}. Monthly windows of
    at most a year go back while the oldest window still has data (at most 5); daily covers this month/week."""
    monthly, currency = {}, "USD"
    end = now
    for _ in range(5):
        start = end - timedelta(days=359)
        rows, currency = _cost_query(az, subscription, group, start, end, "Monthly")
        for month, item, amount in rows:
            monthly.setdefault(item, {})[month] = monthly.setdefault(item, {}).get(month, 0.0) + amount
        if not rows or min(m for m, _, _ in rows) > start.strftime("%Y-%m"):
            break  # the data starts inside this window: nothing older
        end = start - timedelta(seconds=1)
    first = min(now.replace(day=1), now - timedelta(days=now.weekday()))
    daily = {}
    rows, _ = _cost_query(az, subscription, group, first, now, "Daily")
    for day, item, amount in rows:
        daily.setdefault(item, {})[day] = daily.setdefault(item, {}).get(day, 0.0) + amount
    rnd = lambda d: {i: {k: round(v, 4) for k, v in p.items()} for i, p in d.items()}  # noqa: E731
    return {"monthly": rnd(monthly), "daily": rnd(daily), "currency": str(currency)[:8]}


# ---- tokens ---------------------------------------------------------------------------------------------

def _tokens(az, resource_id, start, end):
    """Per deployment: {'YYYY-MM-DD': {metric: n}} (UTC days). Queried in chunks of at most 28 days."""
    models = {}
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=28), end)
        d = _az(az, ["monitor", "metrics", "list", "--resource", resource_id, "--metrics", *METRICS,
                     "--aggregation", "Total", "--interval", "P1D",
                     "--start-time", chunk_start.strftime("%Y-%m-%dT%H:%M:00Z"),
                     "--end-time", chunk_end.strftime("%Y-%m-%dT%H:%M:00Z"),
                     "--filter", "ModelDeploymentName eq '*'"])
        for m in d.get("value", []):
            name = m["name"]["value"]
            for series in m.get("timeseries", []):
                deployment = next((x["value"] for x in series.get("metadatavalues", [])), "?")
                days = models.setdefault(str(deployment)[:60], {})
                for point in series.get("data", []):
                    n = point.get("total") or 0
                    if n:
                        day = days.setdefault(point.get("timeStamp", "")[:10], {})
                        day[name] = day.get(name, 0) + n
        chunk_start = chunk_end
    return models


def _monthly_tokens(models):
    """{deployment: {'YYYY-MM-DD': metrics}} -> {deployment: {'YYYY-MM': metrics}}."""
    out = {}
    for deployment, days in models.items():
        for day, metrics in days.items():
            month = out.setdefault(deployment, {}).setdefault(day[:7], {})
            for k, v in metrics.items():
                month[k] = month.get(k, 0) + v
    return out


def _epoch_seconds(x):
    """A saved time stamp as epoch seconds; text, NaN or out of range -> 0 (= long ago)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 <= x <= LATEST_EPOCH:
        return 0.0
    return float(x)


def _load_history(path):
    """The saved history file, with every part in the expected shape; None if there is no usable file."""
    saved = read_json(path) if path else None
    if not (isinstance(saved, dict) and isinstance(saved.get("monthly"), dict)):
        return None
    for key in ("monthly", "daily", "tokens"):
        part = saved.get(key)
        saved[key] = {k: v for k, v in part.items() if isinstance(v, dict)} if isinstance(part, dict) else {}
    saved["fetched"] = _epoch_seconds(saved.get("fetched"))
    return saved


def _save_history(cache_dir, data):
    if cache_dir:
        with contextlib.suppress(OSError):
            write_private_json(cache_dir, HISTORY_FILE, data)


def history(az, subscription, group, resource_id, cache_dir, refresh_hours, force, now):
    """Saved history if fresh enough, otherwise refreshed: invoices and monthly token totals per deployment
    for the last 90 days (merged into older saved months, which Azure Monitor no longer has).
    Any failure except an expired session keeps the saved history (None if there is none). With a saved file the
    failure time is recorded and no new attempt is made for 15 minutes, not even on "Refresh now"."""
    path = os.path.join(cache_dir, HISTORY_FILE) if cache_dir else None
    saved = _load_history(path)
    max_age = min(refresh_hours * 3600, 900) if force else refresh_hours * 3600
    t = time.time()
    if saved and (0 <= t - saved["fetched"] < max_age
                  or 0 <= t - _epoch_seconds(saved.get("failed")) < FAILURE_PAUSE):
        return saved
    try:
        fresh = _invoices(az, subscription, group, now)
    except SessionExpired:
        raise
    except Exception:  # throttled (429), timeout, az failed, unexpected answer
        if saved:
            saved["failed"] = time.time()
            _save_history(cache_dir, saved)
        return saved  # may be None: totals then come from token estimates only
    fresh["tokens"] = dict((saved or {}).get("tokens") or {})
    fresh["fetched"] = time.time()
    # Saved before the token queries: on a first run they can outlast the provider's time budget (the process
    # then exits), and the invoices would be lost and queried again. The next token refresh then comes with the
    # next invoice refresh; this month's tokens are read live on every run anyway.
    _save_history(cache_dir, fresh)
    if resource_id:
        try:
            recent = _monthly_tokens(_tokens(az, resource_id, now - timedelta(days=90), now))
        except Exception:
            recent = {}
        for deployment, months in recent.items():
            merged = dict(fresh["tokens"].get(deployment) or {})
            merged.update(months)
            fresh["tokens"][deployment] = merged
        if recent:
            _save_history(cache_dir, fresh)
    return fresh


# ---- money ----------------------------------------------------------------------------------------------

def estimate(tokens, prices):
    """Tokens x list price ($ per million). None if a used token type has no valid price."""
    total = 0.0
    for metric, field in PRICE_FIELD.items():
        n = tokens.get(metric, 0)
        if not n:
            continue
        price = prices.get(field) if isinstance(prices, dict) else None
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
            return None
        total += n * float(price) / 1e6
    return total


def money(x, currency="USD"):
    return f"${x:,.2f}" if currency == "USD" else f"{x:,.2f} {currency}"


def model_lines(live, saved_tokens, prices, now):
    """Per deployment: today · this week · this month · all time (estimated $, or output tokens without a price),
    and the estimated totals {'today','week','month'} -> (amount, every deployment priced?)."""
    starts = period_starts(now)
    this_month = now.strftime("%Y-%m")
    totals = {p: [0.0, True] for p in starts}
    lines = []
    for deployment in sorted(set(live) | set(saved_tokens)):
        days = live.get(deployment, {})
        per = {p: sum_days(days, first) for p, first in starts.items()}
        all_time = {}
        for month, metrics in (saved_tokens.get(deployment) or {}).items():
            if month < this_month and isinstance(metrics, dict):
                for k, v in metrics.items():
                    all_time[k] = all_time.get(k, 0) + v
        for k, v in per["month"].items():
            all_time[k] = all_time.get(k, 0) + v
        if not any(all_time.values()):
            continue
        price = prices.get(deployment, {})
        cells = []
        for p, t in list(per.items()) + [("all", all_time)]:
            cost = estimate(t, price)
            if p in totals:
                if cost is None and any(t.values()):
                    totals[p][1] = False
                totals[p][0] += cost or 0
            if not any(t.values()):
                cells.append("–")
            elif cost is not None:
                cells.append(f"≈{money(cost)}")
            else:
                cells.append(f"{short_number(t.get('OutputTokens', 0))} out")
        sort_key = estimate(all_time, price)
        lines.append((sort_key if sort_key is not None else -1, f"{deployment}: " + " · ".join(cells)))
    lines.sort(key=lambda x: -x[0])
    return [line for _, line in lines], {p: (v[0], v[1]) for p, v in totals.items()}


def invoice_lines(hist, now):
    """Per invoice item: today · this week · this month · all time (actual, delayed), and the totals."""
    starts = period_starts(now)
    this_month = now.strftime("%Y-%m")
    currency = hist.get("currency") or "USD"
    monthly, daily = hist.get("monthly") or {}, hist.get("daily") or {}
    totals = {"today": 0.0, "week": 0.0, "month": 0.0, "earlier": 0.0}
    lines = []
    for item in set(monthly) | set(daily):
        months = monthly.get(item) or {}
        days = daily.get(item) or {}
        today = sum_days(days, starts["today"]).get("$", 0.0)
        week = sum_days(days, starts["week"]).get("$", 0.0)
        month = float(months.get(this_month, 0.0))
        earlier = sum(float(v) for k, v in months.items() if k < this_month)
        totals["today"] += today
        totals["week"] += week
        totals["month"] += month
        totals["earlier"] += earlier
        if month + earlier + week > 0.0005:
            lines.append((month + earlier, f"{item}: " + " · ".join(money(v, currency) for v in (today, week, month, month + earlier))))
    lines.sort(key=lambda x: -x[0])
    out = [line for _, line in lines[:MAX_ITEMS]]
    if len(lines) > MAX_ITEMS:
        out.append(f"… and {len(lines) - MAX_ITEMS} more items")
    first = min((k for m in monthly.values() for k in m), default=None)
    return out, totals, first


def read(settings):
    az = find_tool("az")
    if not az:
        raise NotConfigured("az not installed")
    group, resource = valid_name(settings.get("resource_group")), valid_name(settings.get("resource"))
    if not group:
        raise NotConfigured("resource_group missing in config.json")
    budget = valid_budget(settings.get("budget_usd"))
    prices = settings.get("prices_usd_per_million") if isinstance(settings.get("prices_usd_per_million"), dict) else {}
    hours = settings.get("invoice_refresh_hours")
    hours = float(hours) if isinstance(hours, (int, float)) and not isinstance(hours, bool) and 1 <= hours <= 168 else 6.0
    now = datetime.now(timezone.utc)

    subscription = _az(az, ["account", "show", "--query", "id"], timeout=20)
    resource_id = _az(az, ["cognitiveservices", "account", "show", "-n", resource, "-g", group, "--query", "id"],
                      timeout=20) if resource else None

    live = {}
    if resource_id:
        try:
            first = min(now.replace(day=1), now - timedelta(days=now.weekday()))
            live = _tokens(az, resource_id, first.replace(hour=0, minute=0, second=0, microsecond=0), now)
        except SessionExpired:
            raise
        except Exception:
            live = None
    try:
        hist = history(az, subscription, group, resource_id, settings.get("cache_dir"), hours,
                       settings.get("force") is True, now)
    except SessionExpired:
        raise
    except Exception:
        hist = None

    models, est = model_lines(live or {}, (hist or {}).get("tokens") or {}, prices, now)
    if hist:
        items, invoiced, first_month = invoice_lines(hist, now)
    else:
        items, invoiced, first_month = [], None, None
    currency = (hist or {}).get("currency") or "USD"

    def total(period):
        e = est[period][0] if live is not None else 0.0
        i = invoiced[period] if invoiced else 0.0
        return max(e, i), est[period][1] or i >= e

    rows = []
    if live is None and not invoiced:
        rows.append(row("Spend", value="unknown"))
    else:
        for period, label in (("today", "Today"), ("week", "This week"), ("month", "This month")):
            amount, exact = total(period)
            sign = "≈" if exact else "≥"
            if period == "month" and budget:
                rows.append(row(label, percent=100.0 * amount / budget,
                                value=f"{sign}{money(amount, currency)} / {money(budget, currency)}"))
            else:
                rows.append(row(label, value=f"{sign}{money(amount, currency)}"))
        if invoiced is not None:
            rows.append(row("All time", value=f"≈{money(invoiced['earlier'] + total('month')[0], currency)}"))
        else:
            rows.append(row("All time", value="waiting for Azure Cost Management"))

    details = ["Columns: today · this week · this month · all time"]
    if models:
        details.append("Models, estimated from tokens (up to date):")
        details += [f"· {line}" for line in models]
    if items:
        details.append("Invoice items, actual (Claude arrives 1-2 days late):")
        details += [f"· {line}" for line in items]
    if first_month:
        details.append(f"All time since {first_month}; invoices checked "
                       f"{time.strftime('%b %d %H:%M', time.localtime(_epoch_seconds(hist.get('fetched'))))}")
    if any(" out" in line for line in models):
        details.append("Models shown in tokens have no price in config.json (≥ = at least)")
    details.append("Each total = max(token estimate, invoice); weeks start Monday; days are UTC")
    return result("MICROSOFT FOUNDRY", rows=rows, details=details)
