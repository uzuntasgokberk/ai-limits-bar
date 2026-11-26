"""Turns provider results into SwiftBar menu text. No network, files or processes: pure functions (tested).

SwiftBar syntax: one menu item per line; everything after ` | ` is parameters (href=, bash=, color= ...);
`---` is a separator and a line starting with `--` is a submenu item. Therefore every external string goes
through `clean()`, parameter values with spaces through `quoted()`, and file paths through `param_path()`.
Click targets are `LINKS`, `open` on the settings file, and this plugin's own launcher with `--choose` (the
"Providers…" panel), `--prices` (the "Foundry prices…" window) or `--show` (the "Menu bar shows" choice). A row
label from a provider reaches a `--show` parameter only through `choice_param()`, which accepts nothing but a strict
allow-list; no provider text reaches a `--choose` or `--prices` click (the windows get their data from files).
"""
import math
import re
import time
import unicodedata

UNSAFE_CATEGORIES = {"Cc", "Cf", "Zl", "Zp", "Cs"}
# Characters outside category M* that still join the character before them into one grapheme: emoji skin-tone
# modifiers, halfwidth katakana voiced marks, Thai and Lao AM.
JOINERS = {chr(c) for c in range(0x1F3FB, 0x1F400)} | {"\uFF9E", "\uFF9F", "\u0E33", "\u0EB3"}
STATUSES = {"ok", "unknown", "expired"}
GREY = "#8E8E93"
ORANGE = "#E8A33D"
RED = "#E5484D"
MONO = "font=Menlo size=12"
DEFAULT_WIDTH = 12
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

LINKS = {
    "claude": "https://claude.ai/settings/usage",
    "codex": "https://chatgpt.com/codex/settings/usage",
    "gemini": "https://antigravity.google/",
    "copilot": "https://github.com/settings/billing/usage",
    "foundry": "https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/costanalysis",
}
NAMES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "copilot": "Copilot", "foundry": "Foundry"}
# Section headers: fixed text per provider (never taken from provider data).
HEADERS = {"claude": "Claude", "codex": "Codex (GPT)", "gemini": "Gemini", "copilot": "GitHub Copilot",
           "foundry": "Microsoft Foundry"}
HEADER_STYLE = "font=HelveticaNeue-Bold size=14"
NOT_SET_UP = " · not set up on this Mac"
CHOOSER_ITEM = "Menu bar shows"
HIGHEST_ITEM = "Highest of all"
# Row labels that may go into a click parameter: ASCII letters and digits first, then also space . _ - (no quote,
# backslash, colon, pipe, `$`, parentheses, control or format characters), at most 30 characters (normalize's limit).
CHOICE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,29}")
# A row's `window` / `group` (see common.row): never shown, only compared.
KIND = re.compile(r"[a-z0-9][a-z0-9_-]{0,15}")
LABEL_MAX = 18  # characters of a row label shown in the menu (e.g. "Claude GPT Weekly"); the column adds 2 spaces


def header(key, href=None, suffix=""):
    """Bold section title in the normal text colour (follows light/dark mode)."""
    return f"{clean(HEADERS.get(key, NAMES.get(key, key)) + suffix, 60)}{_params(HEADER_STYLE, href)}"


def _joins_previous(c):
    """Combining marks (M*) and `JOINERS`; also characters unassigned in this Python's Unicode table (Cn), which
    may be marks in the newer table SwiftBar uses."""
    return unicodedata.category(c)[0] == "M" or unicodedata.category(c) == "Cn" or c in JOINERS


def clean(text, limit=80):
    """For every external string: `|` -> `¦`; control, format, line/paragraph separator and lone surrogate
    characters (Cc, Cf, Zl, Zp, Cs, including U+2028, U+0085, VT, FF) become spaces; runs of dashes collapse
    to one (no `--` submenu or `---` separator); leading dashes, spaces and combining marks are removed.
    A detail line is `--` + text, and SwiftBar tests that prefix per grapheme: a leading mark would merge with
    the second dash, and the line would leave the submenu."""
    s = "".join(" " if unicodedata.category(c) in UNSAFE_CATEGORIES else c for c in str(text).replace("|", "¦"))
    s = re.sub(r"-{2,}", "-", s)
    start = 0
    while start < len(s) and (s[start] in "- " or _joins_previous(s[start])):
        start += 1
    return s[start:start + limit]


