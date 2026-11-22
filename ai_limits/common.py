"""Shared helpers: tool lookup, shell-free subprocesses, HTTPS/localhost JSON, time parsing, result shapes.

Security rules (also in SECURITY.md):
- Credentials are never refreshed, written to disk, cached, printed or passed as process arguments.
  They only travel in an HTTPS request header (or a localhost header for a local service's CSRF token).
- Subprocesses run without a shell, with fixed arguments and absolute paths.
- HTTPS only for remote calls; redirects are never followed; response bodies are capped at 1 MB.
- Requests to 127.0.0.1 never go through a proxy (environment or macOS system settings).
- Error text never reaches the menu; only fixed messages defined in this code (see `Unavailable`).
"""
import contextlib
import json
import math
import os
import pwd
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# The home folder comes from the passwd database, never from $HOME (see ENV below).
_USER = pwd.getpwuid(os.getuid())
HOME = _USER.pw_dir
# SwiftBar may not have Homebrew on PATH; tools are looked up in known folders and run by absolute path.
SEARCH_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", os.path.join(HOME, ".local", "bin"), "/usr/bin", "/bin"]
SECURITY = "/usr/bin/security"
MAX_BODY = 1_000_000


def clean_env():
    """The only environment this tool and its child tools (security, codex, gh, az) ever see. It is built
    from scratch and NEVER copied from os.environ: SwiftBar puts the query parameters of a
    `swiftbar://refreshplugin?...&KEY=value` link into the plugin's environment, so inherited variables
    (proxies, HOME, CODEX_HOME, certificate overrides ...) are attacker-controlled."""
    env = {"HOME": HOME, "USER": _USER.pw_name, "LOGNAME": _USER.pw_name,
           "PATH": ":".join(SEARCH_DIRS + ["/usr/sbin", "/sbin"]),
           "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    try:
        tmp = os.confstr("CS_DARWIN_USER_TEMP_DIR")  # the per-user temporary folder on macOS
        if tmp and os.path.isdir(tmp):
            env["TMPDIR"] = tmp
    except (OSError, ValueError):
        pass
    return env


ENV = clean_env()


class NotConfigured(Exception):
    """The provider is not installed or not signed in on this Mac: its section is hidden."""


class SessionExpired(Exception):
    """The saved session is no longer valid; the user has to open the provider's own tool."""


class Unavailable(Exception):
    """Temporarily unreadable. The message comes ONLY from this code (no external data) and is shown
    next to the last cached values, e.g. 'Antigravity not running'."""


def write_private_json(folder, filename, data):
    """Atomic write of a 0600 JSON file into a 0700 folder; a symlinked folder is refused."""
    if os.path.islink(folder):
        raise OSError("cache folder is a symlink")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700, follow_symlinks=False)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")  # 0600, O_EXCL
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(folder, filename))
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def read_json(path):
    """Parsed JSON or None (missing, unreadable or not JSON)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def find_tool(name):
    """Absolute path of a tool (Apple Silicon Homebrew, Intel Homebrew, ~/.local/bin, system), or None."""
    for folder in SEARCH_DIRS:
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def run(argv, timeout):
    """Run a command without a shell; returns (exit code, stdout, stderr)."""
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENV)
    return p.returncode, p.stdout, p.stderr


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are not followed: urllib would copy the Authorization header to the new URL (even http)."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def local_opener():
    """Opener for 127.0.0.1: an empty ProxyHandler replaces the default one, which would send the request (and
    its CSRF header) to an http_proxy or a macOS system proxy, whose default bypass list lacks 127.0.0.1."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


_LOCAL_OPENER = local_opener()


def _read_json(request, timeout, opener=_OPENER):
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        if code in (401, 403):
            raise SessionExpired(f"HTTP {code}") from None
        if code == 404:
            raise NotConfigured("HTTP 404") from None
        if 300 <= code < 400:
            raise RuntimeError("redirect refused") from None
        raise RuntimeError(f"HTTP {code}") from None
    except Exception:
        raise RuntimeError("request failed") from None
    if len(body) > MAX_BODY:
        raise RuntimeError("response too large")
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("response is not JSON") from None


def https_json(url, headers, timeout=10, body=None):
    """GET (or JSON POST when `body` is given). 401/403 -> SessionExpired, 404 -> NotConfigured.
    Every error is re-raised with a fixed message and no chained context, so headers never leak."""
    if not url.startswith("https://"):
        raise ValueError("https only")
    extra = {"Content-Type": "application/json"} if body is not None else {}
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "ai-limits-bar/1.0",
                                                              "Accept": "application/json", **extra, **headers})
    return _read_json(request, timeout)


def localhost_json(port, path, headers, body, timeout=8):
    """JSON POST to a service on this Mac only (127.0.0.1, never through a proxy), e.g. the Antigravity
    language server."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 or not path.startswith("/"):
        raise ValueError("invalid local address")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **headers})
    return _read_json(request, timeout, _LOCAL_OPENER)


_FRACTION = re.compile(r"(\.\d{1,6})\d*")


def to_epoch(value):
    """ISO-8601 text or an epoch number (seconds or milliseconds) -> epoch seconds; unreadable, infinite, NaN or
    too large for a float -> None."""
    if value in (None, "", 0) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value / 1000 if value > 1e12 else value)
        except OverflowError:
            return None
        return seconds if math.isfinite(seconds) else None
    text = _FRACTION.sub(r"\1", str(value).strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def result(title, status="ok", rows=None, details=None, note=None):
    """The dictionary every provider returns. It only carries numbers, times and short labels."""
    return {"title": title, "status": status, "rows": rows or [], "details": details or [],
            "note": note, "time": time.time()}


def row(label, percent=None, value=None, resets=None):
    """One menu line: a percentage bar and/or a value, and an optional reset time."""
    return {"label": label, "percent": percent, "value": value, "resets": resets}


def short_number(n):
    """1234567 -> '1.2M'."""
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{suffix}"
    return str(int(n))


_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")


def shape_key(key):
    """A dict key as it may appear in debug output: a plain field name, otherwise `<key>` (keys can be ids)."""
    return key if isinstance(key, str) and _FIELD_NAME.fullmatch(key) else "<key>"


def shape(value, depth=0, max_depth=4):
    """Structure of a JSON value with field names and value types only: dict -> keys (see `shape_key`),
    list -> first items, every other value (strings too: ids, slugs) -> its type name. Used only for optional
    debug output."""
    if isinstance(value, dict) and depth < max_depth:
        items = sorted((shape_key(k), shape(v, depth + 1)) for k, v in value.items())
        return "{" + ",".join(f"{k}={v}" for k, v in items) + "}"
    if isinstance(value, list) and depth < max_depth:
        return "[" + ";".join(shape(v, depth + 1) for v in value[:4]) + "]"
    return type(value).__name__
