"""Microsoft Foundry (Azure): spend today, this week, this month and all time; per-item details.

Identity: the Azure CLI session (`az login`); no keys. Two sources, both through az:
- Azure Monitor on `resource` (UTC days): token metrics (input, output, cache read, cache write 5 min / 1 h), and
  Azure's own estimated cost per deployment (`FoundryModelEstimatedCost`, preview, USD; a separate query). A
  deployment's estimated $ is Azure's estimate where Azure has one for it, otherwise tokens x the per-deployment
  prices in config.json (see model_lines). Azure keeps these metrics for 93 days, so monthly totals per deployment
  are also kept in the history file.
- Invoices (Cost Management for `resource_group`, grouped by meter). Actual amounts, but Claude models are
  billed through the Azure Marketplace and arrive 1-2 days late. Claude meters do not name the model.
Cost Management throttles frequent queries (HTTP 429) and past months don't change, so the invoice and token
history is refreshed at most every `invoice_refresh_hours` (15 minutes on "Refresh now"), not again within 15
minutes of a failed attempt, and kept in `foundry-history.json` in the cache folder (0600; per-item and
per-deployment amounts and token totals only). After each successful token read, the deployments with tokens but
no Azure estimate, and the token types they used (no amounts), are saved in `foundry-deployments.json` for the
"Foundry prices…" window, which reads no provider. Prices are set there or with --set-price (main.py), or by hand
in config.json.

Totals: each period = max(model estimates, invoiced amount). All time = invoiced months before this one + this
month's total. A total is marked `≈` only if it can be complete, otherwise `≥` (at least): see spend_rows. The
section counts the deployments whose missing price makes a shown row `≥` (`unpriced` in the result). The menu never
shows the subscription id, tenant, resource or resource-group name; deployment names and invoice item (model and
service) names appear only in Details, and in the cache. Disabled by default: set it up in config.json.
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
# Price key -> metric. For Anthropic deployments InputTokens is taken to exclude cache reads and writes (Anthropic's
# convention; Azure doesn't say). Other deployments report no cache metrics at all (see model_lines).
PRICE_FIELD = {"InputTokens": "input", "OutputTokens": "output", "cacheReadInputTokens": "cache_read",
               "ephemeral5mInputTokens": "cache_write_5m", "ephemeral1hInputTokens": "cache_write_1h"}
# Documented by Azure Monitor for Anthropic model deployments only.
CACHE_METRICS = ("cacheReadInputTokens", "ephemeral5mInputTokens", "ephemeral1hInputTokens")
# Azure's own estimated cost of a deployment's usage, in USD (preview; "Applies to Foundry models only"). Present for
# some models (seen for Azure OpenAI ones), absent for others (seen for Anthropic ones): see model_lines.
COST_METRIC = "FoundryModelEstimatedCost"
MAX_DAILY_ESTIMATE = 1_000_000.0  # USD per deployment and day; a larger total is taken as broken data
MAX_TOKENS = 1e15  # per deployment, metric and day
PRICE_KEYS = tuple(PRICE_FIELD.values())  # the order of the "Foundry prices" window's columns
MAX_PRICE = 10000.0  # USD per million tokens; the most expensive list price is far below it
# A typed price: ASCII digits only (float() would also take 'nan', 'inf', '1e3', '1_0' and non-ASCII digits), with
# '.' or ',' before the decimals, so '2,5' typed with a decimal comma is 2.5.
PRICE_TEXT = re.compile(r"[0-9]{1,5}(?:[.,][0-9]{1,6})?|[.,][0-9]{1,6}")
AZURE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._()-]{0,89}")
HISTORY_FILE = "foundry-history.json"
DEPLOYMENTS_FILE = "foundry-deployments.json"  # in the cache folder: what the prices window lists
MAX_DEPLOYMENTS = 50
PERIOD_KEY = re.compile(r"\d{4}-\d{2}(-\d{2})?")
METER_STOP = {"inp", "outp", "opt", "cd", "wr", "shortco", "glbl", "gl", "std", "1m", "tokens", "unit", "cached",
              "batch", "regnl", "dz", "rgnl", "longco", "loco", "shco"}  # *co: the short and long context tiers
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


def valid_price(x):
    """A price from config.json: a finite number from 0 to MAX_PRICE (USD per million tokens); otherwise None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) and 0 <= x <= MAX_PRICE else None