def quoted(text, limit=200):
    """Parameter value containing spaces (tooltip): SwiftBar splits on spaces, so the value is wrapped in
    double quotes, and quotes, backslashes, equals signs and backticks are removed so it cannot break out
    and start a new parameter such as bash=."""
    s = clean(text, limit)
    for c in "\"'\\=¦`":
        s = s.replace(c, " ")
    return '"' + " ".join(s.split()) + '"'


def _number(x, low, high):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) and low <= x <= high else None


def _epoch(x):
    """Epoch seconds or milliseconds between 2000 and 2100; text, NaN or out of range -> None."""
    x = _number(x, 0, 4.2e12)
    if x is None:
        return None
    if x > 1e11:
        x /= 1000
    return x if 946684800 <= x <= 4102444800 else None


def _text(x, limit):
    return None if x is None else str(x).encode("utf-8", "replace").decode("utf-8")[:limit]


def _count(x, high=999):
    """A whole number from 0 to `high` (not a bool, not a float); anything else -> None."""
    return x if isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= high else None


def _kind(x):
    """A row's machine-readable `window` or `group`: a short lowercase id such as "5h", "weekly" or "3p"; else None."""
    return x if isinstance(x, str) and KIND.fullmatch(x) else None


def normalize(r):
    """Brings a provider result or a cached result into a safe shape; None if it is not a result at all.
    One broken value (NaN, a time given as text, a missing field) can therefore not break the whole menu.
    `unpriced` (Foundry only): how many deployments with tokens have no usable price; a count or None."""
    if not isinstance(r, dict):
        return None
    rows = []
    for x in r.get("rows") if isinstance(r.get("rows"), list) else []:
        if isinstance(x, dict):
            rows.append({"label": _text(x.get("label", ""), 30) or "", "percent": _number(x.get("percent"), 0, 1000),
                         "value": _text(x.get("value"), 200), "resets": _epoch(x.get("resets")),
                         "window": _kind(x.get("window")), "group": _kind(x.get("group"))})
    details = r.get("details") if isinstance(r.get("details"), list) else []
    debug = r.get("debug") if isinstance(r.get("debug"), list) else []
    return {"title": _text(r.get("title"), 30) or "?",
            "status": r.get("status") if r.get("status") in STATUSES else "unknown",
            "rows": rows[:12], "details": [_text(d, 200) for d in details[:30]],
            "note": _text(r.get("note"), 120), "time": _epoch(r.get("time")),
            "debug": [_text(d, 900) for d in debug[:60]], "unpriced": _count(r.get("unpriced"))}


