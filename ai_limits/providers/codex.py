"""Codex (ChatGPT subscription): 5-hour and weekly limits.

Source: Codex's own `app-server` interface (JSON-RPC over stdio). A short-lived `codex app-server` is started,
sent the `initialize` request, the `initialized` notification and then only the `account/rateLimits/read`
request, and stopped. Codex manages (and if needed renews) its own session; this module never sees, reads or
writes a Codex token. Every message is built by `message()`, which allows exactly these two requests and one
notification, so methods that change anything, such as `account/rateLimitResetCredit/consume`, can never be sent.

Why not ~/.codex/auth.json directly: the ChatGPT/Codex desktop app does not renew that file, so a direct HTTP
call failed with 401 even while the app was open. The app-server answers in 1-2 seconds.
Fallback: the last `rate_limits` record in the newest `~/.codex/sessions/**/rollout-*.jsonl`.
"""
import glob
import json
import os
import select
import subprocess
import time

from ai_limits.common import ENV, NotConfigured, SessionExpired, find_tool, result, row, to_epoch

SESSIONS = os.path.expanduser("~/.codex/sessions")
# Read-only allow-list: nothing else can be sent.
REQUESTS = {"initialize", "account/rateLimits/read"}
NOTIFICATIONS = {"initialized"}
MAX_LINE = 2_000_000   # one message from codex
MAX_TOTAL = 5_000_000  # everything codex may print during one exchange


def window_name(minutes):
    if not minutes:
        return "Window"
    if abs(minutes - 300) < 2:
        return "5-hour"
    if abs(minutes - 10080) < 60:
        return "Weekly"
    return f"{minutes / 60:.0f}-hour" if minutes < 2880 else f"{minutes / 1440:.0f}-day"


def window_kind(minutes):
    """The row's machine-readable window (common.row): "5h", "weekly", or None for any other length."""
    name = window_name(minutes) if minutes else None
    return {"5-hour": "5h", "Weekly": "weekly"}.get(name)


def message(method, msg_id=None, params=None):
    """One JSON-RPC line: a request (with an id) from `REQUESTS` or a notification (no id) from
    `NOTIFICATIONS`. Anything else raises, before a single byte is written."""
    if msg_id is None:
        if method not in NOTIFICATIONS:
            raise ValueError("notification not allowed")
        msg = {"jsonrpc": "2.0", "method": method}
    else:
        if method not in REQUESTS:
            raise ValueError("method not allowed")
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
    return (json.dumps(msg) + "\n").encode()


class Channel:
    """JSON-RPC over two raw pipe descriptors, with one deadline and one byte budget for the whole exchange.
    After select() the bytes are read with os.read() into this object's own buffer: a buffered readline() could
    hold a complete line that select() no longer reports, and would block on a partial line past the deadline."""

    def __init__(self, read_fd, write_fd, deadline, max_line=MAX_LINE, max_total=MAX_TOTAL):
        self.read_fd, self.write_fd, self.deadline = read_fd, write_fd, deadline
        self.max_line, self.max_total = max_line, max_total
        self.buffer, self.received = b"", 0

    def _left(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise RuntimeError("codex did not answer in time")
        return left

    def send(self, method, msg_id=None, params=None):
        """The only way anything is written to codex."""
        data = message(method, msg_id, params)
        while data:
            _, writable, _ = select.select([], [self.write_fd], [], self._left())
            if writable:
                data = data[os.write(self.write_fd, data):]

    def wait(self, msg_id):
        """The response with this id; other messages (notifications, non-JSON lines) are skipped."""
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if len(line) > self.max_line:
                    raise RuntimeError("codex message too long")
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("id") == msg_id:
                    return d
            if len(self.buffer) > self.max_line:
                raise RuntimeError("codex message too long")
            readable, _, _ = select.select([self.read_fd], [], [], self._left())
            if not readable:
                continue
            chunk = os.read(self.read_fd, 65536)
            if not chunk:
                raise RuntimeError("codex closed its output")
            self.received += len(chunk)
            if self.received > self.max_total:
                raise RuntimeError("codex printed too much")
            self.buffer += chunk


def _app_server(timeout=12):
    codex = find_tool("codex")
    if not codex:
        raise NotConfigured("codex not installed")
    p = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, bufsize=0, env=ENV)
    try:
        channel = Channel(p.stdout.fileno(), p.stdin.fileno(), time.monotonic() + timeout)
        channel.send("initialize", 1, {"clientInfo": {"name": "ai-limits-bar", "title": "AI Limits Bar",
                                                      "version": "1.0"}})
        channel.wait(1)
        channel.send("initialized")
        channel.send("account/rateLimits/read", 2)
        d = channel.wait(2)
    finally:
        p.kill()
        p.wait(timeout=5)
        p.stdin.close()
        p.stdout.close()
    if "error" in d:
        error = d.get("error")
        text = str(error.get("message", "") if isinstance(error, dict) else error or "").lower()
        if any(s in text for s in ("auth", "login", "token", "401")):
            raise SessionExpired("not signed in")
        raise RuntimeError("codex returned an error")
    return d.get("result") if isinstance(d.get("result"), dict) else {}


def parse(r):
    """-> (rows, details)."""
    rl = r.get("rateLimits") if isinstance(r.get("rateLimits"), dict) else {}
    rows = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict) and w.get("usedPercent") is not None:
            rows.append(row(window_name(w.get("windowDurationMins")), percent=float(w["usedPercent"]),
                            resets=to_epoch(w.get("resetsAt")), window=window_kind(w.get("windowDurationMins"))))
    if not rows:
        raise RuntimeError("expected fields missing")
    details = []
    if rl.get("planType"):
        details.append(f"Plan: {str(rl['planType'])[:20]}")
    credits = rl.get("credits") if isinstance(rl.get("credits"), dict) else {}
    if credits.get("hasCredits"):
        details.append(f"Credit balance: {str(credits.get('balance'))[:12]}")
    if rl.get("rateLimitReachedType"):
        details.append("Limit reached")
    return rows, details


def _find_rate_limits(x):
    if isinstance(x, dict):
        if isinstance(x.get("rate_limits"), dict):
            return x["rate_limits"]
        for v in x.values():
            found = _find_rate_limits(v)
            if found:
                return found
    return None


def _fallback():
    """Last rate_limits record of the newest Codex session log (only its last ~2 MB is read)."""
    files = glob.glob(os.path.join(SESSIONS, "*", "*", "*", "rollout-*.jsonl"))
    if not files:
        return None
    path = max(files, key=os.path.getmtime)
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 2_000_000))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            rl = _find_rate_limits(json.loads(line))
        except ValueError:
            continue
        rows = []
        for key in ("primary", "secondary"):
            w = (rl or {}).get(key)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                rows.append(row(window_name(w.get("window_minutes")), percent=float(w["used_percent"]),
                                resets=to_epoch(w.get("resets_at")), window=window_kind(w.get("window_minutes"))))
        if rows:
            r = result("CODEX (GPT)", rows=rows)
            r["time"] = os.path.getmtime(path)
            return r
    return None


def read(settings):
    try:
        rows, details = parse(_app_server())
    except SessionExpired:
        r = _fallback() or result("CODEX (GPT)", status="unknown")
        r["status"] = "expired"
        r["note"] = "Not signed in: run codex login"
        return r
    return result("CODEX (GPT)", rows=rows, details=details)
