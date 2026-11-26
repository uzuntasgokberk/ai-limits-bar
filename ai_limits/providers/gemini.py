"""Gemini: Antigravity (`agy` CLI or the Antigravity app) usage limits; Gemini CLI as a fallback.

Primary source: the running Antigravity language server. It writes its local port and a CSRF token to
`~/.gemini/antigravity/daemon/hub_<port>.json` (mode 0600). This module only asks 127.0.0.1, with two read
requests; it never touches the Google account token, the server manages its own session (and fetches the
numbers from Google itself):
- `RetrieveUserQuotaSummary` (body `{}`, never `forceRefresh`): the quota groups of Antigravity's Usage screen,
  e.g. Gemini models and Claude/GPT models, each with a 5-hour and a weekly window. Shown as one row per window.
- `GetUserStatus`: the plan line; and, when the summary is missing, fails or has no usable value (an older
  Antigravity, an account without it), each model's `quotaInfo` as before: models sharing one pool are one line.
Each request has its own try, so a failed summary never costs the per-model rows.
When Antigravity is not running (or neither request gives rows), the Gemini CLI fallback is tried, then the
last values are shown with a note.

Fallback: Gemini CLI (`~/.gemini/oauth_creds.json`, Code Assist quota endpoint), only while its saved token
is still valid. The token is never refreshed; an expired one is silently skipped.
"""
import glob
import json
import math
import os
import re
import stat
import time
import unicodedata

from ai_limits.common import (NotConfigured, SessionExpired, Unavailable, https_json, localhost_json, result, row,
                              to_epoch)

ANTIGRAVITY_DIR = os.path.expanduser("~/.gemini/antigravity")
HUB = os.path.join(ANTIGRAVITY_DIR, "daemon", "hub_*.json")
STATUS_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
SUMMARY_PATH = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
SUMMARY_TIMEOUT = 5  # seconds; with GetUserStatus's 8 this stays under the default 15-second provider timeout
META = {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}
# Quota summary rows: label parts from stable ids, never from the server's display texts when the id is known.
GROUPS = {"gemini": "Gemini", "3p": "Claude GPT"}  # bucketId prefix (observed: gemini-5h, 3p-weekly ...)
WINDOWS = {"5h": "5-hour", "weekly": "Weekly"}  # bucket `window` (observed: "5h", "weekly")
WINDOW_ORDER = {"5h": 0, "weekly": 1}  # per group: 5-hour first, like Claude and Codex
LABEL_DROP = re.compile(r"[^A-Za-z0-9 ._-]+")  # what render.CHOICE_LABEL does not allow
KIND = re.compile(r"[a-z0-9][a-z0-9_-]{0,15}")  # a row's `group` / `window` (render.KIND)
CODE_ASSIST = "https://cloudcode-pa.googleapis.com/v1internal"
CLI_CREDENTIALS = os.path.expanduser("~/.gemini/oauth_creds.json")
CSRF = re.compile(r"[A-Za-z0-9_.:-]{8,200}")


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0  # removed since glob() listed it


