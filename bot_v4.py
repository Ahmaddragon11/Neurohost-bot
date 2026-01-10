# -----------------------------------------------------------------------------
# NEUROHOST BOT CONTROLLER V4 - Time, Power & Smart Hosting Edition
# -----------------------------------------------------------------------------
import os
import sys
import time
import logging
import sqlite3
import subprocess
import signal
import shutil
import asyncio
import threading
import json
try:
    import psutil
except ImportError:
    psutil = None
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
from telegram.error import BadRequest

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8004754960:AAE_jGAX52F_vh7NwxI6nha94rngL6umy3U")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8049455831"))
DEVELOPER_USERNAME = "@ahmaddragon"
DB_FILE = "neurohost_v3_5.db"
BOTS_DIR = "bots"

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# -- File logging for uncaught exceptions and runtime errors --
from logging.handlers import RotatingFileHandler

ERROR_LOG_FILE = os.getenv("NEUROHOST_ERROR_LOG", "neurohost_errors.log")

def setup_file_logging(log_file=ERROR_LOG_FILE):
    try:
        os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.ERROR)
        fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
        logging.getLogger().addHandler(fh)
    except Exception as e:
        # If file logging cannot be set up, ensure at least console logger continues
        logger.warning("Failed to set up file logging: %s", e)

# Catch unhandled exceptions from threads/processes
def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        # let default handler handle keyboard interrupt
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

sys.excepthook = handle_uncaught_exception

# Asyncio exception handler
def asyncio_exception_handler(loop, context):
    try:
        msg = context.get("exception") or context.get("message")
        logger.error("Asyncio exception: %s", msg)
        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as ef:
            ef.write(f"\n===== Asyncio exception: {datetime.utcnow().isoformat()} =====\n{msg}\n")
    except Exception:
        pass

# --- Helpers for V4 ---

