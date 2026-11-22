"""Entry point run by SwiftBar through plugin/ai-limits.5m.sh (every 5 minutes; the `5m` in the name).

Flow: config.json -> enabled providers in order -> use the cache if it is fresh, otherwise read the provider
in its own thread with its own timeout -> cache successful reads -> render.menu().
- A provider that is off (`enabled` not true, or no entry) is not read at all: no process, no request, no cache
  read. It only appears, unticked, in the "Providers…" panel (--choose).
- NotConfigured: the provider is not on this Mac, its section is hidden (the panel says why, from the names saved
  in providers-state.json by the last menu run).
- Unavailable / any error: the last cached values are shown with a short note; no cache -> note only
  (or hidden for Unavailable).

Nothing is taken from the environment: SwiftBar copies the query parameters of a refresh link, which any web page
or app can open, into it. It is replaced by a fixed one (common.ENV) before anything else runs, and every switch
is a command-line argument, which a link can't set (SwiftBar passes none, except the --choose, --show and --config
that this plugin's own "Providers…" and "Menu bar shows" items ask for):
  --refresh      read every provider again. "Refresh now" is a plain SwiftBar `refresh=true` item; the launcher
                 adds --refresh when SwiftBar reports that click. A cache younger than 60 seconds is still used
                 (a refresh can also be triggered by a link), the cache stays the fallback, and the menu bar keeps
                 the previous values until the new ones are ready. Right after a --show (see keep_values), that one
                 run shows the new choice from the cached values, whatever their age, and reads only providers that
                 have none (and those forced to fail with --fail).
  --config PATH  read this file instead of config.json (config.json is then not created from the example)
  --cache DIR    cache folder instead of ~/Library/Caches/ai-limits-bar
  --skip a,b     skip these providers for this run
  --fail a,b     make these providers fail (to check that their cached values stay visible)
  --debug        print the traceback to stderr if the menu could not be built at all
  --choose       show the "Providers…" panel (checkboxes; Save writes them all at once) and print nothing. SwiftBar
                 waits for it, then re-runs the plugin, but only after exit code 0, which safe_main guarantees.
                 A second --choose while a panel is open exits at once.
  --toggle NAME  turn one provider on or off in the config file and print nothing (for the terminal).
  --show highest|PROVIDER:LIMIT
                 set what the menu bar number shows (`menu_bar` in the config file) and print nothing: the highest
                 current percentage, or one limit, e.g. `claude:5-hour`. A "Menu bar shows" click runs it; the value
                 is checked again here (a known provider; a limit from render.CHOICE_LABEL), anything else is refused.
                 It then leaves a marker in the cache folder so the refresh SwiftBar starts after the click does not
                 re-read the providers (choosing a number needs no new data).
                 With --choose, --toggle or --show, the other switches except --config (and --cache for --choose)
                 are ignored.
"""
import argparse
import contextlib
import fcntl
import importlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

# Second layer behind the launcher's `env -i`: whatever environment this process was started with is dropped
# before any project module is imported (common.py builds its HTTP openers at import time, and urllib reads
# http_proxy and friends then; provider modules resolve `~`), and replaced by the fixed one.
os.environ.clear()

from ai_limits import panel, render  # noqa: E402
from ai_limits.common import ENV, HOME, NotConfigured, Unavailable, write_private_json  # noqa: E402

os.environ.update(ENV)

from ai_limits.providers import NAMES  # noqa: E402

