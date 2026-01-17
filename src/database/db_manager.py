import sqlite3
from datetime import datetime
from src.config.config import ADMIN_ID

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
        # Bots table extended with time/power/sleep/restart fields
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
                warned_low INTEGER DEFAULT 0,
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

        # Migrations
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
        plan = self.get_user_plan(user_id)
        plan_limits = {'free': 86400, 'pro': 604800, 'ultra': 10**12}
        plan_power = {'free': 30.0, 'pro': 60.0, 'ultra': 100.0}
        total_seconds = plan_limits.get(plan, 86400)
        power = plan_power.get(plan, 30.0)

        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO bots (user_id, token, name, folder, main_file, total_seconds, remaining_seconds, power_max, power_remaining) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                      (user_id, token, name, folder, main_file, total_seconds, total_seconds, power, power))
            return c.lastrowid

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
