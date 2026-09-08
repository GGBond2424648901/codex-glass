# Privacy

This tool reads Codex CLI local session logs from `~/.codex/sessions/**.jsonl` on your machine to compute usage summaries.

## What it processes

- Session metadata (e.g., `cwd`)
- Model identifiers
- Token usage counters (`token_count` events)
- Rate limit snapshots (if present in the logs)

## What it does not do

- It does not send your data to third parties.
- It does not require network access.
- It does not expose a public endpoint by default.

## UI and export behavior

- The dashboard is designed to run locally and bind to `127.0.0.1` by default.
- Workspace displays are anonymized in the UI so screenshots are safer to share.
- Raw local logs still contain original metadata on your machine.
- If you add your own custom exports or patches, review them before sharing publicly.

## SQLite imports and public releases

- The application's own SQLite index stores token/model/time facts and source paths, not chat message bodies. Original session files may contain messages and credentials elsewhere; never share the entire Codex directory.
- Import is initiated through an explicit local file selection and confirmation. The source is read-only, and the target is backed up before merging with deduplication.
- An import backup can include original local paths and historical account quota snapshots. Treat it as private.
- Current account quota uses local snapshots, not imported historical account state.
- The native application talks to its loopback HTTP backend. It does not require an external cloud service; dependency installation and GitHub downloads naturally use the network.
- Repository screenshots use isolated illustrative fixtures, not a user's database. Build artifacts exclude databases, sessions, logs, tokens, and local configuration.
