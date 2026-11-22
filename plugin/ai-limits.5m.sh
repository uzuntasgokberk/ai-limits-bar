#!/bin/zsh -f
# <xbar.title>AI Limits Bar</xbar.title>
# <xbar.version>v1.0.0</xbar.version>
# <xbar.desc>Usage limits of Claude, Codex, Gemini, GitHub Copilot and Microsoft Foundry.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <swiftbar.runInBash>false</swiftbar.runInBash>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>false</swiftbar.hideSwiftBar>
#
# SwiftBar runs this file every 5 minutes (the "5m" in its name) and shows its output as a menu.
# It finds a Python 3 interpreter and runs ai_limits/main.py in isolated mode (-I).
#
# The environment is NOT trusted: SwiftBar copies the query parameters of a
# `swiftbar://refreshplugin?name=...&KEY=value` link into it, and any web page or app can open such a link.
# Therefore:
# - `runInBash` is false, so SwiftBar starts this file directly instead of through a login shell.
# - The interpreter is `zsh -f`: it reads no user startup files and, unlike /bin/sh (bash), does not import
#   functions or tracing options from the environment.
# - Only shell keywords and builtins run here, then Python starts with an EMPTY environment (`env -i`).
#   The one thing kept is whether "Refresh now" was clicked, passed as the --refresh argument.
if [[ "${SWIFTBAR_PLUGIN_REFRESH_REASON-}" == MenuAction ]]; then
  set -- --refresh "$@"
fi
ROOT=${0:A:h:h}
for PY in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [[ -x $PY ]]; then
    exec /usr/bin/env -i "$PY" -I "$ROOT/ai_limits/main.py" "$@"
  fi
done
print -r -- "⚠︎"
print -r -- "---"
print -r -- "AI Limits Bar: Python 3 not found (install Xcode Command Line Tools or Homebrew python)"
