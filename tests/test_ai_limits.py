"""Offline tests: rendering rules, safety of menu output, provider parsing and the plugin's failure paths.

Run: python3 -I tests/test_ai_limits.py   (add --gui for the two tests that open the Providers… window)
No test may reach a real provider. Two structural guards enforce it: setUpModule replaces every way a provider
module reaches the outside (its subprocess, HTTP and tool-lookup functions, and the home-folder paths it reads) with
a guard, and load_main() replaces main.call with one, so a test that wants provider data installs its own fake; a
guard raises RealProviderRead, which no `except Exception` in the plugin can swallow. Every run of the plugin as a
process goes through run_launcher(), which skips all providers except those forced to fail.
The default run opens no window and needs no Keychain, network or ~/Library/Caches; it runs on any macOS with
Python 3.9 or later (CI: GitHub's macOS runner, with the system Python and the latest Python). Time limits are
generous for slow machines but always shorter than the wrong behaviour they rule out.

Every run of the plugin or of main.main() here passes a temporary --config and --cache, and every enabled
provider is skipped, forced to fail or replaced: no test reads a real account. Network use is loopback only.
All sample values are made up.
"""
import contextlib
import fcntl
import http.server
import importlib.util
import io
import json
import os
import pwd
import re
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
import urllib.request
from datetime import datetime, timezone
from unittest import mock

sys.dont_write_bytecode = True  # keep __pycache__ out of the plugin folder
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_limits import common, panel, render  # noqa: E402
from ai_limits.providers import NAMES, claude, codex, copilot, foundry, gemini  # noqa: E402

PLUGIN = os.path.join(ROOT, "plugin", "ai-limits.5m.sh")
# Folder permissions don't stop root, so the "read-only folder" checks only make sense for a normal user.
IS_ROOT = os.geteuid() == 0
NOW = 1_800_000_000.0
ALL = ",".join(NAMES)


class RealProviderRead(BaseException):
    """A test reached a real provider read. A BaseException, so the plugin's own `except Exception` (per provider in
    main, around the Antigravity call, in the Foundry history) cannot turn it into a quiet "Couldn't read"."""


def provider_guard(where):
    def guard(*args, **kwargs):
        raise RealProviderRead(f"{where} was called: a test reached a real provider read; install a fake first")
    return guard


# Every way a provider module reaches the outside, as bound in that module (tests that need one patch it themselves,
# and the guard is back when their patch ends). common's own functions stay real: the loopback tests use them.
GUARDED_CALLS = [(claude, "run"), (claude, "https_json"), (codex, "find_tool"), (copilot, "run"),
                 (copilot, "find_tool"), (foundry, "run"), (foundry, "find_tool"), (gemini, "https_json"),
                 (gemini, "localhost_json")]
_GUARD_HOME = tempfile.TemporaryDirectory()


def setUpModule():
    """The suite-wide guard against real provider reads (see the module docstring). The home-folder paths the
    providers read without a tool (Antigravity's hub files, the Gemini CLI and Codex session files) point into an
    empty temporary folder."""
    for module, name in GUARDED_CALLS:
        mock.patch.object(module, name, provider_guard(f"{module.__name__}.{name}")).start()
    empty = _GUARD_HOME.name
    for module, name, path in [(gemini, "ANTIGRAVITY_DIR", os.path.join(empty, "antigravity")),
                               (gemini, "HUB", os.path.join(empty, "antigravity", "daemon", "hub_*.json")),
                               (gemini, "CLI_CREDENTIALS", os.path.join(empty, "oauth_creds.json")),
                               (codex, "SESSIONS", os.path.join(empty, "sessions"))]:
        mock.patch.object(module, name, path).start()


def tearDownModule():
    mock.patch.stopall()
    _GUARD_HOME.cleanup()


def run_launcher(args, *, config, cache, env=None, command=(PLUGIN,), allow_real_providers=False):
    """The only way tests start the plugin as a process. --skip is added LAST, so it wins over a --skip the test
    passed itself, and skips every provider except those the test forces to fail with --fail (main.call raises for
    them before any provider code runs). allow_real_providers=True is the explicit opt-out; no test uses it.
    --config and --cache must be inside the temporary folder."""
    temp = os.path.realpath(tempfile.gettempdir())
    for path in (config, cache):
        assert os.path.realpath(path).startswith(temp + os.sep), f"not a temporary path: {path}"
    argv = [*command, "--config", config, "--cache", cache, *args]
    if not allow_real_providers:
        failing = set()
        for i, arg in enumerate(args):
            if arg == "--fail" and i + 1 < len(args):
                failing |= set(args[i + 1].split(","))
            elif arg.startswith("--fail="):
                failing |= set(arg.split("=", 1)[1].split(","))
        argv += ["--skip", ",".join(n for n in NAMES if n not in failing)]
    return subprocess.run(argv, capture_output=True, encoding="utf-8",
                          env=env if env is not None else dict(os.environ), timeout=30)


def res(title="X", rows=None, status="ok", **k):
    return {"title": title, "status": status, "rows": rows or [], "details": k.get("details", []),
            "note": k.get("note"), "time": NOW - 60}


def row(label, percent=None, value=None, resets=None):
    return {"label": label, "percent": percent, "value": value, "resets": resets}


# ---- SwiftBar's menu parser, ported ------------------------------------------------------------------------

LINE_BREAKS = re.compile("[\n\x0b\x0c\r\x85\u2028\u2029]")


def menu_lines(text):
    """Lines as SwiftBar splits the output: U+000A-U+000D, U+0085, U+2028, U+2029."""
    return LINE_BREAKS.split(text)


def swiftbar_params(line):
    """Port of SwiftBar's parameter parser. The first `|` ends the title. Key: everything up to the next `=`,
    trimmed and lower-cased; spaces after `=` are skipped. Value: if it starts with `"` or `'`, everything up to
    the matching quote (a backslash escapes the next character, both are kept), otherwise everything up to the
    next space or tab. A later duplicate key overwrites an earlier one."""
    if "|" not in line:
        return {}
    text, params, i = line.split("|", 1)[1], {}, 0
    while True:
        while i < len(text) and text[i] in " \t":
            i += 1
        eq = text.find("=", i)
        if i >= len(text) or eq < 0:
            return params
        key, i = text[i:eq].strip().lower(), eq + 1
        while i < len(text) and text[i] in " \t":
            i += 1
        if i < len(text) and text[i] in "\"'":
            quote, start = text[i], i + 1
            i = start
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
            value, i = text[start:i], i + 1
        else:
            start = i
            while i < len(text) and text[i] not in " \t":
                i += 1
            value = text[start:i]
        params[key] = value


def params_of(line):
    return list(swiftbar_params(line))


def top_level(text):
    """Main-menu lines (no submenu items, no separators): where a provider's section would be."""
    return [x for x in menu_lines(text) if not x.startswith("--")]


def providers_line(text):
    """The "Providers…" line of a menu (exactly one)."""
    found = [x for x in menu_lines(text) if x.split(" | ")[0] == render.PROVIDERS_ITEM]
    assert len(found) == 1, found
    return found[0]


def is_submenu(line):
    """SwiftBar tests the `--` prefix per grapheme: a combining mark after the second dash merges with it."""
    return line.startswith("--") and not (len(line) > 2 and (unicodedata.category(line[2])[0] == "M"
                                                             or line[2] in render.JOINERS))


class SwiftBarParser(unittest.TestCase):
    def test_port_follows_the_rules(self):
        self.assertEqual(swiftbar_params('T | a=1 B="x y" c=\'p "q\' a=2'), {"a": "2", "b": "x y", "c": 'p "q'})
        self.assertEqual(swiftbar_params('T | k="a\\"b" z=1'), {"k": 'a\\"b', "z": "1"})
        self.assertEqual(swiftbar_params("no parameters"), {})
        self.assertEqual(menu_lines("a\u2028b\x85c\rd"), ["a", "b", "c", "d"])

    def test_port_skips_spaces_after_the_equals_sign(self):
        self.assertEqual(swiftbar_params('T | a= "x y" b=  z'), {"a": "x y", "b": "z"})

    def test_port_sees_the_old_settings_injection(self):
        # What the old code printed for a crafted path: the command is replaced. The tests below rely on this.
        old = 'Open settings | bash=/usr/bin/open param1="x" bash=/bin/sh param1=-c x="" terminal=false'
        self.assertEqual(swiftbar_params(old)["bash"], "/bin/sh")


# ---- render -----------------------------------------------------------------------------------------------

class Render(unittest.TestCase):
    def test_bar(self):
        self.assertEqual(render.bar(37), "████░░░░░░")
        self.assertEqual(render.bar(0), "░" * 10)
        self.assertEqual(render.bar(140), "█" * 10)

    def test_colour_thresholds(self):
        self.assertIsNone(render.color(74.9))
        self.assertEqual(render.color(75), render.ORANGE)
        self.assertEqual(render.color(90), render.RED)

    def test_reset_text_has_date_and_countdown(self):
        self.assertEqual(render.reset_text(NOW - 1, NOW), "↻ reset time passed (stale data)")
        day = r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \w{3} \d{1,2} \d\d:\d\d"
        self.assertRegex(render.reset_text(NOW + 600, NOW), rf"^↻ {day} · in 10m$")
        self.assertRegex(render.reset_text(NOW + 3630, NOW), rf"^↻ {day} · in 1h 0m$")
        self.assertRegex(render.reset_text(NOW + 3 * 86400 + 120, NOW), rf"^↻ {day} · in 3d 0h$")

    def test_stale_percent_is_not_shown(self):
        text = render.menu([("codex", res("CODEX", [row("5-hour", 44.0, resets=NOW - 86400)]))], NOW)
        self.assertNotIn("44%", text)
        self.assertIn("reset time passed", text)
        self.assertTrue(text.startswith("–"))

    def test_title_shows_highest_percent(self):
        text = render.menu([("claude", res("CLAUDE", [row("5-hour", 33.0), row("Weekly", 94.0)])),
                            ("copilot", res("GITHUB COPILOT", [row("AI credits", 57.0)]))], NOW)
        first = text.splitlines()[0]
        self.assertTrue(first.startswith("94%"))
        self.assertIn(f"color={render.RED}", first)
        self.assertIn('tooltip="Highest: Claude Weekly 94% · Claude 94% · Copilot 57%"', first)

    def test_long_label_does_not_touch_bar(self):
        text = render.menu([("claude", res("CLAUDE", [row("5-hour", 81.0), row("Weekly Opus", 83.0)]))], NOW)
        long_line = next(x for x in text.splitlines() if x.startswith("Weekly Opus"))
        short_line = next(x for x in text.splitlines() if x.startswith("5-hour"))
        self.assertRegex(long_line, r"^Weekly Opus {2,}█")
        self.assertEqual(long_line.index("█"), short_line.index("█"))

    def test_value_without_budget_has_no_percent(self):
        text = render.menu([("foundry", res("FOUNDRY", [row("This month", value="$3.40")]))], NOW)
        self.assertIn("This month  $3.40", text)
        self.assertNotIn("%", text.split("\n", 1)[1])

    def test_refresh_item_runs_no_command(self):
        lines = render.menu([], NOW, True, "/tmp/config.json").splitlines()
        line = next(x for x in lines if x.startswith("Refresh now"))
        self.assertEqual(params_of(line), ["refresh"])

    def test_headers_are_bold_fixed_names(self):
        text = render.menu([("copilot", res("ANYTHING FROM DATA", [row("AI credits", 26.0)])),
                            ("foundry", res("x | bash=/bin/sh", [row("Today", value="$7")]))], NOW)
        lines = text.splitlines()
        copilot_line = next(x for x in lines if x.startswith("GitHub Copilot"))
        self.assertIn("font=HelveticaNeue-Bold", copilot_line)
        self.assertNotIn("sfimage=", copilot_line)
        self.assertNotIn("color=", copilot_line)  # normal text colour, readable in light and dark mode
        self.assertTrue(any(x.startswith("Microsoft Foundry | ") for x in lines))
        self.assertNotIn("ANYTHING FROM DATA", text)  # the header never comes from provider data

    def test_no_providers(self):
        self.assertIn("No supported AI tool found", render.menu([], NOW))

    def test_injection_is_neutralised(self):
        bad = res("FOUNDRY | bash=/bin/sh", [row("a\n--INJ | bash=/bin/sh", value="1 | bash=/bin/sh param1=-c")],
                  details=["--hidden | href=file:///etc", "x\u2028--INJ | bash=/bin/sh"])
        lines = menu_lines(render.menu([("foundry", bad)], NOW, True, "/Users/demo/config.json"))
        for line in lines:
            params = swiftbar_params(line)
            if line.startswith("Open settings"):
                self.assertEqual(params["bash"], "/usr/bin/open")
                continue
            self.assertFalse({"bash", "shell", "param1", "terminal"} & set(params), line)
            self.assertIn(params.get("href"), (None, *render.LINKS.values()), line)
        self.assertFalse(any(x.startswith(("INJ", "-INJ", "--INJ")) for x in lines))  # no injected line
        self.assertEqual(sum(1 for x in lines if x.startswith("Open settings")), 1)

    def test_settings_path_cannot_inject(self):
        for bad in ('/tmp/x" bash=/bin/sh param1=-c param2="id" x="', "/tmp/a\nInjected | bash=/bin/sh",
                    '/tmp/a\\" bash=/bin/sh x="', "/tmp/a\\b", "/tmp/a\u2028b", "/tmp/a\rb", "/tmp/a\u200eb",
                    "config.json", "-n/x", "", None, 5):
            self.assertIsNone(render.param_path(bad), repr(bad))
            text = render.menu([], NOW, True, bad)
            self.assertNotIn("Open settings", text)
            self.assertFalse(any("bash" in swiftbar_params(x) for x in menu_lines(text)), repr(bad))

    def test_settings_path_with_space_and_apostrophe(self):
        path = "/Users/demo/My Folder/it's config.json"
        self.assertEqual(render.param_path(path), f'"{path}"')
        line = next(x for x in menu_lines(render.menu([], NOW, True, path)) if x.startswith("Open settings"))
        self.assertEqual(swiftbar_params(line), {"bash": "/usr/bin/open", "param1": path, "terminal": "false"})

    def test_config_error_line(self):
        lines = render.menu([], NOW, True, "/Users/demo/config.json", True).splitlines()
        warning = lines.index("config.json could not be read: using defaults"
                              f" | size=12 color={render.ORANGE}")
        self.assertTrue(lines[warning + 1].startswith("Open settings"))
        self.assertNotIn("could not be read", render.menu([], NOW, True, "/Users/demo/config.json"))

    def test_unicode_separators_removed(self):
        for c in ("\u2028", "\u2029", "\u0085", "\x0b", "\x0c", "\u200e", "\ud800"):
            self.assertNotIn(c, render.clean(f"a{c}---b{c}--c"))
        self.assertEqual(render.clean("a---b--c-d"), "a-b-c-d")
        self.assertNotIn("--", render.quoted("x --- y"))

    def test_leading_combining_marks_cannot_leave_the_submenu(self):
        self.assertEqual(render.clean("\u0301\u0301- -\u20ddx\u0301"), "x\u0301")
        for mark in ("\u0301", "\u20dd", "\u0903", "\U0001F3FB", "\uff9e"):
            details = [f"{mark}x", f"-{mark}y", f" {mark}{mark}-z", f"{mark}--|w"]
            text = render.menu([("claude", res("CLAUDE", [row("5-hour", 12.0)], details=details))], NOW)
            subs = [x for x in menu_lines(text) if x.startswith("--") and x != "---"]
            self.assertEqual(len(subs), len(details) + 1)  # + "Last read"
            self.assertTrue(all(is_submenu(x) for x in subs), subs)

    def test_normalize_repairs_broken_data(self):
        broken = {"title": 5, "status": "bad", "rows": [
            {"label": "a", "percent": float("nan"), "resets": "2031-02-11T00:00:00Z"},
            {"label": "b", "percent": 1e309, "resets": 1e20}, 7, "x"], "details": "text", "time": -5}
        n = render.normalize(broken)
        self.assertEqual(n["status"], "unknown")
        self.assertEqual(len(n["rows"]), 2)
        self.assertTrue(all(x["percent"] is None and x["resets"] is None for x in n["rows"]))
        self.assertEqual(n["details"], [])
        self.assertIsNone(n["time"])
        self.assertIsNone(render.normalize([1]))
        self.assertEqual(render.normalize({"rows": [{"resets": 1_900_000_000_000}]})["rows"][0]["resets"], 1.9e9)

    def test_broken_section_does_not_break_menu(self):
        bad = {"title": "X", "status": "ok", "rows": [{"label": "a", "percent": float("nan")}], "details": []}
        text = render.menu([("claude", bad), ("copilot", res("GITHUB COPILOT", [row("AI credits", 26.0)]))], NOW)
        self.assertIn("Claude · could not be displayed", text)
        self.assertIn("AI credits", text)