def parse_price(text):
    """A typed price (the prices window, --set-price): '2', '2.5', '2,5' or '.5', spaces around it ignored, at most
    MAX_PRICE. Empty means no price (None). Anything else raises ValueError. Whole numbers stay int."""
    if not isinstance(text, str):
        raise ValueError("not text")
    s = text.strip(" \t")
    if not s:
        return None
    if not PRICE_TEXT.fullmatch(s):
        raise ValueError("not a price")
    s = s.replace(",", ".")
    value = float(s)  # only ASCII digits and at most one dot get here
    if value > MAX_PRICE:
        raise ValueError("too large")
    return value if "." in s else int(s)


def price_text(x):
    """A price as the prices window shows it: '2', '2.5', '0.075'; '' if it is not a valid price."""
    x = valid_price(x)
    if x is None:
        return ""
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def save_deployments(cache_dir, used, current=None):
    """foundry-deployments.json: the deployments with tokens but no Azure estimate (model_lines' 'by_settings') and
    the token types each used, for the prices window (which reads no provider), and under "current" the token types
    each used in a shown period (today, this week, this month; model_lines' 'current'). Only names that pass
    valid_name, at most MAX_DEPLOYMENTS; no amounts. Without `current` the part is left out (unknown)."""
    names = sorted(n for n in used if valid_name(n))[:MAX_DEPLOYMENTS]
    data = {"time": time.time(), "deployments": {n: [m for m in PRICE_FIELD if m in used[n]] for n in names}}
    if current is not None:
        data["current"] = {n: [m for m in PRICE_FIELD if m in current[n]] for n in names if n in current}
    if cache_dir:
        with contextlib.suppress(OSError):
            write_private_json(cache_dir, DEPLOYMENTS_FILE, data)


def _load_part(cache_dir, key):
    """{deployment: [token types]} from one part of foundry-deployments.json, checked strictly: names must pass
    valid_name, token types must be known metrics, at most MAX_DEPLOYMENTS; anything else is left out. None if the
    file or the part is missing or not an object."""
    saved = read_json(os.path.join(cache_dir, DEPLOYMENTS_FILE)) if cache_dir else None
    found = saved.get(key) if isinstance(saved, dict) else None
    if not isinstance(found, dict):
        return None
    out = {}
    for name, used in found.items():
        if valid_name(name) and isinstance(used, list) and len(out) < MAX_DEPLOYMENTS:
            out[name] = [m for m in PRICE_FIELD if m in used]
    return out


def load_deployments(cache_dir):
    """{deployment: [token types it used]}: the deployments the prices window offers (see save_deployments)."""
    return _load_part(cache_dir, "deployments") or {}


def load_current(cache_dir):
    """{deployment: [token types used today, this week or this month]}, or None if the file doesn't say (an older
    file): the window then treats every deployment as one that changes a shown row."""
    return _load_part(cache_dir, "current")


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


# ---- token metrics and Azure's estimate ------------------------------------------------------------------