def bar(percent, width=10):
    filled = max(0, min(width, round((percent or 0) / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def color(percent):
    if percent is None:
        return None
    if percent >= 90:
        return RED
    if percent >= 75:
        return ORANGE
    return None


def clock(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def day_month(epoch):
    t = time.localtime(epoch)
    return f"{MONTHS[t.tm_mon - 1]} {t.tm_mday}"


def remaining(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes // 1440}d {minutes % 1440 // 60}h"


def reset_text(epoch, now):
    """'↻ Fri Mar 14 08:00 · in 3d 19h'"""
    if not epoch:
        return ""
    left = epoch - now
    if left <= 0:
        return "↻ reset time passed (stale data)"
    return f"↻ {DAYS[time.localtime(epoch).tm_wday]} {day_month(epoch)} {clock(epoch)} · in {remaining(left)}"


def current_percent(r, now):
    """A percentage whose reset time has passed is no longer true: None."""
    if r.get("percent") is None:
        return None
    if r.get("resets") and r["resets"] <= now:
        return None
    return float(r["percent"])


def _params(*parts):
    p = " ".join(x for x in parts if x)
    return f" | {p}" if p else ""


def label_width(results):
    """Label column sized to the longest label in the menu (8..LABEL_MAX characters + 2 spaces), so long labels
    such as 'Claude GPT Weekly' never touch the bar and every row lines up in Menlo on any screen."""
    lengths = [len(clean(x.get("label", ""), LABEL_MAX)) for _, r in results if r for x in r.get("rows", [])]
    return max(10, min(LABEL_MAX + 2, max(lengths, default=8) + 2))


def unpriced_text(count, clickable=False):
    """Fixed text with a number: why the Foundry totals are marked `≥`, and what a click on it does."""
    text = f"{count} model has no price" if count == 1 else f"{count} models have no price"
    return f"{text}: set prices…" if clickable else f"{text} (see Details)"


def section(key, r, now, width=DEFAULT_WIDTH, prices_action=None):
    """Menu lines of one provider. Foundry also gets a line saying how many models have no price, if any; with
    `prices_action` (from `window_action`) a click on it opens the prices window."""
    href = f"href={LINKS[key]}" if key in LINKS else None
    out = [header(key, href)]
    if r.get("note"):
        note_color = ORANGE if r["status"] == "expired" else GREY
        out.append(f"{clean(r['note'])}{_params('size=12', f'color={note_color}', href)}")
    pad = " " * width
    for x in r.get("rows", []):
        label = clean(x["label"], LABEL_MAX).ljust(width)
        p = current_percent(x, now)
        if x.get("percent") is not None:
            body = f"{label}unknown" if p is None else f"{label}{bar(p)} {p:>3.0f}%"
            c = color(p)
            out.append(f"{clean(body)}{_params(MONO, f'color={c}' if c else None, href)}")
            if x.get("value"):
                out.append(f"{pad}{clean(x['value'])}{_params(MONO, f'color={GREY}', href)}")
        else:
            out.append(f"{clean(label + (x.get('value') or 'unknown'))}{_params(MONO, href)}")
        reset = reset_text(x.get("resets"), now)
        if reset:
            out.append(f"{pad}{clean(reset)}{_params(MONO, f'color={GREY}', href)}")
    if not r.get("rows") and not r.get("note"):
        out.append(f"unknown{_params('size=12', f'color={GREY}', href)}")
    if key == "foundry" and r.get("unpriced"):
        text = unpriced_text(r["unpriced"], clickable=bool(prices_action))
        out.append(f"{text}{_params('size=12', f'color={ORANGE}', *(prices_action or [href]))}")
    details = [clean(d, 140) for d in r.get("details", [])]
    if r.get("time"):
        details.append(f"Last read {day_month(r['time'])} {clock(r['time'])}")
    out.append(f"Details{_params('size=11', f'color={GREY}')}")
    out.extend(f"--{d}{_params('size=12')}" for d in details)
    return out


def menu_bar_choice(value):
    """The `menu_bar` setting as (provider, limit) for one pinned row, or None for the highest of all. Anything else
    (wrong type, unknown provider, an empty or over-long limit, a control or format character) means highest. The
    limit only has to be safe to display and to compare: it never goes into a click parameter."""
    if not isinstance(value, dict):
        return None
    provider, limit = value.get("provider"), value.get("limit")
    if not isinstance(provider, str) or provider not in NAMES or not isinstance(limit, str):
        return None
    if not 1 <= len(limit) <= 30 or any(unicodedata.category(c) in UNSAFE_CATEGORIES for c in limit):
        return None
    return provider, limit


def headline(results, now, choice):
    """(percent or None, tooltip prefix or None): the number in the menu bar and what it is. Pinned: the first row of
    that provider with this label and a percentage; if it is missing or its percentage is not current, None, so the
    title shows a dash instead of another limit the user would mistake for it. Highest: the largest current
    percentage (the first one on a tie)."""
    if choice:
        key, limit = choice
        for k, r in results:
            if k != key:
                continue
            for x in r.get("rows", []):
                if x.get("percent") is not None and x.get("label") == limit:
                    p = current_percent(x, now)
                    if p is not None:
                        return p, f"Showing {NAMES[key]} {limit} {p:.0f}%"
                    break
            break
        return None, f"{NAMES[key]} {limit} not available"
    best = None
    for k, r in results:
        for x in r.get("rows", []):
            p = current_percent(x, now)
            if p is not None and (best is None or p > best[0]):
                best = (p, k, x.get("label"))
    if best is None:
        return None, None
    return best[0], f"Highest: {NAMES.get(best[1], best[1])} {best[2]} {best[0]:.0f}%"


def _percent_text(p):
    return "–" if p is None else f"{p:.0f}%"


def ring_tooltip(results, rings, now):
    """Tooltip parts with the ring strip, in the menu's order: a provider with a ring gives its 5-hour and weekly
    percentage (one value for a single-limit provider, "–" where there is none), every other provider its summary.
    rings: ring.specs() dicts."""
    by_key = {x["key"]: x for x in rings}
    parts = []
    for k, r in results:
        x = by_key.get(k)
        if x is None:
            parts.append(f"{NAMES.get(k, k)} {summary(r, now)}")
        elif x.get("single"):
            parts.append(f"{x['name']} {_percent_text(x['right'])}")
        else:
            parts.append(f"{x['name']} 5-hour {_percent_text(x['right'])}, weekly {_percent_text(x['left'])}")
    return parts


_BASE64 = re.compile(r"[A-Za-z0-9+/]{4,200000}={0,2}")


def strip_param(strip):
    """`image=<light>,<dark>` for SwiftBar (it picks the first on a light menu bar, the second on a dark one), or
    None unless both are plain base64 text: nothing else may reach the menu bar line."""
    if not isinstance(strip, (tuple, list)) or len(strip) != 2:
        return None
    if not all(isinstance(x, str) and _BASE64.fullmatch(x) for x in strip):
        return None
    return f"image={strip[0]},{strip[1]}"


def summary(r, now):
    """Short value for the hover tooltip: highest current percentage, else first value, else '?'."""
    percents = [p for p in (current_percent(x, now) for x in r.get("rows", [])) if p is not None]
    if percents:
        return f"{max(percents):.0f}%"
    for x in r.get("rows", []):
        if x.get("value") and x["value"] != "unknown":
            return clean(x["value"].split(" (")[0], 24)
    return "?"


def icon(percent):
    if percent is None:
        return "gauge.with.dots.needle.bottom.0percent"
    if percent < 40:
        return "gauge.with.dots.needle.33percent"
    if percent < 60:
        return "gauge.with.dots.needle.50percent"
    if percent < 85:
        return "gauge.with.dots.needle.67percent"
    return "gauge.with.dots.needle.100percent"


def param_path(path):
    """`param1=` value of "Open settings": the path in double quotes, or None (the item is then not shown).
    SwiftBar ends a quoted value at the matching quote (a backslash escapes the next character) and splits lines
    on control and line-separator characters, so a path containing `"`, a backslash or an unsafe character could
    end the value early and add parameters (bash=) or whole menu lines. Such paths are refused, not escaped. A
    relative path is refused too: `open` could read it as an option or resolve it against another folder."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    if '"' in path or "\\" in path or any(unicodedata.category(c) in UNSAFE_CATEGORIES for c in path):
        return None
    return f'"{path}"'


SHELL_SPECIAL = set("'$`;&|<>")


def command_path(path):
    """`bash=` value for this plugin's launcher: `param_path`, and also None if the path contains a character in
    `SHELL_SPECIAL`. When SwiftBar runs a plugin through its shell wrapper (runInBash), it quotes the arguments
    safely but only wraps the script path in single quotes when it has a space, so such characters in the
    user's folder name would be read by the shell. Spaces, parentheses and non-ASCII letters are fine."""
    if isinstance(path, str) and SHELL_SPECIAL & set(path):
        return None
    return param_path(path)


PROVIDERS_ITEM = "Providers…"
PRICES_ITEM = "Foundry prices…"


def window_action(switch, launcher_path, toggle_config=None):
    """The parameters of a click that opens one of this plugin's windows: this plugin's launcher with `switch`
    (`--choose` or `--prices`, fixed here), the --config file this run uses if any (so the window edits the same
    file), and `refresh=true`, so SwiftBar re-runs the plugin once the window is closed. None if the launcher is
    refused by `command_path` or the config path by `param_path`: the line then has no action."""
    launcher = command_path(launcher_path)
    config = []
    if toggle_config is not None:
        path = param_path(toggle_config)
        config = ["param2=--config", f"param3={path}"] if path else None
    if launcher is None or config is None or switch not in ("--choose", "--prices"):
        return None
    return [f"bash={launcher}", f"param1={switch}", *config, "terminal=false", "refresh=true"]


def providers_item(launcher_path, toggle_config=None):
    """The "Providers…" line: a click runs this plugin's launcher with `--choose`, which opens a panel of checkboxes
    (a menu closes on every click, so several providers can't be ticked in the menu itself); see `window_action`."""
    action = window_action("--choose", launcher_path, toggle_config)
    return f"{PROVIDERS_ITEM}{_params(*action)}" if action else PROVIDERS_ITEM


def prices_item(action):
    """The "Foundry prices…" line: a click opens the prices window (`--prices`); `action` from `window_action`, or
    None for a line without action."""
    return f"{PRICES_ITEM}{_params(*action)}" if action else PRICES_ITEM


def choice_param(provider, label):
    """`param2=` value that pins one row, or None (the entry then has no action). The label comes from a provider's
    answer, so it must pass `CHOICE_LABEL`; then the value is written in double quotes and SwiftBar's parser returns
    exactly `provider:label` (a quoted value runs to the matching quote; this text has neither quote nor backslash).
    SwiftBar passes it to the launcher as one argument; its shell wrapper, if ever used, quotes it as one word."""
    if provider not in NAMES or not isinstance(label, str) or not CHOICE_LABEL.fullmatch(label):
        return None
    return f'"{provider}:{label}"'


def choice_rows(results):
    """[(key, label)] for every row that has a percentage, in display order, each pair once (the first wins)."""
    seen = []
    for key, r in results:
        for x in r.get("rows", []):
            pair = (key, x.get("label"))
            if x.get("percent") is not None and isinstance(pair[1], str) and pair not in seen:
                seen.append(pair)
    return seen


def chooser_lines(results, choice, launcher_path, toggle_config=None, config_error=False):
    """The "Menu bar shows" submenu: "Highest of all", then one entry per row with a percentage, the current choice
    checked. A pinned choice that matches no row is listed too, checked and marked "(not available)", so it can be
    changed. A click runs the launcher with `--show highest|provider:label` and re-runs the plugin, like
    `providers_item`; while config.json is unreadable, or a path is refused, the entries have no action."""
    launcher = command_path(launcher_path)
    config = []
    if toggle_config is not None:
        path = param_path(toggle_config)
        config = ["param3=--config", f"param4={path}"] if path else None
    clickable = launcher is not None and config is not None and not config_error

    def entry(title, value, checked):
        action = [f"bash={launcher}", "param1=--show", f"param2={value}", *config, "terminal=false",
                  "refresh=true"] if clickable and value else []
        return f"--{clean(title, 70)}{_params('checked=true' if checked else None, *action)}"

    lines = [CHOOSER_ITEM, entry(HIGHEST_ITEM, "highest", choice is None)]
    listed = False
    for key, label in choice_rows(results):
        checked = choice == (key, label)
        listed = listed or checked
        lines.append(entry(f"{NAMES.get(key, key)} · {label}", choice_param(key, label), checked))
    if choice and not listed:
        lines.append(entry(f"{NAMES[choice[0]]} · {choice[1]} (not available)", None, True))
    return lines


def menu(results, now=None, refresh=False, settings_path=None, config_error=False, providers=None,
         launcher_path=None, toggle_config=None, menu_bar=None, rings=None, strip=None, gauge=True):
    """results: [(key, result)] in display order (hidden providers are simply not in the list).
    settings_path: the config file (absolute); config_error: it exists but could not be read.
    providers: [(key, enabled)] -> the "Providers…" item (see `providers_item`), the "Menu bar shows" submenu and
    the all-off text; None -> none of them. menu_bar: the `menu_bar` setting (see `menu_bar_choice`).
    rings: ring.specs() (the tooltip then lists each ring's values); strip: ring.strip()'s (light, dark) images, the
    menu bar item is then that image alone; without it (or if it is not plain base64) the gauge and percentage are
    text, as before. gauge: the "Show gauge and percentage" setting; with it off and no ring, the text says "–"."""
    now = now or time.time()
    choice = menu_bar_choice(menu_bar)
    try:
        top, prefix = headline(results, now, choice)
        if rings:
            parts = ([prefix] if prefix else []) + ring_tooltip(results, rings, now)
        else:
            parts = ([prefix] if prefix else []) + [f"{NAMES.get(k, k)} {summary(r, now)}" for k, r in results]
        tooltip = " · ".join(parts) or "No providers found"
    except Exception:
        top, tooltip = None, "unreadable"
    try:
        width = label_width(results)
    except Exception:
        width = DEFAULT_WIDTH
    image = strip_param(strip)
    if image:
        lines = [f"{_params(image, f'tooltip={quoted(tooltip, 400)}').lstrip()}", "---"]
    else:
        if not gauge and not rings:
            top = None  # nothing else to show: the gauge with a dash, so the item never disappears
        c = color(top)
        lines = [f"{f'{top:.0f}%' if top is not None else '–'}"
                 f"{_params(f'sfimage={icon(top)}', f'color={c}' if c else None, f'tooltip={quoted(tooltip, 400)}')}",
                 "---"]
    # "Foundry prices…" (and the click on the section's "no price" line): only with a Foundry section, the
    # Providers… item's data, and a readable config.json.
    has_foundry = any(k == "foundry" for k, _ in results)
    prices_action = None
    if has_foundry and providers is not None and not config_error:
        try:
            prices_action = window_action("--prices", launcher_path, toggle_config)
        except Exception:
            prices_action = None
    if not results:
        all_off = providers is not None and not any(p[1] for p in providers)
        text = ("All providers are off: turn one on under Providers…" if all_off
                else "No supported AI tool found on this Mac")
        lines.append(f"{text}{_params('size=12', f'color={GREY}')}")
    for i, (key, r) in enumerate(results):
        if i:
            lines.append("---")
        try:
            lines.extend(section(key, r, now, width, prices_action if key == "foundry" else None))
        except Exception:
            lines.append(header(key, suffix=" · could not be displayed"))
    lines.append("---")
    if refresh:
        lines.append(f"Refresh now{_params('refresh=true')}")
    if providers is not None:
        lines.append(providers_item(launcher_path, toggle_config))
        try:
            lines.extend(chooser_lines(results, choice, launcher_path, toggle_config, config_error))
        except Exception:
            pass  # the menu stays usable without the chooser
        if has_foundry:
            lines.append(prices_item(prices_action))
    if config_error:
        lines.append(f"config.json could not be read: using defaults{_params('size=12', f'color={ORANGE}')}")
    path = param_path(settings_path)
    if path:
        lines.append(f"Open settings{_params('bash=/usr/bin/open', f'param1={path}', 'terminal=false')}")
    lines.append(f"Updated {clock(now)}{_params('size=11', f'color={GREY}')}")
    return "\n".join(lines)