class ProvidersMenu(unittest.TestCase):
    LAUNCHER = "/Users/demo/ai-limits-bar/plugin/ai-limits.5m.sh"
    STATE = [("claude", True), ("codex", True), ("gemini", False), ("copilot", True), ("foundry", False)]

    def menu(self, providers=None, launcher=LAUNCHER, toggle_config=None):
        return render.menu([], NOW, True, "/Users/demo/config.json", False,
                           self.STATE if providers is None else providers, launcher, toggle_config)

    def test_item_opens_the_panel_through_the_launcher(self):
        text = self.menu()
        lines = menu_lines(text)
        line = providers_line(text)
        self.assertLess(lines.index("Refresh now | refresh=true"), lines.index(line))
        self.assertLess(lines.index(line), next(i for i, x in enumerate(lines) if x.startswith("Open settings")))
        self.assertEqual(swiftbar_params(line), {"bash": self.LAUNCHER, "param1": "--choose", "terminal": "false",
                                                 "refresh": "true"})
        submenus = [x for x in lines if x.startswith("--") and x != "---"]
        self.assertEqual([x.split(" | ")[0] for x in submenus], ["--Highest of all"])  # only the menu bar chooser
        for name in render.HEADERS.values():
            self.assertNotIn(name, text)  # the provider list lives in the panel

    def test_item_carries_a_custom_config(self):
        path = "/Users/demo/My Folder/it's (a) $test.json"  # arguments are quoted safely by SwiftBar
        params = swiftbar_params(providers_line(self.menu(toggle_config=path)))
        self.assertEqual((params["bash"], params["param1"], params["param2"], params["param3"]),
                         (self.LAUNCHER, "--choose", "--config", path))

    def test_refused_paths_leave_the_item_without_action(self):
        launchers = ['/x/a" bash=/bin/sh x="', "/x/a\nInjected | bash=/bin/sh", "/x/a\\b", "plugin/x.sh", None]
        launchers += [f"/Users/demo/ai{c}limits/plugin/ai-limits.5m.sh" for c in "'$`;&|<>"]
        launchers += [f"/Users/demo/my ai{c}x/ai-limits.5m.sh" for c in "'$`;&|<>"]
        cases = [(x, None) for x in launchers] + [(self.LAUNCHER, '/tmp/c" bash=/bin/sh x="'),
                                                 (self.LAUNCHER, "/tmp/a\nb")]
        for launcher, config in cases:
            text = self.menu(launcher=launcher, toggle_config=config)
            self.assertEqual(providers_line(text), render.PROVIDERS_ITEM, (launcher, config))
            self.assertFalse(any("bash=/bin/sh" in x for x in menu_lines(text)))
        for c in "'$`;&|<>":
            path = f"/Users/demo/ai{c}limits/plugin/ai-limits.5m.sh"
            self.assertIsNone(render.command_path(path))
            self.assertIsNotNone(render.param_path(path))  # only the command is stricter

    def test_spaces_parentheses_and_letters_are_fine(self):
        launcher = "/Users/demo/My Tools (2031)/Çalışma ai-limits/plugin/ai-limits.5m.sh"
        self.assertEqual(render.command_path(launcher), f'"{launcher}"')
        self.assertEqual(swiftbar_params(providers_line(self.menu(launcher=launcher)))["bash"], launcher)

    def test_all_off_text(self):
        off = self.menu([(k, False) for k in NAMES])
        self.assertIn(f"All providers are off: turn one on under Providers… | size=12 color={render.GREY}", off)
        self.assertNotIn("No supported AI tool found", off)
        some_on = self.menu([(k, k == "codex") for k in NAMES])
        self.assertIn("No supported AI tool found on this Mac", some_on)
        self.assertNotIn("All providers are off", some_on)
        self.assertNotIn(render.PROVIDERS_ITEM, render.menu([], NOW, True, "/Users/demo/config.json"))  # no data


class MenuBarChoice(unittest.TestCase):
    """Which limit the menu bar number shows: "highest" (default) or one pinned provider row."""

    LAUNCHER = "/Users/demo/ai-limits-bar/plugin/ai-limits.5m.sh"
    STATE = [(n, True) for n in NAMES]
    FUTURE = NOW + 3600

    def results(self, claude=None, codex=None):
        claude = [row("5-hour", 31.0, resets=self.FUTURE), row("Weekly", 57.0, resets=self.FUTURE),
                  row("Weekly Opus", 44.0, resets=self.FUTURE)] if claude is None else claude
        if codex is None:
            codex = [row("5-hour", 12.0, resets=self.FUTURE), row("Weekly", 62.0, resets=self.FUTURE)]
        return [("claude", res("CLAUDE", claude)), ("codex", res("CODEX", codex))]

    def menu(self, menu_bar=None, results=None, launcher=LAUNCHER, toggle_config=None, config_error=False):
        return render.menu(self.results() if results is None else results, NOW, True, "/Users/demo/config.json",
                           config_error, self.STATE, launcher, toggle_config, menu_bar)

    @staticmethod
    def title(text):
        first = menu_lines(text)[0]
        return first.split(" | ")[0], swiftbar_params(first)

    @staticmethod
    def chooser(text):
        lines = menu_lines(text)
        start = end = lines.index(render.CHOOSER_ITEM) + 1
        while end < len(lines) and lines[end].startswith("--") and lines[end] != "---":
            end += 1
        return lines[start:end]

    def action(self, value, checked=False, config=None):
        params = {"bash": self.LAUNCHER, "param1": "--show", "param2": value, "terminal": "false", "refresh": "true"}
        if config:
            params.update(param3="--config", param4=config)
        if checked:
            params["checked"] = "true"
        return params

    def assert_no_action(self, line):
        params = swiftbar_params(line)
        self.assertFalse([k for k in params if k.startswith("param") or k in ("bash", "shell", "refresh", "href")],
                         line)

    def test_highest_is_the_default_and_says_what_it_is(self):
        for menu_bar in (None, "highest"):
            text = self.menu(menu_bar)
            title, params = self.title(text)
            self.assertEqual(title, "62%")
            self.assertEqual(params, {"sfimage": render.icon(62.0),
                                      "tooltip": "Highest: Codex Weekly 62% · Claude 57% · Codex 62%"})
            entries = self.chooser(text)
            self.assertEqual([x.split(" | ")[0] for x in entries],
                             ["--Highest of all", "--Claude · 5-hour", "--Claude · Weekly", "--Claude · Weekly Opus",
                              "--Codex · 5-hour", "--Codex · Weekly"])
            self.assertEqual(swiftbar_params(entries[0]), self.action("highest", checked=True))
            self.assertEqual(swiftbar_params(entries[1]), self.action("claude:5-hour"))
            self.assertEqual(swiftbar_params(entries[3]), self.action("claude:Weekly Opus"))
            self.assertEqual(swiftbar_params(entries[5]), self.action("codex:Weekly"))

    def test_chooser_sits_next_to_providers(self):
        lines = menu_lines(self.menu())
        providers = lines.index(providers_line(self.menu()))
        self.assertEqual(lines[providers + 1], render.CHOOSER_ITEM)
        self.assertEqual(swiftbar_params(render.CHOOSER_ITEM), {})  # the submenu header does nothing
        self.assertTrue(lines[providers + 8].startswith("Open settings"))
        self.assertNotIn(render.CHOOSER_ITEM, render.menu(self.results(), NOW, True, "/Users/demo/config.json"))

    def test_pinned_row_drives_title_icon_colour_and_tooltip(self):
        text = self.menu({"provider": "claude", "limit": "5-hour"})
        title, params = self.title(text)
        self.assertEqual(title, "31%")  # not the highest (62%)
        self.assertEqual(params, {"sfimage": render.icon(31.0),
                                  "tooltip": "Showing Claude 5-hour 31% · Claude 57% · Codex 62%"})
        entries = self.chooser(text)
        self.assertEqual(swiftbar_params(entries[0]), self.action("highest"))
        self.assertEqual(swiftbar_params(entries[1]), self.action("claude:5-hour", checked=True))
        self.assertEqual(sum("checked" in swiftbar_params(x) for x in entries), 1)
        hot = self.results(codex=[row("Weekly", 95.0, resets=self.FUTURE)])
        self.assertEqual(self.title(self.menu(None, hot))[1].get("color"), render.RED)  # highest: red
        pinned = self.title(self.menu({"provider": "claude", "limit": "5-hour"}, hot))
        self.assertEqual(pinned[0], "31%")
        self.assertNotIn("color", pinned[1])  # the colour follows the shown limit, not the 95% one
        for percent, colour in ((92.0, render.RED), (80.0, render.ORANGE)):
            mine = self.results(claude=[row("5-hour", percent, resets=self.FUTURE)])
            self.assertEqual(self.title(self.menu({"provider": "claude", "limit": "5-hour"}, mine))[1]["color"], colour)
        spaced = self.menu({"provider": "claude", "limit": "Weekly Opus"})
        self.assertEqual(self.title(spaced)[0], "44%")
        self.assertEqual(swiftbar_params(self.chooser(spaced)[3]), self.action("claude:Weekly Opus", checked=True))

    def test_first_matching_row_wins_and_each_row_is_listed_once(self):
        twice = self.results(claude=[row("5-hour", value="n/a"), row("5-hour", 31.0, resets=self.FUTURE),
                                     row("5-hour", 50.0, resets=self.FUTURE)])
        text = self.menu({"provider": "claude", "limit": "5-hour"}, twice)
        self.assertEqual(self.title(text)[0], "31%")  # rows without a percentage don't count
        self.assertEqual([x.split(" | ")[0] for x in self.chooser(text)].count("--Claude · 5-hour"), 1)

    def test_pinned_but_unavailable_shows_a_dash(self):
        pin = {"provider": "claude", "limit": "5-hour"}
        cases = {
            "provider off, hidden or not set up": [("codex", res("CODEX", [row("Weekly", 62.0, resets=self.FUTURE)]))],
            "row gone": self.results(claude=[row("Weekly", 57.0, resets=self.FUTURE)]),
            "no percentage": self.results(claude=[row("5-hour", value="unlimited")]),
            "reset time passed": self.results(claude=[row("5-hour", 31.0, resets=NOW - 60)]),
            "nothing at all": [],
        }
        for cause, results in cases.items():
            text = self.menu(pin, results)
            title, params = self.title(text)
            self.assertEqual(title, "–", cause)  # never the highest in its place
            self.assertEqual(params["sfimage"], render.icon(None), cause)
            self.assertNotIn("color", params, cause)
            self.assertTrue(params["tooltip"].startswith("Claude 5-hour not available"), (cause, params["tooltip"]))
            entries = self.chooser(text)
            checked = [x for x in entries if "checked" in swiftbar_params(x)]
            self.assertEqual(len(checked), 1, cause)
            if cause == "reset time passed":  # the row is still listed: its own entry is the checked one
                self.assertEqual(swiftbar_params(checked[0]), self.action("claude:5-hour", checked=True))
            else:
                self.assertEqual(checked[0], "--Claude · 5-hour (not available) | checked=true", cause)
                self.assert_no_action(checked[0])
        self.assertEqual(self.title(self.menu(pin, []))[1]["tooltip"], "Claude 5-hour not available")

    def test_invalid_values_mean_highest(self):
        for value in (5, [], {}, "lowest", "", None, True, {"provider": "evil", "limit": "5-hour"},
                      {"provider": "claude"}, {"provider": "claude", "limit": 5}, {"provider": "claude", "limit": ""},
                      {"provider": "claude", "limit": "x" * 31}, {"provider": "claude", "limit": "a\nb"},
                      {"provider": "claude", "limit": "a\u200eb"}, {"provider": ["claude"], "limit": "5-hour"},
                      {"provider": "Claude", "limit": "5-hour"}):
            text = self.menu(value)
            title, params = self.title(text)
            self.assertEqual(title, "62%", value)
            self.assertTrue(params["tooltip"].startswith("Highest: Codex Weekly 62%"), value)
            entries = self.chooser(text)
            self.assertEqual(swiftbar_params(entries[0]), self.action("highest", checked=True), value)
            self.assertFalse(any("(not available)" in x for x in entries), value)

    def test_hand_pinned_label_outside_the_allow_list_matches_but_is_not_clickable(self):
        results = [("gemini", res("GEMINI", [row("Demo (High)", 47.0, resets=self.FUTURE)]))]
        text = self.menu({"provider": "gemini", "limit": "Demo (High)"}, results)
        self.assertEqual(self.title(text)[0], "47%")
        entry = self.chooser(text)[1]
        self.assertTrue(entry.startswith("--Gemini · Demo (High) | checked=true"))
        self.assert_no_action(entry)

    def test_hostile_labels_add_no_action_and_no_line(self):
        labels = ["a|b", "a'b", 'a"b', "a\\b", "a\nb", "a\u2028b", "-lead", "x bash=/bin/sh", "x param1=-c",
                  "x refresh=true", "a:b", "Demo (High)", "x$(touch y)", "a\tb", "é-umlaut", "x" * 31]
        for label in labels:
            results = self.results(claude=[row(label, 99.0, resets=self.FUTURE),
                                           row("5-hour", 31.0, resets=self.FUTURE)])
            for menu_bar in (None, {"provider": "claude", "limit": label}):
                text = self.menu(menu_bar, results)
                lines = menu_lines(text)
                self.assertLessEqual(set(swiftbar_params(lines[0])), {"sfimage", "color", "tooltip"}, (label, lines[0]))
                for line in lines:
                    params = swiftbar_params(line)
                    if "bash" in params:
                        self.assertIn(params["bash"], (self.LAUNCHER, "/usr/bin/open"), line)
                    self.assertNotIn("/bin/sh", params.get("bash", ""), line)
                entries = self.chooser(text)
                self.assertEqual(len(entries), 1 + 2 + 2, (label, entries))  # highest + 2 Claude + 2 Codex rows
                self.assert_no_action(entries[1])
                self.assertEqual(swiftbar_params(entries[2])["param2"], "claude:5-hour")  # the next item is intact

    def test_refused_paths_or_unreadable_config_leave_entries_without_action(self):
        launchers = ['/x/a" bash=/bin/sh x="', "/x/a\nb", "plugin/x.sh", None, "/Users/demo/ai;limits/x.sh",
                     "/Users/demo/my ai$x/x.sh"]
        cases = [dict(launcher=x) for x in launchers] + [dict(toggle_config='/tmp/c" bash=/bin/sh x="'),
                                                         dict(config_error=True)]
        for case in cases:
            entries = self.chooser(self.menu(**case))
            self.assertEqual(len(entries), 6, case)
            for line in entries:
                self.assert_no_action(line)
            self.assertIn("checked", swiftbar_params(entries[0]), case)  # the current choice is still shown
        launcher = "/Users/demo/My Tools (2031)/Çalışma ai-limits/plugin/ai-limits.5m.sh"
        self.assertEqual(swiftbar_params(self.chooser(self.menu(launcher=launcher))[1])["bash"], launcher)
        config = "/Users/demo/My Folder/it's (a) $test.json"
        entries = self.chooser(self.menu(toggle_config=config))
        self.assertEqual(swiftbar_params(entries[0]), self.action("highest", checked=True, config=config))
        self.assertEqual(swiftbar_params(entries[1]), self.action("claude:5-hour", config=config))

    def test_choice_param_and_allow_list(self):
        self.assertEqual(render.choice_param("claude", "Weekly Opus 4.5"), '"claude:Weekly Opus 4.5"')
        for label in ("5-hour", "Weekly", "All models", "2 models", "AI credits", "This month", "2.5-pro input",
                      "Weekly claude-opus_4", "x" * 30):
            self.assertIsNotNone(render.choice_param("claude", label), label)
        for label in ("", "x" * 31, " lead", "-lead", ".lead", "a:b", "a'b", 'a"b', "a\\b", "a|b", "a$b", "a(b)",
                      "a\nb", "a\tb", "é", "a\u200eb", 5, None):
            self.assertIsNone(render.choice_param("claude", label), repr(label))
        self.assertIsNone(render.choice_param("evil", "5-hour"))


