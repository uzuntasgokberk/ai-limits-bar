# Contributing

Thank you for helping. This repository is maintained by one person, so the process is short and fixed.

## Issues first, pull requests by invitation

- **Open an issue** for a bug, a wrong or missing value, or a question. Use the templates: they ask for the macOS,
  SwiftBar and Python versions, the provider and its tool's version, and the note the menu showed.
- **Pull requests are by invitation only.** If the fix is agreed in the issue, the maintainer asks you for one; an
  uninvited pull request is closed with a pointer to the issue.
- **Security problems never go into an issue**: see [SECURITY.md](SECURITY.md).
- Never paste tokens or other credentials, `config.json`, files from `~/Library/Caches/ai-limits-bar/`, account,
  tenant or subscription ids, or resource or deployment names into an issue or a pull request. Reproduce with fake
  values; for a crash, `plugin/ai-limits.5m.sh --debug >/dev/null` shows the error without the menu and its numbers.

## How a change is made

- Keep it small and tied to one case the plugin got wrong.
- Every behaviour change needs a test in `tests/test_ai_limits.py`, written first. The tests stay offline: no network
  (loopback only), no real provider, no Keychain, no window unless the test is one of the opt-in `--gui` tests.
- Standard library only, and Python 3.9 compatible (`/usr/bin/python3` on macOS is 3.9).
- Anything that reaches the menu goes through `render.clean`, `render.quoted` or `render.param_path` (and
  `render.command_path` for a command); click targets are fixed in `render.py`, and a provider's text goes into a
  click parameter only through `render.choice_param` and its allow-list.
- Nothing is read from the environment: SwiftBar links can set it. Switches are command-line arguments.
- The tool stays read-only towards the accounts: no new write endpoint or method, no credential refresh, no credential
  on disk, in a log, in the cache or in a process argument.
- Commits follow Conventional Commits (`feat(gemini): ...`, `fix(render): ...`, `docs: ...`).
- Contributions are licensed under the repository's license, [MIT](LICENSE).

## Run the checks locally

Python 3.9 or later on macOS. From the repository root:

```bash
python3 -I tests/test_ai_limits.py          # the offline suite (no window, no network, no Keychain)
python3 -I tests/test_ai_limits.py --gui    # also the two tests that open the Providers… window for a second
```

Run it with `/usr/bin/python3` as well when you touch the Python code, so 3.9 compatibility is checked. CI
(`.github/workflows/ci.yml`) runs the offline suite on GitHub's macOS runner with the system Python and the latest
Python; both jobs, `tests (system python)` and `tests (latest python)`, must pass.