def _total(x, high):
    """A metric point's total if it is a finite number above 0 and at most `high`; otherwise None (no data)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 < x <= high:
        return None
    return x


def _metric_days(az, resource_id, metrics, start, end, high):
    """Per deployment: {'YYYY-MM-DD': {metric: total}} (UTC days) for `metrics`, queried in chunks of at most 28
    days. A point whose total is not a number above 0 and at most `high` counts as no data."""
    models = {}
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=28), end)
        d = _az(az, ["monitor", "metrics", "list", "--resource", resource_id, "--metrics", *metrics,
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
                    n = _total(point.get("total"), high)
                    if n is not None:
                        day = days.setdefault(str(point.get("timeStamp", ""))[:10], {})
                        day[name] = day.get(name, 0) + n
        chunk_start = chunk_end
    return {deployment: days for deployment, days in models.items() if days}


def _tokens(az, resource_id, start, end):
    """Per deployment: {'YYYY-MM-DD': {metric: n}} (UTC days) of the token metrics."""
    return _metric_days(az, resource_id, METRICS, start, end, MAX_TOKENS)


def _estimates(az, resource_id, start, end):
    """Azure's own estimated cost per deployment (COST_METRIC, preview, USD): {deployment: {'YYYY-MM-DD': USD}}. A
    query of its own, so that a resource or an az without this metric never costs the token metrics. A day whose total
    is not a number above 0 and at most MAX_DAILY_ESTIMATE is no data, and a deployment without such a day is left
    out: Azure has no estimate for some models (none for the Anthropic deployments seen so far)."""
    days = _metric_days(az, resource_id, [COST_METRIC], start, end, MAX_DAILY_ESTIMATE)
    out = {}
    for deployment, per_day in days.items():
        found = {day: m[COST_METRIC] for day, m in per_day.items() if COST_METRIC in m}
        if found:
            out[deployment] = found
    return out


def _monthly_tokens(models):
    """{deployment: {'YYYY-MM-DD': metrics}} -> {deployment: {'YYYY-MM': metrics}}."""
    out = {}
    for deployment, days in models.items():
        for day, metrics in days.items():
            month = out.setdefault(deployment, {}).setdefault(day[:7], {})
            for k, v in metrics.items():
                month[k] = month.get(k, 0) + v
    return out


def _monthly_estimates(estimates):
    """{deployment: {'YYYY-MM-DD': USD}} -> {deployment: {'YYYY-MM': USD}}."""
    out = {}
    for deployment, days in estimates.items():
        months = out.setdefault(deployment, {})
        for day, usd in days.items():
            months[day[:7]] = round(months.get(day[:7], 0.0) + usd, 6)
    return out


def _amounts(x, high):
    """{period: USD} from a saved or queried estimate: only text keys and amounts above 0 and at most `high`."""
    if not isinstance(x, dict):
        return {}
    return {k: float(v) for k, v in x.items() if isinstance(k, str) and _total(v, high) is not None}


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
    for key in ("monthly", "daily", "tokens", "estimates"):
        part = saved.get(key)
        saved[key] = {k: v for k, v in part.items() if isinstance(v, dict)} if isinstance(part, dict) else {}
    saved["fetched"] = _epoch_seconds(saved.get("fetched"))
    return saved


def _save_history(cache_dir, data):
    if cache_dir:
        with contextlib.suppress(OSError):
            write_private_json(cache_dir, HISTORY_FILE, data)


def history(az, subscription, group, resource_id, cache_dir, refresh_hours, force, now):
    """Saved history if fresh enough, otherwise refreshed: invoices, and monthly token totals and monthly totals of
    Azure's estimated cost per deployment for the last 90 days (merged into older saved months, which Azure Monitor
    no longer has).
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
    fresh["estimates"] = dict((saved or {}).get("estimates") or {})
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
        try:  # Azure's estimate: a preview metric, so any failure only means no new months
            recent = _monthly_estimates(_estimates(az, resource_id, now - timedelta(days=90), now))
        except Exception:
            recent = {}
        for deployment, months in recent.items():
            merged = dict(fresh["estimates"].get(deployment) or {})
            merged.update(months)
            fresh["estimates"][deployment] = merged
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
        price = valid_price(prices.get(field) if isinstance(prices, dict) else None)
        if price is None:
            return None
        total += n * price / 1e6
    return total


def money(x, currency="USD"):
    return f"${x:,.2f}" if currency == "USD" else f"{x:,.2f} {currency}"


