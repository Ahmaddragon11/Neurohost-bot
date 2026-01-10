# -----------------------------------------------------------------------------
# NEUROHOST BOT CONTROLLER V3.5 - CREATIVE EDITION
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
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                status TEXT DEFAULT 'pending',
                bot_limit INTEGER DEFAULT 3,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
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
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO bots (user_id, token, name, folder, main_file) VALUES (?, ?, ?, ?, ?)", 
                      (user_id, token, name, folder, main_file))
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

db = Database(DB_FILE)

# -----------------------------------------------------------------------------
# PROCESS MANAGER
# -----------------------------------------------------------------------------
class ProcessManager:
    def __init__(self):
        self.processes = {}

    async def start_bot(self, bot_id, application):
        bot_data = db.get_bot(bot_id)
        if not bot_data: return False, "البوت غير موجود."
        
        _, user_id, token, name, _, folder, main_file, _, _ = bot_data
        bot_path = os.path.abspath(os.path.join(BOTS_DIR, folder))
        
        # Auto-install requirements
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
            
            asyncio.create_task(self.watch_errors(bot_id, stderr_file, user_id, application))
            return True, "🚀 تم التشغيل بنجاح."
        except Exception as e:
            return False, str(e)

    async def watch_errors(self, bot_id, log_file, user_id, application):
        last_pos = os.path.getsize(log_file)
        while bot_id in self.processes and self.processes[bot_id].poll() is None:
            await asyncio.sleep(2)
            if os.path.getsize(log_file) > last_pos:
                with open(log_file, 'r') as f:
                    f.seek(last_pos)
                    lines = f.readlines()
                    new_errors = []
                    for line in lines:
                        # Filter out INFO and DEBUG messages, keep only actual errors/tracebacks
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
                            except: pass
                last_pos = os.path.getsize(log_file)

    def stop_bot(self, bot_id):
        bot_data = db.get_bot(bot_id)
        pid = bot_data[7] if bot_data else None
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except: pass
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
            except: pass
        return 0, 0

pm = ProcessManager()

# -----------------------------------------------------------------------------
# CONVERSATION STATES
# -----------------------------------------------------------------------------
WAIT_FILE_UPLOAD, WAIT_MANUAL_TOKEN, WAIT_EDIT_CONTENT, WAIT_FEEDBACK = range(4)

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
        [InlineKeyboardButton("➕ استضافة بوت جديد", callback_data="add_bot")],
        [InlineKeyboardButton("📂 بوتاتي المستضافة", callback_data="my_bots")],
        [InlineKeyboardButton("📊 حالة النظام", callback_data="sys_status")],
        [InlineKeyboardButton("ℹ️ التفاصيل والمعلومات", callback_data="bot_details")]
    ]
    if user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👑 لوحة التحكم", callback_data="admin_panel")])
    
    await update.message.reply_text(
        f"🚀 *NeuroHost V3.5 - Creative Edition*\nأهلاً بك {user.first_name}!\n\n💡 _ملاحظة: البوت قيد التطوير ويتحسن باستمرار._",
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
        [InlineKeyboardButton("➕ استضافة بوت جديد", callback_data="add_bot")],
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
        except:
            context.user_data['auto_refresh'] = False
            break

# --- BOT DETAILS & FEEDBACK ---
async def bot_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    text = (
        f"ℹ️ *تفاصيل NeuroHost V3.5*\n\n"
        f"🌟 *الإصدار:* 3.5 (Creative Edition)\n"
        f"👨‍💻 *المطور:* {DEVELOPER_USERNAME}\n"
        f"🛠 *الحالة:* قيد التطوير المستمر\n\n"
        f"📝 هذا البوت مصمم لتوفير بيئة استضافة آمنة وسهلة لبوتات التيليجرام مع ميزات مراقبة متقدمة.\n\n"
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
        keyboard.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"manage_{bid}")])
    
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
        try: await context.bot.send_message(chat_id=user_id, text="🎉 تم قبول طلبك!")
        except: pass
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
    except: content = "لا يمكن العرض."
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
        with open(file_path, 'r') as f:
            match = re.search(r'[0-9]{8,10}:[a-zA-Z0-9_-]{35}', f.read())
            if match: token = match.group(0)
    except: pass
    
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(add_bot_conv)
    app.add_handler(feedback_conv)
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

    print("🚀 NeuroHost V3.5 is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