class Panel(unittest.TestCase):
    """The panel's pure parts: its argument list and the strict parsing of its output. No window is opened."""

    ROWS = [("claude", "Claude", True, ""), ("codex", "Codex (GPT)", False, ""),
            ("copilot", "GitHub Copilot", True, render.NOT_SET_UP)]

    def test_script_is_fixed_and_data_are_arguments(self):
        nasty = [("claude", "x'); $.NSTask.launch(); ('", True, "\n\"\\`${x}"),
                 ("codex", "line\nbreak", False, "' + run() + '")]
        plain = panel.arguments(self.ROWS)
        hostile = panel.arguments(nasty)
        self.assertEqual(plain[:5], ["/usr/bin/osascript", "-l", "JavaScript", "-e", panel.SCRIPT])
        self.assertIs(hostile[4], panel.SCRIPT)  # byte-identical whatever the input
        self.assertEqual(hostile[5:], ["panel", "claude", "x'); $.NSTask.launch(); ('", "on", "\n\"\\`${x}",
                                       "codex", "line\nbreak", "off", "' + run() + '"])
        self.assertEqual(plain[5:9], ["panel", "claude", "Claude", "on"])
        self.assertEqual(plain[-4:], ["copilot", "GitHub Copilot", "on", render.NOT_SET_UP])
        with self.assertRaises(ValueError):
            panel.arguments(self.ROWS, "anything")
        self.assertNotIn("argv[", panel.SCRIPT.split("function run(argv)")[0])  # no code before run()

    def test_output_is_parsed_strictly(self):
        names = ("claude", "codex", "copilot")
        good = "claude=on\ncodex=off\ncopilot=on\n"
        self.assertEqual(panel.parse(0, good, names), {"claude": True, "codex": False, "copilot": True})
        self.assertEqual(panel.parse(0, good.rstrip("\n"), names), {"claude": True, "codex": False, "copilot": True})
        for code, out in [(1, good), (0, "cancel\n"), (0, ""), (0, "\n"), (0, "garbage"),
                          (0, "claude=on\ncodex=off\n"),                                # missing key
                          (0, good + "evil=on\n"), (0, good.replace("copilot", "gemini")),  # unknown key
                          (0, good + "claude=off\n"),                                    # repeated key
                          (0, good.replace("off", "maybe")), (0, good.replace("=", " = ")),
                          (0, good.replace("\n", "\r\n")), (0, good + "\n"), (0, " " + good),
                          (0, good + "#active=true\n"), (0, None), (-9, "")]:
            self.assertIsNone(panel.parse(code, out, names), repr((code, out)))

    def test_show_runs_osascript_without_a_shell(self):
        with mock.patch.object(panel, "run", return_value=(0, "cancel\n", "")) as run:
            self.assertEqual(panel.show(self.ROWS), (0, "cancel\n", ""))
        argv, = run.call_args.args
        self.assertEqual(argv, panel.arguments(self.ROWS))
        self.assertEqual(run.call_args.kwargs, {"timeout": panel.TIMEOUT})
        for failure in (subprocess.TimeoutExpired("osascript", 600), OSError("missing")):
            with mock.patch.object(panel, "run", side_effect=failure):
                self.assertEqual(panel.show(self.ROWS), (1, "", ""))


# ---- common -----------------------------------------------------------------------------------------------

class _Recorder(http.server.BaseHTTPRequestHandler):
    """Records every request and sends the server's fixed reply."""

    def log_message(self, *args):
        pass

    def _answer(self):
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        status, headers, body = self.server.reply
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)

    do_GET = do_POST = _answer


class _Server(http.server.HTTPServer):
    def handle_error(self, request, client_address):
        pass  # the client closing early (body cap) is expected


@contextlib.contextmanager
def loopback_server(status=200, headers=None, body=b"{}"):
    server = _Server(("127.0.0.1", 0), _Recorder)
    server.seen, server.reply = [], (status, headers or {}, body)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class Common(unittest.TestCase):
    def test_redirects_are_not_followed(self):
        self.assertIsNone(common._NoRedirect().redirect_request(None, None, 302, "", {}, "http://x"))
        with self.assertRaises(ValueError):
            common.https_json("http://example.com", {})

    def test_localhost_only(self):
        for port in (0, 70000, "80", True):
            with self.assertRaises(ValueError):
                common.localhost_json(port, "/x", {}, {})
        with self.assertRaises(ValueError):
            common.localhost_json(80, "@evil.com/x", {}, {})

    def test_localhost_never_uses_a_proxy(self):
        with loopback_server() as proxy, loopback_server(body=b'{"ok": true}') as target:
            proxy_url = f"http://127.0.0.1:{proxy.server_port}"
            probe = f"http://127.0.0.1:{target.server_port}/probe"
            # 1. http_proxy in the environment.
            with mock.patch.dict(os.environ, {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url}, clear=True):
                with urllib.request.build_opener(common._NoRedirect).open(probe, timeout=5) as r:
                    r.read()  # the module's old opener: goes to the proxy
                self.assertEqual((len(proxy.seen), len(target.seen)), (1, 0))
                answer = common.localhost_json(target.server_port, "/env", {"X-Codeium-Csrf-Token": "demo-csrf"}, {})
            # 2. A macOS system proxy without 127.0.0.1 in its bypass list, opener built while it is set.
            with mock.patch.object(urllib.request, "getproxies", return_value={"http": proxy_url}), \
                    mock.patch.object(urllib.request, "proxy_bypass", return_value=False):
                with urllib.request.build_opener(common._NoRedirect).open(probe, timeout=5) as r:
                    r.read()
                self.assertEqual((len(proxy.seen), len(target.seen)), (2, 1))
                request = urllib.request.Request(f"http://127.0.0.1:{target.server_port}/system", data=b"{}")
                system = common._read_json(request, 5, common.local_opener())
        self.assertEqual((answer, system), ({"ok": True}, {"ok": True}))
        self.assertEqual(len(proxy.seen), 2)  # only the two probes of the old opener
        self.assertEqual([path for _, path, _ in target.seen], ["/env", "/system"])
        self.assertEqual(target.seen[0][2].get("X-Codeium-Csrf-Token"), "demo-csrf")

    def test_real_redirect_is_refused_and_not_followed(self):
        with loopback_server() as elsewhere:
            location = {"Location": f"http://127.0.0.1:{elsewhere.server_port}/next"}
            for status in (301, 302, 303, 307, 308):
                with loopback_server(status, location) as start:
                    with self.assertRaisesRegex(RuntimeError, "redirect refused"):
                        common.localhost_json(start.server_port, "/x", {"X-Codeium-Csrf-Token": "demo"}, {})
                    self.assertEqual(len(start.seen), 1)
        self.assertEqual(elsewhere.seen, [])

    def test_body_over_1_mb_is_refused(self):
        with loopback_server(body=b" " * common.MAX_BODY + b"{}") as big:
            with self.assertRaisesRegex(RuntimeError, "too large"):
                common.localhost_json(big.server_port, "/x", {}, {})
        with loopback_server(body=b" " * (common.MAX_BODY - 2) + b"{}") as fits:
            self.assertEqual(common.localhost_json(fits.server_port, "/x", {}, {}), {})

    def test_to_epoch(self):
        import calendar
        expected = float(calendar.timegm((2031, 5, 17, 6, 41, 23)))
        self.assertEqual(common.to_epoch("2031-05-17T06:41:23Z"), expected)
        self.assertAlmostEqual(common.to_epoch("2031-05-17T06:41:23.123456789+00:00"), expected + 0.123456, places=5)
        self.assertEqual(common.to_epoch(expected * 1000), expected)
        self.assertIsNone(common.to_epoch("not a date"))
        self.assertIsNone(common.to_epoch(True))

    def test_to_epoch_overflow_and_non_finite(self):
        for value in (10 ** 400, -10 ** 400, 10 ** 320, float("inf"), float("-inf"), float("nan")):
            self.assertIsNone(common.to_epoch(value), value if isinstance(value, float) else "huge int")

    def test_debug_shape_has_no_values(self):
        uuid = "3f2b8c1e-9d4a-4e7b-8a6c-1b2d3e4f5a6b"
        data = {"account": {"uuid": uuid, "slug": "demo-team_01"}, uuid: {"percent": 12},
                "Mixed": True, "items": [{"id": "abc"}, 4.5]}
        text = common.shape(data)
        for value in (uuid, "demo-team_01", "abc", "Mixed", "12", "4.5"):
            self.assertNotIn(value, text)
        self.assertEqual(text, "{<key>=bool,<key>={percent=int},account={slug=str,uuid=str},items=[{id=str};float]}")
        self.assertEqual(claude.debug_line(uuid, {"k": "v"}), "<key>={k=str}")
        self.assertEqual(claude.debug_line("five_hour", {"utilization": 12.0, "resets_at": "2031-03-10T14:20:00Z"}),
                         "five_hour={resets_at=str,utilization=float}")

    def test_env_is_fixed_and_home_comes_from_passwd(self):
        home = pwd.getpwuid(os.getuid()).pw_dir
        hostile = {"HOME": "/tmp/not-the-home", "PATH": "/tmp/evil-bin", "http_proxy": "http://127.0.0.1:9",
                   "CODEX_HOME": "/tmp/evil", "SSL_CERT_FILE": "/tmp/evil.pem"}
        with mock.patch.dict(os.environ, hostile):
            spec = importlib.util.spec_from_file_location("ai_limits_common_copy",
                                                          os.path.join(ROOT, "ai_limits", "common.py"))
            copy = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(copy)
            rebuilt = common.clean_env()
        expected = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL"}
        for env in (copy.ENV, common.ENV, rebuilt):
            self.assertLessEqual(expected, set(env))
            self.assertLessEqual(set(env), expected | {"TMPDIR"})
            self.assertEqual(env["HOME"], home)
            self.assertNotIn("evil", env["PATH"])
        self.assertEqual(copy.HOME, home)
        self.assertIn(os.path.join(home, ".local", "bin"), copy.SEARCH_DIRS)


# ---- providers --------------------------------------------------------------------------------------------

