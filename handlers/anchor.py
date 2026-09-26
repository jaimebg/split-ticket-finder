"""One message edited in place, resent when it can't be edited.

Shared by the search builder and the results view: both keep a single live
message per flow rather than sending a new one per step.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from handlers.search.draft import Rows

logger = logging.getLogger(__name__)


def markup(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.label, callback_data=b.data) for b in row]
         for row in rows]
    )


async def render_anchor(bot, chat_id: int, message_id: int | None,
                        text: str, rows: Rows) -> int:
    """Show *text* in the anchor, returning the live message id.

    The returned id differs from *message_id* when a resend was needed.
    Callers must store it back, or every later edit targets a message that
    is no longer there.

    An identical edit is not an error: Telegram rejects it with
    "Message is not modified", which is a no-op, not a failure -- the same
    case §6.6 calls out for the progress message. Any other edit failure
    means the panel is unusable, so it is resent.
    """
    reply_markup = markup(rows)

    if message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode="HTML", reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return message_id
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return message_id
            logger.info("Anchor %s unusable (%s) — resending.", message_id, exc)
        except Forbidden as exc:
            logger.info("Anchor %s forbidden (%s) — resending.", message_id, exc)

    message = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    return message.message_id