CONFIG = os.path.join(ROOT, "config.json")
EXAMPLE = os.path.join(ROOT, "config.example.json")
LAUNCHER = os.path.join(ROOT, "plugin", "ai-limits.5m.sh")  # run by the "Providers…" item with --choose
LOCK_SECONDS = 3.0  # --toggle waits at most this long for another change (or an open panel) to finish
STATE_FILE = "providers-state.json"  # in the cache folder: providers the last menu run found not set up
STATE_MAX_AGE = 3600.0  # seconds; an older state file gives no "not set up" notes
KEEP_FILE = "keep-values.json"  # in the cache folder: written by --show, lets the next --refresh run skip re-reading
KEEP_SECONDS = 10.0  # an older marker counts for nothing
CACHE = os.path.join(HOME, "Library", "Caches", "ai-limits-bar")
VALID_NAME = re.compile(r"[a-z0-9_]{1,20}")
REFRESH_FLOOR = 60.0  # seconds: even a forced refresh uses a cache this young


class ArgumentError(Exception):
    """A malformed argument. Raised instead of argparse's exit, so the fallback menu is shown."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message)


def parse_args(argv):
    """Known switches; unknown arguments are ignored, a malformed known one raises ArgumentError."""
    parser = _Parser(prog="ai-limits.5m.sh", add_help=False, allow_abbrev=False)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--cache")
    parser.add_argument("--skip", default="")
    parser.add_argument("--fail", default="")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--toggle")
    parser.add_argument("--choose", action="store_true")
    parser.add_argument("--show")
    args, _ = parser.parse_known_args(argv)
    if args.config == "" or args.cache == "":
        parser.error("empty path")
    if args.show == "":
        parser.error("--show takes highest or PROVIDER:LIMIT")
    return args


def _names(text):
    return set(filter(None, text.split(",")))


def valid_name(name):
    return isinstance(name, str) and VALID_NAME.fullmatch(name) is not None and name in NAMES


def load_config(path, create):
    """(settings, could_not_be_read). config.json (personal, git-ignored) is created from config.example.json on
    first run (mode 0600) when `create` is set. A file that exists but is not a JSON object falls back to the
    example, and the menu says so (otherwise a typo would silently hide Foundry)."""
    if create and not os.path.exists(path) and os.path.exists(EXAMPLE):
        with contextlib.suppress(OSError):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f, open(EXAMPLE, encoding="utf-8") as src:
                shutil.copyfileobj(src, f)
    unreadable = False
    for candidate in (path, EXAMPLE):
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            return data, unreadable
        unreadable = unreadable or candidate == path
    return {"providers": [{"name": n, "enabled": n != "foundry"} for n in NAMES]}, unreadable


def provider_entries(config):
    """{name: settings} with the FIRST entry of each valid provider name, in config order. The read loop, the
    panel and --toggle all use this, so a duplicate entry can't make them disagree."""
    providers = config.get("providers")
    entries = {}
    for p in providers if isinstance(providers, list) else []:
        name = p.get("name") if isinstance(p, dict) else None
        if valid_name(name) and name not in entries:
            entries[name] = p
    return entries


