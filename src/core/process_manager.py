import os
import sys
import time
import signal
import asyncio
import subprocess
import logging
import html
from datetime import datetime
try:
    import psutil
except ImportError:
    psutil = None

from src.config.config import BOTS_DIR, ERROR_LOG_FILE
from src.utils.helpers import seconds_to_human

logger = logging.getLogger(__name__)

class ProcessManager:
    def __init__(self, db):
        self.db = db
        self.processes = {}
        self._enforce_task = None
        self.restart_cooldown = 60  # seconds
        self.restart_power_cost = 2.0  # percent
        self.restart_time_cost = 60  # seconds
        self.restart_anti_loop_limit = 5  # max restarts in window
        self.restart_window_seconds = 3600  # 1 hour window for anti-loop
        self.power_drain_factor = 0.02  # multiplier for cpu*seconds -> power%

    async def start_bot(self, bot_id, application, use_recovery=False):
        bot_data = self.db.get_bot(bot_id)
        if not bot_data: return False, "البوت غير موجود."
        
        # indices based on DB schema
        _, user_id, token, name, status, folder, main_file, _, _, start_time, total_seconds, remaining_seconds, power_max, power_remaining, last_checked, sleep_mode, auto_recovery_used, restart_count, last_restart_at, last_sleep_reason, warned_low = bot_data

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
            self.db.update_bot_status(bot_id, "running", p.pid)
            
            now = int(time.time())
            if not start_time:
                self.db.update_bot_resources(bot_id, last_checked=datetime.utcnow().isoformat())
                # Update start_time separately since update_bot_resources doesn't handle it
                import sqlite3
                with sqlite3.connect(self.db.db_file) as conn:
                    conn.execute("UPDATE bots SET start_time = ? WHERE id = ?", (now, bot_id))
            else:
                self.db.update_last_checked(bot_id)

            self.db.reset_restart_count(bot_id)

            application.create_task(self.watch_errors(bot_id, stderr_file, user_id, application))
            application.create_task(self._watch_process_exit(bot_id, p, user_id, application))
            return True, "🚀 تم التشغيل بنجاح."
        except Exception as e:
            logger.exception("Failed to start bot %s: %s", bot_id, e)
            return False, str(e)

    async def _watch_process_exit(self, bot_id, process, user_id, application):
        while True:
            await asyncio.sleep(1)
            if process.poll() is not None:
                code = process.returncode
                self.db.add_error_log(bot_id, f"Process exited with code {code}")
                if bot_id in self.processes: del self.processes[bot_id]
                if code != 0:
                    await asyncio.sleep(2)
                    await self._handle_unexpected_exit(bot_id, user_id, application, exit_code=code)
                else:
                    self.db.update_bot_status(bot_id, "stopped", None)
                break

    async def _handle_unexpected_exit(self, bot_id, user_id, application, exit_code=1):
        bot = self.db.get_bot(bot_id)
        if not bot: return
        
        sleep_mode = bot[15]
        remaining_seconds = bot[11]
        power_remaining = bot[13]
        auto_recovery_used = bot[16]
        restart_count = bot[17]
        last_restart_at = bot[18]

        if restart_count >= self.restart_anti_loop_limit:
            self.db.set_sleep_mode(bot_id, True, reason="anti_loop")
            self.db.log_restart_event(bot_id, "Auto-restart disabled due to too many restarts.")
            try:
                await application.bot.send_message(chat_id=bot[1], text=f"⚠️ البوت {bot[3]} تم إيقافه آلياً بسبب تكرار الإعادات.")
            except Exception: pass
            return

        if last_restart_at:
            try:
                lr = datetime.fromisoformat(last_restart_at)
                if (datetime.utcnow() - lr).total_seconds() < self.restart_cooldown:
                    self.db.log_restart_event(bot_id, "Restart skipped due to cooldown.")
                    return
            except Exception: pass

        if (remaining_seconds <= 0 or power_remaining <= 0) and self.db.can_user_recover(bot[1]) and auto_recovery_used == 0:
            self.db.use_user_recovery(bot[1])
            self.db.mark_bot_auto_recovery_used(bot_id)
            self.db.log_restart_event(bot_id, "Auto-recovery used to restart bot for free.")
            success, msg = await self.start_bot(bot_id, application, use_recovery=True)
            if success:
                try:
                    await application.bot.send_message(chat_id=bot[1], text=f"🔄 تم استعادة {bot[3]} باستخدام Auto-Recovery المجانية.")
                except Exception: pass
                return

        if remaining_seconds <= 0 or power_remaining <= 0 or sleep_mode:
            self.db.set_sleep_mode(bot_id, True, reason="expired_or_no_power")
            try:
                await application.bot.send_message(chat_id=bot[1], text=f"⚠️ البوت {bot[3]} توقف بسبب نفاد الوقت أو الطاقة ودخل وضع السكون.")
            except Exception: pass
            return

        new_power = max(0.0, power_remaining - self.restart_power_cost)
        new_remaining = max(0, remaining_seconds - self.restart_time_cost)
        self.db.update_bot_resources(bot_id, remaining_seconds=new_remaining, power_remaining=new_power, last_checked=datetime.utcnow().isoformat())
        self.db.increment_restart(bot_id)
        self.db.log_restart_event(bot_id, f"Auto-restarting after exit code {exit_code}")
        await asyncio.sleep(3)
        success, msg = await self.start_bot(bot_id, application)
        if success:
            try:
                await application.bot.send_message(chat_id=bot[1], text=f"♻️ تم إعادة تشغيل البوت {bot[3]} تلقائياً.")
            except Exception: pass
        else:
            self.db.log_restart_event(bot_id, f"Auto-restart failed: {msg}")

    async def watch_errors(self, bot_id, log_file, user_id, application):
        last_pos = os.path.getsize(log_file) if os.path.exists(log_file) else 0
        while bot_id in self.processes and self.processes[bot_id].poll() is None:
            await asyncio.sleep(2)
            if os.path.exists(log_file) and os.path.getsize(log_file) > last_pos:
                try:
                    with open(log_file, 'r') as f:
                        f.seek(last_pos)
                        lines = f.readlines()
                        new_errors = []
                        for line in lines:
                            if any(x in line.upper() for x in ["ERROR", "CRITICAL", "TRACEBACK", "EXCEPTION"]):
                                new_errors.append(line)
                            elif not any(x in line.upper() for x in ["INFO", "DEBUG", "HTTP REQUEST"]):
                                new_errors.append(line)
                        if new_errors:
                            error_text = "".join(new_errors).strip()
                            if error_text:
                                self.db.add_error_log(bot_id, error_text)
                                try:
                                    bot_info = self.db.get_bot(bot_id)
                                    safe_error = html.escape(error_text[:500])
                                    await application.bot.send_message(
                                        chat_id=user_id,
                                        text=f"⚠️ <b>تنبيه خطأ حقيقي في البوت: {html.escape(bot_info[3])}</b>\n\n<code>{safe_error}</code>",
                                        parse_mode="HTML"
                                    )
                                except Exception: pass
                    last_pos = os.path.getsize(log_file)
                except Exception: pass

    def stop_bot(self, bot_id):
        bot_data = self.db.get_bot(bot_id)
        pid = bot_data[7] if bot_data else None
        if pid:
            try:
                if psutil and psutil.pid_exists(pid):
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception: pass
        if bot_id in self.processes: del self.processes[bot_id]
        self.db.update_bot_status(bot_id, "stopped", None)
        return True

    def get_bot_usage(self, bot_id):
        if not psutil: return 0, 0
        bot_data = self.db.get_bot(bot_id)
        pid = bot_data[7] if bot_data else None
        if pid:
            try:
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    if proc.is_running():
                        return proc.cpu_percent(interval=0.1), proc.memory_info().rss / 1024 / 1024
            except Exception: pass
        return 0, 0

    async def _enforce_loop(self, application):
        while True:
            try:
                running = self.db.get_all_running_bots()
                now = time.time()
                for bot in running:
                    bot_id = bot[0]
                    remaining = bot[11] or 0
                    power = bot[13] or 0.0
                    last_checked = bot[14]
                    warned_low = bot[20]

                    try:
                        last_ts = int(datetime.fromisoformat(last_checked).timestamp())
                    except Exception:
                        last_ts = int(now)
                    elapsed = int(now - last_ts)
                    if elapsed <= 0: continue

                    cpu, _ = self.get_bot_usage(bot_id)
                    drain_factor = self.power_drain_factor
                    if cpu < 2.0: drain_factor *= 0.2

                    new_remaining = max(0, int(remaining - elapsed))
                    power_drain = (cpu / 100.0) * elapsed * drain_factor
                    new_power = max(0.0, float(power - power_drain))

                    self.db.update_bot_resources(bot_id, remaining_seconds=new_remaining, power_remaining=new_power, last_checked=datetime.utcnow().isoformat())

                    if new_remaining > 0 and new_remaining <= 600 and not warned_low:
                        try:
                            await application.bot.send_message(chat_id=bot[1], text=f"⚠️ تنبيه: البوت {bot[3]} سيتوقف خلال {seconds_to_human(new_remaining)}. يرجى إضافة وقت لتجنب السكون.")
                            import sqlite3
                            with sqlite3.connect(self.db.db_file) as conn:
                                conn.execute("UPDATE bots SET warned_low = 1 WHERE id = ?", (bot_id,))
                        except Exception: pass

                    if new_remaining == 0 or new_power == 0.0:
                        self.db.set_sleep_mode(bot_id, True, reason="expired")
                        try:
                            await application.bot.send_message(chat_id=bot[1], text=f"⚠️ البوت {bot[3]} دخل وضع السكون بسبب نفاد الوقت أو الطاقة.")
                        except Exception: pass
                        self.stop_bot(bot_id)
            except Exception: pass
            await asyncio.sleep(30)

    async def start_background_tasks(self, application):
        if self._enforce_task is None:
            self._enforce_task = application.create_task(self._enforce_loop(application))