class Providers(unittest.TestCase):
    def test_claude_limits_include_model_specific_weekly(self):
        limits = [{"kind": "session", "percent": 27, "resets_at": "2031-03-10T14:20:00Z"},
                  {"kind": "weekly_all", "percent": 53, "resets_at": "2031-03-14T08:00:00Z"},
                  {"kind": "weekly_scoped", "percent": 68, "resets_at": "2031-03-14T08:00:00Z",
                   "scope": {"model": {"display_name": "Opus", "id": None}, "surface": None}}]
        self.assertEqual([x["label"] for x in claude.rows_from_limits(limits)], ["5-hour", "Weekly", "Weekly Opus"])
        self.assertEqual([x["label"] for x in claude.rows_from_limits(limits[:2])], ["5-hour", "Weekly"])
        self.assertIn("Opus share", claude.shares([{"allowance_percent_of_weekly": 40, "limit_index": 2,
                                                    "used_percent_of_weekly": 21}], limits)[0])

    def test_codex_allow_list_is_read_only(self):
        self.assertEqual(codex.REQUESTS, {"initialize", "account/rateLimits/read"})
        self.assertEqual(codex.NOTIFICATIONS, {"initialized"})

    def test_codex_parse(self):
        data = {"rateLimits": {"primary": {"usedPercent": 23, "windowDurationMins": 300, "resetsAt": 1_800_003_600},
                               "secondary": {"usedPercent": 62, "windowDurationMins": 10080,
                                             "resetsAt": 1_800_345_600},
                               "planType": "example", "credits": {"hasCredits": False}},
                "rateLimitResetCredits": {"credits": [{"status": "available", "title": "Example reset"}]}}
        rows, details = codex.parse(data)
        self.assertEqual([x["label"] for x in rows], ["5-hour", "Weekly"])
        self.assertIn("Plan: example", details)
        self.assertFalse(any("reset" in (x["label"] + str(x["value"])).lower() for x in rows))  # not shown
        with self.assertRaises(RuntimeError):
            codex.parse({})

    def test_gemini_antigravity_pools(self):
        reset_a, reset_b = "2031-06-01T10:00:00Z", "2031-05-28T00:00:00Z"
        answer = {"userStatus": {"userTier": {"id": "demo-tier", "name": "Example Plan"},
                                 "cascadeModelConfigData": {"clientModelConfigs": [
                                     {"label": "Gemini Demo Fast (High)",
                                      "quotaInfo": {"remainingFraction": 1, "resetTime": reset_a}},
                                     {"label": "Example Model (Thinking)",
                                      "quotaInfo": {"remainingFraction": 1, "resetTime": reset_a}},
                                     {"label": "Gemini Demo Pro",
                                      "quotaInfo": {"remainingFraction": 0.35, "resetTime": reset_b}},
                                     {"label": "Gemini Demo Lite"}]}}}
        with mock.patch.object(gemini, "localhost_json", lambda *a, **k: answer):
            rows, details = gemini.antigravity(1234, "csrf-token")
            answer["userStatus"]["cascadeModelConfigData"]["clientModelConfigs"].pop(2)
            single_pool, _ = gemini.antigravity(1234, "csrf-token")
        self.assertEqual([x["label"] for x in rows], ["Demo Pro", "2 models"])
        self.assertAlmostEqual(rows[0]["percent"], 65.0)
        self.assertIn("Plan: Example Plan", details)
        self.assertEqual([x["label"] for x in single_pool], ["All models"])

    def test_gemini_hub_file_must_be_own_private_regular_file(self):
        hub = {"pid": os.getpid(), "httpPort": 1234, "csrfToken": "abcdefgh12"}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gemini, "HUB", os.path.join(d, "hub_*.json")):
            elsewhere = os.path.join(d, "elsewhere.json")
            with open(elsewhere, "w") as f:
                json.dump(hub, f)
            os.chmod(elsewhere, 0o600)
            os.symlink(elsewhere, os.path.join(d, "hub_1.json"))
            os.mkfifo(os.path.join(d, "hub_2.json"), 0o600)
            self.assertIsNone(gemini.hub())  # a symlink and a FIFO (which must not block): both ignored
            path = os.path.join(d, "hub_3.json")
            with open(path, "w") as f:
                json.dump(hub, f)
            os.chmod(path, 0o644)
            self.assertIsNone(gemini.hub())  # readable by others
            os.chmod(path, 0o600)
            self.assertEqual(gemini.hub(), (1234, "abcdefgh12"))
            with mock.patch("os.getuid", return_value=os.getuid() + 1):
                self.assertIsNone(gemini.hub())  # owned by someone else

    def test_gemini_falls_back_when_antigravity_fails(self):
        cli_rows = [row("demo-pro", percent=31.0)]
        with mock.patch.object(gemini, "hub", return_value=(1234, "demo-csrf-token")), \
                mock.patch.object(gemini, "antigravity", side_effect=RuntimeError("request failed")):
            with mock.patch.object(gemini, "_gemini_cli", return_value=cli_rows):
                r = gemini.read({})
            self.assertEqual(r["details"], ["Source: Gemini CLI"])
            self.assertEqual(r["rows"], cli_rows)
            with mock.patch.object(gemini, "_gemini_cli", return_value=None):
                with self.assertRaisesRegex(common.Unavailable, "Antigravity did not answer"):
                    gemini.read({})

    def test_gemini_cli_buckets(self):
        rows = gemini.quota_rows({"buckets": [
            {"modelId": "gemini-demo-pro", "tokenType": "REQUESTS", "remainingFraction": 0.38,
             "resetTime": "2031-05-28T08:00:00Z"},
            {"modelId": "gemini-demo-fast", "remainingFraction": 0.87}, {"modelId": "x"}, "bad"]})
        self.assertEqual([x["label"] for x in rows], ["demo-pro", "demo-fast"])
        self.assertEqual(gemini.quota_rows(None), [])

    def test_copilot_parse(self):
        r = copilot.parse({"token_based_billing": True, "quota_reset_date_utc": "2032-02-01T00:00:00.000Z",
                           "quota_snapshots": {"premium_interactions": {"entitlement": 6000, "remaining": 4740,
                                                                        "percent_remaining": 79.0,
                                                                        "credits_used": 1260}}})
        self.assertEqual(r["rows"][0]["label"], "AI credits")
        self.assertEqual(r["rows"][0]["value"], "1,260 / 6,000")
        self.assertAlmostEqual(r["rows"][0]["percent"], 21.0)
        with self.assertRaises(common.NotConfigured):
            copilot.parse({"quota_snapshots": {}})

    def test_copilot_errors(self):
        cases = [("gh: API rate limit exceeded (HTTP 403)", common.Unavailable),
                 ("gh: API rate limit exceeded for user ID 4014040, request 404:401. (HTTP 403)", common.Unavailable),
                 ("HTTP 401: Bad credentials", common.NotConfigured),
                 ("To get started with GitHub CLI, please run:  gh auth login", common.NotConfigured),
                 ("gh: Not Found (HTTP 404)", common.NotConfigured),
                 ("connection reset", RuntimeError)]
        with mock.patch.object(copilot, "find_tool", return_value="/opt/demo/gh"):
            for err, expected in cases:
                with mock.patch.object(copilot, "run", return_value=(1, "", err)):
                    with self.assertRaises(expected, msg=err) as caught:
                        copilot.read({})
                    if expected is common.Unavailable:
                        self.assertEqual(str(caught.exception), "GitHub refused the request")

    def test_foundry_names_and_estimate(self):
        self.assertEqual(foundry.valid_name("my-resource_group.1"), "my-resource_group.1")
        for bad in ("@~/.ssh/id_rsa", "-x", "a b", "x=@y", "", None, 5):
            self.assertIsNone(foundry.valid_name(bad))
        self.assertIsNone(foundry.valid_budget(float("inf")))
        self.assertIsNone(foundry.valid_budget(True))
        prices = {"input": 2.0, "output": 7.0, "cache_read": 0.25, "cache_write_5m": 3.0}
        tokens = {"InputTokens": 4000, "OutputTokens": 250000, "cacheReadInputTokens": 3000000,
                  "ephemeral5mInputTokens": 300000}
        self.assertAlmostEqual(foundry.estimate(tokens, prices), 0.008 + 1.75 + 0.75 + 0.9)
        self.assertIsNone(foundry.estimate({"ephemeral1hInputTokens": 5}, prices))  # unpriced type


class CodexChannel(unittest.TestCase):
    """The app-server reader and writer on plain pipes: codex is never started."""

    def setUp(self):
        self.fds = []

    def tearDown(self):
        for fd in list(self.fds):
            self.close(fd)

    def close(self, fd):
        self.fds.remove(fd)  # never closed twice: the number could already belong to another file
        os.close(fd)

    def channel(self, timeout=2.0, **caps):
        """(channel, write end of its input, read end of its output)."""
        from_codex, to_us = os.pipe()
        from_us, to_codex = os.pipe()
        self.fds += [from_codex, to_us, from_us, to_codex]
        return codex.Channel(from_codex, to_codex, time.monotonic() + timeout, **caps), to_us, from_us

    def test_two_lines_in_one_chunk_are_both_found(self):
        ch, to_us, _ = self.channel()
        data = b'{"id": 1, "result": {}}\n{"method": "note"}\nnot json\n{"id": 2, "result": {"x": 3}}\n'
        os.write(to_us, data)
        self.close(to_us)  # if the second answer were not taken from the buffer, the next read would hit EOF
        self.assertEqual(ch.wait(1), {"id": 1, "result": {}})
        self.assertEqual(ch.wait(2), {"id": 2, "result": {"x": 3}})
        self.assertEqual(ch.received, len(data))

    def test_partial_line_hits_the_deadline(self):
        ch, to_us, _ = self.channel(timeout=0.3)
        os.write(to_us, b'{"id": 1, "result": {"half')
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "in time"):
            ch.wait(1)
        self.assertLess(time.monotonic() - start, 3.0)  # stops at its deadline instead of waiting for a newline

    def test_end_of_output_stops_at_once(self):
        ch, to_us, _ = self.channel(timeout=10)
        os.write(to_us, b'{"id": 1, "res')
        self.close(to_us)
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            ch.wait(1)
        self.assertLess(time.monotonic() - start, 5.0)  # well before the 10 s deadline

    def test_size_caps_span_the_whole_exchange(self):
        ch, to_us, _ = self.channel(max_line=40)
        os.write(to_us, b"x" * 80)
        with self.assertRaisesRegex(RuntimeError, "too long"):
            ch.wait(1)
        ch, to_us, _ = self.channel(max_total=100)
        os.write(to_us, b'{"id": 1}\n' + b'{"method": "note"}\n' * 3)
        self.assertEqual(ch.wait(1), {"id": 1})  # 67 bytes so far
        os.write(to_us, b'{"method": "note"}\n' * 3)
        with self.assertRaisesRegex(RuntimeError, "too much"):
            ch.wait(2)  # the budget did not start again for this wait

    def test_only_allowed_messages_are_written(self):
        ch, _, from_us = self.channel()
        for method, msg_id in (("account/rateLimitResetCredit/consume", 3), ("initialize", None),
                               ("initialized", 4), ("account/rateLimits/read", None)):
            with self.assertRaises(ValueError):
                ch.send(method, msg_id)
        self.assertEqual(select.select([from_us], [], [], 0)[0], [])  # nothing was written
        ch.send("initialize", 1, {"clientInfo": {"name": "test"}})
        ch.send("initialized")
        ch.send("account/rateLimits/read", 2)
        sent = [json.loads(x) for x in os.read(from_us, 65536).splitlines()]
        self.assertEqual([(m.get("id"), m["method"]) for m in sent],
                         [(1, "initialize"), (None, "initialized"), (2, "account/rateLimits/read")])
        self.assertNotIn("id", sent[1])


FAKE_CODEX = """#!{python} -I
import json, sys
with open({log!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
    for line in sys.stdin:
        log.write(line)
        log.flush()
        msg = json.loads(line)
        if {mode!r} == "silent":
            continue
        if msg.get("method") == "initialize":
            print(json.dumps({{"id": msg["id"], "result": {{}}}}), flush=True)
        elif msg.get("method") == "account/rateLimits/read":
            answer = {{"id": msg["id"], "result": {{"rateLimits": {{"primary": {{
                "usedPercent": 29, "windowDurationMins": 300, "resetsAt": 1800003600}}}}}}}}
            if {mode!r} == "signed-out":
                answer = {{"id": msg["id"], "error": {{"message": "Unauthorized: login required"}}}}
            # a notification and the answer in one write
            sys.stdout.write(json.dumps({{"method": "note"}}) + "\\n" + json.dumps(answer) + "\\n")
            sys.stdout.flush()
"""


class CodexAppServer(unittest.TestCase):
    """_app_server() with a fake `codex` script: the real process, pipes and handshake, no real Codex."""

    def run_fake(self, mode, timeout=10):
        with tempfile.TemporaryDirectory() as d:
            log, script = os.path.join(d, "log"), os.path.join(d, "codex")
            with open(script, "w") as f:
                f.write(FAKE_CODEX.format(python=sys.executable, log=log, mode=mode))
            os.chmod(script, 0o700)
            try:
                with mock.patch.object(codex, "find_tool", return_value=script):
                    return codex._app_server(timeout=timeout), None, self.read_log(log)
            except Exception as e:
                return None, e, self.read_log(log)

    @staticmethod
    def read_log(path):
        with contextlib.suppress(OSError), open(path) as f:
            return [json.loads(x) for x in f.read().splitlines()]
        return []

    def test_handshake_and_answer(self):
        result, error, log = self.run_fake("normal")
        self.assertIsNone(error)
        self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 29)
        self.assertEqual(log[0], ["app-server"])
        self.assertEqual([(m.get("id"), m["method"]) for m in log[1:]],
                         [(1, "initialize"), (None, "initialized"), (2, "account/rateLimits/read")])
        rows, _ = codex.parse(result)
        self.assertEqual([x["label"] for x in rows], ["5-hour"])

    def test_signed_out_and_silent(self):
        _, error, _ = self.run_fake("signed-out")
        self.assertIsInstance(error, common.SessionExpired)
        start = time.monotonic()
        _, error, log = self.run_fake("silent", timeout=0.5)
        self.assertIsInstance(error, RuntimeError)
        self.assertLess(time.monotonic() - start, 8)  # the fake's start-up plus the 0.5 s deadline, not a hang
        self.assertEqual([m["method"] for m in log[1:]], ["initialize"])  # nothing more without an answer


