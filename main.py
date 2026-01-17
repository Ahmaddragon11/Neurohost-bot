import os
import sys
import asyncio
import logging
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.config.config import (
    TOKEN, DB_FILE, BOTS_DIR, setup_file_logging,
    handle_uncaught_exception, asyncio_exception_handler
)
from src.database.db_manager import Database
from src.core.process_manager import ProcessManager
from src.handlers.bot_handlers import BotHandlers, WAIT_FILE_UPLOAD, WAIT_MANUAL_TOKEN, WAIT_FEEDBACK, WAIT_GITHUB_URL, WAIT_DEPLOY_CONFIRM

# Logging
logger = logging.getLogger(__name__)

def main():
    if not os.path.exists(BOTS_DIR): os.makedirs(BOTS_DIR)
    
    # Initialize components
    db = Database(DB_FILE)
    pm = ProcessManager(db)
    handlers = BotHandlers(db, pm)
    
    app = ApplicationBuilder().token(TOKEN).build()

    # Conversations
    add_bot_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handlers.add_bot_start, pattern="^add_bot$")],
        states={
            WAIT_FILE_UPLOAD: [MessageHandler(filters.Document.ALL, handlers.handle_bot_file)],
            WAIT_MANUAL_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_manual_token)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    
    feedback_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handlers.feedback_start, pattern="^send_feedback$")],
        states={WAIT_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_feedback)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    gh_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handlers.deploy_github_start, pattern="^deploy_github$")],
        states={
            WAIT_GITHUB_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_github_url)],
            WAIT_DEPLOY_CONFIRM: [CallbackQueryHandler(handlers.handle_gh_confirm, pattern="^gh_confirm$"), CallbackQueryHandler(handlers.handle_gh_cancel, pattern="^gh_cancel$")]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    # Register handlers
    app.add_handler(CommandHandler("start", handlers.start))
    
    async def post_init(application):
        await pm.start_background_tasks(application)
    
    app.post_init = post_init
    app.add_handler(add_bot_conv)
    app.add_handler(feedback_conv)
    app.add_handler(gh_conv)
    
    app.add_handler(CallbackQueryHandler(handlers.main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(handlers.my_bots, pattern="^my_bots$"))
    app.add_handler(CallbackQueryHandler(handlers.manage_bot, pattern="^manage_"))
    app.add_handler(CallbackQueryHandler(handlers.start_bot_action, pattern="^start_"))
    app.add_handler(CallbackQueryHandler(handlers.stop_bot_action, pattern="^stop_"))
    app.add_handler(CallbackQueryHandler(handlers.confirm_delete, pattern="^confirm_del_"))
    app.add_handler(CallbackQueryHandler(handlers.delete_bot_action, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(handlers.view_logs, pattern="^logs_"))
    app.add_handler(CallbackQueryHandler(handlers.sys_status, pattern="^sys_status$"))
    app.add_handler(CallbackQueryHandler(handlers.bot_details, pattern="^bot_details$"))
    app.add_handler(CallbackQueryHandler(handlers.admin_panel, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(handlers.list_pending_users, pattern="^pending_users$"))
    app.add_handler(CallbackQueryHandler(handlers.handle_approval, pattern="^(approve|reject)_"))
    app.add_handler(CallbackQueryHandler(handlers.list_files, pattern="^files_"))
    app.add_handler(CallbackQueryHandler(handlers.file_view, pattern="^fview_"))
    app.add_handler(CallbackQueryHandler(handlers.file_delete, pattern="^fdel_"))
    app.add_handler(CallbackQueryHandler(handlers.show_time_panel, pattern="^timepanel_"))
    app.add_handler(CallbackQueryHandler(handlers.add_time_action, pattern="^add_time_"))
    app.add_handler(CallbackQueryHandler(handlers.attempt_recover, pattern="^recover_"))

    # Sys hooks
    sys.excepthook = handle_uncaught_exception
    setup_file_logging()
    try:
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(asyncio_exception_handler)
    except Exception: pass

    print("🚀 NeuroHost Bot is running (Modular Version)...")
    app.run_polling()

if __name__ == "__main__":
    main()
