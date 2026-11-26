# AI Limits Bar

See how much of your AI usage limits is left, right from the macOS menu bar: **Claude**, **Codex (ChatGPT)**, **Gemini (Antigravity)**, **GitHub Copilot** and **Microsoft Foundry** in one place.

**macOS only:** it is a SwiftBar plugin and does not run on Windows or Linux (see [Requirements](#requirements)).

The menu bar shows a gauge and a percentage: by default the highest current usage of all limits, or one limit you choose under **Menu bar shows** (for example `57%`, orange from 75%, red from 90%). Hover for a one-line summary that starts with what the number is, click for the details. Each limit has a bar, its percentage and when it resets:

```
Claude
5-hour       ██████░░░░  57%
             ↻ Mon Mar 10 14:20 · in 2h 5m
Weekly       ███░░░░░░░  31%
             ↻ Fri Mar 14 08:00 · in 3d 19h
Weekly Opus  ████░░░░░░  44%
             ↻ Fri Mar 14 08:00 · in 3d 19h
---
Codex (GPT)
5-hour       █░░░░░░░░░  12%
             ↻ Mon Mar 10 17:35 · in 5h 20m
```

A provider that isn't set up on your Mac is hidden automatically, and so are limits your plan doesn't have (for example a model-specific weekly limit). A provider you don't use at all can be turned off under **Providers…**; it is then never read.

## What it shows

| Provider | Shows | Reads from |
|---|---|---|
| **Claude** | 5-hour, weekly and model-specific weekly limits (e.g. *Weekly Opus*), this week's usage by surface | Claude Code's sign-in (macOS Keychain), the endpoint behind Claude Code's `/usage` |
| **Codex (GPT)** | 5-hour and weekly limits, plan | A short-lived `codex app-server` (Codex's own interface); this tool never touches Codex credentials |
| **Gemini** | 5-hour and weekly limits of each quota group (*Gemini 5-hour*, *Gemini Weekly*, *Claude GPT 5-hour*, *Claude GPT Weekly*), reset time, plan. Without the quota summary: model quotas (models sharing one pool are one line) | The running Antigravity (`agy`) language server on `127.0.0.1`; Gemini CLI as a fallback (model quotas only) |
| **GitHub Copilot** | Monthly AI-credit (or premium-request) budget, used / total, reset date | `gh api copilot_internal/user` (the GitHub CLI owns the token) |
| **Microsoft Foundry** | Spend today, this week, this month and all time. Details: every model and invoice item for the same four periods | Azure CLI: Azure Monitor metrics (tokens and Azure's estimated cost) and Cost Management. Off by default |

**Gemini limits.** The quota groups and their windows are the ones on Antigravity's Usage screen: Gemini models, and Claude and GPT models, each with a 5-hour and a weekly limit. They come from Antigravity's local server, which gets them from Google itself; the plugin only asks that server. A window with no reset time shows none. A limit the server lists without a value is not shown as a bar; Details says *no value reported* for it. An account or Antigravity version without this quota summary shows the per-model lines as before, and so does the Gemini CLI fallback, which has no windows. This was verified on one Google AI Pro account with all limits untouched, so how the numbers look at partly used and exhausted limits is not confirmed yet.

**Read-only.** This tool never changes anything in your accounts. For example, it can't use a limit reset: Codex messages are restricted to an allow-list of two read-only requests and one notification, and Claude's reset endpoint is never called. Do that in the official app or website. The only thing it ever changes is its own settings file, `config.json`, when you turn a provider on or off, choose what the menu bar shows or set Foundry prices.

## Requirements

- **macOS only.** It does not run on Windows or Linux: it is a SwiftBar plugin (SwiftBar is a macOS app), the Claude sign-in is read from the macOS Keychain, and the **Providers…** and **Foundry prices…** windows are native macOS panels.
- [SwiftBar](https://github.com/swiftbar/SwiftBar) (free, open source).
- Python 3.9 or newer, at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` or `/usr/bin/python3`. The Xcode Command Line Tools provide the last one (see [Install](#install)); Homebrew's Python works too.

Then, for each provider you want to see, install its tool and sign in. Only the providers you want are needed; the others are simply hidden. Do this before you expect a provider in the menu: the plugin never signs in for you.

| Provider | Install | Sign in |
|---|---|---|
| **Claude** | [Claude Code](https://docs.anthropic.com/claude-code): `brew install --cask claude-code`, or the installer in its docs | Run `claude`, then `/login` with a Claude subscription (Pro, Max or Team). An API key has no subscription limits to show. The plugin reads the "Claude Code-credentials" Keychain item, so `claude` can be installed anywhere. |
| **Codex (GPT)** | [Codex CLI](https://github.com/openai/codex): `brew install --cask codex` | `codex login` with ChatGPT |
| **Gemini** | [Antigravity](https://antigravity.google/): `brew install --cask antigravity`; fallback: Gemini CLI, `brew install gemini-cli` | Sign in to Antigravity with Google and keep it running; for the fallback, run `gemini` once and sign in with Google |
| **GitHub Copilot** | [GitHub CLI](https://cli.github.com/): `brew install gh` | `gh auth login`; the account needs a Copilot plan |
| **Microsoft Foundry** | [Azure CLI](https://learn.microsoft.com/cli/azure/): `brew install azure-cli` | `az login`, then set `resource_group` in `config.json` (see [Settings](#settings)) |

SwiftBar doesn't use your shell's `PATH`, so `codex`, `gh` and `az` are looked up in `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, `/usr/bin` and `/bin`. A tool installed somewhere else (for example through nvm) shows as *not set up on this Mac*. Install it with Homebrew, or link it into `~/.local/bin` (`mkdir -p ~/.local/bin && ln -s "$(command -v gh)" ~/.local/bin/gh`); a Node-based tool such as `codex` also needs `node` in one of those folders.

## Install

Before you start: install [Homebrew](https://brew.sh). It also installs the Xcode Command Line Tools, which provide `git` and `/usr/bin/python3`. Without Homebrew: run `xcode-select --install`, and download SwiftBar from its [releases page](https://github.com/swiftbar/SwiftBar/releases) instead of the `brew` line below.

1. Install SwiftBar and get the code:

   ```sh
   brew install --cask swiftbar
   git clone https://github.com/uzuntasgokberk/ai-limits-bar.git ~/ai-limits-bar
   ```

2. Point SwiftBar at the `plugin` folder. It contains a single file, `ai-limits.5m.sh`, which SwiftBar runs every 5 minutes (the `5m` in its name).
   - **SwiftBar not used yet:** set the folder, then start SwiftBar:

     ```sh
     defaults write com.ameba.SwiftBar PluginDirectory "$HOME/ai-limits-bar/plugin"
     open -a SwiftBar
     ```

     (If SwiftBar asks for a plugin folder on its first start instead, choose `~/ai-limits-bar/plugin`.)
   - **SwiftBar already has a plugin folder** with other plugins: keep it, and link the single file into it. SwiftBar follows the link, and the launcher resolves its own real path, so it still finds the rest of the repository:

     ```sh
     ln -s ~/ai-limits-bar/plugin/ai-limits.5m.sh "/path/to/your/plugin/folder/"
     ```

     SwiftBar's Preferences show the current plugin folder. Then choose *Refresh all* in SwiftBar's menu.

3. On the first run, `config.json` is created next to `config.example.json`. Providers that aren't set up on your Mac are hidden; Microsoft Foundry stays off until you set it up (see [Settings](#settings)). If nothing is set up yet, the menu bar shows `–` and the menu says *No supported AI tool found on this Mac*: install and sign in to a provider (see [Requirements](#requirements)), and it appears on the next refresh, or at once with **Refresh now**. To check, open **Providers…**: *· not set up on this Mac* marks any provider that is on but still missing.

4. For Claude, macOS may ask whether the "Claude Code-credentials" Keychain item may be used. Choose *Always Allow*, otherwise the question comes back on every read.

To start SwiftBar at login: SwiftBar menu → Preferences → Launch at login. To update later: `git -C ~/ai-limits-bar pull`. To remove it: delete the link from your plugin folder (or point SwiftBar at another folder), then delete `~/ai-limits-bar` and the cache folder `~/Library/Caches/ai-limits-bar`.

## Settings

On first run `config.json` is created from `config.example.json` (it is git-ignored). Open it from the menu with **Open settings**. If it is not valid JSON, the menu says *config.json could not be read: using defaults* and the settings from `config.example.json` are used until you fix it.

- `providers`: the order of the sections. A provider that is not listed is off. Per provider:
  - `enabled`: `true` to read and show it. The **Providers…** panel sets this value for you (see [Turning providers on and off](#turning-providers-on-and-off)).
  - `refresh_minutes`: how often it is read (the cache is used in between).
  - `timeout_seconds`: how long a read may take before the last values are shown instead.
- Foundry (off by default; turn it on under **Providers…** or set `enabled` to `true`). It also needs `resource_group`; without it, the panel shows it as *not set up on this Mac*:
  - `resource_group`: the invoice is filtered by it.
  - `resource`: the Foundry resource whose token metrics are read.
  - `budget_usd`: optional monthly budget. With a budget, the month line gets a percentage bar.
  - `invoice_refresh_hours`: how often the invoice history is fetched (default 6; at most every 15 minutes on *Refresh now*). Cost Management throttles frequent queries, and past months don't change.
  - `prices_usd_per_million`: per deployment name: `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h`, in US dollars per million tokens (at most 10,000). They are used only for models Azure has no cost estimate for; a price for a model Azure estimates is ignored (see *How Foundry spend is computed* below). **Foundry prices…** in the menu sets them for you (see [Foundry prices](#foundry-prices)). A deployment with tokens, no Azure estimate and no price (or no price for a token type it used) is counted in tokens only and Details names it *(no price)*. If it has tokens today, this week or this month, those totals are marked `≥` and the section counts it in *N models have no price*; if its tokens are all older, no total depends on it (earlier months reach *All time* through the invoice) and it is not counted.
- `menu_bar`: what the number in the menu bar shows. `"highest"` (the default) is the highest current percentage of all limits; `{"provider": "claude", "limit": "5-hour"}` is that one limit, named exactly as listed under **Menu bar shows** (see [The menu](#the-menu)), which sets it for you. A name the menu can't offer as a click can still be written here by hand. An invalid value means highest.
- `debug_shapes`: stores the *structure* of the Claude response (field names and value types only, no values) in the cache, to help when an endpoint changes.

The plugin ignores environment variables, because a SwiftBar link can set them. Switches for testing and troubleshooting are command-line arguments (see [Troubleshooting](#troubleshooting)), which a link cannot set.

**How Foundry spend is computed.** All of it is read with the Azure CLI, from Azure Monitor and Cost Management:

- **Azure's estimated cost** per model deployment (the Azure Monitor metric `FoundryModelEstimatedCost`, a preview), up to date like the token metrics. Azure has it for some models and not for others (see below).
- **Token metrics** per deployment, multiplied by your prices for the models Azure has no estimate for (see [Foundry prices](#foundry-prices)).
- **The invoice** is exact, but late: Claude models on Azure are billed through the Marketplace and reach it one to two days late, other models hours later.

Each model is handled by the same rule, whoever publishes it:

1. If Azure has an estimate for the deployment, it is used, and a price you set for it is ignored. A day with tokens but no estimate (yet) makes that period `≥`.
2. Otherwise its tokens × your prices.
3. Otherwise it is counted in tokens only and Details marks it *(no price)*. If it has tokens today, this week or this month, those totals are `≥` and the section counts it in *N models have no price*. **Foundry prices…** lists it either way. The invoice later shows its actual cost.

Details names each model's source: *(Azure estimate)*, *(price from settings)* or *(no price)*.

Which models have an estimate:

- **Checked**, on one account in November 2026: an Azure OpenAI deployment had one, and a Claude deployment had none. The Azure OpenAI estimate there appears to price cached input at the cached rate (inferred from its size, not documented).
- **From Microsoft's documentation**: the metric is described as "Estimated USD cost of model usage for this account. Applies to Foundry models only. This value is an estimate and is not an invoice or billed charge." The documentation does not say which models it covers. Foundry's catalog has models sold by Azure (all Azure OpenAI models and selected models from other providers, such as DeepSeek, Meta, Microsoft and Mistral AI) and models from partners and community, which need Azure Marketplace (Microsoft's partner list includes Anthropic's Claude). The Foundry portal shows estimated cost for Claude deployments billed in Claude Consumption Units, and none for older Claude deployments billed per token; whether this metric follows the same split is not documented.
- **Not checked**: models from Microsoft, Meta, Mistral AI, DeepSeek and other publishers. For such a model you see either an automatic amount marked *(Azure estimate)* in Details, or *(no price)* there (counted in the orange *N models have no price* line while it has tokens this week or month), with **Foundry prices…** to set one; in both cases the invoice later.

Microsoft documents the token metrics for "all the models available in the resource, including Azure OpenAI, DeepSeek, and Phi", and cache tokens (reads and writes) for Anthropic deployments only.

*Today*, *This week* (from Monday) and *This month* each show the larger of the model estimates and the invoice. *All time* adds the invoiced totals of previous months to this month's figure. Azure Monitor keeps these metrics for only 93 days, so monthly token and estimate totals per model are kept in the cache too. Azure's estimate and your prices are in US dollars; with an invoice in another currency, the larger of the two compares amounts in different currencies.

The sign says whether a total can be complete:

- `≈`: the token metrics were read and every model with tokens in that period has an amount for it (Azure's estimate for each of its days with tokens, or your price).
- `≥` (at least): otherwise, even when the invoice is larger than the estimate, because the invoice may not contain that usage yet. A new model without a price and nothing invoiced yet shows `≥$0.00`. Every period is `≥` when the token metrics could not be read or `resource` is not set. *All time* takes this month's sign.

Prompt caching, for models priced from your settings: Azure reports cache reads and writes separately only for Claude models. For other models all input tokens are priced at the `input` price, cached or not. For Azure OpenAI models the input tokens appear to include cached input (inferred on the account above), so a price from settings would be several times too high with heavy caching; that is why Azure's estimate is used whenever it exists. Details says *Priced from settings, without cache tokens* when this can apply.

Details lists every model and every invoice item (Azure OpenAI models by name, Claude grouped because its invoice lines don't name the model, other models and services by their service name), each for today · this week · this month · all time.

## Turning providers on and off

Choose **Providers…** in the menu. A small window opens with one checkbox per provider; tick the ones you use, untick the others, and click **Save** (or **Cancel** to change nothing). The window stays open while you tick and untick. It is a separate window because macOS closes a menu as soon as one of its items is clicked, and SwiftBar has no menu items that stay open.

- **Off** (unticked) means the provider is not read at all (no process, no request, not even its cache) and has no section in the menu. Use this for a service you don't have, e.g. untick Gemini if you don't use Gemini.
- **On, with *· not set up on this Mac*** next to it means the provider is on, but the last menu update found its tool missing or not signed in (see [Requirements](#requirements)), or, for Foundry, `resource_group` missing. That is why it has no section.
- **Save** writes `enabled` for all providers to `config.json` at once, and the menu is rebuilt right after the window closes. Like **Refresh now**, that reads the providers that are on again (values read less than a minute ago are kept). The file is rewritten in place with one provider per line; your values and their order are kept. While the menu says *config.json could not be read*, the window doesn't open: fix the file first.
- The window closes by itself, without changes, after 10 minutes. Choosing **Providers…** again while it is open does nothing.
- If the repository's path contains a `"` or a backslash, **Providers…** can't be clicked (and **Open settings** is left out). The same goes for **Providers…** if it contains `'`, `$`, `;`, `&`, `|`, `<`, `>` or a backtick. Use the terminal command below then.
- From a terminal, one provider at a time: `~/ai-limits-bar/plugin/ai-limits.5m.sh --toggle gemini` turns Gemini on or off (it prints nothing; the menu changes on its next refresh). The window itself: `--choose`.

## Foundry prices

Azure estimates the cost of some models itself; for the others, Foundry spend is estimated from tokens × your price per model (see [How Foundry spend is computed](#settings)). Choose **Foundry prices…** in the menu, or click the orange *N models have no price* line in the Foundry section. A small window lists the model deployments that have tokens but no Azure estimate: first those whose missing price makes a total `≥` today, this week or this month (marked *no price*, or *missing Output* when only some token types used have one), then those with a price, then those whose missing price concerns only older usage (marked *· old usage only*: their price changes only Details), with fields for **Input**, **Output**, **Cache read**, **Write 5m** and **Write 1h**, in US dollars per million tokens as on the model's pricing page.

- Type a number such as `2.5`, `2,5` or `0.075` (at most 10,000), or leave the field empty for no price. Something else shows the window again with a note.
- Cache fields apply to Claude models: Azure reports no cache tokens for other models, so all of their input is priced as **Input** (see the prompt caching note above).
- **Save** writes only the fields you changed to `prices_usd_per_million` in `config.json`, in one write; your other values and their order are kept, and a deployment left without any price is removed. The menu is rebuilt right after the window closes: Foundry is read again with the new prices (its invoice history is kept), every other provider keeps the values already read.
- The list comes from the last Foundry read, so a deployment shows up once it has tokens and Foundry has been read since. At most 15 are listed; set the others from a terminal. A deployment whose name has characters other than letters, digits, `.`, `_`, `-` and parentheses is not listed: set its price in `config.json`.- The window closes by itself, without changes, after 10 minutes. Choosing it again while it is open does nothing, and neither **Providers…** nor `--toggle` can change `config.json` meanwhile. The path rule of **Providers…** applies here too.
- From a terminal: `~/ai-limits-bar/plugin/ai-limits.5m.sh --set-price demo-model input=1.25 output=10 cache_read=0.125` sets those fields of one deployment (fields: `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h`); `input=` with nothing after it removes one. It prints nothing; the menu changes on its next refresh. The window itself: `--prices`.

## The menu

- **Refresh now**: reads everything again (values read less than a minute ago are kept); the menu bar keeps the current values until the new ones are ready.
- **Providers…**: turns providers on and off in a small window (see above).
- **Menu bar shows**: chooses what the number in the menu bar is: *Highest of all*, or one limit such as *Claude · 5-hour*. The new number shows at once, from the values already read, whatever their age; only a provider with no values yet is read. With one limit, the number, gauge and colour follow that limit only, so another limit reaching 90% no longer turns the menu bar red; the tooltip says which limit is shown. If it can't be read (provider off or not set up, the limit gone, or its reset time passed), the menu bar shows `–`; a limit that is no longer listed stays in the submenu, checked and marked *(not available)*, so you can change it. Gemini's window lines (*Gemini 5-hour* …) keep their names; its per-model lines, shown only without the quota summary, can regroup as usage changes, so a chosen one can become not available. A Gemini line chosen before the window lines existed (such as *All models*) shows as not available once they appear: choose again. A limit whose name has characters other than letters, digits, spaces, `.`, `_` and `-` is listed but can't be chosen with a click (set it in `config.json` instead, see [Settings](#settings)). The path rule of **Providers…** applies here too.
- **Foundry prices…** (only with a Foundry section): sets the price of each model in a small window (see [Foundry prices](#foundry-prices)).
- **Open settings**: opens `config.json` in your default editor. It is left out if the folder's path contains a `"` or a backslash.
- **Updated**: when the menu was last built.
- **SwiftBar**: SwiftBar's own menu (preferences, plugin folder, launch at login, quit). SwiftBar's *About*, *Run in Terminal*, *Last updated* and *Disable plugin* items are hidden.
- Each section's header opens the provider's usage page; **Details** has extra information and the last read time.
- Microsoft Foundry's totals are marked `≈` or `≥` (see [How Foundry spend is computed](#settings)); an orange *2 models have no price: set prices…* line counts the models with tokens today, this week or this month that have neither an Azure estimate nor a price: they are why a total is `≥`. Models without a price whose tokens are all older appear only in Details. A click on the line opens **Foundry prices…**; without such models there is no line.

## Privacy and security

In short:

- Credentials are never refreshed, stored, logged or printed.
- Where possible the provider's own tool does the talking (Codex app-server, GitHub CLI, Azure CLI, the Antigravity server).
- The menu never shows your email, username, tenant or subscription id. Foundry's *Details* submenu does list the model deployment names you chose in Azure and the invoice meter names; keep it closed on a screen recording if those are private.
- No log files are written. Besides `config.json` (created on the first run, rewritten when you turn a provider on or off, choose what the menu bar shows or set Foundry prices), the only files are a few small cache files in `~/Library/Caches/ai-limits-bar/` (folder 0700, files 0600). They hold numbers, times and labels (including those Foundry deployment and meter names), and are overwritten on every read. Foundry's monthly invoice, token and estimated-cost totals are kept there as well (`foundry-history.json`), and the names of the deployments without an Azure estimate, for the prices window (`foundry-deployments.json`, no amounts).

See [SECURITY.md](SECURITY.md) for the full model and how to report a problem.

## Unofficial

This project is not affiliated with Anthropic, OpenAI, Google, GitHub or Microsoft. It reads the same endpoints the official tools use for their own usage screens. These are undocumented and may change. A section that can no longer be read shows its last values with a note instead of breaking the menu.

## Troubleshooting

The plugin ignores environment variables; these switches are command-line arguments:

- See the raw menu output: `~/ai-limits-bar/plugin/ai-limits.5m.sh`
- A provider has no section: open **Providers…**. Unticked means it is off; *· not set up on this Mac* means its tool was not found or is not signed in (see [Requirements](#requirements)).
- Turn a provider on or off: `--toggle gemini`; open the window: `--choose`
- Choose what the menu bar shows: `--show claude:5-hour` (provider, a colon, then the limit's name as listed under **Menu bar shows**); back to the highest: `--show highest`
- Set Foundry prices: `--set-price demo-model input=1.25 output=10 cache_read=0.125` (see [Foundry prices](#foundry-prices)); open the window: `--prices`
- *AI Limits Bar could not run*: `~/ai-limits-bar/plugin/ai-limits.5m.sh --debug` prints the error to the terminal. Add `>/dev/null` to see only the error, without the menu and your numbers (for example for an issue).
- Read every provider again instead of using the cache (values read less than a minute ago are still used): `--refresh`
- Force one provider to fail and check that only its section shows a note: `~/ai-limits-bar/plugin/ai-limits.5m.sh --fail claude`
- Skip a provider for one run: `--skip foundry` (several: `--skip claude,codex`)
- Use another settings file or cache folder: `--config PATH`, `--cache DIR`. With `--config`, `config.json` is not created.
- *config.json could not be read: using defaults*: the file is not valid JSON. Fix it with **Open settings**; until then **Providers…** and **Foundry prices…** open no window.
- *Session expired: open Claude Code once*: Claude Code renews its sign-in when it runs; this tool never does.
- *Not signed in: run codex login*: the Codex CLI has no ChatGPT sign-in yet.
- *Antigravity not running*: start `agy` (or the Antigravity app); the last values stay visible meanwhile.
- *Antigravity did not answer*: Antigravity is running but its local server gave no usable answer, and the Gemini CLI fallback had nothing either. Restarting Antigravity usually helps.
- Gemini shows model lines (such as *All models*) instead of *Gemini 5-hour* and *Gemini Weekly*: Antigravity's local server gave no quota summary (an older Antigravity, or an account without one), or Details says *Source: Gemini CLI* (the fallback has no windows).
- *GitHub refused the request*: `gh` got HTTP 403, often a temporary rate limit. The last values stay visible; without any, the section is hidden until the next successful read.
- If macOS asks whether the "Claude Code-credentials" Keychain item may be used, choose *Always Allow* to let it read your Claude limits.

## Development

```sh
python3 -I tests/test_ai_limits.py          # offline: no network, no Keychain, no window
python3 -I tests/test_ai_limits.py --gui    # also the tests that open the Providers… and Foundry prices windows for a second
```

CI runs the offline suite on GitHub's macOS runner with the system Python (3.9) and the latest Python. Code layout:

| Path | Contents |
|---|---|
| `ai_limits/main.py` | Order, cache, timeouts, `--choose`, `--toggle`, `--show`, `--prices` and `--set-price` |
| `ai_limits/panel.py` | The **Providers…** and **Foundry prices…** windows (fixed scripts run by `osascript`) |
| `ai_limits/render.py` | Menu text and output safety |
| `ai_limits/common.py` | HTTP and subprocess helpers |
| `ai_limits/providers/` | One module per provider |

## Contributing

Bug reports and questions are welcome as issues; the templates ask for what is needed. Never paste tokens, `config.json` or files from the cache. Pull requests are by invitation: see [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems privately: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
