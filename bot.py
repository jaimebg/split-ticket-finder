"""Flight Finder Telegram bot — entry point.

Handler registration order matters (first-match routing):
  1. ConversationHandler  — intercepts menu_search + conversation callbacks
  2. History handlers     — hist_view_*, hist_rerun_*, menu_history
  3. Favorites handlers   — menu_favorites, savefav_*, delfav_*
  4. menu_main callback   — generic fallback for the "Back" button
  5. /start command        — always available
"""

import asyncio
import logging
import sys

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

import config
from config import BOT_TOKEN, OWNER_ID
from db import init_db
from handlers.favorites import get_favorites_handlers
from handlers.history import get_history_handlers
from handlers.search_flow import build_search_conversation
from handlers.start import main_menu_callback, start_command
from scheduler import scheduler_loop

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Run once after the Application is initialized (before polling)."""
    await init_db()
    logger.info("Database initialized.")

    # Keep a reference on the Application: asyncio only holds a weak reference to
    # running tasks, so a bare create_task() can be garbage-collected mid-flight.
    application.bot_data["scheduler_task"] = asyncio.create_task(
        scheduler_loop(application.bot, OWNER_ID)
    )
    logger.info("Scheduler task created.")


async def post_shutdown(application: Application) -> None:
    """Release provider connection pools on the way out."""
    from providers.registry import close_all

    await close_all()
    logger.info("Provider connections closed.")


def main() -> None:
    """Build the Application, register handlers, and start polling."""
    try:
        config.validate()
    except config.ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ── 1. ConversationHandler (search flow) — FIRST for priority ──
    app.add_handler(build_search_conversation())

    # ── 2. History callback handlers ───────────────────────────────
    for handler in get_history_handlers():
        app.add_handler(handler)

    # ── 3. Favorites callback handlers ─────────────────────────────
    for handler in get_favorites_handlers():
        app.add_handler(handler)

    # ── 4. Main-menu fallback (Back button) ────────────────────────
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^menu_main$"))

    # ── 5. /start command — always available ───────────────────────
    app.add_handler(CommandHandler("start", start_command))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