def config_text(data, ascii_only=False):
    """The config file in the style of config.example.json: 2-space top level, each `providers` entry on its own
    line as compact JSON, other keys as plain json.dumps. NaN and Infinity are written back as they were read."""
    def dump(x):
        return json.dumps(x, ensure_ascii=ascii_only)

    parts = []
    for key, value in data.items():
        if key == "providers" and isinstance(value, list) and value:
            entries = ",\n".join(f"    {dump(p)}" for p in value)
            parts.append(f"  {dump(key)}: [\n{entries}\n  ]")
        else:
            parts.append(f"  {dump(key)}: {dump(value)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


class Busy(RuntimeError):
    """Another change to the config file (or an open panel) holds the lock."""


@contextlib.contextmanager
def _folder_lock(folder, seconds):
    """Exclusive lock on the config's folder, held while the file is read, changed and written (and while the
    panel is open), so two changes can't both start from the old file. Never waits forever: SwiftBar waits for a
    click's command before it rebuilds the menu."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        deadline = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("config file is busy") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # releases the lock


def _write_atomic(target, payload, mode):
    """Temp file in the same folder (mkstemp: 0600, O_EXCL), then `mode`, then os.replace: a reader sees the old
    or the new file, never a half one. Not write_private_json: that would chmod the repository folder to 0700."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=f".{os.path.basename(target)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _change_config_locked(path, create, change):
    """Loads the config (load_config), lets change(data) edit it in place and say whether anything changed, then
    writes it in one atomic write. The caller holds the lock. An unreadable file or no change: nothing is written.
    Returns whether the file was written."""
    data, unreadable = load_config(path, create)
    if unreadable or not change(data):
        return False
    try:
        payload = config_text(data).encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate (from a \ud800 escape) can only be written escaped
        payload = config_text(data, ascii_only=True).encode("utf-8")
    target = os.path.realpath(path)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = 0o600
    _write_atomic(target, payload, mode)
    return True


def _set_enabled_locked(path, changes, create):
    """set_enabled without taking the lock (the caller holds it). Returns whether the file was written."""
    def change(data):
        if not isinstance(data.get("providers"), list):
            data["providers"] = []
        entries = provider_entries(data)
        changed = False
        for name, on in changes.items():
            if not valid_name(name):
                continue
            entry = entries.get(name)
            current = entry is not None and entry.get("enabled") is True
            on = (not current) if on is None else on is True
            if on == current:
                continue
            if entry is None:
                entry = {"name": name, "enabled": True}
                data["providers"].append(entry)
                entries[name] = entry
            else:
                entry["enabled"] = on
            changed = True
        return changed
    return _change_config_locked(path, create, change)


def set_enabled(path, changes, create, lock_seconds=None):
    """Turns providers on or off in ONE atomic write. changes: {name: True | False | None (= flip)}; invalid names
    are ignored; on for a provider without an entry appends `{"name": NAME, "enabled": true}`, off for one without
    an entry changes nothing; no `providers` list -> one is created. It starts from what the menu was drawn from
    (load_config: the file, or the example while there is no file). A file that exists but is not a JSON object is
    left untouched (the menu shows the config error line). Every other key and value keeps its place. A symlinked
    config is resolved first, so the link stays; the file keeps its mode (a new one gets 0600). Nothing is written
    if nothing changes. Raises Busy if the lock can't be had in time. Returns whether the file was written."""
    target = os.path.realpath(path)
    with _folder_lock(os.path.dirname(target), LOCK_SECONDS if lock_seconds is None else lock_seconds):
        return _set_enabled_locked(path, changes, create)


def toggle(path, name, create):
    """--toggle NAME: flips one provider (true -> false, anything else -> true). See set_enabled."""
    return valid_name(name) and set_enabled(path, {name: None}, create)


def parse_choice(value):
    """--show value -> "highest" or {"provider", "limit"}. The provider must be a known name and the limit must pass
    render.CHOICE_LABEL (the same allow-list the menu uses before it puts a label into a click); anything else is
    refused with ArgumentError, before the config file is opened."""
    if value == "highest":
        return "highest"
    provider, colon, limit = value.partition(":")
    if not colon or not valid_name(provider) or not render.CHOICE_LABEL.fullmatch(limit):
        raise ArgumentError("--show takes highest or PROVIDER:LIMIT")
    return {"provider": provider, "limit": limit}


def set_menu_bar(path, value, create):
    """--show: writes `menu_bar` with the same lock and writer as set_enabled (unreadable file: no write). A missing
    key already means highest, so setting highest then writes nothing."""
    def change(data):
        if data.get("menu_bar") == value or (value == "highest" and "menu_bar" not in data):
            return False
        data["menu_bar"] = value
        return True
    target = os.path.realpath(path)
    with _folder_lock(os.path.dirname(target), LOCK_SECONDS):
        return _change_config_locked(path, create, change)


def mark_keep_values(cache):
    """After --show: SwiftBar re-runs the plugin as a menu-click refresh, which the launcher turns into --refresh,
    but a new menu bar choice needs no new data. {"time": now} in the cache folder makes that one run use the values
    already read. An error here only means the refresh is forced, as before."""
    with contextlib.suppress(Exception):
        write_private_json(cache, KEEP_FILE, {"time": time.time()})


def keep_values(cache):
    """For a --refresh run: True if a --show just left a valid marker (a regular file holding a JSON object whose
    time is a finite number at most KEEP_SECONDS old). This run then shows every provider that has a cached result
    from that cache, whatever its age, and reads only providers without one, not forced. Whenever a --refresh run looks,
    the marker is removed, valid or not (a symlinked one is not followed, just removed). The marker can only skip
    this one re-read; anything unexpected means False, i.e. a forced refresh."""
    path = os.path.join(cache, KEEP_FILE)
    valid = False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                data = json.loads(f.read(4096))
                t = data.get("time") if isinstance(data, dict) else None
                if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t):
                    valid = 0 <= time.time() - t <= KEEP_SECONDS
    except Exception:
        valid = False  # missing, a symlink (not followed), unreadable or not JSON
    with contextlib.suppress(OSError):
        if os.path.lexists(path):
            os.remove(path)
    return valid


def save_state(cache, not_set_up):
    """Names the menu run found not set up on this Mac, for the panel's notes (--choose reads no provider)."""
    write_private_json(cache, STATE_FILE, {"not_set_up": sorted(not_set_up), "time": time.time()})


def load_not_set_up(cache):
    """The names from the last menu run's state file; nothing if it is missing, unreadable or older than an hour."""
    try:
        with open(os.path.join(cache, STATE_FILE), encoding="utf-8") as f:
            state = json.load(f)
        age = time.time() - _number(state.get("time"), 0, 0, 4102444800)
        names = state.get("not_set_up")
        if not -60 <= age <= STATE_MAX_AGE or not isinstance(names, list):
            return set()
        return {n for n in names if valid_name(n)}
    except Exception:
        return set()


def choose(path, cache, create):
    """--choose: the "Providers…" panel. The config folder's lock is taken at once (a second panel exits without
    doing anything) and held until the panel is closed, then Save is applied with set_enabled's writer in one write.
    An unreadable config, Cancel, a timeout or any unexpected panel output changes nothing. Returns whether the file
    was written."""
    target = os.path.realpath(path)
    try:
        with _folder_lock(os.path.dirname(target), 0):
            config, unreadable = load_config(path, create)
            if unreadable:
                return False
            entries = provider_entries(config)
            not_set_up = load_not_set_up(cache)
            rows = []
            for name in NAMES:
                on = entries.get(name, {}).get("enabled") is True
                rows.append((name, render.HEADERS[name], on, render.NOT_SET_UP if on and name in not_set_up else ""))
            code, out, _ = panel.show(rows)
            chosen = panel.parse(code, out, NAMES)
            if chosen is None:
                return False
            return _set_enabled_locked(path, chosen, create)
    except Busy:
        return False  # a panel is already open (or a --toggle is running)


def cache_read(folder, name):
    try:
        with open(os.path.join(folder, f"{name}.json"), encoding="utf-8") as f:
            return render.normalize(json.load(f))
    except Exception:
        return None


def cache_write(folder, name, r):
    write_private_json(folder, f"{name}.json", r)


def _number(x, default, low, high):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return default
    return min(max(float(x), low), high)


def call(name, settings, forced_failures):
    if name in forced_failures:
        raise RuntimeError("test")
    return importlib.import_module(f"ai_limits.providers.{name}").read(settings)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config_path = os.path.abspath(args.config) if args.config else CONFIG
    cache = os.path.abspath(args.cache) if args.cache else CACHE
    if args.choose:
        choose(config_path, cache, create=not args.config)
        return  # nothing is printed: the click's refresh=true re-runs the plugin for the new menu
    if args.toggle is not None:
        toggle(config_path, args.toggle, create=not args.config)
        return
    if args.show is not None:
        set_menu_bar(config_path, parse_choice(args.show), create=not args.config)  # may refuse or raise Busy
        # Also when nothing changed or config.json is unreadable. SwiftBar's own runs and clicks pass no --cache, so
        # the click and its refresh both use the default cache folder; --cache only matters for tests and terminals.
        mark_keep_values(cache)
        return  # nothing is printed: the click's refresh=true re-runs the plugin for the new menu
    config, config_error = load_config(config_path, create=not args.config)
    force, forced, skip = args.refresh, _names(args.fail), _names(args.skip)
    keep = False  # right after a --show: show the new choice from the cached values, whatever their age
    if force:
        try:
            keep = keep_values(cache)
        except Exception:
            keep = False
        force = not keep
    debug_shapes = config.get("debug_shapes") is True

    now = time.time()
    order, results, jobs, missing = [], {}, {}, set()
    entries = provider_entries(config)
    pool = ThreadPoolExecutor(max_workers=5)
    for name, p in entries.items():
        if p.get("enabled") is not True or name in skip:
            continue  # off: not read, no cache read, no section
        order.append(name)
        settings = dict(p, debug_shapes=debug_shapes, cache_dir=cache, force=force)
        cached = cache_read(cache, name)
        fresh_for = max(REFRESH_FLOOR, _number(p.get("refresh_minutes"), 5, 1, 1440) * 60 - 30)
        if force:
            fresh_for = REFRESH_FLOOR
        age = now - cached["time"] if cached and cached.get("time") else None
        if name not in forced and cached and (keep or (age is not None and 0 <= age < fresh_for)):
            results[name] = cached  # fresh enough, or kept for the menu bar choice (render marks stale percents)
        else:
            timeout = _number(p.get("timeout_seconds"), 15, 1, 120)
            jobs[name] = (pool.submit(call, name, settings, forced), now + timeout)

    for name, (future, deadline) in jobs.items():
        reason, hide_without_cache = "Couldn't read", False
        try:
            r = render.normalize(future.result(timeout=max(0.1, deadline - time.time())))
        except NotConfigured:
            results[name] = None  # not on this Mac: hidden
            missing.add(name)
            continue
        except Unavailable as e:
            r, reason, hide_without_cache = None, str(e)[:60], True  # fixed messages from our code only
        except Exception:
            r = None  # error text never reaches the menu
        if r and r["status"] in ("ok", "expired"):
            with contextlib.suppress(Exception):
                cache_write(cache, name, r)
        else:
            cached = cache_read(cache, name)
            if cached:
                last = render.clock(cached["time"]) if cached["time"] else "?"
                r = dict(cached, status="unknown", note=f"{reason} · last read {last}")
            elif hide_without_cache:
                r = None
            elif r is None:
                r = render.normalize({"title": render.NAMES.get(name, name).upper(), "status": "unknown",
                                      "note": reason})
        results[name] = r

    with contextlib.suppress(Exception):
        save_state(cache, missing)
    shown = [(n, results[n]) for n in order if results.get(n)]
    providers = [(n, entries.get(n, {}).get("enabled") is True) for n in NAMES]
    print(render.menu(shown, time.time(), True, config_path, config_error, providers, LAUNCHER,
                      config_path if args.config else None, config.get("menu_bar")))


def safe_main():
    """Whatever happens, the menu is never empty. A traceback (with the user's paths) is printed only with
    --debug, and only to stderr."""
    debug = "--debug" in sys.argv[1:]  # looked up here: parsing the arguments may be what failed
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        main()
    except (Exception, SystemExit):
        if debug:
            traceback.print_exc(file=sys.stderr)
        with contextlib.suppress(Exception):
            print(f"⚠︎ | color={render.ORANGE}\n---\nAI Limits Bar could not run | color={render.GREY}\n"
                  "Run plugin/ai-limits.5m.sh --debug in Terminal to see why")
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(0)  # exit without waiting for a stuck worker thread


if __name__ == "__main__":
    safe_main()