class FoundrySpend(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2031, 8, 7, 12, 0, tzinfo=timezone.utc)  # a Thursday; the month began on a Friday

    def test_period_starts(self):
        self.assertEqual(foundry.period_starts(self.now), {"today": "2031-08-07", "week": "2031-08-04",
                                                           "month": "2031-08-01"})

    def test_meter_items(self):
        self.assertEqual(foundry.meter_item("9 demo ShortCo Opt Std Gl 1M Tokens", "Azure OpenAI GPT5"), "GPT-9 demo")
        self.assertEqual(foundry.meter_item("model-x 3 Inp glbl Tokens", "Azure OpenAI"), "model-x 3")
        self.assertEqual(foundry.meter_item("demo-listen-aud-inp-glbl Tokens", "Azure OpenAI"), "demo-listen")
        self.assertEqual(foundry.meter_item("y - example-units", "Claude in Microsoft Foundry (Example hosted)"),
                         "Claude (Example hosted)")
        self.assertEqual(foundry.meter_item("Operations", "Example Service"), "Example Service")

    def test_model_lines_four_periods_and_history(self):
        prices = {"m": {"input": 0, "output": 3.0}}
        live = {"m": {"2031-08-07": {"OutputTokens": 2_000_000}, "2031-08-04": {"OutputTokens": 3_000_000},
                      "2031-08-01": {"OutputTokens": 5_000_000}},
                "unpriced": {"2031-08-07": {"OutputTokens": 7}}}
        saved = {"m": {"2031-07": {"OutputTokens": 10_000_000}, "2031-08": {"OutputTokens": 333}},  # month ignored
                 "old": {"2031-05": {"OutputTokens": 2500}}}
        lines, est = foundry.model_lines(live, saved, prices, self.now)
        self.assertEqual(est["today"], (6.0, False))  # unpriced deployment used today -> "at least"
        self.assertEqual(est["week"][0], 15.0)        # Monday Aug 4 .. Aug 7
        self.assertEqual(est["month"][0], 30.0)
        self.assertEqual(lines[0], "m: ≈$6.00 · ≈$15.00 · ≈$30.00 · ≈$60.00")
        self.assertIn("old: – · – · – · 2.5K out", lines)
        self.assertIn("unpriced: 7 out · 7 out · 7 out · 7 out", lines)

    def test_invoice_lines(self):
        hist = {"currency": "USD",
                "monthly": {"Example Service": {"2031-07": 1.25, "2031-08": 2.0},
                            "Claude (Demo hosted)": {"2031-05": 3.75}},
                "daily": {"Example Service": {"2031-08-07": 1.75, "2031-08-01": 0.25}}}
        lines, totals, first = foundry.invoice_lines(hist, self.now)
        self.assertEqual(lines[0], "Claude (Demo hosted): $0.00 · $0.00 · $0.00 · $3.75")
        self.assertEqual(lines[1], "Example Service: $1.75 · $1.75 · $2.00 · $3.25")
        self.assertEqual(totals, {"today": 1.75, "week": 1.75, "month": 2.0, "earlier": 5.0})
        self.assertEqual(first, "2031-05")

    def save(self, folder, **data):
        common.write_private_json(folder, foundry.HISTORY_FILE, {"monthly": {"x": {"2031-07": 2.75}}, **data})

    def history(self, folder, force=False, resource_id=None):
        return foundry.history("az", "sub", "rg", resource_id, folder, 6, force, self.now)

    def test_history_uses_fresh_file_without_calling_azure(self):
        with tempfile.TemporaryDirectory() as d:
            self.save(d, daily={}, currency="USD", fetched=time.time())
            h = foundry.history(None, "sub", "rg", None, d, 6, False, self.now)
        self.assertEqual(h["monthly"], {"x": {"2031-07": 2.75}})

    def test_history_keeps_saved_file_on_any_failure(self):
        failures = (foundry.Throttled(), RuntimeError("az failed"), subprocess.TimeoutExpired("az", 40),
                    ValueError("not JSON"), KeyError("properties"))
        for failure in failures:
            with tempfile.TemporaryDirectory() as d, mock.patch.object(foundry, "_invoices", side_effect=failure):
                self.save(d, fetched=0)
                self.assertEqual(self.history(d, force=True)["monthly"], {"x": {"2031-07": 2.75}}, repr(failure))
                self.assertIsNone(self.history(None), repr(failure))  # no saved file: nothing to fall back to
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(foundry, "_invoices", side_effect=common.SessionExpired("az session")):
            self.save(d, fetched=0)
            with self.assertRaises(common.SessionExpired):
                self.history(d)

    def test_history_pauses_after_a_failure(self):
        path = None
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(foundry, "_invoices", side_effect=RuntimeError("az failed")) as invoices:
            path = os.path.join(d, foundry.HISTORY_FILE)
            self.save(d, fetched="not a number")  # unreadable time stamp: treated as old, no crash
            self.history(d)
            self.assertEqual(invoices.call_count, 1)
            self.assertAlmostEqual(common.read_json(path)["failed"], time.time(), delta=60)
            self.history(d)
            self.history(d, force=True)  # not even "Refresh now" retries within 15 minutes
            self.assertEqual(invoices.call_count, 1)
            self.save(d, fetched=0, failed=time.time() - foundry.FAILURE_PAUSE - 1)
            self.history(d)
            self.assertEqual(invoices.call_count, 2)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(foundry, "_invoices", side_effect=RuntimeError):
            self.assertIsNone(self.history(d))
            self.assertEqual(os.listdir(d), [])  # without a saved file nothing is recorded

    def test_history_is_saved_before_the_token_queries(self):
        class Killed(BaseException):
            """Stands in for the process exiting when the provider's time budget ends."""

        invoices = {"monthly": {"Example Service": {"2031-08": 0.75}}, "daily": {}, "currency": "USD"}
        old_tokens = {"my-deployment": {"2031-04": {"OutputTokens": 7}}}
        resource = "/subscriptions/demo/resourceGroups/rg/providers/x/accounts/demo"
        for failure in (Killed(), RuntimeError("az failed")):
            with tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(foundry, "_invoices", side_effect=lambda *a: json.loads(json.dumps(invoices))), \
                    mock.patch.object(foundry, "_tokens", side_effect=failure):
                self.save(d, fetched=0, tokens=old_tokens)
                with contextlib.suppress(Killed):
                    self.history(d, resource_id=resource)
                on_disk = common.read_json(os.path.join(d, foundry.HISTORY_FILE))
            self.assertEqual(on_disk["monthly"], invoices["monthly"], repr(failure))
            self.assertEqual(on_disk["tokens"], old_tokens)  # older months are kept
            self.assertAlmostEqual(on_disk["fetched"], time.time(), delta=60)
        recent = {"my-deployment": {"2031-08-05": {"OutputTokens": 9}}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(foundry, "_invoices", side_effect=lambda *a: json.loads(json.dumps(invoices))), \
                mock.patch.object(foundry, "_tokens", return_value=recent):
            self.save(d, fetched=0, tokens=old_tokens)
            self.history(d, resource_id=resource)
            merged = common.read_json(os.path.join(d, foundry.HISTORY_FILE))["tokens"]
        self.assertEqual(merged, {"my-deployment": {"2031-04": {"OutputTokens": 7}, "2031-08": {"OutputTokens": 9}}})

    def test_saved_time_stamps(self):
        for bad in ("yesterday", None, True, float("nan"), float("inf"), 1e30, -5):
            self.assertEqual(foundry._epoch_seconds(bad), 0.0)
        self.assertEqual(foundry._epoch_seconds(1.9e9), 1.9e9)


# ---- main.py in this process ------------------------------------------------------------------------------

def file_state(path):
    with open(path, "rb") as f:
        return os.stat(path).st_mtime_ns, f.read()


def load_main():
    """main.py as a fresh module, with main.call replaced by the provider guard: a test that wants provider data
    sets its own fake. Its default CACHE and CONFIG point into the guard's temporary folder, so a run without --cache
    or --config never touches ~/Library/Caches/ai-limits-bar or the real config.json. Importing main.py replaces
    os.environ, so this process's environment is restored."""
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location("ai_limits_main", os.path.join(ROOT, "ai_limits", "main.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    module.call = provider_guard("main.call")
    defaults = tempfile.mkdtemp(dir=_GUARD_HOME.name)
    module.CACHE = os.path.join(defaults, "cache")
    module.CONFIG = os.path.join(defaults, "config.json")
    return module


class Version(unittest.TestCase):
    def test_launcher_metadata_matches_the_package_version(self):
        import ai_limits
        with open(PLUGIN, encoding="utf-8") as f:
            found = re.findall(r"^# <xbar\.version>v([0-9.]+)</xbar\.version>$", f.read(), re.MULTILINE)
        self.assertEqual(found, [ai_limits.__version__])


class ProviderGuard(unittest.TestCase):
    """Proof that the suite-wide guard holds: without a fake, a test hits the guard, not a provider."""

    def test_main_without_a_fake_hits_the_guard(self):
        m = load_main()
        with tempfile.TemporaryDirectory() as d:
            config = os.path.join(d, "config.json")
            with open(config, "w") as f:
                json.dump({"providers": [{"name": n, "enabled": True} for n in NAMES]}, f)
            with self.assertRaisesRegex(RealProviderRead, "main.call"), contextlib.redirect_stdout(io.StringIO()):
                m.main(["--config", config, "--cache", os.path.join(d, "cache")])

    def test_the_real_call_still_cannot_reach_a_provider(self):
        # What the real main.call runs (each provider's read) stops at the provider modules' own guards.
        for provider, settings, where in [(claude, {}, "claude.run"), (codex, {}, "codex.find_tool"),
                                          (copilot, {}, "copilot.find_tool"),
                                          (foundry, {"resource_group": "rg"}, "foundry.find_tool")]:
            with self.assertRaisesRegex(RealProviderRead, where):
                provider.read(settings)
        with self.assertRaises(common.NotConfigured):
            gemini.read({})  # no hub file and no Gemini CLI sign-in in the empty guard folder: nothing real is read
        self.assertTrue(gemini.HUB.startswith(_GUARD_HOME.name))
        self.assertTrue(codex.SESSIONS.startswith(_GUARD_HOME.name))
        m = load_main()
        self.assertTrue(m.CACHE.startswith(_GUARD_HOME.name) and m.CONFIG.startswith(_GUARD_HOME.name))

    def test_every_process_run_goes_through_run_launcher(self):
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            source = f.read()
        run, popen = "subprocess." + "run(", "subprocess." + "Popen("  # split, so this line doesn't count itself
        self.assertEqual(source.count(run), 1)  # the one in run_launcher
        self.assertEqual(source.count(popen), 0)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(AssertionError):
                run_launcher([], config=os.path.join(ROOT, "config.json"), cache=os.path.join(d, "c"))


class Main(unittest.TestCase):
    """main.main() with `call` replaced, so no provider is ever really read."""

    def setUp(self):
        self.main = load_main()
        self.calls = []
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.cache = os.path.join(self.dir, "cache")
        self.config = os.path.join(self.dir, "config.json")
        with open(self.config, "w") as f:
            json.dump({"providers": [{"name": "copilot", "enabled": True, "refresh_minutes": 5}]}, f)

    def run_main(self, *args, outcome=None):
        def fake(name, settings, forced):
            self.calls.append((name, settings["force"]))
            if isinstance(outcome, BaseException):
                raise outcome
            return {"title": "GITHUB COPILOT", "status": "ok", "rows": [row("AI credits", 23.0)], "details": [],
                    "note": None, "time": time.time()}
        self.main.call = fake
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.main.main(["--config", self.config, "--cache", self.cache, *args])
        return out.getvalue()

    def write_cache(self, age):
        common.write_private_json(self.cache, "copilot.json", {
            "title": "GITHUB COPILOT", "status": "ok", "details": [], "note": None, "time": time.time() - age,
            "rows": [row("AI credits", 31.0, resets=time.time() + 86400 * 5)]})

    def test_refresh_has_a_60_second_floor(self):
        self.write_cache(30)
        self.assertIn("31%", self.run_main("--refresh"))
        self.assertEqual(self.calls, [])  # younger than 60 seconds: served from the cache
        self.write_cache(120)
        self.run_main()
        self.assertEqual(self.calls, [])  # a normal run uses it for almost 5 minutes
        self.assertIn("23%", self.run_main("--refresh"))
        self.assertEqual(self.calls, [("copilot", True)])

    def test_unavailable_shows_cached_values_or_hides(self):
        self.write_cache(3600)
        out = self.run_main(outcome=common.Unavailable("GitHub refused the request"))
        self.assertIn("GitHub refused the request · last read", out)
        self.assertIn("31%", out)
        os.remove(os.path.join(self.cache, "copilot.json"))
        out = self.run_main(outcome=common.Unavailable("GitHub refused the request"))
        self.assertFalse(any("GitHub Copilot" in x for x in top_level(out)))
        self.assertIn("No supported AI tool found", out)
        self.assertEqual(self.main.load_not_set_up(self.cache), set())  # unavailable is not "not set up"

    def write_config(self, providers):
        with open(self.config, "w") as f:
            json.dump({"providers": providers}, f)

    def test_disabled_provider_is_never_read(self):
        self.write_config([{"name": "copilot", "enabled": True}, {"name": "gemini", "enabled": False}])
        for name in ("copilot", "gemini"):
            common.write_private_json(self.cache, f"{name}.json", {
                "title": name.upper(), "status": "ok", "details": [], "note": None, "time": time.time() - 3600,
                "rows": [row("Quota", 31.0)]})
        gemini_cache = os.path.join(self.cache, "gemini.json")
        before = file_state(gemini_cache)
        with mock.patch.object(self.main, "cache_read", wraps=self.main.cache_read) as reads:
            for args in ((), ("--fail", "gemini"), ("--refresh",)):
                out = self.run_main(*args)
                self.assertFalse(any(x.startswith("Gemini") for x in top_level(out)), args)
                self.assertNotIn("Couldn't read", out)  # --fail gemini would have shown a note had it been read
                self.assertNotIn("Gemini", out)  # not in the menu at all
        self.assertEqual({name for name, _ in self.calls}, {"copilot"})
        self.assertNotIn("gemini", {c.args[1] for c in reads.call_args_list})  # its cache is not even read
        self.assertEqual(file_state(gemini_cache), before)

    def test_missing_entry_is_off(self):
        out = self.run_main()  # setUp's config lists only copilot
        self.assertEqual({name for name, _ in self.calls}, {"copilot"})
        self.assertNotIn("All providers are off", out)

    def test_not_set_up_is_saved_for_the_panel(self):
        out = self.run_main(outcome=common.NotConfigured("gh not installed"))
        self.assertNotIn("GitHub Copilot", out)  # hidden; the panel explains it
        self.assertEqual(self.main.load_not_set_up(self.cache), {"copilot"})
        path = os.path.join(self.cache, self.main.STATE_FILE)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.run_main()  # read fine this time: no longer "not set up"
        self.assertEqual(self.main.load_not_set_up(self.cache), set())

    def test_state_file_is_checked(self):
        path = os.path.join(self.cache, self.main.STATE_FILE)
        for state in ({"not_set_up": ["copilot"], "time": time.time() - 2 * 3600},  # stale
                      {"not_set_up": ["copilot"], "time": time.time() + 3600},      # from the future
                      {"not_set_up": "copilot", "time": time.time()}, {"time": time.time()}, [1], "x"):
            common.write_private_json(self.cache, self.main.STATE_FILE, state)
            self.assertEqual(self.main.load_not_set_up(self.cache), set(), state)
        common.write_private_json(self.cache, self.main.STATE_FILE,
                                  {"not_set_up": ["copilot", "evil", 5, "../x"], "time": time.time()})
        self.assertEqual(self.main.load_not_set_up(self.cache), {"copilot"})
        os.remove(path)
        self.assertEqual(self.main.load_not_set_up(self.cache), set())

    def test_first_entry_of_a_name_decides(self):
        self.write_config([{"name": "gemini", "enabled": False}, {"name": "gemini", "enabled": True}])
        self.run_main()
        self.assertEqual(self.calls, [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.main.main(["--toggle", "gemini", "--config", self.config])
        with open(self.config) as f:
            self.assertEqual(json.load(f)["providers"], [{"name": "gemini", "enabled": True},
                                                         {"name": "gemini", "enabled": True}])
        self.run_main()
        self.assertEqual([name for name, _ in self.calls], ["gemini"])

    def test_menu_offers_the_real_launcher(self):
        params = swiftbar_params(providers_line(self.run_main("--skip", ALL)))
        self.assertEqual(params, {"bash": self.main.LAUNCHER, "param1": "--choose", "param2": "--config",
                                  "param3": self.config, "terminal": "false", "refresh": "true"})
        self.assertTrue(os.path.isfile(self.main.LAUNCHER))

    def test_arguments(self):
        args = self.main.parse_args(["--bogus", "x", "--skip", "a,b", "--conf", "y", "-h", "--refresh"])
        self.assertEqual((args.skip, args.config, args.refresh, args.debug), ("a,b", None, True, False))
        for bad in (["--config"], ["--cache", "--refresh"], ["--config", ""], ["--debug=yes"]):
            with self.assertRaises(self.main.ArgumentError, msg=bad):
                self.main.parse_args(bad)

    def test_relative_config_path_is_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.dir)
        try:
            out = io.StringIO()
            self.main.call = lambda *a: self.fail("no provider may be read")
            with contextlib.redirect_stdout(out):
                self.main.main(["--config", "config.json", "--cache", "cache", "--skip", ALL])
        finally:
            os.chdir(here)
        line = next(x for x in out.getvalue().splitlines() if x.startswith("Open settings"))
        self.assertEqual(os.path.realpath(swiftbar_params(line)["param1"]), os.path.realpath(self.config))

    def test_broken_config_falls_back_with_a_note(self):
        for text in ("{not json", "[1, 2]", '"text"'):
            with open(self.config, "w") as f:
                f.write(text)
            config, unreadable = self.main.load_config(self.config, create=False)
            self.assertTrue(unreadable, text)
            self.assertIsInstance(config.get("providers"), list)
        missing = os.path.join(self.dir, "missing.json")
        self.assertFalse(self.main.load_config(missing, create=False)[1])
        self.assertFalse(os.path.exists(missing))  # created only for the default config.json

    def test_cache_permissions_and_cleanup(self):
        folder = os.path.join(self.dir, "private")
        self.main.cache_write(folder, "copilot", {"time": 1})
        self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(folder, "copilot.json")).st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(folder)), ["copilot.json"])  # no temp file left behind
        link = os.path.join(self.dir, "link")
        os.symlink(folder, link)
        with self.assertRaises(OSError):
            self.main.cache_write(link, "copilot", {"time": 1})


class Toggle(unittest.TestCase):
    """--toggle on temporary config files only: every call goes through `toggle()` below, which always passes
    --config."""

    ORIGINAL = ('{"providers": [{"name": "claude", "enabled": true, "refresh_minutes": 5, '
                '"zz_unknown": [1, 2.5, null, "x", {"b": 2, "a": 1}], "timeout_seconds": 15}, '
                '{"timeout_seconds": 9, "name": "gemini", "enabled": true, "note": "caf\u00e9"}, "not an entry"], '
                '"debug_shapes": false, "custom": {"z": 1, "a": [true]}, "number": 2.5e-3, "text": "x"}')

    def setUp(self):
        self.main = load_main()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = os.path.join(folder.name, "settings")
        os.mkdir(self.dir)
        self.config = os.path.join(self.dir, "config.json")
        self.write(self.ORIGINAL)

    def write(self, text, path=None):
        with open(path or self.config, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path=None):
        with open(path or self.config, "rb") as f:
            return f.read()

    def toggle(self, name, config=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.main.main(["--toggle", name, "--config", config or self.config, "--cache",
                            os.path.join(self.dir, "..", "cache")])
        self.assertEqual(out.getvalue(), "")  # a toggle prints nothing
        self.files_now(config)

    def files_now(self, config=None):
        folder = os.path.dirname(os.path.realpath(config or self.config))
        names = os.listdir(folder)
        self.assertFalse([x for x in names if x.endswith(".tmp")], names)  # no temp file left behind
        return names

    def test_round_trip_keeps_everything_else(self):
        original = json.loads(self.ORIGINAL)
        self.toggle("gemini")
        once = json.loads(self.read())
        self.assertIs(once["providers"][1]["enabled"], False)
        once["providers"][1]["enabled"] = True
        self.assertEqual(json.dumps(once), json.dumps(original))  # same values, types and key order
        self.toggle("gemini")
        self.assertEqual(json.dumps(json.loads(self.read())), json.dumps(original))
        self.toggle("claude")
        self.assertIs(json.loads(self.read())["providers"][0]["enabled"], False)

    def test_file_format_is_one_provider_per_line(self):
        self.toggle("gemini")
        data = json.loads(self.read())
        lines = self.read().decode("utf-8").splitlines()
        entries = [json.dumps(p, ensure_ascii=False) for p in data["providers"]]
        self.assertEqual(lines[:2], ["{", '  "providers": ['])
        self.assertEqual(lines[2:5], [f"    {entries[0]},", f"    {entries[1]},", f"    {entries[2]}"])
        self.assertEqual(lines[5], "  ],")
        self.assertEqual(lines[6:], ['  "debug_shapes": false,', '  "custom": {"z": 1, "a": [true]},',
                                     '  "number": 0.0025,', '  "text": "x"', "}"])
        self.assertIn('"note": "café"', lines[3])  # ensure_ascii=False

    def test_missing_entry_and_missing_list(self):
        self.write('{"providers": [{"name": "claude", "enabled": false}]}')
        self.toggle("codex")
        self.assertEqual(json.loads(self.read())["providers"],
                         [{"name": "claude", "enabled": False}, {"name": "codex", "enabled": True}])
        self.write('{"debug_shapes": true}')
        self.toggle("claude")
        self.assertEqual(json.loads(self.read()), {"debug_shapes": True, "providers": [{"name": "claude",
                                                                                        "enabled": True}]})
        self.write('{"providers": "oops", "debug_shapes": true}')
        self.toggle("claude")
        self.assertEqual(list(json.loads(self.read()).items()),
                         [("providers", [{"name": "claude", "enabled": True}]), ("debug_shapes", True)])
        for value in ("yes", 1, None):
            self.write(json.dumps({"providers": [{"name": "codex", "enabled": value}]}))
            self.toggle("codex")
            self.assertIs(json.loads(self.read())["providers"][0]["enabled"], True)  # anything but true -> true

    def test_unreadable_file_and_unknown_names_are_left_alone(self):
        for text in ("{not json", "[1, 2]", '"text"', ""):
            self.write(text)
            before = self.read()
            self.toggle("gemini")
            self.assertEqual(self.read(), before, text)
        self.write(self.ORIGINAL)
        for name in ("evil", "", "../config", "gemini,claude", "GEMINI", "gemini\n"):
            self.toggle(name)
            self.assertEqual(self.read().decode("utf-8"), self.ORIGINAL, repr(name))

    def test_mode_is_kept(self):
        for mode in (0o644, 0o600, 0o640):
            os.chmod(self.config, mode)
            self.toggle("gemini")
            self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), mode)

    def test_symlinked_config_keeps_the_link(self):
        elsewhere = os.path.join(os.path.dirname(self.dir), "linked")
        os.mkdir(elsewhere)
        link = os.path.join(elsewhere, "config.json")
        os.symlink(self.config, link)
        self.toggle("gemini", config=link)
        self.assertTrue(os.path.islink(link))
        self.assertIs(json.loads(self.read())["providers"][1]["enabled"], False)  # the target was updated
        self.assertEqual(os.listdir(elsewhere), ["config.json"])

    def test_cache_is_not_touched(self):
        cache = os.path.join(os.path.dirname(self.dir), "cache")
        common.write_private_json(cache, "gemini.json", {"time": 1, "rows": []})
        path = os.path.join(cache, "gemini.json")
        before = (os.stat(path).st_mtime_ns, self.read(path))
        self.toggle("gemini")
        self.toggle("gemini")
        self.assertEqual((os.stat(path).st_mtime_ns, self.read(path)), before)
        self.assertEqual(os.listdir(cache), ["gemini.json"])

    def test_new_file_starts_from_the_example(self):
        example = os.path.join(self.dir, "example.json")
        self.write('{"providers": [{"name": "claude", "enabled": true}, {"name": "gemini", "enabled": true}]}',
                   example)
        target = os.path.join(self.dir, "new.json")
        with mock.patch.object(self.main, "EXAMPLE", example):
            self.toggle("gemini", config=target)  # --config to a file that is not there: what the menu showed
            self.assertEqual(json.loads(self.read(target))["providers"],
                             [{"name": "claude", "enabled": True}, {"name": "gemini", "enabled": False}])
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
            default = os.path.join(self.dir, "default.json")
            self.assertTrue(self.main.toggle(default, "claude", create=True))  # the default config.json rule
            self.assertEqual(json.loads(self.read(default))["providers"][0], {"name": "claude", "enabled": False})
            self.assertEqual(stat.S_IMODE(os.stat(default).st_mode), 0o600)

    def test_default_config_as_swiftbar_uses_it(self):
        """No --config, as in SwiftBar's own runs and clicks; CONFIG and EXAMPLE point into the temp folder."""
        default, example = os.path.join(self.dir, "default.json"), os.path.join(self.dir, "example.json")
        self.write(json.dumps({"providers": [{"name": n, "enabled": n != "foundry"} for n in NAMES]}), example)
        cache = os.path.join(os.path.dirname(self.dir), "cache")
        outputs = []
        with mock.patch.object(self.main, "CONFIG", default), mock.patch.object(self.main, "EXAMPLE", example):
            for args in (["--cache", cache, "--skip", ALL], ["--toggle", "gemini"], ["--cache", cache, "--skip", ALL]):
                self.assertEqual(os.path.dirname(self.main.CONFIG), self.dir)  # never the real config.json
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.main.main(args)
                outputs.append(out.getvalue())
        self.assertEqual(outputs[1], "")
        self.assertEqual(stat.S_IMODE(os.stat(default).st_mode), 0o600)  # created from the example on first run
        self.assertIs(json.loads(self.read(default))["providers"][2]["enabled"], False)
        for menu in (outputs[0], outputs[2]):
            params = swiftbar_params(providers_line(menu))
            self.assertEqual(params, {"bash": self.main.LAUNCHER, "param1": "--choose", "terminal": "false",
                                      "refresh": "true"})  # the default file needs no --config
        self.files_now(default)

    def test_set_enabled_writes_several_changes_at_once(self):
        with mock.patch.object(self.main, "_write_atomic", wraps=self.main._write_atomic) as writes:
            self.assertTrue(self.main.set_enabled(self.config, {"claude": False, "gemini": False, "codex": True,
                                                                "foundry": False, "evil": True}, False))
        self.assertEqual(writes.call_count, 1)
        data = json.loads(self.read())
        self.assertEqual([(p["name"], p["enabled"]) for p in data["providers"] if isinstance(p, dict)],
                         [("claude", False), ("gemini", False), ("codex", True)])  # foundry: no entry, stays off
        original = json.loads(self.ORIGINAL)
        data["providers"][0]["enabled"] = data["providers"][1]["enabled"] = True
        del data["providers"][3]
        self.assertEqual(json.dumps(data), json.dumps(original))  # everything else as it was
        before = self.read()
        with mock.patch.object(self.main, "_write_atomic") as writes:
            self.assertFalse(self.main.set_enabled(self.config, {"claude": False, "codex": True, "copilot": False},
                                                   False))
        writes.assert_not_called()  # nothing changes: nothing is written
        self.assertEqual(self.read(), before)
        self.files_now()

    def test_nan_infinity_and_lone_surrogates(self):
        self.write('{"providers": [{"name": "gemini", "enabled": true, "x": NaN, "y": -Infinity}], '
                   '"label": "a\\ud800b"}')
        self.toggle("gemini")
        text = self.read().decode("utf-8")
        data = json.loads(text)
        self.assertIs(data["providers"][0]["enabled"], False)
        self.assertIn('"x": NaN, "y": -Infinity', text)
        self.assertEqual(data["label"], "a\ud800b")

    def test_failures_leave_the_file_unchanged(self):
        before = self.read()
        lock = os.open(self.dir, os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)  # another change, or an open panel
            with mock.patch.object(self.main, "LOCK_SECONDS", 0.2), self.assertRaises(self.main.Busy):
                self.toggle("gemini")
        finally:
            os.close(lock)
        self.assertEqual(self.read(), before)
        if not IS_ROOT:
            os.chmod(self.dir, 0o500)  # the folder can't take the temp file
            try:
                with self.assertRaises(OSError):
                    self.toggle("gemini")
            finally:
                os.chmod(self.dir, 0o700)
        self.assertEqual(self.read(), before)
        self.files_now()


