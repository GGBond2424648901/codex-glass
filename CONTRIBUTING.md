# Contributing

Thanks for taking the time to contribute!

## Scope

This project is a **local-only** dashboard that parses Codex CLI session logs from `~/.codex/sessions/**.jsonl` and renders aggregated usage/cost estimates.

## Development setup

Requirements:

- Python 3.13 is the validated development/build environment for 4.1.
- PyQt5 is required for desktop development and the full Windows test suite.

Run locally:

```bash
python3 -m pip install -r requirements.txt
python3 monitor.py open --no-browser
```

The native desktop uses PyQt5 under `codex_glass/desktop/`. Core calculations, SQLite storage, HTTP services and CLI tools have separate package directories. The optional browser page is `assets/web/dashboard.html`; no Node.js toolchain is required. See [Architecture](docs/ARCHITECTURE.md).

## What to include in PRs

- A clear description of the user-facing change
- Screenshots for UI changes (redact local paths if needed)
- Notes about any config changes

## Privacy & security

- Do **not** commit any local session logs or configs:
  - `~/.codex/sessions/**`
  - `~/.codex/monitor_config.json`
  - `~/.codex/monitor.log`, `~/.codex/monitor.pid`
- Keep the dashboard default bind to `127.0.0.1` (localhost).
- If you discover a security issue, please follow `SECURITY.md`.

## Coding guidelines

- Prefer small, focused changes.
- Use qualified `codex_glass.*` imports and the shared resource resolver; never assume the caller's working directory is the repository.
- Format with `python -m black codex_glass tests monitor.py web_dashboard.py desktop_widget.py` (`requirements-dev.txt`).
- Keep the backend standard-library only. Desktop dependencies are isolated in `requirements-desktop.txt`.
- Avoid heavy refactors unless needed for the fix.
- UI: keep pages fast and avoid large payloads by default.

## Testing

Run on an interactive Windows desktop with PyQt5 installed:

```powershell
$env:QT_QPA_PLATFORM = 'windows'
python -m unittest discover -s tests -v
python -m tests.capture_release work/captures
```

Use temporary fixture indexes for parser/import tests. Never modify a user's real history database during testing. Public screenshots must use anonymous fixture data.

## Pricing updates

Pricing is an **estimate** and can change over time.

- Update `codex_glass/core/usage.py` builtin rates.
- Keep README in sync.
- Mention the source and tier in the PR description.