def _read_hub_file(path):
    """Parsed hub file, or None unless it is a regular file owned by this user that others can't access.
    It is opened first and checked through its descriptor (no symlink is followed, a FIFO can't block)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return None
        return json.loads(f.read(65536))


def hub():
    """(port, csrf) of a running Antigravity server, or None. Files others could write or read are not trusted."""
    for path in sorted(glob.glob(HUB), key=_mtime, reverse=True):
        try:
            h = _read_hub_file(path)
            if not isinstance(h, dict):
                continue
            pid, port, csrf = h.get("pid"), h.get("httpPort"), h.get("csrfToken")
            if not (isinstance(pid, int) and isinstance(port, int) and isinstance(csrf, str) and CSRF.fullmatch(csrf)):
                continue
            os.kill(pid, 0)  # is the process alive?
            return port, csrf
        except (OSError, ValueError):
            continue
    return None


def _short(label):
    return str(label or "model").replace(" (Thinking)", "").replace("Gemini ", "")[:22]


def _post(port, csrf, path, body, timeout=8):
    return localhost_json(port, path, {"Connect-Protocol-Version": "1", "X-Codeium-Csrf-Token": csrf}, body,
                          timeout=timeout)


def _status(port, csrf):
    """GetUserStatus -> its `userStatus` ({} if missing). Raises when the request fails."""
    d = _post(port, csrf, STATUS_PATH, {"metadata": META})
    return d.get("userStatus") if isinstance(d.get("userStatus"), dict) else {}


def _plan(status):
    tier = status.get("userTier") if isinstance(status.get("userTier"), dict) else {}
    if tier.get("name") or tier.get("id"):
        return [f"Plan: {str(tier.get('name') or tier.get('id'))[:40]}"]
    return []


def model_rows(status):
    """GetUserStatus's per-model rows (the shape before the quota summary): each model's `quotaInfo`, models sharing
    one pool (same fraction and reset time) as one line; plus the plan line."""
    models = (status.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
    pools = {}
    for m in models if isinstance(models, list) else []:
        q = m.get("quotaInfo") if isinstance(m, dict) else None
        if not isinstance(q, dict) or q.get("remainingFraction") is None:
            continue
        key = (round(float(q["remainingFraction"]), 3), q.get("resetTime"))
        pools.setdefault(key, []).append(_short(m.get("label") or m.get("modelId")))
    rows, details = [], []
    for (left, resets), names in sorted(pools.items(), key=lambda x: x[0][0]):
        if len(names) == 1:
            label = names[0]
        elif len(pools) == 1:
            label = "All models"
        else:
            label = f"{len(names)} models"
        rows.append(row(label, percent=max(0.0, min(100.0, (1 - left) * 100)), resets=to_epoch(resets)))
        if len(names) > 1:
            details.append(f"{label} share one quota: " + ", ".join(names))
    return rows, details + _plan(status)


def _fraction(x):
    """remainingFraction as a finite number, or None (missing, text, bool, NaN)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def _label_text(text, limit):
    """Text reduced to what render.CHOICE_LABEL allows: accents folded (é -> e), other characters replaced by a
    space, spaces collapsed, starting with a letter or digit, at most `limit` characters."""
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    s = " ".join(LABEL_DROP.sub(" ", folded).split()).lstrip(" ._-")
    return s[:limit].rstrip()


def _window(bucket):
    """The bucket's window ("5h", "weekly" ...): its `window`, else the last part of its bucketId."""
    w = bucket.get("window")
    if isinstance(w, str) and w:
        return w
    b = bucket.get("bucketId")
    return b.rsplit("-", 1)[1] if isinstance(b, str) and "-" in b else ""


def _group_key(bucket, window):
    """bucketId without its window: "gemini-5h" -> "gemini", "3p-weekly" -> "3p"."""
    b = bucket.get("bucketId") if isinstance(bucket.get("bucketId"), str) else ""
    if window and b.endswith("-" + window):
        return b[:-len(window) - 1]
    return b.rsplit("-", 1)[0]


def _summary_label(group, key, window, index):
    """'Gemini 5-hour', 'Claude GPT Weekly'. Unknown ids: the group's own name without a trailing 'models' (else the
    id, else 'Group N') and the window id as it is. Always passes render.CHOICE_LABEL (30 characters)."""
    name = re.sub(r"(?i)\s*\bmodels?$", "", _label_text(group.get("displayName"), 60))
    name = GROUPS.get(key) or name or _label_text(key, 20).capitalize() or f"Group {index + 1}"
    when = WINDOWS.get(window) or _label_text(window, 12)
    when = when[:1].upper() + when[1:] if when else "Limit"
    return f"{name[:29 - len(when)].rstrip()} {when}"


def _unique(label, used):
    out, n = label, 2
    while out in used:
        out = f"{label[:28 - len(str(n))].rstrip()} {n}"
        n += 1
    used.add(out)
    return out