def model_lines(live, saved_tokens, prices, now, live_estimates=None, saved_estimates=None):
    """Per deployment: today · this week · this month · all time, the totals {'today','week','month'} ->
    (amount, complete?), and lists of deployment names. The same rule for every model, whoever publishes it:
    - Azure's estimate (COST_METRIC), if Azure has one for the deployment this month (`live_estimates`, per day) or
      in a saved month (`saved_estimates`). A price from settings is then never used for it: for models whose
      InputTokens include cached input (as for Azure OpenAI ones), tokens x price would price that cached input at the
      full rate, several times too high. A day or month with tokens but without an estimate makes the period
      incomplete (`≥`): the estimate may lag, or not cover that usage.
    - Otherwise tokens x the price from settings ($ per million, per token type); a period with tokens and no usable
      price is incomplete.
    A cell is `≈$x` (complete), `≥$x` (part of it), `N out` (output tokens, nothing priced) or `–` (no usage).
    Names: 'unpriced' (no Azure estimate, and tokens without a usable price today, this week or this month, so a
    shown row is `≥` because of it: the section's orange count), 'unpriced_older' (no Azure estimate and no usable
    price, but no such tokens in a shown period: only Details and its all-time cell depend on it; the main All time
    row takes earlier months from the invoice), 'by_settings' ({deployment: token types used} for every deployment
    without an Azure estimate: those the prices window offers), 'current' (the same, token types used in a shown
    period), 'pending' (Azure estimate, but token days or months without one) and 'uncached_input' (priced from
    settings, input tokens, no cache tokens: Azure reports cache tokens only for Anthropic deployments, so that input
    is priced as uncached)."""
    live_estimates, saved_estimates = live_estimates or {}, saved_estimates or {}
    starts = period_starts(now)
    this_month = now.strftime("%Y-%m")
    totals = {p: [0.0, True] for p in starts}
    lines = []
    names = {"unpriced": [], "unpriced_older": [], "uncached_input": [], "by_settings": {}, "current": {},
             "pending": []}
    for deployment in sorted(set(live) | set(saved_tokens)):
        days = live.get(deployment, {})
        months = {m: v for m, v in (saved_tokens.get(deployment) or {}).items()
                  if m < this_month and isinstance(v, dict)}
        per = {p: sum_days(days, first) for p, first in starts.items()}
        all_time = {}
        for metrics in months.values():
            for k, v in metrics.items():
                all_time[k] = all_time.get(k, 0) + v
        for k, v in per["month"].items():
            all_time[k] = all_time.get(k, 0) + v
        if not any(all_time.values()) and not any(per["week"].values()):
            continue  # no usage at all (this week can reach into last month, before the history has that month)
        est_days = _amounts(live_estimates.get(deployment), MAX_DAILY_ESTIMATE)
        est_months = _amounts(saved_estimates.get(deployment), 31 * MAX_DAILY_ESTIMATE)
        azure = bool(est_days or est_months)
        price = prices.get(deployment, {}) if isinstance(prices, dict) else {}
        cells, amounts = [], {}
        for p, t in list(per.items()) + [("all", all_time)]:
            if not azure:
                cost = estimate(t, price)
                amount, complete = cost or 0.0, cost is not None or not any(t.values())
            elif p != "all":
                first = starts[p]
                amount = sum(v for day, v in est_days.items() if day >= first)
                complete = all(est_days.get(day) for day, m in days.items() if day >= first and any(m.values()))
            else:
                amount = amounts["month"][0] + sum(v for m, v in est_months.items() if m < this_month)
                complete = amounts["month"][1] and all(est_months.get(m) for m, v in months.items()
                                                       if any(v.values()))
            amounts[p] = (amount, complete)
            has = any(t.values()) or amount > 0
            if p in totals:
                totals[p][0] += amount
                totals[p][1] = totals[p][1] and (complete or not has)
            if not has:
                cells.append("–")
            elif complete:
                cells.append(f"≈{money(amount)}")
            elif amount > 0:
                cells.append(f"≥{money(amount)}")
            else:
                cells.append(f"{short_number(t.get('OutputTokens', 0))} out")
        all_amount, all_complete = amounts["all"]
        if azure:
            source = "Azure estimate"
            if not all_complete:
                names["pending"].append(deployment)
        else:
            names["by_settings"][deployment] = [m for m in PRICE_FIELD if all_time.get(m) or per["week"].get(m)]
            current = [m for m in PRICE_FIELD if per["week"].get(m) or per["month"].get(m)]
            if current:
                names["current"][deployment] = current
            if not all(complete for _, complete in amounts.values()):
                source = "no price"
                shown = not (amounts["week"][1] and amounts["month"][1])  # today is inside both
                names["unpriced" if shown else "unpriced_older"].append(deployment)
            else:
                source = "price from settings"
                if all_time.get("InputTokens") and not any(all_time.get(m) for m in CACHE_METRICS):
                    names["uncached_input"].append(deployment)
        sort_key = all_amount if all_complete or all_amount > 0 else -1
        lines.append((sort_key, f"{deployment} ({source}): " + " · ".join(cells)))
    lines.sort(key=lambda x: -x[0])
    return [line for _, line in lines], {p: (v[0], v[1]) for p, v in totals.items()}, names