class KeepValues(unittest.TestCase):
    """A "Menu bar shows" click must show the new number at once: --show leaves a short-lived marker, and the
    --refresh run SwiftBar starts right after the click then uses the values already read."""

    def setUp(self):
        self.main = load_main()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.cache = os.path.join(self.dir, "cache")
        self.config = os.path.join(self.dir, "config.json")
        self.marker = os.path.join(self.cache, self.main.KEEP_FILE)
        self.write_config([{"name": "claude", "enabled": True, "refresh_minutes": 5}])
        self.calls = []

        def fake(name, settings, forced):
            self.calls.append((name, settings["force"]))
            return {"title": name.upper(), "status": "ok", "details": [], "note": None, "time": time.time(),
                    "rows": [row("5-hour", 88.0, resets=time.time() + 3600)]}
        self.main.call = fake

    def write_config(self, providers, **extra):
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"providers": providers, **extra}, f)

    def cache_claude(self, age=120):
        """Older than the 60 s refresh floor, younger than refresh_minutes (5 min - 30 s)."""
        common.write_private_json(self.cache, "claude.json", {
            "title": "CLAUDE", "status": "ok", "details": [], "note": None, "time": time.time() - age,
            "rows": [row("5-hour", 31.0, resets=time.time() + 3600), row("Weekly", 57.0, resets=time.time() + 3600)]})

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.main.main([*args, "--config", self.config, "--cache", self.cache])
        return out.getvalue()

    def write_marker(self, text):
        os.makedirs(self.cache, exist_ok=True)
        with open(self.marker, "w", encoding="utf-8") as f:
            f.write(text)

    def test_show_then_refresh_uses_the_values_already_read(self):
        self.cache_claude()
        self.assertEqual(self.run_main("--show", "claude:5-hour"), "")
        with open(self.marker, encoding="utf-8") as f:
            marker = json.load(f)
        self.assertEqual(list(marker), ["time"])
        self.assertAlmostEqual(marker["time"], time.time(), delta=30)
        self.assertEqual(stat.S_IMODE(os.stat(self.marker).st_mode), 0o600)
        text = self.run_main("--refresh")
        self.assertEqual(self.calls, [])  # not re-read: the cached 31% is shown at once
        self.assertTrue(menu_lines(text)[0].startswith("31% | "))
        self.assertFalse(os.path.exists(self.marker))  # used once
        self.run_main("--refresh")
        self.assertEqual(self.calls, [("claude", True)])  # the next forced refresh reads again

    def test_marker_is_written_when_nothing_changes_and_when_config_is_unreadable(self):
        self.write_config([{"name": "claude", "enabled": True}], menu_bar={"provider": "claude", "limit": "5-hour"})
        with mock.patch.object(self.main, "_write_atomic") as config_writes:
            self.run_main("--show", "claude:5-hour")
        config_writes.assert_not_called()
        self.assertTrue(os.path.exists(self.marker))
        os.remove(self.marker)
        with open(self.config, "w") as f:
            f.write("{not json")
        self.run_main("--show", "claude:5-hour")
        self.assertTrue(os.path.exists(self.marker))
        with open(self.config) as f:
            self.assertEqual(f.read(), "{not json")

    def test_no_marker_after_a_refusal_or_a_busy_lock(self):
        with self.assertRaises(self.main.ArgumentError):
            self.run_main("--show", "evil:5-hour")
        self.assertFalse(os.path.exists(self.marker))
        lock = os.open(self.dir, os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)  # a panel is open
            with mock.patch.object(self.main, "LOCK_SECONDS", 0.2), self.assertRaises(self.main.Busy):
                self.run_main("--show", "claude:5-hour")
        finally:
            os.close(lock)
        self.assertFalse(os.path.exists(self.marker))

    def test_bad_markers_do_not_suppress_and_are_removed(self):
        now = time.time()
        target = os.path.join(self.dir, "elsewhere.json")
        with open(target, "w") as f:
            json.dump({"time": now}, f)
        cases = {"expired": json.dumps({"time": now - 11}), "future": json.dumps({"time": now + 5}),
                 "garbage": "{not json", "list": "[1]", "text time": '{"time": "now"}', "bool time": '{"time": true}',
                 "nan time": '{"time": NaN}', "empty": "", "symlink": None}
        for case, text in cases.items():
            self.cache_claude()
            self.calls.clear()
            if text is None:
                os.makedirs(self.cache, exist_ok=True)
                os.symlink(target, self.marker)
            else:
                self.write_marker(text)
            self.run_main("--refresh")
            self.assertEqual(self.calls, [("claude", True)], case)  # forced, as without a marker
            self.assertFalse(os.path.lexists(self.marker), case)
        self.assertTrue(os.path.exists(target))  # a symlinked marker is removed, not what it points to

    def test_without_a_marker_or_without_refresh_nothing_changes(self):
        self.cache_claude()
        self.run_main("--refresh")
        self.assertEqual(self.calls, [("claude", True)])  # plain --refresh: forced as before
        self.cache_claude()
        self.calls.clear()
        self.write_marker(json.dumps({"time": time.time()}))
        self.run_main()
        self.assertEqual(self.calls, [])  # a normal run uses the fresh cache anyway
        self.assertTrue(os.path.exists(self.marker))  # and leaves the marker alone (it simply expires)

    def test_cached_values_of_any_age_are_kept_for_that_run(self):
        self.cache_claude(age=20 * 60)  # older than refresh_minutes: a scheduled run would read it again
        self.run_main("--show", "claude:5-hour")
        text = self.run_main("--refresh")
        self.assertEqual(self.calls, [])  # kept, whatever its age: the click is always instant
        self.assertTrue(menu_lines(text)[0].startswith("31% | "))
        self.run_main()  # the next scheduled run (no --refresh, no marker): the normal freshness rule again
        self.assertEqual(self.calls, [("claude", False)])

    def test_a_forced_failure_still_goes_through_call(self):
        self.cache_claude()
        self.run_main("--show", "claude:5-hour")
        self.run_main("--refresh", "--fail", "claude")
        self.assertEqual(self.calls, [("claude", False)])  # --fail wins over the kept cache

    def test_a_provider_without_values_is_still_read(self):
        self.write_config([{"name": "claude", "enabled": True}, {"name": "codex", "enabled": True}])
        self.cache_claude()
        self.run_main("--show", "codex:5-hour")
        text = self.run_main("--refresh")
        self.assertEqual(self.calls, [("codex", False)])  # read, but not as a forced refresh (Foundry's force too)
        self.assertTrue(menu_lines(text)[0].startswith("88% | "))


