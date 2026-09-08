# Security

This project is designed to run locally and reads local Codex CLI session logs.

## Do not expose the dashboard publicly

- Default bind address is `127.0.0.1` (localhost). Keep it that way.
- Avoid binding to `0.0.0.0` or opening the port to your LAN/public Internet.

The dashboard can display:
- Model names
- Token usage and estimated costs
- Anonymized workspace labels in the UI

Raw local Codex logs on disk may still contain original paths and other sensitive metadata.

## Sensitive files to never commit

- `~/.codex/sessions/**` (local session logs)
- `~/.codex/monitor_config.json` (your local config)
- `~/.codex/monitor.log`, `~/.codex/monitor.pid`
