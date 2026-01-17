import os
import time
import logging
import sqlite3
import shutil
import re
import html
import asyncio
import subprocess
from datetime import datetime
try:
    import psutil
except ImportError:
    psutil = None

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from src.config.config import ADMIN_ID, DEVELOPER_USERNAME, DB_FILE, BOTS_DIR
from src.utils.helpers import seconds_to_human, render_bar

logger = logging.getLogger(__name__)

# Conversation States
WAIT_FILE_UPLOAD, WAIT_MANUAL_TOKEN, WAIT_EDIT_CONTENT, WAIT_FEEDBACK, WAIT_GITHUB_URL, WAIT_DEPLOY_CONFIRM = range(6)

class BotHandlers:
    def __init__(self, db, pm):
        self.db = db
        self.pm = pm

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        self.db.add_user(user.id, user.username)
        user_data = self.db.get_user(user.id)
        
        if user_data[2] == 'pending' and user.id != ADMIN_ID:
            await update.message.reply_text("⏳ <b>طلبك قيد المراجعة</b>\nسيتم إشعارك فور موافقة المالك على دخولك.", parse_mode="HTML")
            try:
                await context.application.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🔔 <b>طلب انضمام جديد</b>\nالمستخدم: @{user.username} (<code>{user.id}</code>)",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ قبول", callback_data=f"approve_{user.id}"),
                        InlineKeyboardButton("❌ رفض", callback_data=f"reject_{user.id}")
                    ]]),
                    parse_mode="HTML"
                )
            except Exception: pass
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

    async def main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user = update.effective_user
        user_data = self.db.get_user(user.id)
        
        if user_data[2] != 'approved' and user.id != ADMIN_ID:
            await query.edit_message_text("🚫 لا تملك صلاحية الوصول.")
            return

        context.user_data['menu_token'] = context.user_data.get('menu_token', 0) + 1
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

    async def auto_refresh_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id):
        user_id = update.effective_user.id
        current_menu_token = context.user_data.get('menu_token', 0) + 1
        context.user_data['menu_token'] = current_menu_token
        context.user_data['auto_refresh'] = True
        
        last_update = 0
        refresh_interval = 10 

        while context.user_data.get('auto_refresh', False):
            try:
                if context.user_data.get('menu_token') != current_menu_token:
                    break

                await asyncio.sleep(1)
                if not context.user_data.get('auto_refresh', False): break
                
                now = time.time()
                if now - last_update < refresh_interval:
                    continue

                bot = self.db.get_bot(bot_id)
                if not bot: break
                
                cpu, mem = self.pm.get_bot_usage(bot_id)
                status_icon = "🟢" if bot[4] == "running" else "🔴"
                
                text = (
                    f"🤖 <b>إدارة البوت: {html.escape(bot[3])}</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🆔 ID: <code>{bot[0]}</code>\n"
                    f"📡 الحالة: {status_icon} {bot[4]}\n"
                    f"🖥 المعالج: <code>{cpu}%</code>\n"
                    f"🧠 الذاكرة: <code>{mem:.2f} MB</code>\n"
                    f"📄 الملف: <code>{html.escape(bot[6])}</code>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"⏱ <i>تحديث تلقائي نشط (كل {refresh_interval} ثوانٍ)...</i>"
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
                
                if context.user_data.get('menu_token') != current_menu_token:
                    break

                await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
                last_update = time.time()

            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    context.user_data['auto_refresh'] = False
                    break
            except Exception:
                context.user_data['auto_refresh'] = False
                break

    async def bot_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        text = (
            f"ℹ️ *تفاصيل NeuroHost V4 – Time, Power & Smart Hosting Edition*\n\n"
            f"🌟 *الإصدار:* 4.0 (Time & Power Edition)\n"
            f"👨‍💻 *المطور:* {DEVELOPER_USERNAME}\n"
            f"🛠 *الحالة:* قيد التطوير والتحسين المستمر\n\n"
            f"💡 يمكنك إرسال ملاحظاتك أو أفكارك للمطور مباشرة عبر الزر أدناه."
        )
        
        keyboard = [
            [InlineKeyboardButton("👨‍💻 تواصل مع المطور", url=f"https://t.me/{DEVELOPER_USERNAME.replace('@', '')}")],
            [InlineKeyboardButton("📝 إرسال ملاحظة/فكرة", callback_data="send_feedback")],
            [InlineKeyboardButton("🔙 عودة", callback_data="main_menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def feedback_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.message.reply_text("📝 من فضلك اكتب ملاحظتك أو فكرتك وسأقوم بإيصالها للمطور:")
        return WAIT_FEEDBACK

    async def handle_feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        text = update.message.text
        self.db.add_feedback(user.id, text)
        
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📩 *ملاحظة جديدة من مستخدم*\nالمستخدم: @{user.username} ({user.id})\n\nالمحتوى:\n`{text}`",
                parse_mode="Markdown"
            )
        except Exception: pass
        
        await update.message.reply_text("✅ شكراً لك! تم إرسال ملاحظتك بنجاح.")
        return ConversationHandler.END

    async def manage_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data['menu_token'] = context.user_data.get('menu_token', 0) + 1
        context.user_data['auto_refresh'] = False
        
        bot_id = int(query.data.split("_")[1])
        bot = self.db.get_bot(bot_id)
        if not bot:
            await query.edit_message_text("❌ البوت غير موجود.")
            return

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
        context.application.create_task(self.auto_refresh_task(update, context, bot_id))

    async def view_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data['menu_token'] = context.user_data.get('menu_token', 0) + 1
        context.user_data['auto_refresh'] = False
        
        bot_id = int(query.data.split("_")[1])
        logs = self.db.get_bot_logs(bot_id)
        
        text = "📜 *سجل الأخطاء الحقيقية فقط:*\n\n"
        if not logs:
            text += "لا توجد أخطاء برمجية مسجلة حالياً."
        for err, ts in logs:
            text += f"⏰ `{ts}`\n❌ `{err[:300]}...`\n\n"
        
        keyboard = [[InlineKeyboardButton("🔙 عودة", callback_data=f"manage_{bot_id}")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def show_time_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data['menu_token'] = context.user_data.get('menu_token', 0) + 1
        context.user_data['auto_refresh'] = False
        
        bot_id = int(query.data.split("_")[1])
        bot = self.db.get_bot(bot_id)
        if not bot:
            await query.edit_message_text("❌ البوت غير موجود.")
            return
        remaining = bot[11]
        total = bot[10]
        power = bot[13]
        plan = self.db.get_user_plan(bot[1])

        text = (
            f"⏳ *لوحة استضافة الوقت والطاقة: {bot[3]}*\n"
            f"━━━━━━━━━━━━━━\n"
            f"💼 الخطة: *{plan}*\n"
            f"⏳ الوقت المستغرق: `{seconds_to_human(total - remaining)}`\n"
            f"🕒 المتبقي: `{seconds_to_human(remaining)}`\n"
            f"{render_bar((remaining / total * 100) if total else 0)}\n"
            f"⚡ الطاقة المتبقية: `{power}%`\n"
            f"{render_bar(power)}\n"
            f"━━━━━━━━━━━━━━\n"
            f"اختر كمية وقت لإضافتها:"
        )

        keyboard = [
            [InlineKeyboardButton("➕ 1 ساعة", callback_data=f"add_time_{bot_id}_3600"), InlineKeyboardButton("➕ 12 ساعة", callback_data=f"add_time_{bot_id}_43200")],
            [InlineKeyboardButton("➕ 24 ساعة", callback_data=f"add_time_{bot_id}_86400"), InlineKeyboardButton("➕ 7 أيام", callback_data=f"add_time_{bot_id}_604800")],
        ]

        if bot[15] == 1 and self.db.can_user_recover(bot[1]):
            keyboard.append([InlineKeyboardButton("🔧 استعادة (Auto-Recovery)", callback_data=f"recover_{bot_id}")])

        keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data=f"manage_{bot_id}")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def attempt_recover(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        bot_id = int(query.data.split("_")[1])
        bot = self.db.get_bot(bot_id)
        if not bot: return
        if not self.db.can_user_recover(bot[1]):
            await query.edit_message_text("❌ لقد استخدمت استعادة اليوم بالفعل. حاول غداً.")
            return
        if bot[15] == 0:
            await query.edit_message_text("❌ البوت ليس في وضع السكون.")
            return
        self.db.use_user_recovery(bot[1])
        self.db.mark_bot_auto_recovery_used(bot_id)
        self.db.set_bot_time_power(bot_id, total_seconds=3600, power_max=20.0)
        self.db.update_bot_resources(bot_id, remaining_seconds=3600, power_remaining=20.0, last_checked=datetime.utcnow().isoformat())
        self.db.set_sleep_mode(bot_id, False)
        success, msg = await self.pm.start_bot(bot_id, context.application, use_recovery=True)
        if success:
            await query.edit_message_text("✅ تم استعادة البوت وتشغيله باستخدام Auto-Recovery المجانية.")
        else:
            await query.edit_message_text(f"⚠️ تم استعادة الموارد لكن فشل التشغيل: {msg}")

    async def add_time_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = query.data.split("_")
        bot_id = int(parts[2]); seconds = int(parts[3])
        bot = self.db.get_bot(bot_id)
        if not bot: return
        user_plan = self.db.get_user_plan(bot[1])
        plan_limits = {'free': 86400, 'pro': 604800, 'ultra': 10**12}
        plan_max = plan_limits.get(user_plan, 86400)
        current_total = bot[10] or 0
        if current_total + seconds > plan_max:
            await query.answer("⚠️ لا يمكنك تجاوز حد خطتك.")
            return
        added_power = min(100.0, (seconds / plan_max) * 100.0)
        new_total = current_total + seconds
        new_remaining = (bot[11] or 0) + seconds
        new_power = min(100.0, (bot[13] or 0) + added_power)
        self.db.update_bot_resources(bot_id, remaining_seconds=new_remaining, power_remaining=new_power, last_checked=datetime.utcnow().isoformat())
        import sqlite3
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("UPDATE bots SET total_seconds = ?, warned_low = 0 WHERE id = ?", (new_total, bot_id))

        if bot[15] == 1:
            self.db.set_sleep_mode(bot_id, False)
            success, msg = await self.pm.start_bot(bot_id, context.application)
            if success:
                await query.edit_message_text("✅ تمت إضافة الوقت بنجاح وتم إيقاظ البوت وتشغيله.")
            else:
                await query.edit_message_text(f"✅ تمت إضافة الوقت بنجاح. ولكن: {msg}")
        else:
            await query.edit_message_text("✅ تمت إضافة الوقت والطاقة بنجاح.")

    async def my_bots(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data['menu_token'] = context.user_data.get('menu_token', 0) + 1
        context.user_data['auto_refresh'] = False
        bots = self.db.get_user_bots(update.effective_user.id)
        
        if not bots:
            await query.edit_message_text("📂 لا تملك أي بوتات مستضافة حالياً.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="main_menu")]]))
            return

        keyboard = []
        for bid, name, status, _ in bots:
            icon = "🟢" if status == "running" else "🔴"
            bot = self.db.get_bot(bid)
            remaining = bot[11]
            expires = seconds_to_human(remaining) if remaining and remaining>0 else "منتهي"
            sleep_icon = " 🛌" if bot[15]==1 else ""
            label = f"{icon} {name}{sleep_icon} — ⏳ {expires} — ⚡ {int(bot[13] or 0)}%"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"manage_{bid}")])
        
        keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data="main_menu")])
        await query.edit_message_text("📂 *قائمة بوتاتك المستضافة:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def sys_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if psutil:
            cpu = psutil.cpu_percent()
            mem = psutil.virtual_memory().percent
            usage_text = f"🖥 المعالج: `{cpu}%`\n🧠 الذاكرة: `{mem}%`"
        else:
            usage_text = "⚠️ معلومات النظام غير متوفرة."
        
        running_bots = len(self.db.get_all_running_bots())
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
            f"🚀 البوتات المشغلة حالياً: `{running_bots}`\n"
            f"━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="main_menu")]]), parse_mode="Markdown")

    async def admin_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if update.effective_user.id != ADMIN_ID: return
        pending = self.db.get_pending_users()
        keyboard = [
            [InlineKeyboardButton(f"👥 طلبات الانضمام ({len(pending)})", callback_data="pending_users")],
            [InlineKeyboardButton("🔙 عودة", callback_data="main_menu")]
        ]
        await query.edit_message_text("👑 *لوحة تحكم المالك*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def list_pending_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        pending = self.db.get_pending_users()
        if not pending:
            await query.edit_message_text("✅ لا توجد طلبات معلقة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="admin_panel")]]))
            return
        keyboard = [[InlineKeyboardButton(f"👤 @{u[1]} ({u[0]})", callback_data=f"viewuser_{u[0]}")] for u in pending]
        keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data="admin_panel")])
        await query.edit_message_text("👥 *الطلبات المعلقة:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def handle_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        try:
            data_parts = query.data.split("_")
            action = data_parts[0]
            user_id = int(data_parts[1])
            
            if action == "approve":
                self.db.update_user_status(user_id, 'approved')
                await query.edit_message_text(f"✅ تم قبول المستخدم <code>{user_id}</code> بنجاح.", parse_mode="HTML")
                try:
                    await context.bot.send_message(chat_id=user_id, text="🎉 <b>تم قبول طلبك بنجاح!</b> يمكنك الآن استخدام البوت عبر /start", parse_mode="HTML")
                except Exception: pass
            elif action == "reject":
                self.db.update_user_status(user_id, 'blocked')
                await query.edit_message_text(f"❌ تم رفض وحظر المستخدم <code>{user_id}</code>.", parse_mode="HTML")
                try:
                    await context.bot.send_message(chat_id=user_id, text="🚫 نعتذر، تم رفض طلب انضمامك.")
                except Exception: pass
        except Exception as e:
            await query.edit_message_text(f"❌ حدث خطأ أثناء معالجة الطلب: {e}")

    async def list_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data['menu_token'] = context.user_data.get('menu_token', 0) + 1
        context.user_data['auto_refresh'] = False
        
        bot_id = int(query.data.split("_")[1])
        bot = self.db.get_bot(bot_id)
        bot_path = os.path.join(BOTS_DIR, bot[5])
        files = [f for f in os.listdir(bot_path) if os.path.isfile(os.path.join(bot_path, f))]
        keyboard = [[InlineKeyboardButton(f"📄 {f}", callback_data=f"fview_{bot_id}_{f}")] for f in files]
        keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data=f"manage_{bot_id}")])
        await query.edit_message_text(f"📁 *ملفات البوت: {bot[3]}*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def file_view(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        _, bot_id, filename = query.data.split("_", 2)
        bot = self.db.get_bot(int(bot_id))
        file_path = os.path.join(BOTS_DIR, bot[5], filename)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()[:1000]
        except Exception:
            content = "لا يمكن العرض."
        keyboard = [[InlineKeyboardButton("🗑 حذف", callback_data=f"fdel_{bot_id}_{filename}")], [InlineKeyboardButton("🔙 عودة", callback_data=f"files_{bot_id}")]]
        await query.edit_message_text(f"📄 `{filename}`\n\n```python\n{content}\n```", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def file_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        _, bot_id, filename = query.data.split("_", 2)
        bot = self.db.get_bot(int(bot_id))
        if filename == bot[6]:
            await query.message.reply_text("❌ لا يمكن حذف الملف الرئيسي.")
            return
        os.remove(os.path.join(BOTS_DIR, bot[5], filename))
        query.data = f"files_{bot_id}"
        await self.list_files(update, context)

    async def add_bot_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.message.reply_text("📤 أرسل ملف البوت (.py):")
        return WAIT_FILE_UPLOAD

    async def handle_bot_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        
        token = None
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                match = re.search(r'[0-9]{8,10}:[a-zA-Z0-9_-]{35}', f.read())
                if match: token = match.group(0)
        except Exception: pass
        
        context.user_data['new_bot'] = {'name': doc.file_name, 'folder': folder, 'main_file': doc.file_name}
        if token:
            self.db.add_bot(update.effective_user.id, token, doc.file_name, folder, doc.file_name)
            await update.message.reply_text("✅ تم الكشف عن التوكن وإضافة البوت!")
            return ConversationHandler.END
        else:
            await update.message.reply_text("⚠️ أرسل التوكن يدوياً:")
            return WAIT_MANUAL_TOKEN

    async def handle_manual_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        token = update.message.text
        data = context.user_data['new_bot']
        self.db.add_bot(update.effective_user.id, token, data['name'], data['folder'], data['main_file'])
        await update.message.reply_text("✅ تمت الإضافة بنجاح!")
        return ConversationHandler.END

    async def deploy_github_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.message.reply_text("🔗 أرسل رابط GitHub (مثال: https://github.com/username/repo):")
        return WAIT_GITHUB_URL

    async def handle_github_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        url = update.message.text.strip()
        user = update.effective_user
        if not url.startswith('https://github.com/'):
            await update.message.reply_text("❌ رابط غير صالح.")
            return WAIT_GITHUB_URL

        folder = f"gh_{user.id}_{int(time.time())}"
        dest = os.path.join(BOTS_DIR, folder)
        try:
            proc = subprocess.run(["git", "clone", url, dest], capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                await update.message.reply_text(f"❌ فشل الاستنساخ: {proc.stderr[:500]}")
                return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ: {e}")
            return ConversationHandler.END

        found = None
        for c in ['main.py', 'bot.py', 'app.py']:
            for root, dirs, files in os.walk(dest):
                if c in files:
                    found = os.path.relpath(os.path.join(root, c), dest)
                    break
            if found: break

        token = None
        for root, dirs, files in os.walk(dest):
            for f in files:
                if f.endswith('.py'):
                    try:
                        with open(os.path.join(root, f), 'r', encoding='utf-8') as fh:
                            m = re.search(r'[0-9]{8,10}:[a-zA-Z0-9_-]{35}', fh.read())
                            if m: token = m.group(0); break
                    except Exception: pass
            if token: break

        req_found = any('requirements.txt' in files for root, dirs, files in os.walk(dest))
        context.user_data['gh_deploy'] = {'folder': folder, 'path': dest, 'main_file': found, 'token': token, 'has_reqs': req_found}

        text = f"🔎 تم استنساخ المستودع. ملف التشغيل: `{found or 'غير موجود'}`\n"
        if req_found: text += "🔧 يوجد ملف requirements.txt\n"
        text += "✅ تم اكتشاف توكن\n" if token else "⚠️ لم يتم اكتشاف توكن تلقائياً.\n"
        
        keyboard = [[InlineKeyboardButton("✅ نشر", callback_data="gh_confirm")], [InlineKeyboardButton("❌ إلغاء", callback_data="gh_cancel")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return WAIT_DEPLOY_CONFIRM

    async def handle_gh_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = context.user_data.get('gh_deploy')
        if not data: return ConversationHandler.END
        folder, main_file, token = data['folder'], data['main_file'] or 'main.py', data['token']
        bot_id = self.db.add_bot(update.effective_user.id, token, os.path.basename(folder), folder, main_file)
        await query.edit_message_text(f"✅ تم نشر المستودع بنجاح. ID: {bot_id}")
        return ConversationHandler.END

    async def handle_gh_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = context.user_data.get('gh_deploy')
        if data: shutil.rmtree(data['path'], ignore_errors=True)
        await query.edit_message_text("❌ تم إلغاء النشر.")
        return ConversationHandler.END

    async def start_bot_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        bot_id = int(query.data.split("_")[1])
        success, msg = await self.pm.start_bot(bot_id, context.application)
        await query.message.reply_text(msg)

    async def stop_bot_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        bot_id = int(query.data.split("_")[1])
        self.pm.stop_bot(bot_id)
        await query.message.reply_text("🛑 تم الإيقاف.")

    async def confirm_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data['menu_token'] = context.user_data.get('menu_token', 0) + 1
        context.user_data['auto_refresh'] = False
        bot_id = int(query.data.split("_")[2])
        keyboard = [[InlineKeyboardButton("✅ حذف", callback_data=f"del_{bot_id}"), InlineKeyboardButton("❌ تراجع", callback_data=f"manage_{bot_id}")]]
        await query.edit_message_text("⚠️ حذف نهائي؟", reply_markup=InlineKeyboardMarkup(keyboard))

    async def delete_bot_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        bot_id = int(query.data.split("_")[1])
        bot = self.db.get_bot(bot_id)
        self.pm.stop_bot(bot_id)
        if bot: shutil.rmtree(os.path.join(BOTS_DIR, bot[5]), ignore_errors=True)
        self.db.delete_bot(bot_id)
        await query.message.reply_text("🗑 تم الحذف.")
        await self.my_bots(update, context)
