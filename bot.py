"""Flight Finder Telegram bot entry point."""

import logging

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from config import BOT_TOKEN
from db import init_db
from handlers.favorites import get_favorites_handlers
from handlers.history import get_history_handlers
from handlers.search_flow import build_search_conversation
from handlers.start import main_menu_callback, start_command

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Run once after the Application is initialized (before polling)."""
    await init_db()
    logger.info("Database initialized.")


def main() -> None:
    """Build the Application, register handlers, and start polling."""
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ── Commands ──────────────────────────────────────────────
    app.add_handler(CommandHandler("start", start_command))

    # ── Conversations ─────────────────────────────────────────
    app.add_handler(build_search_conversation())

    # ── Callback queries ──────────────────────────────────────
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^menu_main$"))

    # ── Favorites handlers ─────────────────────────────────────
    for handler in get_favorites_handlers():
        app.add_handler(handler)

    # ── History handlers ──────────────────────────────────────
    for handler in get_history_handlers():
        app.add_handler(handler)

    # ── Start ─────────────────────────────────────────────────
    logger.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
