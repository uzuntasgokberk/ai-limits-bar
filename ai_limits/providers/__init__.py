"""One module per provider. Each exposes `read(settings) -> common.result(...)` and may raise
`NotConfigured` (section hidden), `SessionExpired`, or `Unavailable` (cached values shown with a note)."""

NAMES = ("claude", "codex", "gemini", "copilot", "foundry")