def seconds_to_human(s):
    if s is None: return "--"
    s = int(s)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, seconds = divmod(s, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return ' '.join(parts)


def render_bar(percent, length=12):
    try:
        p = max(0, min(100, int(percent)))
    except Exception as e:
        logger.exception("render_bar failed to parse percent %r: %s", percent, e)
        p = 0
    full = int((p / 100.0) * length)
    return '█' * full + '░' * (length - full) + f" {p}%"


# -----------------------------------------------------------------------------
# DATABASE MANAGER
# -----------------------------------------------------------------------------
class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        # Users table with plan and daily recovery tracking
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                status TEXT DEFAULT 'pending',
                bot_limit INTEGER DEFAULT 3,
                plan TEXT DEFAULT 'free',
                last_recovery_date DATE DEFAULT NULL,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Bots table extended with time/power/sleep/restart fields (appended to keep compatibility)
        c.execute('''
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                token TEXT,
                name TEXT,
                status TEXT DEFAULT 'stopped',
                folder TEXT,
                main_file TEXT DEFAULT 'main.py',
                pid INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                -- New columns appended for V4
                start_time INTEGER DEFAULT NULL,
                total_seconds INTEGER DEFAULT 0,
                remaining_seconds INTEGER DEFAULT 0,
                power_max REAL DEFAULT 100.0,
                power_remaining REAL DEFAULT 100.0,
                last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sleep_mode INTEGER DEFAULT 0,
                auto_recovery_used INTEGER DEFAULT 0,
                restart_count INTEGER DEFAULT 0,
                last_restart_at TIMESTAMP DEFAULT NULL,
                last_sleep_reason TEXT DEFAULT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                error_text TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(bot_id) REFERENCES bots(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

        # Ensure older DBs get new columns (ALTER TABLE ADD COLUMN if missing)
        def ensure_column(table, column_def, column_name):
            c.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in c.fetchall()]
            if column_name not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")

        ensure_column('users', "plan TEXT DEFAULT 'free'", 'plan')
        ensure_column('users', "last_recovery_date DATE DEFAULT NULL", 'last_recovery_date')

        ensure_column('bots', 'start_time INTEGER DEFAULT NULL', 'start_time')
        ensure_column('bots', 'total_seconds INTEGER DEFAULT 0', 'total_seconds')
        ensure_column('bots', 'remaining_seconds INTEGER DEFAULT 0', 'remaining_seconds')
        ensure_column('bots', 'power_max REAL DEFAULT 100.0', 'power_max')
        ensure_column('bots', 'power_remaining REAL DEFAULT 100.0', 'power_remaining')
        ensure_column('bots', "last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP", 'last_checked')
        ensure_column('bots', 'sleep_mode INTEGER DEFAULT 0', 'sleep_mode')
        ensure_column('bots', 'auto_recovery_used INTEGER DEFAULT 0', 'auto_recovery_used')
        ensure_column('bots', 'restart_count INTEGER DEFAULT 0', 'restart_count')
        ensure_column('bots', 'last_restart_at TIMESTAMP DEFAULT NULL', 'last_restart_at')
        ensure_column('bots', "last_sleep_reason TEXT DEFAULT NULL", 'last_sleep_reason')
        ensure_column('bots', 'warned_low INTEGER DEFAULT 0', 'warned_low')

        conn.commit()
        conn.close()

    def add_user(self, user_id, username):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        status = 'approved' if user_id == ADMIN_ID else 'pending'
        c.execute("INSERT OR IGNORE INTO users (user_id, username, status) VALUES (?, ?, ?)", (user_id, username, status))
        conn.commit()
        conn.close()

    def get_user(self, user_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        return row

    def update_user_status(self, user_id, status):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        conn.commit()
        conn.close()

    def get_pending_users(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE status = 'pending'")
        rows = c.fetchall()
        conn.close()
        return rows

    def add_bot(self, user_id, token, name, folder, main_file='main.py'):
        # Determine defaults based on user plan
        plan = self.get_user_plan(user_id)
        plan_limits = {'free': 86400, 'pro': 604800, 'ultra': 10**12}
        plan_power = {'free': 30.0, 'pro': 60.0, 'ultra': 100.0}
        total_seconds = plan_limits.get(plan, 86400)
        power = plan_power.get(plan, 30.0)

        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO bots (user_id, token, name, folder, main_file, total_seconds, remaining_seconds, power_max, power_remaining) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                      (user_id, token, name, folder, main_file, total_seconds, total_seconds, power, power))
            bot_id = c.lastrowid
            conn.commit()
            return bot_id
        finally:
            conn.close()

    def get_user_bots(self, user_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("SELECT id, name, status, pid FROM bots WHERE user_id = ?", (user_id,))
        rows = c.fetchall()
        conn.close()
        return rows

    def get_bot(self, bot_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("SELECT * FROM bots WHERE id = ?", (bot_id,))
        row = c.fetchone()
        conn.close()
        return row

    def update_bot_status(self, bot_id, status, pid=None):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("UPDATE bots SET status = ?, pid = ? WHERE id = ?", (status, pid, bot_id))
        conn.commit()
        conn.close()

    def add_error_log(self, bot_id, error_text):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("INSERT INTO error_logs (bot_id, error_text) VALUES (?, ?)", (bot_id, error_text))
        conn.commit()
        conn.close()

    def get_bot_logs(self, bot_id, limit=5):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("SELECT error_text, timestamp FROM error_logs WHERE bot_id = ? ORDER BY timestamp DESC LIMIT ?", (bot_id, limit))
        rows = c.fetchall()
        conn.close()
        return rows

    def add_feedback(self, user_id, text):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("INSERT INTO feedback (user_id, text) VALUES (?, ?)", (user_id, text))
        conn.commit()
        conn.close()

    def delete_bot(self, bot_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        c.execute("DELETE FROM error_logs WHERE bot_id = ?", (bot_id,))
        conn.commit()
        conn.close()

    # -- New utilities for time/power systems --
    def set_bot_time_power(self, bot_id, total_seconds, power_max):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("UPDATE bots SET total_seconds = ?, remaining_seconds = ?, power_max = ?, power_remaining = ? WHERE id = ?",
                  (total_seconds, total_seconds, power_max, power_max, bot_id))
        conn.commit()
        conn.close()

    def get_all_running_bots(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("SELECT * FROM bots WHERE status = 'running'")
        rows = c.fetchall()
        conn.close()
        return rows

    def update_bot_resources(self, bot_id, remaining_seconds=None, power_remaining=None, last_checked=None):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        updates = []
        params = []
        if remaining_seconds is not None:
            updates.append('remaining_seconds = ?')
            params.append(remaining_seconds)
        if power_remaining is not None:
            updates.append('power_remaining = ?')
            params.append(power_remaining)
        if last_checked is not None:
            updates.append('last_checked = ?')
            params.append(last_checked)
        if not updates:
            conn.close(); return
        params.append(bot_id)
        c.execute(f"UPDATE bots SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        conn.close()

    def set_sleep_mode(self, bot_id, sleep=1, reason=None):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("UPDATE bots SET sleep_mode = ?, status = 'stopped', last_sleep_reason = ? WHERE id = ?", (1 if sleep else 0, reason, bot_id))
        conn.commit()
        conn.close()

    def can_user_recover(self, user_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("SELECT last_recovery_date FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if not row: return False
        last = row[0]
        today = datetime.utcnow().date().isoformat()
        return last != today

    def use_user_recovery(self, user_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        today = datetime.utcnow().date().isoformat()
        c.execute("UPDATE users SET last_recovery_date = ? WHERE user_id = ?", (today, user_id))
        conn.commit()
        conn.close()

    def mark_bot_auto_recovery_used(self, bot_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("UPDATE bots SET auto_recovery_used = 1 WHERE id = ?", (bot_id,))
        conn.commit()
        conn.close()

    def increment_restart(self, bot_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("UPDATE bots SET restart_count = restart_count + 1, last_restart_at = ? WHERE id = ?", (datetime.utcnow().isoformat(), bot_id))
        conn.commit()
        conn.close()

    def reset_restart_count(self, bot_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("UPDATE bots SET restart_count = 0 WHERE id = ?", (bot_id,))
        conn.commit()
        conn.close()

    def update_last_checked(self, bot_id, ts=None):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        if ts is None: ts = datetime.utcnow().isoformat()
        c.execute("UPDATE bots SET last_checked = ? WHERE id = ?", (ts, bot_id))
        conn.commit()
        conn.close()

    def get_user_plan(self, user_id):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute("SELECT plan FROM users WHERE user_id = ?", (user_id,))
        r = c.fetchone()
        conn.close()
        return r[0] if r else 'free'

    def log_restart_event(self, bot_id, text):
        self.add_error_log(bot_id, f"[RESTART] {text}")

db = Database(DB_FILE)

# -----------------------------------------------------------------------------
# PROCESS MANAGER
# -----------------------------------------------------------------------------
class ProcessManager:
    def __init__(self):
        self.processes = {}
        self._monitor_task = None
        self._enforce_task = None
        self.restart_cooldown = 60  # seconds
        self.restart_power_cost = 2.0  # percent
        self.restart_time_cost = 60  # seconds
        self.restart_anti_loop_limit = 5  # max restarts in window
        self.restart_window_seconds = 3600  # 1 hour window for anti-loop
        self.power_drain_factor = 0.02  # multiplier for cpu*seconds -> power%

    async def start_bot(self, bot_id, application, use_recovery=False):
        bot_data = db.get_bot(bot_id)
        if not bot_data: return False, "البوت غير موجود."
        # indices preserved: id, user_id, token, name, status, folder, main_file, pid, created_at, start_time, total_seconds, remaining_seconds, power_max, power_remaining, last_checked, sleep_mode, ...
        _, user_id, token, name, status, folder, main_file, _, _, start_time, total_seconds, remaining_seconds, power_max, power_remaining, last_checked, sleep_mode, auto_recovery_used, restart_count, last_restart_at, last_sleep_reason = bot_data[:20]

        if sleep_mode:
            return False, "⚠️ البوت في وضع السكون. أضف وقتًا لإعادة تشغيله."
        if remaining_seconds <= 0 or power_remaining <= 0:
            return False, "⚠️ انتهى وقت الاستضافة أو الطاقة. أضف وقتًا أو طاقة لإعادة التشغيل."

        bot_path = os.path.abspath(os.path.join(BOTS_DIR, folder))
        req_path = os.path.join(bot_path, "requirements.txt")
        if os.path.exists(req_path):
            subprocess.Popen([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=bot_path)

        try:
            env = os.environ.copy()
            env["BOT_TOKEN"] = token if token else ""

            logs_path = os.path.join(bot_path, "logs")
            os.makedirs(logs_path, exist_ok=True)
            stderr_file = os.path.join(logs_path, "stderr.log")

            p = subprocess.Popen(
                [sys.executable, main_file],
                cwd=bot_path, env=env,
                stdout=open(os.path.join(logs_path, "stdout.log"), "a"),
                stderr=open(stderr_file, "a"),
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            self.processes[bot_id] = p
            db.update_bot_status(bot_id, "running", p.pid)
            # Set start_time and last_checked if not set
            now = int(time.time())
            if not start_time:
                conn = sqlite3.connect(db.db_file); c = conn.cursor(); c.execute("UPDATE bots SET start_time = ?, last_checked = ? WHERE id = ?", (now, now, bot_id)); conn.commit(); conn.close()
            else:
                db.update_last_checked(bot_id)

            # Reset restart counter on successful start
            db.reset_restart_count(bot_id)

            # Watch errors and process exit
            asyncio.create_task(self.watch_errors(bot_id, stderr_file, user_id, application))
            asyncio.create_task(self._watch_process_exit(bot_id, p, user_id, application))
            return True, "🚀 تم التشغيل بنجاح."
        except Exception as e:
            logger.exception("Failed to start bot %s: %s", bot_id, e)
            try:
                with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as ef:
                    import traceback
                    ef.write(f"\n===== Bot start exception ({bot_id}): {datetime.utcnow().isoformat()} =====\n")
                    traceback.print_exc(file=ef)
            except Exception:
                pass
            return False, str(e)

    async def _watch_process_exit(self, bot_id, process, user_id, application):
        # Wait for process to exit
        while True:
            await asyncio.sleep(1)
            if process.poll() is not None:
                code = process.returncode
                db.add_error_log(bot_id, f"Process exited with code {code}")
                # Remove from tracking
                if bot_id in self.processes: del self.processes[bot_id]
                # If non-zero or unexpected stop, try auto-restart
                if code != 0:
                    await asyncio.sleep(2)
                    await self._handle_unexpected_exit(bot_id, user_id, application, exit_code=code)
                break

    async def _handle_unexpected_exit(self, bot_id, user_id, application, exit_code=1):
        bot = db.get_bot(bot_id)
        if not bot: return
        # check sleep or expired
        sleep_mode = bot[15]
        remaining_seconds = bot[11]
        power_remaining = bot[13]
        # correct indices: auto_recovery_used = 16, restart_count = 17, last_restart_at = 18
        auto_recovery_used = bot[16]
        restart_count = bot[17]
        last_restart_at = bot[18]

        # Anti-loop: decline if too many restarts in window
        if restart_count >= self.restart_anti_loop_limit:
            db.set_sleep_mode(bot_id, True, reason="anti_loop")
            db.log_restart_event(bot_id, "Auto-restart disabled due to too many restarts.")
            try:
                await application.bot.send_message(chat_id=bot[1], text=f"⚠️ البوت {bot[3]} تم إيقافه آلياً بسبب تكرار الإعادات.")
            except Exception as e:
                logger.exception("Failed to notify user %s about auto-sleep for bot %s: %s", bot[1], bot[3], e)
            return

        # Check cooldown
        if last_restart_at:
            try:
                lr = datetime.fromisoformat(last_restart_at)
                if (datetime.utcnow() - lr).total_seconds() < self.restart_cooldown:
                    db.log_restart_event(bot_id, "Restart skipped due to cooldown.")
                    return
            except Exception as e:
                logger.exception("Failed to evaluate restart cooldown for bot %s: %s", bot_id, e)

        # If no time/power, try auto-recovery (if user still has daily recovery and not used for this bot)
        if (remaining_seconds <= 0 or power_remaining <= 0) and db.can_user_recover(bot[1]) and auto_recovery_used == 0:
            # Use auto-recovery: one free restart
            db.use_user_recovery(bot[1])
            db.mark_bot_auto_recovery_used(bot_id)
            db.log_restart_event(bot_id, "Auto-recovery used to restart bot for free.")
            success, msg = await self.start_bot(bot_id, application, use_recovery=True)
            if success:
                try:
                    await application.bot.send_message(chat_id=bot[1], text=f"🔄 تم استعادة {bot[3]} باستخدام Auto-Recovery المجانية.")
                except Exception as e:
                    logger.exception("Failed to notify user %s after auto-recovery for bot %s: %s", bot[1], bot[3], e)
                return

        # If still no resources, do not restart
        if remaining_seconds <= 0 or power_remaining <= 0 or sleep_mode:
            db.set_sleep_mode(bot_id, True, reason="expired_or_no_power")
            try:
                await application.bot.send_message(chat_id=bot[1], text=f"⚠️ البوت {bot[3]} توقف بسبب نفاد الوقت أو الطاقة ودخل وضع السكون.")
            except Exception as e:
                logger.exception("Failed to notify user %s about sleep for bot %s: %s", bot[1], bot[3], e)
            return

        # Deduct restart cost
        new_power = max(0.0, power_remaining - self.restart_power_cost)
        new_remaining = max(0, remaining_seconds - self.restart_time_cost)
        db.update_bot_resources(bot_id, remaining_seconds=new_remaining, power_remaining=new_power, last_checked=datetime.utcnow().isoformat())
        db.increment_restart(bot_id)
        db.log_restart_event(bot_id, f"Auto-restarting after exit code {exit_code}")
        # Attempt to restart within 5 seconds
        await asyncio.sleep(3)
        success, msg = await self.start_bot(bot_id, application)
        if success:
            try:
                await application.bot.send_message(chat_id=bot[1], text=f"♻️ تم إعادة تشغيل البوت {bot[3]} تلقائياً.")
            except Exception as e:
                logger.exception("Failed to notify user %s after auto-restart for bot %s: %s", bot[1], bot[3], e)
        else:
            db.log_restart_event(bot_id, f"Auto-restart failed: {msg}")

    async def watch_errors(self, bot_id, log_file, user_id, application):
        last_pos = os.path.getsize(log_file) if os.path.exists(log_file) else 0
        while bot_id in self.processes and self.processes[bot_id].poll() is None:
            await asyncio.sleep(2)
            if os.path.exists(log_file) and os.path.getsize(log_file) > last_pos:
                with open(log_file, 'r') as f:
                    f.seek(last_pos)
                    lines = f.readlines()
                    new_errors = []
                    for line in lines:
                        if "ERROR" in line.upper() or "CRITICAL" in line.upper() or "TRACEBACK" in line.upper() or "EXCEPTION" in line.upper():
                            new_errors.append(line)
                        elif not any(x in line.upper() for x in ["INFO", "DEBUG", "HTTP REQUEST"]):
                            new_errors.append(line)
                    if new_errors:
                        error_text = "".join(new_errors).strip()
                        if error_text:
                            db.add_error_log(bot_id, error_text)
                            try:
                                bot_info = db.get_bot(bot_id)
                                await application.bot.send_message(
                                    chat_id=user_id,
                                    text=f"⚠️ *تنبيه خطأ حقيقي في البوت: {bot_info[3]}*\n\n```\n{error_text[:500]}\n```",
                                    parse_mode="Markdown"
                                )
                            except Exception as e:
                                logger.exception("Failed to send error notification to user %s for bot %s: %s", user_id, bot_id, e)
                last_pos = os.path.getsize(log_file)

    def stop_bot(self, bot_id):
        bot_data = db.get_bot(bot_id)
        pid = bot_data[7] if bot_data else None
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception as e:
                logger.exception("Failed to terminate process %s for bot %s: %s", pid, bot_id, e)
        if bot_id in self.processes: del self.processes[bot_id]
        db.update_bot_status(bot_id, "stopped", None)
        return True

    def get_bot_usage(self, bot_id):
        if not psutil: return 0, 0
        bot_data = db.get_bot(bot_id)
        pid = bot_data[7] if bot_data else None
        if pid:
            try:
                proc = psutil.Process(pid)
                if proc.is_running():
                    return proc.cpu_percent(interval=0.1), proc.memory_info().rss / 1024 / 1024
            except Exception as e:
                logger.exception("psutil process info failed for pid %s: %s", pid, e)
                return 0, 0
        return 0, 0

    # Background enforcement and monitoring
    async def _enforce_loop(self, application):
        while True:
            try:
                running = db.get_all_running_bots()
                now = time.time()
                for bot in running:
                    bot_id = bot[0]
                    pid = bot[7]
                    remaining = bot[11] or 0
                    power = bot[13] or 0.0
                    last_checked = bot[14]
                    warned_low = bot[20] if len(bot) > 20 else 0

                    # compute elapsed since last_checked
                    try:
                        last_ts = int(datetime.fromisoformat(last_checked).timestamp())
                    except Exception as e:
                        logger.exception("Failed to parse last_checked for bot %s: %s", bot_id, e)
                        last_ts = int(now)
                    elapsed = int(now - last_ts)
                    if elapsed <= 0:
                        continue

                    cpu, _ = self.get_bot_usage(bot_id)

                    # idle detection: if very low CPU, reduce drain multiplier
                    drain_factor = self.power_drain_factor
                    if cpu < 2.0:
                        drain_factor *= 0.2

                    # deduct time and power based on elapsed and CPU
                    new_remaining = max(0, int(remaining - elapsed))
                    power_drain = (cpu / 100.0) * elapsed * drain_factor
                    new_power = max(0.0, float(power - power_drain))

                    db.update_bot_resources(bot_id, remaining_seconds=new_remaining, power_remaining=new_power, last_checked=datetime.utcnow().isoformat())

                    # Low-time warning (10 minutes)
                    if new_remaining > 0 and new_remaining <= 600 and not warned_low:
                        try:
                            await application.bot.send_message(chat_id=bot[1], text=f"⚠️ تنبيه: البوت {bot[3]} سيتوقف خلال {seconds_to_human(new_remaining)}. يرجى إضافة وقت لتجنب السكون.")
                            conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("UPDATE bots SET warned_low = 1 WHERE id = ?", (bot_id,)); conn.commit(); conn.close()
                        except Exception as e:
                            logger.exception("Failed to send low-time warning to user %s for bot %s: %s", bot[1], bot_id, e)


                    # If now expired
                    if new_remaining == 0 or new_power == 0.0:
                        # enforce sleep
                        db.set_sleep_mode(bot_id, True, reason="expired")
                        try:
                            await application.bot.send_message(chat_id=bot[1], text=f"⚠️ البوت {bot[3]} دخل وضع السكون بسبب نفاد الوقت أو الطاقة.")
                        except Exception as e:
                            logger.exception("Failed to notify user %s about sleep for bot %s: %s", bot[1], bot_id, e)
                        self.stop_bot(bot_id)

                # cleanup / sleep
            except Exception as e:
                logger.exception("Enforcement loop error: %s", e)
            await asyncio.sleep(30)

    async def start_background_tasks(self, application):
        if self._enforce_task is None:
            self._enforce_task = asyncio.create_task(self._enforce_loop(application))

pm = ProcessManager()

# -----------------------------------------------------------------------------
# CONVERSATION STATES
# -----------------------------------------------------------------------------
WAIT_FILE_UPLOAD, WAIT_MANUAL_TOKEN, WAIT_EDIT_CONTENT, WAIT_FEEDBACK, WAIT_GITHUB_URL, WAIT_DEPLOY_CONFIRM = range(6)

# -----------------------------------------------------------------------------
# HANDLERS
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username)
    user_data = db.get_user(user.id)
    
    if user_data[2] == 'pending' and user.id != ADMIN_ID:
        await update.message.reply_text("⏳ *طلبك قيد المراجعة*\nسيتم إشعارك فور موافقة المالك على دخولك.", parse_mode="Markdown")
        await context.application.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 *طلب انضمام جديد*\nالمستخدم: @{user.username} ({user.id})",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ قبول", callback_data=f"approve_{user.id}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"reject_{user.id}")
            ]]),
            parse_mode="Markdown"
        )
        return

    if user_data[2] == 'blocked':
        await update.message.reply_text("🚫 تم حظرك من استخدام البوت.")
        return

    keyboard = [
        [InlineKeyboardButton("➕ استضافة بوت جديد", callback_data="add_bot"), InlineKeyboardButton("🔁 نشر من GitHub", callback_data="deploy_github")],
        [InlineKeyboardButton("📂 بوتاتي المستضافة", callback_data="my_bots")],
        [InlineKeyboardButton("📊 حالة النظام", callback_data="sys_status")],
        [InlineKeyboardButton("ℹ️ التفاصيل والمعلومات", callback_data="bot_details")]
    ]
    if user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👑 لوحة التحكم", callback_data="admin_panel")])
    
    await update.message.reply_text(
        f"🚀 *NeuroHost V4 – Time, Power & Smart Hosting Edition*\nأهلاً بك {user.first_name}!\n\n💡 _ملاحظة: البوت قيد التطوير ويتحسن باستمرار._",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_data = db.get_user(user.id)
    
    if user_data[2] != 'approved' and user.id != ADMIN_ID:
        await query.edit_message_text("🚫 لا تملك صلاحية الوصول.")
        return

    # Stop any active auto-refresh for this user
    context.user_data['auto_refresh'] = False

    keyboard = [
        [InlineKeyboardButton("➕ استضافة بوت جديد", callback_data="add_bot"), InlineKeyboardButton("🔁 نشر من GitHub", callback_data="deploy_github")],
        [InlineKeyboardButton("📂 بوتاتي المستضافة", callback_data="my_bots")],
        [InlineKeyboardButton("📊 حالة النظام", callback_data="sys_status")],
        [InlineKeyboardButton("ℹ️ التفاصيل والمعلومات", callback_data="bot_details")]
    ]
    if user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👑 لوحة التحكم", callback_data="admin_panel")])
    
    await query.edit_message_text(
        "🎮 *القائمة الرئيسية*\nاختر ما تريد القيام به:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# --- AUTO REFRESH LOGIC ---
async def auto_refresh_task(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id):
    user_id = update.effective_user.id
    context.user_data['auto_refresh'] = True
    
    while context.user_data.get('auto_refresh', False):
        await asyncio.sleep(2) # Refresh every 2 seconds to avoid flood
        if not context.user_data.get('auto_refresh', False): break
        
        bot = db.get_bot(bot_id)
        if not bot: break
        
        cpu, mem = pm.get_bot_usage(bot_id)
        status_icon = "🟢" if bot[4] == "running" else "🔴"
        
        text = (
            f"🤖 *إدارة البوت: {bot[3]}*\n"
            f"━━━━━━━━━━━━━━\n"
            f"🆔 ID: `{bot[0]}`\n"
            f"📡 الحالة: {status_icon} {bot[4]}\n"
            f"🖥 المعالج: `{cpu}%`\n"
            f"🧠 الذاكرة: `{mem:.2f} MB`\n"
            f"📄 الملف: `{bot[6]}`\n"
            f"━━━━━━━━━━━━━━\n"
            f"⏱ _تحديث تلقائي نشط..._"
        )
        
        keyboard = []
        if bot[4] == "stopped":
            keyboard.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{bot_id}")])
        else:
            keyboard.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{bot_id}")])
        
        keyboard.extend([
            [InlineKeyboardButton("📂 الملفات", callback_data=f"files_{bot_id}"), InlineKeyboardButton("📜 السجلات", callback_data=f"logs_{bot_id}")],
            [InlineKeyboardButton("🗑 حذف البوت", callback_data=f"confirm_del_{bot_id}")],
            [InlineKeyboardButton("🔙 عودة", callback_data="my_bots")]
        ])
        
        try:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                context.user_data['auto_refresh'] = False
                break
        except Exception as e:
            logger.exception("Failed to update auto-refresh message for user %s, bot %s: %s", update.effective_user.id if update and update.effective_user else 'unknown', bot_id if 'bot_id' in locals() else 'unknown', e)
            context.user_data['auto_refresh'] = False
            break

# --- BOT DETAILS & FEEDBACK ---
async def bot_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    text = (
        f"ℹ️ *تفاصيل NeuroHost V4 – Time, Power & Smart Hosting Edition*\n\n"
        f"🌟 *الإصدار:* 4.0 (Time & Power Edition)\n"
        f"👨‍💻 *المطور:* {DEVELOPER_USERNAME}\n"
        f"🛠 *الحالة:* قيد التطوير والتحسين المستمر\n\n"
        f"📝 تم تحديث النظام لدعم إدارة الوقت والطاقة، الحماية الذكية، وإمكانية النشر من GitHub.\n\n"
        f"💡 يمكنك إرسال ملاحظاتك أو أفكارك للمطور مباشرة عبر الزر أدناه."
    )
    
    keyboard = [
        [InlineKeyboardButton("👨‍💻 تواصل مع المطور", url=f"https://t.me/{DEVELOPER_USERNAME.replace('@', '')}")],
        [InlineKeyboardButton("📝 إرسال ملاحظة/فكرة", callback_data="send_feedback")],
        [InlineKeyboardButton("🔙 عودة", callback_data="main_menu")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def feedback_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📝 من فضلك اكتب ملاحظتك أو فكرتك وسأقوم بإيصالها للمطور:")
    return WAIT_FEEDBACK

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    db.add_feedback(user.id, text)
    
    # Notify Admin
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📩 *ملاحظة جديدة من مستخدم*\nالمستخدم: @{user.username} ({user.id})\n\nالمحتوى:\n`{text}`",
        parse_mode="Markdown"
    )
    
    await update.message.reply_text("✅ شكراً لك! تم إرسال ملاحظتك بنجاح.")
    return ConversationHandler.END

# --- UPDATED MANAGE BOT ---
async def manage_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[1])
    bot = db.get_bot(bot_id)
    if not bot:
        await query.edit_message_text("❌ البوت غير موجود.")
        return

    # Build initial view with time/power summary
    remaining = bot[11]
    power = bot[13]
    status_icon = "🟢" if bot[4] == "running" else "🔴"
    time_bar = render_bar((remaining / bot[10] * 100) if bot[10] else 0)
    power_bar = render_bar(power)
    expires_text = f"ينتهي في: {seconds_to_human(remaining)}" if remaining and remaining>0 else "منتهي"

    text = (
        f"🤖 *إدارة البوت: {bot[3]}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 ID: `{bot[0]}`\n"
        f"📡 الحالة: {status_icon} {bot[4]}\n"
        f"⏳ الوقت المتبقي: `{seconds_to_human(remaining)}` - {expires_text}\n"
        f"{time_bar}\n"
        f"⚡ الطاقة المتبقية: `{power}%`\n"
        f"{power_bar}\n"
        f"📄 الملف: `{bot[6]}`\n"
        f"━━━━━━━━━━━━━━"
    )

    keyboard = []
    if bot[4] == "stopped":
        keyboard.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{bot_id}")])
    else:
        keyboard.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{bot_id}")])

    keyboard.extend([
        [InlineKeyboardButton("⏳ Hosting Time", callback_data=f"timepanel_{bot_id}"), InlineKeyboardButton("📂 الملفات", callback_data=f"files_{bot_id}")],
        [InlineKeyboardButton("📜 السجلات", callback_data=f"logs_{bot_id}"), InlineKeyboardButton("🗑 حذف البوت", callback_data=f"confirm_del_{bot_id}")],
        [InlineKeyboardButton("🔙 عودة", callback_data="my_bots")]
    ])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # Start auto-refresh task
    asyncio.create_task(auto_refresh_task(update, context, bot_id))

# --- UPDATED LOGS VIEW ---
async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[1])
    logs = db.get_bot_logs(bot_id)
    
    text = "📜 *سجل الأخطاء الحقيقية فقط:*\n\n"
    if not logs:
        text += "لا توجد أخطاء برمجية مسجلة حالياً. (يتم تصفية رسائل INFO تلقائياً)"
    for err, ts in logs:
        text += f"⏰ `{ts}`\n❌ `{err[:300]}...`\n\n"
    
    keyboard = [[InlineKeyboardButton("🔙 عودة", callback_data=f"manage_{bot_id}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# --- Time & Power Panel ---
async def show_time_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[1])
    bot = db.get_bot(bot_id)
    if not bot:
        await query.edit_message_text("❌ البوت غير موجود.")
        return
    remaining = bot[11]
    total = bot[10]
    power = bot[13]
    plan = db.get_user_plan(bot[1])

    text = (
        f"⏳ *لوحة استضافة الوقت والطاقة: {bot[3]}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"💼 الخطة: *{plan}*\n"
        f"⏳ الوقت المستغرق: `{seconds_to_human(total - remaining)}`\n"
        f"🕒 المتبقي: `{seconds_to_human(remaining)}`\n"
        f"🔚 تاريخ الانتهاء المتوقع: `{datetime.utcfromtimestamp(int(time.time()+ (remaining or 0))).isoformat()}`\n"
        f"{render_bar((remaining / total * 100) if total else 0)}\n"
        f"⚡ الطاقة المتبقية: `{power}%`\n"
        f"{render_bar(power)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"*حالة السكون:* {'✅ نشط' if bot[15]==1 else '❌ غير نشط'}\n"
        f"*استخدام Auto-Recovery لهذه البوت:* {'✅ مستخدم' if bot[16]==1 else '❌ متاح'}\n"
        f"━━━━━━━━━━━━━━\n"
        f"اختر كمية وقت لإضافتها (سيتم إضافة طاقة متناسبة):"
    )

    keyboard = [
        [InlineKeyboardButton("➕ 1 ساعة", callback_data=f"add_time_{bot_id}_3600"), InlineKeyboardButton("➕ 12 ساعة", callback_data=f"add_time_{bot_id}_43200")],
        [InlineKeyboardButton("➕ 24 ساعة", callback_data=f"add_time_{bot_id}_86400"), InlineKeyboardButton("➕ 7 أيام", callback_data=f"add_time_{bot_id}_604800")],
    ]

    # If bot is sleeping and user can recover, show restore button
    if bot[15] == 1 and db.can_user_recover(bot[1]):
        keyboard.append([InlineKeyboardButton("🔧 استعادة (Auto-Recovery)", callback_data=f"recover_{bot_id}")])

    keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data=f"manage_{bot_id}")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def attempt_recover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[1])
    bot = db.get_bot(bot_id)
    if not bot:
        await query.edit_message_text("❌ البوت غير موجود.")
        return
    # Check if user allowed recovery today
    if not db.can_user_recover(bot[1]):
        await query.edit_message_text("❌ لقد استخدمت استعادة اليوم بالفعل. حاول غداً.")
        return
    # Only allow recover if bot is sleeping or stopped due to expiry
    if bot[15] == 0:
        await query.edit_message_text("❌ البوت ليس في وضع السكون.")
        return
    # Mark recovery used and reset resource minimally and attempt start
    db.use_user_recovery(bot[1])
    db.mark_bot_auto_recovery_used(bot_id)
    # Give small time/power to resume (e.g., 1 hour and 20% power)
    db.set_bot_time_power(bot_id, total_seconds=3600, power_max=20.0)
    db.update_bot_resources(bot_id, remaining_seconds=3600, power_remaining=20.0, last_checked=datetime.utcnow().isoformat())
    db.set_sleep_mode(bot_id, False)
    success, msg = await pm.start_bot(bot_id, context.application, use_recovery=True)
    if success:
        await query.edit_message_text("✅ تم استعادة البوت وتشغيله باستخدام Auto-Recovery المجانية.")
    else:
        await query.edit_message_text(f"⚠️ تم استعادة الموارد لكن فشل التشغيل: {msg}")

async def add_time_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    bot_id = int(parts[2]); seconds = int(parts[3])
    bot = db.get_bot(bot_id)
    if not bot:
        await query.edit_message_text("❌ البوت غير موجود.")
        return
    user_plan = db.get_user_plan(bot[1])
    plan_limits = {'free': 86400, 'pro': 604800, 'ultra': 10**12}
    plan_max = plan_limits.get(user_plan, 86400)
    current_total = bot[10] or 0
    # Prevent exceeding plan
    if current_total + seconds > plan_max:
        await query.answer("⚠️ لا يمكنك تجاوز حد خطتك.")
        return
    # Compute proportional power to add
    # We'll add power proportional to fraction of plan maximum added
    added_power = min(100.0, (seconds / plan_max) * 100.0)
    new_total = current_total + seconds
    new_remaining = (bot[11] or 0) + seconds
    new_power = min(100.0, (bot[13] or 0) + added_power)
    db.update_bot_resources(bot_id, remaining_seconds=new_remaining, power_remaining=new_power, last_checked=datetime.utcnow().isoformat())
    conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("UPDATE bots SET total_seconds = ?, warned_low = 0 WHERE id = ?", (new_total, bot_id)); conn.commit(); conn.close()

    # Wake up if sleeping
    if bot[15] == 1:
        db.set_sleep_mode(bot_id, False)
        # attempt auto-start
        success, msg = await pm.start_bot(bot_id, context.application)
        if success:
            await query.edit_message_text("✅ تمت إضافة الوقت بنجاح وتم إيقاظ البوت وتشغيله.")
        else:
            await query.edit_message_text(f"✅ تمت إضافة الوقت بنجاح. ولكن: {msg}")
    else:
        await query.edit_message_text("✅ تمت إضافة الوقت والطاقة بنجاح.")

# --- OTHER HANDLERS (SAME AS V3 BUT UPDATED UI) ---
async def my_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['auto_refresh'] = False
    bots = db.get_user_bots(update.effective_user.id)
    
    if not bots:
        await query.edit_message_text("📂 لا تملك أي بوتات مستضافة حالياً.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="main_menu")]]))
        return

    keyboard = []
    for bid, name, status, _ in bots:
        icon = "🟢" if status == "running" else "🔴"
        bot = db.get_bot(bid)
        remaining = bot[11]
        expires = seconds_to_human(remaining) if remaining and remaining>0 else "منتهي"
        sleep_icon = " 🛌" if bot[15]==1 else ""
        label = f"{icon} {name}{sleep_icon} — ⏳ {expires} — ⚡ {int(bot[13] or 0)}%"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"manage_{bid}")])
    
    keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data="main_menu")])
    await query.edit_message_text("📂 *قائمة بوتاتك المستضافة:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def sys_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if psutil:
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        usage_text = f"🖥 المعالج: `{cpu}%`\n🧠 الذاكرة: `{mem}%`"
    else:
        usage_text = "⚠️ معلومات النظام غير متوفرة."
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT count(*) FROM bots")
    total_bots = c.fetchone()[0]
    c.execute("SELECT count(*) FROM users")
    total_users = c.fetchone()[0]
    conn.close()
    
    text = (
        f"📊 *إحصائيات النظام الحية*\n"
        f"━━━━━━━━━━━━━━\n"
        f"{usage_text}\n"
        f"👥 المستخدمين: `{total_users}`\n"
        f"🤖 البوتات المستضافة: `{total_bots}`\n"
        f"━━━━━━━━━━━━━━"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="main_menu")]]), parse_mode="Markdown")

# --- ADMIN PANEL ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID: return
    pending = db.get_pending_users()
    keyboard = [
        [InlineKeyboardButton(f"👥 طلبات الانضمام ({len(pending)})", callback_data="pending_users")],
        [InlineKeyboardButton("🔙 عودة", callback_data="main_menu")]
    ]
    await query.edit_message_text("👑 *لوحة تحكم المالك*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def list_pending_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pending = db.get_pending_users()
    if not pending:
        await query.edit_message_text("✅ لا توجد طلبات معلقة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="admin_panel")]]))
        return
    keyboard = [[InlineKeyboardButton(f"👤 @{u[1]} ({u[0]})", callback_data=f"viewuser_{u[0]}")] for u in pending]
    keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data="admin_panel")])
    await query.edit_message_text("👥 *الطلبات المعلقة:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, user_id = query.data.split("_")
    user_id = int(user_id)
    if action == "approve":
        db.update_user_status(user_id, 'approved')
        await query.edit_message_text(f"✅ تم قبول {user_id}")
        try:
            await context.bot.send_message(chat_id=user_id, text="🎉 تم قبول طلبك!")
        except Exception as e:
            logger.exception("Failed to send approval message to user %s: %s", user_id, e)
    else:
        db.update_user_status(user_id, 'blocked')
        await query.edit_message_text(f"❌ تم حظر {user_id}")

# --- FILE MANAGEMENT ---
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[1])
    bot = db.get_bot(bot_id)
    bot_path = os.path.join(BOTS_DIR, bot[5])
    files = [f for f in os.listdir(bot_path) if os.path.isfile(os.path.join(bot_path, f))]
    keyboard = [[InlineKeyboardButton(f"📄 {f}", callback_data=f"fview_{bot_id}_{f}")] for f in files]
    keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data=f"manage_{bot_id}")])
    await query.edit_message_text(f"📁 *ملفات البوت: {bot[3]}*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def file_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, bot_id, filename = query.data.split("_", 2)
    bot = db.get_bot(int(bot_id))
    file_path = os.path.join(BOTS_DIR, bot[5], filename)
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()[:1000]
    except Exception as e:
        logger.exception("Failed to read file %s for viewing: %s", file_path, e)
        content = "لا يمكن العرض."
    keyboard = [[InlineKeyboardButton("🗑 حذف", callback_data=f"fdel_{bot_id}_{filename}")], [InlineKeyboardButton("🔙 عودة", callback_data=f"files_{bot_id}")]]
    await query.edit_message_text(f"📄 `{filename}`\n\n```python\n{content}\n```", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def file_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, bot_id, filename = query.data.split("_", 2)
    bot = db.get_bot(int(bot_id))
    if filename == bot[6]:
        await query.message.reply_text("❌ لا يمكن حذف الملف الرئيسي.")
        return
    os.remove(os.path.join(BOTS_DIR, bot[5], filename))
    query.data = f"files_{bot_id}"
    await list_files(update, context)

# --- ADD BOT FLOW ---
async def add_bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📤 أرسل ملف البوت (.py):")
    return WAIT_FILE_UPLOAD

async def handle_bot_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".py"):
        await update.message.reply_text("❌ ملف .py فقط.")
        return WAIT_FILE_UPLOAD
    folder = f"bot_{update.effective_user.id}_{int(time.time())}"
    path = os.path.join(BOTS_DIR, folder)
    os.makedirs(path, exist_ok=True)
    file = await context.bot.get_file(doc.file_id)
    file_path = os.path.join(path, doc.file_name)
    await file.download_to_drive(file_path)
    
    # Extract token
    token = None
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            match = re.search(r'[0-9]{8,10}:[a-zA-Z0-9_-]{35}', f.read())
            if match: token = match.group(0)
    except Exception as e:
        logger.exception("Failed to read uploaded bot file %s: %s", file_path, e)
    
    context.user_data['new_bot'] = {'name': doc.file_name, 'folder': folder, 'main_file': doc.file_name}
    if token:
        db.add_bot(update.effective_user.id, token, doc.file_name, folder, doc.file_name)
        await update.message.reply_text("✅ تم الكشف عن التوكن وإضافة البوت!")
        return ConversationHandler.END
    else:
        await update.message.reply_text("⚠️ أرسل التوكن يدوياً:")
        return WAIT_MANUAL_TOKEN

async def handle_manual_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text
    data = context.user_data['new_bot']
    db.add_bot(update.effective_user.id, token, data['name'], data['folder'], data['main_file'])
    await update.message.reply_text("✅ تمت الإضافة بنجاح!")
    return ConversationHandler.END

# --- GITHUB DEPLOY FLOW ---
async def deploy_github_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🔗 أرسل رابط GitHub (مثال: https://github.com/username/repo):")
    return WAIT_GITHUB_URL

async def handle_github_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user
    # Basic validation
    if not url.startswith('https://github.com/'):
        await update.message.reply_text("❌ رابط غير صالح. الرجاء إرسال عنوان مستودع GitHub صالح.")
        return WAIT_GITHUB_URL

    folder = f"gh_{user.id}_{int(time.time())}"
    dest = os.path.join(BOTS_DIR, folder)
    try:
        # Attempt to clone
        proc = subprocess.run(["git", "clone", url, dest], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            await update.message.reply_text(f"❌ فشل الاستنساخ: {proc.stderr[:500]}")
            return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ أثناء الاستنساخ: {e}")
        return ConversationHandler.END

    # Detect main file
    candidates = ['main.py', 'bot.py', 'app.py']
    found = None
    for c in candidates:
        for root, dirs, files in os.walk(dest):
            if c in files:
                rel = os.path.relpath(os.path.join(root, c), dest)
                found = rel
                break
        if found: break

    # Detect token in files
    token = None
    for root, dirs, files in os.walk(dest):
        for f in files:
            if f.endswith('.py'):
                try:
                    with open(os.path.join(root, f), 'r', encoding='utf-8') as fh:
                        data = fh.read()
                        m = re.search(r'[0-9]{8,10}:[a-zA-Z0-9_-]{35}', data)
                        if m:
                            token = m.group(0)
                            break
                except Exception as e:
                    logger.exception("Failed to read file %s while scanning for token: %s", os.path.join(root, f), e)
        if token: break

    # Detect requirements
    req_found = False
    for root, dirs, files in os.walk(dest):
        if 'requirements.txt' in files:
            req_found = True
            break

    context.user_data['gh_deploy'] = {'folder': folder, 'path': dest, 'main_file': found, 'token': token, 'has_reqs': req_found}

    text = f"🔎 تم استنساخ المستودع. تم اكتشاف ملف التشغيل: `{found or 'غير موجود'}`\n"
    if req_found:
        text += "🔧 يوجد ملف requirements.txt وسيتم تثبيت الحزم عند التشغيل الأول تلقائياً.\n"
    if token:
        text += "✅ تم اكتشاف توكن داخل المشروع، سيتم عرضه عند النشر.\n"
    else:
        text += "⚠️ لم يتم اكتشاف توكن تلقائياً. ستحتاج لإدخاله يدوياً بعد النشر أو إضافته في إعدادات المشروع.\n"
    text += "\nهل تريد نشر هذا المستودع كبوت؟"

    keyboard = [[InlineKeyboardButton("✅ نشر", callback_data="gh_confirm")], [InlineKeyboardButton("❌ إلغاء", callback_data="gh_cancel")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return WAIT_DEPLOY_CONFIRM

async def handle_gh_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get('gh_deploy')
    if not data:
        await query.edit_message_text("❌ لا توجد بيانات للنشر.")
        return ConversationHandler.END
    folder = data['folder']
    main_file = data['main_file'] or 'main.py'
    token = data['token']
    # Register bot
    name = os.path.basename(folder)
    bot_id = db.add_bot(update.effective_user.id, token, name, folder, main_file)
    await query.edit_message_text(f"✅ تم نشر المستودع كبوت بنجاح. ID: {bot_id}")
    return ConversationHandler.END

async def handle_gh_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get('gh_deploy')
    if data:
        # cleanup cloned folder
        try:
            shutil.rmtree(data['path'], ignore_errors=True)
        except Exception as e:
            logger.exception("Failed to cleanup cloned repo at %s: %s", data.get('path'), e)
    await query.edit_message_text("❌ تم إلغاء النشر.")
    return ConversationHandler.END

# --- ACTIONS ---
async def start_bot_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[1])
    success, msg = await pm.start_bot(bot_id, context.application)
    await query.message.reply_text(msg)

async def stop_bot_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[1])
    pm.stop_bot(bot_id)
    await query.message.reply_text("🛑 تم الإيقاف.")

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[2])
    keyboard = [[InlineKeyboardButton("✅ حذف", callback_data=f"del_{bot_id}"), InlineKeyboardButton("❌ تراجع", callback_data=f"manage_{bot_id}")]]
    await query.edit_message_text("⚠️ حذف نهائي؟", reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_bot_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split("_")[1])
    bot = db.get_bot(bot_id)
    pm.stop_bot(bot_id)
    if bot: shutil.rmtree(os.path.join(BOTS_DIR, bot[5]), ignore_errors=True)
    db.delete_bot(bot_id)
    await query.message.reply_text("🗑 تم الحذف.")
    await my_bots(update, context)

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    if not os.path.exists(BOTS_DIR): os.makedirs(BOTS_DIR)
    app = ApplicationBuilder().token(TOKEN).build()

    # Conversations
    add_bot_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_bot_start, pattern="^add_bot$")],
        states={
            WAIT_FILE_UPLOAD: [MessageHandler(filters.Document.ALL, handle_bot_file)],
            WAIT_MANUAL_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_token)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    
    feedback_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(feedback_start, pattern="^send_feedback$")],
        states={WAIT_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_feedback)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    gh_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deploy_github_start, pattern="^deploy_github$")],
        states={
            WAIT_GITHUB_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_github_url)],
            WAIT_DEPLOY_CONFIRM: [CallbackQueryHandler(handle_gh_confirm, pattern="^gh_confirm$"), CallbackQueryHandler(handle_gh_cancel, pattern="^gh_cancel$")]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    app.add_handler(CommandHandler("start", start))
    # Start background enforcement/monitor tasks when app starts
    app.post_init = lambda _: asyncio.create_task(pm.start_background_tasks(app))
    app.add_handler(add_bot_conv)
    app.add_handler(feedback_conv)
    app.add_handler(gh_conv)
    app.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(my_bots, pattern="^my_bots$"))
    app.add_handler(CallbackQueryHandler(manage_bot, pattern="^manage_"))
    app.add_handler(CallbackQueryHandler(start_bot_action, pattern="^start_"))
    app.add_handler(CallbackQueryHandler(stop_bot_action, pattern="^stop_"))
    app.add_handler(CallbackQueryHandler(confirm_delete, pattern="^confirm_del_"))
    app.add_handler(CallbackQueryHandler(delete_bot_action, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(view_logs, pattern="^logs_"))
    app.add_handler(CallbackQueryHandler(sys_status, pattern="^sys_status$"))
    app.add_handler(CallbackQueryHandler(bot_details, pattern="^bot_details$"))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(list_pending_users, pattern="^pending_users$"))
    app.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|reject)_"))
    app.add_handler(CallbackQueryHandler(list_files, pattern="^files_"))
    app.add_handler(CallbackQueryHandler(file_view, pattern="^fview_"))
    app.add_handler(CallbackQueryHandler(file_delete, pattern="^fdel_"))
    app.add_handler(CallbackQueryHandler(show_time_panel, pattern="^timepanel_"))
    app.add_handler(CallbackQueryHandler(add_time_action, pattern="^add_time_"))
    app.add_handler(CallbackQueryHandler(attempt_recover, pattern="^recover_"))

    if psutil is None:
        logger.warning("psutil is not installed: CPU and memory metrics will be limited. Install requirements.txt to enable full features.")

    # Initialize file logging and asyncio exception handler
    setup_file_logging()
    try:
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(asyncio_exception_handler)
    except Exception as e:
        logger.warning("Failed to set asyncio exception handler: %s", e)

    print("🚀 NeuroHost V4 – Time, Power & Smart Hosting Edition is running...")
    try:
        app.run_polling()
    except Exception as e:
        logger.exception("Fatal error while running the bot: %s", e)
        # also write a compact entry to the error file
        try:
            with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as ef:
                import traceback
                ef.write(f"\n===== Fatal run_polling exception: {datetime.utcnow().isoformat()} =====\n")
                traceback.print_exc(file=ef)
        except Exception:
            pass

if __name__ == "__main__":
    main()
