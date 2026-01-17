import os
import logging
from logging.handlers import RotatingFileHandler
import sys
from datetime import datetime

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8004754960:AAE_jGAX52F_vh7NwxI6nha94rngL6umy3U")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8049455831"))
DEVELOPER_USERNAME = "@ahmaddragon"
DB_FILE = "neurohost_v3_5.db"
BOTS_DIR = "bots"
ERROR_LOG_FILE = os.getenv("NEUROHOST_ERROR_LOG", "neurohost_errors.log")

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

def setup_file_logging(log_file=ERROR_LOG_FILE):
    try:
        os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.ERROR)
        fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
        logging.getLogger().addHandler(fh)
    except Exception as e:
        logger.warning("Failed to set up file logging: %s", e)

def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    try:
        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as ef:
            ef.write(f"\n===== Uncaught exception: {datetime.utcnow().isoformat()} =====\n")
            import traceback
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=ef)
    except Exception:
        pass

def asyncio_exception_handler(loop, context):
    try:
        msg = context.get("exception") or context.get("message")
        logger.error("Asyncio exception: %s", msg)
        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as ef:
            ef.write(f"\n===== Asyncio exception: {datetime.utcnow().isoformat()} =====\n{msg}\n")
    except Exception:
        pass
