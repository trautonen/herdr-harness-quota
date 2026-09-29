# Harness Quota

Harness Quota reads Claude, Codex, and Cursor subscription usage and converts it to one JSON model for scripts and routing. It also provides compact Herdr tab-bar output.

The provider usage endpoints are first-party but are not documented as public APIs. Their paths and response formats can change.

## Unified model

Every available provider has three ordered windows:

| Minutes | Window |
|---:|---|
| 300 | 5 hours |
| 10080 | 1 week |
| 43200 | 30 days |

Claude and Codex do not report a 30-day window, so it is estimated from the recorded weekly usage in the [cache](#cache). A weekly period is identified by its reset time rounded to the hour. The estimate uses the current week and the peak of every completed week that ended within the last 30 days. Each completed week is weighted by how much of it falls inside those 30 days:

```text
round((current weekly usedPercent + Σ peak × overlap) × 7 days / (current week elapsed + Σ overlap × 7 days))
```

Weeks without recorded samples are left out, so a gap in the history does not count as zero usage. A steady 50% per week gives 50%. Until a completed week is recorded, or when the weekly window has no reset time, the 30-day value falls back to:

```text
round(5-hour usedPercent × 0.30 + weekly usedPercent × 0.70)
```

Cursor's Auto and API allowances map to the synthetic 5-hour and weekly windows. These values have `estimated: true`. Cursor's reported total allowance maps to 30 days. When another canonical window is missing, the estimate uses 30% of the shortest reported window and 70% of the longest. A single reported value is copied to missing windows. Every synthesized value is marked as estimated.

Run without a subcommand to get all providers as JSON:

```bash
harness-quota
```

Example complete response:

```json
{
  "schemaVersion": 1,
  "generatedAt": 1741435200,
  "providers": [
    {
      "provider": "claude",
      "status": "ok",
      "capturedAt": 1741435195,
      "windows": [
        {"minutes": 300, "usedPercent": 42, "resetsAt": 1741440000, "estimated": false},
        {"minutes": 10080, "usedPercent": 18, "resetsAt": 1742040000, "estimated": false},
        {"minutes": 43200, "usedPercent": 25, "resetsAt": null, "estimated": true}
      ],
      "error": null
    },
    {
      "provider": "codex",
      "status": "ok",
      "capturedAt": 1741435194,
      "windows": [
        {"minutes": 300, "usedPercent": 61, "resetsAt": 1741440100, "estimated": false},
        {"minutes": 10080, "usedPercent": 33, "resetsAt": 1742040100, "estimated": false},
        {"minutes": 43200, "usedPercent": 41, "resetsAt": null, "estimated": true}
      ],
      "error": null
    },
    {
      "provider": "cursor",
      "status": "ok",
      "capturedAt": 1741435193,
      "windows": [
        {"minutes": 300, "usedPercent": 12, "resetsAt": 1744027193, "estimated": true},
        {"minutes": 10080, "usedPercent": 35, "resetsAt": 1744027193, "estimated": true},
        {"minutes": 43200, "usedPercent": 7, "resetsAt": 1744027193, "estimated": false}
      ],
      "error": null
    }
  ]
}
```

`status` is `ok`, `stale`, or `unavailable`. A failed refresh returns stale cached windows when possible. An unavailable provider has `capturedAt: null`, an empty `windows` array, and a structured `error`.

Remaining quota is `100 - usedPercent`.

## Architecture

The Python source is a package under `src/harness_quota`:

- `providers/claude.py`, `providers/codex.py`, and `providers/cursor.py` own provider credentials, endpoints, payload extraction, and refresh behavior.
- `model.py` defines canonical windows and estimation.
- `auth.py`, `storage.py`, and `transport.py` provide shared boundary utilities.
- `history.py` records refresh samples and estimates the 30-day window from them.
- `service.py` dispatches provider refreshes, while `report.py` combines snapshots into the JSON contract.
- `presentation.py` formats Herdr chips, and `cli.py` handles command parsing and output.

Provider modules do not depend on the CLI or report layer. New providers can implement a refresh adapter and register it in `service.py` without changing the normalized model.

## CLI

```text
harness-quota [options]

-p, --provider all|claude|codex|cursor
--refresh stale|always|never
--stale-after SECONDS
--timeout SECONDS
--pretty
--require-all
--version
```

Defaults are all providers, `--refresh stale`, a 15-minute refresh interval, and a 12-second timeout per provider. Use `--stale-after SECONDS` to change the JSON command's interval. Refreshes run concurrently. A valid report exits successfully even when a provider is unavailable. `--require-all` returns exit status 1 unless every requested provider is current.

Examples:

```bash
harness-quota --pretty
harness-quota --provider claude --refresh always
harness-quota --refresh never
```

## Install the standalone CLI

Python 3.10 or newer is required. The runtime has no third-party dependencies.

From a checkout:

```bash
uv tool install .
```

From Git after publication:

```bash
uv tool install git+https://github.com/trautonen/herdr-harness-quota.git
```

## Claude Code marketplace

The repository contains a Claude Code plugin and marketplace manifest. After the repository is published, install it with:

```bash
claude plugin marketplace add trautonen/herdr-harness-quota
claude plugin install harness-quota@trautonen-tools
```

Claude Code adds the plugin's `bin` directory to its Bash-tool `PATH`, so agents can invoke `harness-quota` without a symlink or global installation.

## Pi package

Install directly from a tagged Git release:

```bash
pi install git:github.com/trautonen/herdr-harness-quota@v0.2.0
```

The package contains a portable `harness-quota` skill. Invoke it explicitly with:

```text
/skill:harness-quota
```

The skill runs its bundled wrapper relative to `SKILL.md`, so it does not require a symlink. The `pi-package` keyword also makes a future npm publication eligible for the Pi package gallery.

## Herdr plugin

Link the checkout:

```bash
herdr plugin link /path/to/herdr-harness-quota
```

Add the command entries from [`examples/herdr-config.toml`](examples/herdr-config.toml) to the Herdr configuration. Existing commands remain available:

```text
herdr-harness-quota chip claude|codex|cursor|all
herdr-harness-quota refresh claude|codex|cursor|all
herdr-harness-quota details
```

Tab-bar reads return cached data immediately and start a detached refresh after 15 minutes. Override the interval with `herdr-harness-quota chip PROVIDER --refresh-after SECONDS`.

## Credentials

The CLI reads existing provider credentials and never includes tokens in cache or output.

### Claude

Credential precedence:

1. `CLAUDE_CODE_OAUTH_TOKEN`
2. `ANTHROPIC_OAUTH_TOKEN`
3. Pi's `anthropic` OAuth entry in `${PI_CODING_AGENT_DIR:-~/.pi/agent}/auth.json`
4. `claudeAiOauth.accessToken` in `$CLAUDE_CONFIG_DIR/.credentials.json` or `~/.claude/.credentials.json`

It calls `GET https://api.anthropic.com/api/oauth/usage` with the same `claude-code/<version>` user agent as the installed Claude Code client. The version is read from Claude Code's package metadata or executable path without launching Claude Code. Set `HARNESS_QUOTA_CLAUDE_VERSION` when the version is not discoverable. Anthropic API keys, browser cookies, and macOS Keychain access are intentionally unsupported because this endpoint requires subscription OAuth and Keychain reads can prompt.

### Codex

Credential precedence:

1. Pi's `openai-codex` OAuth entry
2. `$CODEX_HOME/auth.json` or `~/.codex/auth.json`

It calls `GET https://chatgpt.com/backend-api/wham/usage`. `OPENAI_API_KEY` is intentionally unsupported because an API key must not be sent to the ChatGPT subscription endpoint. The CLI does not launch Codex app-server.

### Cursor

Credential precedence:

1. Pi's `cursor` API key
2. `CURSOR_API_KEY`
3. `~/.cursor/sdk/auth.json`
4. `$CURSOR_CLI_CONFIG`, `$CURSOR_AUTH_FILE`, or the platform Cursor CLI `auth.json`
5. `$CURSOR_STATE_DB` or Cursor Desktop's `state.vscdb`

API keys are exchanged for a short-lived dashboard token. Native refresh tokens are used only in memory and are never written. Browser cookies and macOS Keychain prompts are intentionally unsupported.

## Cache

Snapshots live under `$XDG_CACHE_HOME/herdr-harness-quota`, or `~/.cache/herdr-harness-quota`. The directory uses mode `0700`, snapshot files use mode `0600`, and writes are atomic.

Every successful refresh also appends a sample of all windows to `<provider>.history.jsonl` in the same directory, one JSON object per line. Each write drops samples older than 37 days, which covers the 30-day window plus the oldest overlapping week. History files use mode `0600` and are rewritten atomically. Malformed lines are skipped. Deleting a history file only returns the 30-day estimate to the fallback formula until a new week completes.

## Development

```bash
python3 -m pip install -e ".[dev]"
ruff check src tests
black --check src tests
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
shellcheck bin/harness-quota bin/herdr-harness-quota skills/harness-quota/scripts/harness-quota
claude plugin validate .
```

Run `black src tests` to format the Python source.

## License

MIT
