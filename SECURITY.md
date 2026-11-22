# Security policy

AI Limits Bar runs as your user every few minutes and handles sign-ins to several AI services, so it is built to do as little as possible with them.

## Report a vulnerability privately

Use GitHub's private vulnerability reporting: the repository's **Security** tab, then **Report a vulnerability** ([direct link](https://github.com/uzuntasgokberk/ai-limits-bar/security/advisories/new)). Do not open a public issue, discussion or pull request for a security problem.

Include:

- your macOS, SwiftBar and Python versions (the Python the launcher uses: the first of `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` and `/usr/bin/python3` that exists);
- the provider concerned, if any, and its tool's version;
- the smallest reproduction, with fake values: the file and line, what an attacker controls, and what happens.

**Never send secrets or data**, in a report or anywhere else: no tokens or other credentials, no `config.json`, no files from `~/Library/Caches/ai-limits-bar/`, no account, tenant or subscription ids, no resource or deployment names. Reproduce with fake values; if a real credential was exposed, sign out and in again with the provider's own tool first.

This is a personal project maintained on a best-effort basis. Fixes go to the latest `main` only; update with `git -C ~/ai-limits-bar pull`.

## What counts

- A credential is written to disk, logged, printed, put in the cache or a process argument, or sent anywhere but its own provider.
- A write endpoint or method is called, i.e. anything that changes an account (for example using a limit reset).
- External text (a provider's answer, a cache file, a setting) adds a menu action, a parameter or a whole menu line. The one deliberate exception is a limit's name in a *Menu bar shows* entry: it must pass the allow-list described under *Guarantees*, `--show` checks it again, and it can only set `menu_bar`; a name that does more counts.
- The environment or a SwiftBar link changes behaviour beyond triggering a refresh (for example turning a provider on or off, or sending a request through a proxy).
- A file is written outside the documented ones (see *Files written* below).

The documented limits are not vulnerabilities: the endpoints are unofficial and can change or break; a process running as the same user can read the same files and Keychain item this tool reads; Foundry's *Details* submenu shows deployment and invoice meter names (see *Privacy* below).

## Guarantees

- **No credential is ever refreshed, stored, logged or printed.**
  - Claude: the OAuth access token is read from Claude Code's Keychain item and travels only in the HTTPS request header to `api.anthropic.com`. If the token has expired, the menu asks you to open Claude Code; this tool never refreshes it, because a second client refreshing could sign Claude Code out.
  - Codex: no credential is read. A short-lived `codex app-server` is asked for rate limits; Codex manages its own session.
  - Gemini (Antigravity): no Google credential is read. The local server's port and CSRF token are read from its 0600 file, and the request goes only to `127.0.0.1`, never through a proxy. The file must be a regular file owned by you that others can't read; anything else (a symlink, a world-readable file) is ignored. The Gemini CLI fallback uses its saved token only while it is still valid, and never refreshes it.
  - GitHub Copilot and Microsoft Foundry: the GitHub CLI and Azure CLI own their credentials.
- **Read-only towards your accounts.** The only thing this tool changes is its own settings file, `config.json`, when you turn a provider on or off or choose what the menu bar shows.
  - Every message to Codex goes through an allow-list of two requests (`initialize`, `account/rateLimits/read`) and the `initialized` notification, so methods such as the one that consumes a reset credit cannot be sent.
  - Claude's reset endpoint is never called.
- **Environment.**
  - SwiftBar copies the query parameters of a refresh link into the plugin's environment, and any web page or app can open such a link. The plugin therefore does not trust its environment.
  - Neither its regular runs nor the menu's click commands go through a login shell: with `runInBash` off, SwiftBar starts the file directly with its arguments. The launcher runs under `zsh -f`, which reads no startup files and imports no functions; it uses only shell builtins and starts Python with an empty environment (`env -i`) in isolated mode (`-I`). The only thing passed on is whether *Refresh now* was clicked. The click commands of *Providers…* and *Menu bar shows* get the same plugin environment, so the same rules apply to them.
  - Python and the tools it starts (`security`, `codex`, `gh`, `az`) get a fixed environment: the home folder from the user database, a fixed `PATH` and locale. Proxies, `HOME`, certificate settings and the like are never inherited.
  - Switches are command-line arguments, which a link cannot set. SwiftBar passes none on its regular runs; the only ones it ever passes come from this plugin's own *Providers…* and *Menu bar shows* items (`--choose`, or `--show highest` / `--show PROVIDER:LIMIT`, plus `--config` if one was given). A refresh still uses values read less than a minute ago, and the one right after a *Menu bar shows* click uses the values already read.
- **Subprocesses** run without a shell, with fixed arguments and absolute paths.
  - The *Providers…* window is a fixed script (`panel.py`) run by `/usr/bin/osascript`. Nothing is inserted into the script: provider names, labels and states are passed as arguments. Its output is parsed strictly (each of the five providers exactly once, `on` or `off`); anything else, Cancel, an error or the 10-minute timeout changes nothing.
  - Azure names from `config.json` are validated, because `az` expands `@file` arguments.
- **Network.**
  - HTTPS only for remote services.
  - Requests to `127.0.0.1` bypass every proxy (environment variables and macOS system settings).
  - Redirects are never followed (urllib would forward the `Authorization` header).
  - Response bodies are capped at 1 MB. Codex's output is capped at 5 MB per exchange, under one deadline.
  - Error messages are replaced with fixed text, so headers can't leak through exceptions.
- **Menu output.**
  - SwiftBar turns ` | key=value` into actions (`bash=`, `href=` …), `--` into submenus and `---` into separators. Every external string therefore has `|` replaced and control, format and line-separator characters removed (including U+2028/U+2029/U+0085), runs of dashes collapsed, and leading dashes and combining marks removed.
  - The tooltip is quoted with quotes, backslashes and `=` removed.
  - The settings path of *Open settings* is quoted. A relative path, or one containing a quote, a backslash or a control character, is not shown at all.
  - Click targets are fixed in `render.py`: the provider usage pages, `open` on the settings file, and, for *Providers…* and *Menu bar shows*, this plugin's own launcher with `--choose` or `--show`. A *Menu bar shows* entry carries a limit's name from a provider's answer only if the name passes a strict allow-list (an ASCII letter or digit, then also spaces, `.`, `_` and `-`, at most 30 characters, so no quote, backslash, colon or shell character); otherwise the entry has no action. `--show` accepts only a known provider and a name that passes the same allow-list, and refuses anything else before it opens `config.json`. The launcher path is quoted with the same rules as the settings path and must not contain shell characters (`' $ ; & | < >` or a backtick), in case SwiftBar's shell wrapper is ever used; if it (or a `--config` path passed along) would need escaping, the item is shown without an action.
- **Robustness.**
  - Provider results and cache files are normalised before use: NaN, text dates, missing fields and out-of-range values are dropped.
  - Each section is rendered independently, and a top-level handler prints a fixed fallback menu. A broken value can't blank the menu or show a traceback with your paths; only `--debug` prints the traceback, to the terminal.
- **Files read.**
  - Claude: the Keychain item `Claude Code-credentials`; as a fallback, `~/.claude/stats-cache.json`.
  - Codex: as a fallback, the newest `~/.codex/sessions/**/rollout-*.jsonl` (only its last 2 MB).
  - Gemini: Antigravity's `~/.gemini/antigravity/daemon/hub_*.json`; as a fallback, `~/.gemini/oauth_creds.json`.
  - `config.json` (or `config.example.json`).
- **Files written.**
  - `config.json`: created from `config.example.json` on the first run (0600), and rewritten when providers are turned on or off (the *Providers…* window or `--toggle`) or the menu bar choice changes (`--show`). That is the only file such a change writes: in one atomic write (temp file in the same folder, then rename), under a lock, with its mode kept and a symlink kept as a symlink. A file that is not valid JSON is left untouched. Turning a provider off doesn't touch its cache; an off provider is not read at all.
  - The cache lives in `~/Library/Caches/ai-limits-bar/`. The folder is 0700 and the files are 0600, written atomically with `mkstemp`. A symlinked cache folder is refused.
  - The cache holds numbers, times and labels, including Foundry deployment and invoice meter names (see *Privacy*). `foundry-history.json` holds per-item amounts and per-deployment token totals only. `providers-state.json` holds the names of providers the last menu update found not set up on this Mac, for the window's notes. `keep-values.json` holds only a time: `--show` writes it so the refresh SwiftBar runs right after a *Menu bar shows* click uses the values already read. It counts only if it is at most 10 seconds old, the next forced refresh deletes it whether it counted or not, and all it can do is skip that one re-read. A link cannot create it: only `--show`, a command-line argument, writes it.
  - *Refresh now* only asks SwiftBar to re-run the plugin (`refresh=true`); it runs no command.
  - No log files.
- **Privacy.** The menu never shows email, username, tenant or subscription id. Foundry's *Details* submenu lists the model deployment names you chose in Azure and the invoice meter names (models and services); these are also stored in the cache. Nothing else from the Azure account (no subscription id, tenant, resource or resource-group name) is shown or stored.