def spend_rows(est, invoiced, tokens_read, budget, currency):
    """The main rows: Today, This week, This month and All time.

    Each period shows max(model estimates, invoiced amount). The sign says whether that can be the whole amount:
    - `≈` only if the token metrics were read and every deployment with tokens in that period has an amount for it
      (Azure's estimate for each day with tokens, or a usable price from settings; see model_lines). The estimates
      then cover all model usage up to the last read, and the invoice covers the rest.
    - `≥` (at least) otherwise, whatever the invoice says. The invoice is exact but late (Claude 1-2 days, other
      models hours), so even an invoice larger than the estimate may not yet contain the missing usage, and
      without token metrics (failed, or no `resource`) today's usage is not known at all. 0 versus 0 is `≥` too.
    All time = invoiced months before this one + this month's amount, with this month's sign.
    est: model_lines' totals; invoiced: invoice_lines' totals or None (no invoice history yet)."""
    if not tokens_read and not invoiced:
        return [row("Spend", value="unknown")]
    rows, month = [], (0.0, "≥")
    for period, label in (("today", "Today"), ("week", "This week"), ("month", "This month")):
        e = est[period][0] if tokens_read else 0.0
        amount = max(e, invoiced[period] if invoiced else 0.0)
        sign = "≈" if tokens_read and est[period][1] else "≥"
        if period == "month":
            month = (amount, sign)
            if budget:
                rows.append(row(label, percent=100.0 * amount / budget,
                                value=f"{sign}{money(amount, currency)} / {money(budget, currency)}"))
                continue
        rows.append(row(label, value=f"{sign}{money(amount, currency)}"))
    if invoiced is not None:
        rows.append(row("All time", value=f"{month[1]}{money(invoiced['earlier'] + month[0], currency)}"))
    else:
        rows.append(row("All time", value="waiting for Azure Cost Management"))
    return rows


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

    live, estimates = {}, None
    if resource_id:
        first = min(now.replace(day=1), now - timedelta(days=now.weekday()))
        first = first.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            live = _tokens(az, resource_id, first, now)
        except SessionExpired:
            raise
        except Exception:
            live = None
        if live is not None:  # after a failed token read another az call would only use up the time budget
            try:
                estimates = _estimates(az, resource_id, first, now)
            except Exception:  # a preview metric: any failure means no estimate, never a broken section
                estimates = None
    try:
        hist = history(az, subscription, group, resource_id, settings.get("cache_dir"), hours,
                       settings.get("force") is True, now)
    except SessionExpired:
        raise
    except Exception:
        hist = None

    models, est, names = model_lines(live or {}, (hist or {}).get("tokens") or {}, prices, now, estimates or {},
                                     (hist or {}).get("estimates") or {})
    unpriced = names["unpriced"]
    tokens_read = resource_id is not None and live is not None
    if tokens_read:  # a failed token read keeps the last list, so today's new deployments stay in the window
        save_deployments(settings.get("cache_dir"), names["by_settings"], names["current"])
    if hist:
        items, invoiced, first_month = invoice_lines(hist, now)
    else:
        items, invoiced, first_month = [], None, None
    currency = (hist or {}).get("currency") or "USD"
    rows = spend_rows(est, invoiced, tokens_read, budget, currency)

    details = ["Columns: today · this week · this month · all time"]
    if models:
        details.append("Models (Azure estimate, or tokens × price from settings):")
        details += [f"· {line}" for line in models]
    if items:
        details.append("Invoice items, actual (Claude arrives 1-2 days late):")
        details += [f"· {line}" for line in items]
    if first_month:
        details.append(f"All time since {first_month}; invoices checked "
                       f"{time.strftime('%b %d %H:%M', time.localtime(_epoch_seconds(hist.get('fetched'))))}")
    if unpriced or names["unpriced_older"]:
        details.append("(no price): counted in tokens only; with tokens this week or month it makes those totals ≥. "
                       "Set prices under Foundry prices…")
    if names["pending"]:
        details.append("Some token days or months have no Azure estimate (yet): ≥ = at least")
    if names["uncached_input"]:
        details.append("Priced from settings, without cache tokens: all input is priced as uncached, too high if it "
                       "includes cached input (OpenAI models)")
    details.append("Each total = max(model estimates, invoice); weeks start Monday; days are UTC")
    r = result("MICROSOFT FOUNDRY", rows=rows, details=details)
    r["unpriced"] = len(unpriced)  # the section's "N models have no price" line: only those that change a row
    return r
