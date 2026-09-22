---
name: harness-quota
description: Read normalized Claude, Codex, and Cursor quota. Use when checking remaining coding-harness capacity or selecting a provider.
compatibility: Requires Python 3.10 or newer and network access for refreshes.
---

# Harness quota

Run `scripts/harness-quota` relative to this skill directory. Parse its JSON output.

Each available provider has 5-hour, 1-week, and 30-day windows. Lower `usedPercent` means more quota remains. Ignore providers with status `unavailable`. Treat status `stale` and windows with `estimated: true` with reduced confidence.

Use `--refresh never` when a fast cache-only answer is preferred. Use `--refresh always` when current quota is required.