class Show(unittest.TestCase):
    """--show on temporary config files only: every call passes --config."""

    TEXT = ('{"providers": [{"name": "claude", "enabled": true}, {"name": "codex", "enabled": false}], '
            '"debug_shapes": false, "custom": [1, "x"]}')

    def setUp(self):
        self.main = load_main()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = os.path.join(folder.name, "settings")
        os.mkdir(self.dir)
        self.cache = os.path.join(folder.name, "cache")
        self.config = os.path.join(self.dir, "config.json")
        self.write(self.TEXT)  # load_main's guard stops any provider read if --show ever fell through

    def write(self, text):
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self):
        with open(self.config, encoding="utf-8") as f:
            return f.read()

    def show(self, value):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.main.main(["--show", value, "--config", self.config, "--cache", self.cache])
        self.assertEqual(out.getvalue(), "")  # prints nothing
        self.assertFalse([x for x in os.listdir(self.dir) if x.endswith(".tmp")])
        return self.read()

    def test_pin_and_back(self):
        text = self.show("claude:5-hour")
        data = json.loads(text)
        self.assertEqual(data["menu_bar"], {"provider": "claude", "limit": "5-hour"})
        self.assertEqual(list(data), ["providers", "debug_shapes", "custom", "menu_bar"])
        self.assertEqual(data["custom"], [1, "x"])
        lines = text.splitlines()
        self.assertIn('    {"name": "claude", "enabled": true},', lines)  # still one provider per line
        self.assertIn('  "menu_bar": {"provider": "claude", "limit": "5-hour"}', lines)
        self.assertEqual(json.loads(self.show("claude:Weekly Opus"))["menu_bar"],
                         {"provider": "claude", "limit": "Weekly Opus"})
        data = json.loads(self.show("highest"))
        self.assertEqual(data["menu_bar"], "highest")
        self.assertEqual(list(data), ["providers", "debug_shapes", "custom", "menu_bar"])  # stays in place

    def test_no_change_no_write(self):
        self.write(self.TEXT)
        with mock.patch.object(self.main, "_write_atomic") as writes:
            self.show("highest")  # missing means highest already
            self.write('{"providers": [], "menu_bar": {"provider": "codex", "limit": "Weekly"}}')
            self.show("codex:Weekly")
        writes.assert_not_called()

    def test_refusals_write_nothing(self):
        for bad in ("evil:5-hour", "claude", "claude:", ":5-hour", "CLAUDE:5-hour", "claude:a|b", "claude:Demo (High)",
                    "claude:-x", "claude:" + "x" * 31, "claude:a\nb", "claude:a:b", "claude:a'b", "Highest", "",
                    "claude:é"):
            with self.assertRaises(self.main.ArgumentError, msg=bad):
                self.show(bad)
            self.assertEqual(self.read(), self.TEXT, bad)

    def test_unreadable_config_is_left_alone(self):
        for text in ("{not json", "[1]"):
            self.write(text)
            self.assertEqual(self.show("claude:5-hour"), text)

    def test_set_enabled_keeps_menu_bar(self):
        self.write('{"providers": [{"name": "claude", "enabled": true}], '
                   '"menu_bar": {"provider": "claude", "limit": "5-hour"}, "debug_shapes": false}')
        self.assertTrue(self.main.set_enabled(self.config, {"claude": False, "codex": True}, False))
        lines = self.read().splitlines()
        self.assertEqual(lines[:6], ["{", '  "providers": [', '    {"name": "claude", "enabled": false},',
                                     '    {"name": "codex", "enabled": true}', "  ],",
                                     '  "menu_bar": {"provider": "claude", "limit": "5-hour"},'])

    def test_round_trip_with_the_menu(self):
        def run_menu():
            out = io.StringIO()
            self.main.call = lambda name, settings, forced: {
                "title": "CLAUDE", "status": "ok", "details": [], "note": None, "time": time.time(),
                "rows": [row("5-hour", 31.0, resets=time.time() + 3600),
                         row("Weekly", 57.0, resets=time.time() + 3600)]}
            with contextlib.redirect_stdout(out):
                self.main.main(["--config", self.config, "--cache", self.cache, "--refresh"])
            return out.getvalue()
        self.show("claude:5-hour")
        text = run_menu()
        first = menu_lines(text)[0]
        self.assertTrue(first.startswith("31% | "))
        self.assertTrue(swiftbar_params(first)["tooltip"].startswith("Showing Claude 5-hour 31%"))
        entry = next(x for x in menu_lines(text) if x.startswith("--Claude · 5-hour"))
        self.assertEqual(swiftbar_params(entry), {"checked": "true", "bash": self.main.LAUNCHER, "param1": "--show",
                                                  "param2": "claude:5-hour", "param3": "--config",
                                                  "param4": self.config, "terminal": "false", "refresh": "true"})
        self.show("highest")
        text = run_menu()
        self.assertTrue(menu_lines(text)[0].startswith("57% | "))
        self.assertIn("checked", swiftbar_params(next(x for x in menu_lines(text) if x.startswith("--Highest of all"))))


class Choose(unittest.TestCase):
    """--choose with the panel replaced by a fake: no window is opened. Always a temp --config and --cache."""

    def setUp(self):
        self.main = load_main()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = os.path.join(folder.name, "settings")
        os.mkdir(self.dir)
        self.cache = os.path.join(folder.name, "cache")
        self.config = os.path.join(self.dir, "config.json")
        self.text = ('{"providers": [{"name": "claude", "enabled": true, "x": 1}, '
                     '{"name": "codex", "enabled": true}, {"name": "gemini", "enabled": false}, '
                     '{"name": "copilot", "enabled": true}], "debug_shapes": false}')
        with open(self.config, "w") as f:
            f.write(self.text)
        self.shown = []

    def choose(self, answer=None, side_effect=None):
        """Runs --choose; the fake panel records its rows and answers (code, stdout, stderr)."""
        def fake(rows, mode="panel", timeout=None):
            self.shown.append(rows)
            if side_effect:
                side_effect()
            return answer if answer is not None else (0, "cancel\n", "")
        out = io.StringIO()
        with mock.patch.object(panel, "show", side_effect=fake), contextlib.redirect_stdout(out):
            self.main.main(["--choose", "--config", self.config, "--cache", self.cache])
        self.assertEqual(out.getvalue(), "")  # prints nothing
        self.assertFalse([x for x in os.listdir(self.dir) if x.endswith(".tmp")])
        with open(self.config) as f:
            return f.read()

    @staticmethod
    def answer(**states):
        return 0, "".join(f"{n}={'on' if states.get(n) else 'off'}\n" for n in NAMES), ""

    def test_save_applies_everything_in_one_write(self):
        with mock.patch.object(self.main, "_write_atomic", wraps=self.main._write_atomic) as writes:
            text = self.choose(self.answer(claude=True, gemini=True, foundry=True))
        self.assertEqual(writes.call_count, 1)
        data = json.loads(text)
        self.assertEqual([(p["name"], p["enabled"]) for p in data["providers"]],
                         [("claude", True), ("codex", False), ("gemini", True), ("copilot", False),
                          ("foundry", True)])
        self.assertEqual(data["providers"][0]["x"], 1)
        self.assertEqual(list(data), ["providers", "debug_shapes"])

    def test_panel_rows_and_notes(self):
        common.write_private_json(self.cache, self.main.STATE_FILE,
                                  {"not_set_up": ["copilot", "gemini"], "time": time.time()})
        self.choose()
        self.assertEqual(self.shown, [[("claude", "Claude", True, ""), ("codex", "Codex (GPT)", True, ""),
                                       ("gemini", "Gemini", False, ""),  # off: no note
                                       ("copilot", "GitHub Copilot", True, render.NOT_SET_UP),
                                       ("foundry", "Microsoft Foundry", False, "")]])
        common.write_private_json(self.cache, self.main.STATE_FILE,
                                  {"not_set_up": ["copilot"], "time": time.time() - 7200})
        self.choose()
        self.assertEqual(self.shown[1][3], ("copilot", "GitHub Copilot", True, ""))  # stale state: no note

    def test_cancel_and_bad_answers_change_nothing(self):
        good = self.answer(claude=False)[1]
        for answer in [(0, "cancel\n", ""), (1, good, "error"), (0, good + "evil=on\n", ""),
                       (0, good.replace("foundry=off\n", ""), ""), (0, "garbage", ""), (0, "", ""),
                       (0, good + "#active=true\n", ""), (1, "", "")]:
            with mock.patch.object(self.main, "_write_atomic") as writes:
                self.assertEqual(self.choose(answer), self.text, answer)
            writes.assert_not_called()

    def test_unreadable_config_shows_no_panel(self):
        for text in ("{not json", "[1]"):
            with open(self.config, "w") as f:
                f.write(text)
            self.assertEqual(self.choose(self.answer(claude=True)), text)
        self.assertEqual(self.shown, [])

    def test_second_panel_exits_at_once(self):
        lock = os.open(self.dir, os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)  # a panel is already open
            start = time.monotonic()
            self.assertEqual(self.choose(self.answer(claude=False)), self.text)
            self.assertLess(time.monotonic() - start, 2.0)  # does not even wait LOCK_SECONDS (3 s)
        finally:
            os.close(lock)
        self.assertEqual(self.shown, [])

    def test_toggle_fails_fast_while_the_panel_is_open(self):
        attempts = []

        def toggle_meanwhile():
            with mock.patch.object(self.main, "LOCK_SECONDS", 0.2):
                try:
                    self.main.toggle(self.config, "codex", False)
                except self.main.Busy:
                    attempts.append("busy")
        self.choose(self.answer(claude=True, codex=True, copilot=True), side_effect=toggle_meanwhile)
        self.assertEqual(attempts, ["busy"])
        self.assertEqual([p["enabled"] for p in json.loads(self.read_config())["providers"]][:2], [True, True])

    def read_config(self):
        with open(self.config) as f:
            return f.read()

    def test_missing_config_file_starts_from_what_the_menu_showed(self):
        os.remove(self.config)
        example = os.path.join(self.dir, "example.json")
        with open(example, "w") as f:
            json.dump({"providers": [{"name": n, "enabled": n != "foundry"} for n in NAMES]}, f)
        with mock.patch.object(self.main, "EXAMPLE", example):
            text = self.choose(self.answer(claude=True, codex=True, gemini=False, copilot=True, foundry=False))
        self.assertEqual([p["enabled"] for p in json.loads(text)["providers"]], [True, True, False, True, False])
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)
        self.assertEqual(self.shown[0][2][2], True)  # the panel showed the example's state


@unittest.skipUnless("--gui" in sys.argv, "opens a window: run with --gui")
class PanelOnScreen(unittest.TestCase):
    """Opt-in (python3 -I tests/test_ai_limits.py --gui): the real panel, which closes itself after a second."""

    ROWS = [(n, render.HEADERS[n], n in ("claude", "gemini", "copilot"), render.NOT_SET_UP if n == "copilot" else "")
            for n in NAMES]

    def test_self_test_save_and_cancel(self):
        for _ in range(2):
            start = time.monotonic()
            code, out, err = panel.show(self.ROWS, "selftest-save", timeout=30)
            self.assertLess(time.monotonic() - start, 10)
            self.assertEqual((code, err), (0, ""))
            lines = out.splitlines()
            self.assertEqual([x for x in lines if x.startswith("#")], ["#active=true", "#key=true", "#visible=true"])
            states = panel.parse(0, "\n".join(x for x in lines if not x.startswith("#")), NAMES)
            # the self-test clicks the first checkbox (Claude: on -> off) and then Save
            self.assertEqual(states, {"claude": False, "codex": False, "gemini": True, "copilot": True,
                                      "foundry": False})
            code, out, err = panel.show(self.ROWS, "selftest-cancel", timeout=30)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out.splitlines()[0], "cancel")
            self.assertIsNone(panel.parse(code, out, NAMES))

    def test_choose_with_the_real_panel(self):
        real_show = panel.show

        def self_saving(rows, mode="panel", timeout=panel.TIMEOUT):
            code, out, err = real_show(rows, "selftest-save", 30)
            return code, "".join(x + "\n" for x in out.splitlines() if not x.startswith("#")), err
        with tempfile.TemporaryDirectory() as d:
            config = os.path.join(d, "config.json")
            with open(config, "w") as f:
                json.dump({"providers": [{"name": n, "enabled": True} for n in NAMES]}, f)
            main = load_main()
            with mock.patch.object(panel, "show", self_saving), contextlib.redirect_stdout(io.StringIO()):
                main.main(["--choose", "--config", config, "--cache", os.path.join(d, "cache")])
            with open(config) as f:
                enabled = [p["enabled"] for p in json.load(f)["providers"]]
        self.assertEqual(enabled, [False, True, True, True, True])  # Claude unticked by the self-test


