# Neurohost-bot

NeuroHost V4 — Time, Power & Smart Hosting Edition

Requirements:

- Install runtime dependencies: `pip install -r requirements.txt`
- Ensure `TELEGRAM_BOT_TOKEN` and optional `ADMIN_ID` env vars are set.

Notes:

- The bot uses a local SQLite DB (`neurohost_v3_5.db`) and will migrate schema automatically on first run.
- If `psutil` is not installed, CPU/memory metrics will be disabled but the bot still works.
