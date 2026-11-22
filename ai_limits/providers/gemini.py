"""Gemini: Antigravity (`agy` CLI or the Antigravity app) model quotas; Gemini CLI as a fallback.

Primary source: the running Antigravity language server. It writes its local port and a CSRF token to
`~/.gemini/antigravity/daemon/hub_<port>.json` (mode 0600). This module only asks 127.0.0.1 for
`GetUserStatus`; it never touches the Google account token, the server manages its own session. Each model's
`quotaInfo` (remaining fraction, reset time) is read. Models sharing one pool are shown as one line.
When Antigravity is not running (or its server does not answer), the Gemini CLI fallback is tried, then the
last values are shown with a note.

Fallback: Gemini CLI (`~/.gemini/oauth_creds.json`, Code Assist quota endpoint), only while its saved token
is still valid. The token is never refreshed; an expired one is silently skipped.
"""
import glob
import json
import os
import re
import stat
import time

from ai_limits.common import (NotConfigured, SessionExpired, Unavailable, https_json, localhost_json, result, row,
                              to_epoch)

ANTIGRAVITY_DIR = os.path.expanduser("~/.gemini/antigravity")
HUB = os.path.join(ANTIGRAVITY_DIR, "daemon", "hub_*.json")
STATUS_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
META = {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}
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


def antigravity(port, csrf):
    d = localhost_json(port, STATUS_PATH, {"Connect-Protocol-Version": "1", "X-Codeium-Csrf-Token": csrf},
                       {"metadata": META})
    status = d.get("userStatus") if isinstance(d.get("userStatus"), dict) else {}
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
    tier = status.get("userTier") if isinstance(status.get("userTier"), dict) else {}
    if tier.get("name") or tier.get("id"):
        details.append(f"Plan: {str(tier.get('name') or tier.get('id'))[:40]}")
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
