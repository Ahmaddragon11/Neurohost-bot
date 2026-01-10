# Neurohost-bot

NeuroHost V4 — Time, Power & Smart Hosting Edition

Requirements:

- Install runtime dependencies: `pip install -r requirements.txt`
- Ensure `TELEGRAM_BOT_TOKEN` and optional `ADMIN_ID` env vars are set.

Notes:

- The bot uses a local SQLite DB (`neurohost_v3_5.db`) and will migrate schema automatically on first run.
- If `psutil` is not installed, CPU/memory metrics will be disabled but the bot still works.

Error logging:

- All uncaught exceptions and runtime errors are saved to `neurohost_errors.log` by default. You can change the path with the `NEUROHOST_ERROR_LOG` environment variable.

Deployment tips:

- Install requirements: `pip install -r requirements.txt`
- Set env vars: `TELEGRAM_BOT_TOKEN` (required), `ADMIN_ID` (owner Telegram ID, optional).
- Run the bot with a process manager (systemd, supervisord) or inside a screen/tmux session for production hosting.