def summary_rows(d):
    """RetrieveUserQuotaSummary -> (rows, details), or None when it is not that shape or has no usable value.
    Shape (observed Nov 2026): {response: {groups: [{displayName, description, buckets: [{bucketId, displayName,
    description, window, remainingFraction, resetTime?}]}], description}}. A window that has not started yet (no
    usage in it) came as the int 1 without resetTime; once used, as a float below 1 (e.g. 0.9894) with resetTime.
    An exhausted window (0) was not observed. A bucket without a numeric remainingFraction (e.g. only
    remainingAmount) or with `disabled` gets a detail line instead of a row: it is never shown as 0% used."""
    response = d.get("response") if isinstance(d, dict) else None
    groups = response.get("groups") if isinstance(response, dict) else None
    if not isinstance(groups, list):
        return None
    rows, details, used = [], [], set()
    for i, group in enumerate(groups):
        buckets = group.get("buckets") if isinstance(group, dict) else None
        buckets = [b for b in buckets if isinstance(b, dict)] if isinstance(buckets, list) else []
        windows = [_window(b) for b in buckets]
        for window, bucket in sorted(zip(windows, buckets), key=lambda x: WINDOW_ORDER.get(x[0], 2)):
            key = _group_key(bucket, window)
            label = _summary_label(group, key, window, i)
            fraction = _fraction(bucket.get("remainingFraction"))
            if fraction is None or bucket.get("disabled") is True:
                details.append(f"{label}: no value reported")
                continue
            rows.append(row(_unique(label, used), percent=max(0.0, min(100.0, (1 - fraction) * 100)),
                            resets=to_epoch(bucket.get("resetTime")), window=window if KIND.fullmatch(window) else None,
                            group=key if KIND.fullmatch(key) else None))
    return (rows, details) if rows else None


def antigravity(port, csrf):
    """-> (rows, details). The quota summary's rows when it has any (GetUserStatus then adds only the plan line);
    otherwise GetUserStatus's per-model rows, exactly as before. Raises only when GetUserStatus is needed for the
    rows and fails, so read() can try the Gemini CLI."""
    try:
        summary = summary_rows(_post(port, csrf, SUMMARY_PATH, {}, timeout=SUMMARY_TIMEOUT))
    except Exception:
        summary = None  # older Antigravity, account without a summary, unreadable answer: per-model rows below
    if summary is None:
        return model_rows(_status(port, csrf))
    rows, details = summary
    try:
        details = details + _plan(_status(port, csrf))
    except Exception:
        pass  # the plan line is optional; the summary's rows stay
    return rows, details


def _gemini_cli():
    """Gemini CLI fallback, only while the saved token is valid; otherwise None (no refresh)."""
    try:
        with open(CLI_CREDENTIALS, encoding="utf-8") as f:
            k = json.load(f) or {}
    except (OSError, ValueError):
        return None
    token, expires = k.get("access_token"), k.get("expiry_date")
    if not token or not expires or float(expires) / 1000 < time.time():
        return None
    headers = {"Authorization": f"Bearer {token}"}
    load = https_json(f"{CODE_ASSIST}:loadCodeAssist", headers,
                      body={"metadata": {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED",
                                         "pluginType": "GEMINI"}})
    project = load.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project = project.get("id")
    quota = https_json(f"{CODE_ASSIST}:retrieveUserQuota", headers, body={"project": project} if project else {})
    del token, headers
    return quota_rows(quota)


def quota_rows(quota):
    """Gemini CLI: buckets = [{modelId, tokenType, remainingFraction, resetTime}] -> used-percent rows."""
    rows = []
    for b in (quota.get("buckets") if isinstance(quota, dict) else None) or []:
        if not isinstance(b, dict) or b.get("remainingFraction") is None:
            continue
        label = str(b.get("modelId") or "model").replace("gemini-", "")[:14]
        if b.get("tokenType") and b["tokenType"] != "REQUESTS":
            label = f"{label} {str(b['tokenType']).lower()[:6]}"
        rows.append(row(label, percent=max(0.0, min(100.0, (1 - float(b["remainingFraction"])) * 100)),
                        resets=to_epoch(b.get("resetTime"))))
    rows.sort(key=lambda x: -(x["percent"] or 0))
    return rows


def read(settings):
    server = hub()
    if server:
        try:
            rows, details = antigravity(*server)
        except Exception:
            rows = None  # the server did not answer usefully: try the Gemini CLI instead
        if rows:
            return result("GEMINI", rows=rows[:8], details=details + ["Source: Antigravity"])
    try:
        rows = _gemini_cli()
    except SessionExpired:
        rows = None
    if rows:
        return result("GEMINI", rows=rows[:8], details=["Source: Gemini CLI"])
    if server:
        raise Unavailable("Antigravity did not answer")
    if os.path.isdir(ANTIGRAVITY_DIR):
        raise Unavailable("Antigravity not running")
    raise NotConfigured("neither Antigravity nor Gemini CLI is set up")
