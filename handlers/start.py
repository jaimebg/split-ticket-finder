"""Bot entry point: /start command, main menu, and owner-only auth."""

from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import OWNER_ID

# ── Owner-only decorators ────────────────────────────────────────────────────


def owner_only(func):
    """Decorator for message handlers — rejects non-owner users."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("Private bot.")
            return
        return await func(update, context)

    return wrapper


def owner_only_callback(func):
    """Decorator for callback-query handlers — rejects non-owner users."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.callback_query.answer("Private bot.", show_alert=True)
            return
        return await func(update, context)

    return wrapper


# ── Main menu keyboard ───────────────────────────────────────────────────────

MAIN_MENU_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🔍 Search flights", callback_data="menu_search")],
        [InlineKeyboardButton("⭐ My favorites", callback_data="menu_favorites")],
        [InlineKeyboardButton("📋 Search history", callback_data="menu_history")],
    ]
)

# ── Handlers ─────────────────────────────────────────────────────────────────


@owner_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — send welcome message with main menu."""
    await update.message.reply_text(
        "Welcome to Flight Finder!\n"
        "Use the menu below to get started.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


@owner_only_callback
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Back to menu' — re-show main menu via edit."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Welcome to Flight Finder!\n"
        "Use the menu below to get started.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