# ---- the real plugin --------------------------------------------------------------------------------------

class Plugin(unittest.TestCase):
    """Runs the real launcher offline: every enabled provider is skipped or forced to fail."""

    def run_plugin(self, args, cache, enabled=(), config_text=None, env=None, config=None):
        """run_launcher with a temporary config unless `config` names an existing one."""
        with tempfile.TemporaryDirectory() as d:
            if config is None:
                config = os.path.join(d, "config.json")
                with open(config, "w") as f:
                    f.write(config_text if config_text is not None else
                            json.dumps({"providers": [{"name": n, "enabled": n in enabled} for n in NAMES]}))
            return run_launcher(args, config=config, cache=cache, env=env)

    def test_everything_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.run_plugin(["--skip", "claude,codex,gemini,copilot"], d,
                                enabled=("claude", "codex", "gemini", "copilot"))
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assertTrue(p.stdout.startswith("–"))
        self.assertIn("No supported AI tool found", p.stdout)
        self.assertFalse(any("Foundry" in x for x in top_level(p.stdout)))  # disabled providers have no section

    def test_failed_provider_shows_cached_values(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "copilot.json"), "w") as f:
                json.dump({"title": "GITHUB COPILOT", "status": "ok", "details": [], "note": None,
                           "time": time.time() - 3600,
                           "rows": [row("AI credits", 37.0, resets=time.time() + 86400 * 5)]}, f)
            out = self.run_plugin(["--fail", "copilot"], d, enabled=("copilot",)).stdout
        self.assertIn("Couldn't read · last read", out)
        self.assertIn("37%", out)

    def test_failed_provider_without_cache(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.run_plugin(["--fail", "copilot"], d, enabled=("copilot",)).stdout
        self.assertIn("Couldn't read", out)

    def test_bad_arguments_and_broken_config_still_give_a_menu(self):
        with tempfile.TemporaryDirectory() as d:
            bogus = self.run_plugin(["--bogus", "--skip", ALL, "-x"], d)
            broken = self.run_plugin(["--skip", ALL], d, config_text="{not json")
            missing_value = self.run_plugin(["--skip", ALL, "--config"], d)
            debug = self.run_plugin(["--skip", ALL, "--debug", "--cache"], d)
        for p in (bogus, broken, missing_value, debug):
            self.assertEqual(p.returncode, 0)
            self.assertGreater(len(p.stdout.splitlines()), 2)
        self.assertIn("All providers are off", bogus.stdout)  # its config turns every provider off
        self.assertIn("No supported AI tool found", broken.stdout)
        self.assertIn("config.json could not be read: using defaults", broken.stdout)
        self.assertIn("AI Limits Bar could not run", missing_value.stdout)
        self.assertIn("plugin/ai-limits.5m.sh --debug", missing_value.stdout)
        self.assertEqual(missing_value.stderr, "")  # nothing extra without --debug
        self.assertIn("Traceback", debug.stderr)
        self.assertIn("AI Limits Bar could not run", debug.stdout)

    def test_main_with_this_interpreter_and_empty_environment(self):
        main_py = os.path.join(ROOT, "ai_limits", "main.py")
        with tempfile.TemporaryDirectory() as d:
            config, cache = os.path.join(d, "config.json"), os.path.join(d, "cache")
            command = (sys.executable, "-B", "-I", main_py)
            ok = run_launcher([], config=config, cache=cache, env={}, command=command)
            bad = run_launcher(["--debug", "--config"], config=config, cache=cache, env={}, command=command)
        self.assertEqual((ok.returncode, ok.stderr), (0, ""))
        self.assertIn("No supported AI tool found", ok.stdout)
        self.assertEqual(bad.returncode, 0)
        self.assertIn("AI Limits Bar could not run", bad.stdout)
        self.assertIn("Traceback", bad.stderr)

    def test_toggle_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            config, cache = os.path.join(d, "config.json"), os.path.join(d, "cache")
            with open(config, "w") as f:
                json.dump({"providers": [{"name": n, "enabled": True} for n in NAMES]}, f)
            others = ",".join(n for n in NAMES if n != "gemini")
            toggled = self.run_plugin(["--toggle", "gemini"], cache, config=config)
            with open(config) as f:
                after = json.load(f)
            # --fail gemini: if the toggle had not worked, Gemini would show a failure note, not be read for real
            off = self.run_plugin(["--skip", others, "--fail", "gemini"], cache, config=config)
            self.run_plugin(["--toggle", "gemini"], cache, config=config)
            on = self.run_plugin(["--skip", others, "--fail", "gemini"], cache, config=config)
            cache_files = os.listdir(cache) if os.path.isdir(cache) else []
        self.assertEqual((toggled.returncode, toggled.stdout, toggled.stderr), (0, "", ""))
        self.assertEqual([p["enabled"] for p in after["providers"]], [n != "gemini" for n in NAMES])
        self.assertEqual(off.returncode, 0)
        self.assertFalse(any(x.startswith("Gemini") for x in top_level(off.stdout)))
        self.assertNotIn("Couldn't read", off.stdout)
        self.assertNotIn("Gemini", off.stdout)
        self.assertEqual(swiftbar_params(providers_line(off.stdout))["param3"], config)  # the panel edits this file
        self.assertIn("No supported AI tool found", off.stdout)  # the others are on, only skipped
        self.assertTrue(any(x.startswith("Gemini") for x in top_level(on.stdout)))
        self.assertIn("Couldn't read", on.stdout)
        self.assertEqual(cache_files, ["providers-state.json"])  # no provider cache: nothing was read

    def test_toggle_always_exits_0(self):
        """SwiftBar refreshes the menu after a click only if the command exits 0."""
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "settings")
            os.mkdir(folder)
            config, cache = os.path.join(folder, "config.json"), os.path.join(d, "cache")
            cases = {"evil name": (["--toggle", "evil"], '{"providers": []}'),
                     "invalid JSON": (["--toggle", "gemini"], "{not json"),
                     "missing value": (["--toggle"], '{"providers": []}')}
            if not IS_ROOT:
                cases["read-only folder"] = (["--toggle", "gemini"], '{"providers": []}')
            results = {}
            for label, (args, text) in cases.items():
                with open(config, "w") as f:
                    f.write(text)
                if label == "read-only folder":
                    os.chmod(folder, 0o500)  # the toggle fails: it can't write its temp file
                try:
                    results[label] = self.run_plugin(args, cache, config=config)
                finally:
                    os.chmod(folder, 0o700)
                with open(config) as f:
                    self.assertEqual(f.read(), text, label)  # nothing changed
        for label, p in results.items():
            self.assertEqual(p.returncode, 0, label)
        self.assertEqual(results["evil name"].stdout + results["invalid JSON"].stdout, "")
        if not IS_ROOT:
            self.assertIn("AI Limits Bar could not run", results["read-only folder"].stdout)

    def test_symlinked_launcher_finds_the_repository(self):
        """README install option: the single launcher file linked into another plugin folder."""
        with tempfile.TemporaryDirectory() as d:
            link = os.path.join(d, "plugins", "ai-limits.5m.sh")
            os.mkdir(os.path.dirname(link))
            os.symlink(PLUGIN, link)
            config, cache = os.path.join(d, "config.json"), os.path.join(d, "cache")
            with open(config, "w") as f:
                json.dump({"providers": [{"name": n, "enabled": True} for n in NAMES]}, f)
            menu = run_launcher([], config=config, cache=cache, command=(link,))
            toggled = run_launcher(["--toggle", "gemini"], config=config, cache=cache, command=(link,))
            with open(config) as f:
                after = json.load(f)
            linked_folder = sorted(os.listdir(os.path.dirname(link)))
        self.assertEqual((menu.returncode, menu.stderr), (0, ""))
        self.assertIn("No supported AI tool found", menu.stdout)
        self.assertEqual(swiftbar_params(providers_line(menu.stdout))["bash"], os.path.realpath(PLUGIN))
        self.assertEqual((toggled.returncode, toggled.stdout, toggled.stderr), (0, "", ""))
        self.assertEqual([p["enabled"] for p in after["providers"]], [n != "gemini" for n in NAMES])
        self.assertEqual(linked_folder, ["ai-limits.5m.sh"])

    def test_show_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            config, cache = os.path.join(d, "config.json"), os.path.join(d, "cache")
            with open(config, "w") as f:
                json.dump({"providers": [{"name": n, "enabled": True} for n in NAMES]}, f)
            pinned = self.run_plugin(["--show", "claude:5-hour"], cache, config=config)
            with open(config) as f:
                after = json.load(f)
            menu = self.run_plugin(["--skip", ALL], cache, config=config)
            with open(config, "rb") as f:
                before_refusal = f.read()
            refused = self.run_plugin(["--show", "claude:a|b"], cache, config=config)
            with open(config, "rb") as f:
                after_refusal = f.read()
        self.assertEqual((pinned.returncode, pinned.stdout, pinned.stderr), (0, "", ""))
        self.assertEqual(after["menu_bar"], {"provider": "claude", "limit": "5-hour"})
        lines = menu_lines(menu.stdout)
        self.assertTrue(lines[0].startswith("– | "))  # nothing was read: the pinned limit is not available
        self.assertEqual(swiftbar_params(lines[0])["tooltip"], "Claude 5-hour not available")
        self.assertIn("--Claude · 5-hour (not available) | checked=true", lines)
        self.assertEqual(refused.returncode, 0)
        self.assertIn("AI Limits Bar could not run", refused.stdout)
        self.assertEqual(after_refusal, before_refusal)

    def test_show_then_the_click_refresh_through_the_launcher(self):
        with tempfile.TemporaryDirectory() as d:
            config, cache = os.path.join(d, "config.json"), os.path.join(d, "cache")
            marker = os.path.join(cache, "keep-values.json")
            with open(config, "w") as f:
                json.dump({"providers": [{"name": n, "enabled": True} for n in NAMES]}, f)
            shown = self.run_plugin(["--show", "claude:5-hour"], cache, config=config)
            written = os.path.exists(marker)
            plain = self.run_plugin([], cache, config=config)  # a scheduled run: the marker stays
            kept = os.path.exists(marker)
            click_env = dict(os.environ, SWIFTBAR_PLUGIN_REFRESH_REASON="MenuAction")
            clicked = self.run_plugin([], cache, config=config, env=click_env)  # the launcher adds --refresh
            consumed = not os.path.exists(marker)
        self.assertEqual((shown.returncode, shown.stdout, shown.stderr), (0, "", ""))
        self.assertTrue(written and kept and consumed, (written, kept, consumed))
        for p in (plain, clicked):
            self.assertEqual((p.returncode, p.stderr), (0, ""))
            self.assertIn("--Claude · 5-hour (not available) | checked=true", menu_lines(p.stdout))

    def test_hostile_environment_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "marker")
            touch = f"/usr/bin/touch {marker}"
            fake_bin, zdot, home, pylib = (os.path.join(d, x) for x in ("bin", "zdot", "home", "pylib"))
            for folder in (fake_bin, zdot, home, pylib):
                os.mkdir(folder)
            for name in ("env", "dirname", "python3", "zsh", "sh", "bash", "cd"):
                with open(os.path.join(fake_bin, name), "w") as f:
                    f.write(f"#!/bin/sh\n{touch}\n")
                os.chmod(os.path.join(fake_bin, name), 0o755)
            for folder in (zdot, home):
                for name in (".zshenv", ".zshrc", ".zprofile", ".zlogin"):
                    with open(os.path.join(folder, name), "w") as f:
                        f.write(f"{touch}\n")
            with open(os.path.join(pylib, "sitecustomize.py"), "w") as f:
                f.write(f"import os\nos.system({touch!r})\n")
            hostile = {
                "BASH_FUNC_cd%%": f"() {{ {touch}; }}", "BASH_FUNC_dirname%%": f"() {{ {touch}; }}",
                "SHELLOPTS": "xtrace", "PS4": f"$({touch})", "ZDOTDIR": zdot, "PATH": fake_bin, "HOME": home,
                "ENV": os.path.join(zdot, ".zshenv"), "BASH_ENV": os.path.join(zdot, ".zshenv"),
                "PYTHONPATH": pylib, "PYTHONSTARTUP": os.path.join(pylib, "sitecustomize.py"),
                "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                "AI_LIMITS_CONFIG": 'x" bash=/bin/sh param1=-c x="', "AI_LIMITS_TOGGLE": "gemini",
                "TOGGLE": "gemini", "param1": "--choose", "param2": "gemini", "AI_LIMITS_CHOOSE": "1",
                "SWIFTBAR_PLUGIN_REFRESH_REASON": "MenuAction",
            }
            config = os.path.join(d, "config.json")
            with open(config, "w") as f:
                json.dump({"providers": [{"name": n, "enabled": True} for n in NAMES]}, f)
            with open(config, "rb") as f:
                before = f.read()
            p = self.run_plugin(["--skip", ALL], os.path.join(d, "cache"), env=hostile, config=config)
            touched = os.path.exists(marker)
            with open(config, "rb") as f:
                after = f.read()
        self.assertFalse(touched)
        self.assertEqual(after, before)  # the environment never toggles anything
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assertIn("No supported AI tool found", p.stdout)
        lines = menu_lines(p.stdout)
        self.assertFalse(any("bash=/bin/sh" in x for x in lines))
        commands = {swiftbar_params(x)["bash"] for x in lines if "bash" in swiftbar_params(x)}
        self.assertEqual(commands, {"/usr/bin/open", os.path.realpath(PLUGIN)})

    def test_plugin_folder_holds_only_the_launcher(self):
        names = os.listdir(os.path.join(ROOT, "plugin"))
        self.assertNotIn("__pycache__", names)
        self.assertEqual([x for x in names if not x.startswith(".")], ["ai-limits.5m.sh"])  # .DS_Store is fine


if __name__ == "__main__":
    if "--gui" in sys.argv:
        sys.argv.remove("--gui")
    unittest.main()
