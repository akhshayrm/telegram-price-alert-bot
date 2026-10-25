"""
bot.py - The Telegram price-alert bot. Run this file to start the bot:

    python bot.py

Commands:
    /start                 - welcome + help
    /track SYMBOL PRICE    - alert me when SYMBOL crosses PRICE
    /list                  - show my alerts
    /remove NUMBER         - delete one alert
    /price SYMBOL          - show the current price
"""

import asyncio
import logging
import os
import sqlite3
import sys

from dotenv import load_dotenv
from telegram import Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes

import database
import prices

# ---------------------------------------------------------------------------
# Configuration (loaded from the .env file)
# ---------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_INTERVAL_MINUTES = float(os.getenv("CHECK_INTERVAL_MINUTES", "3"))

# How many times to try sending a message before giving up.
SEND_ATTEMPTS = 4

# Log to the console AND to bot.log, so errors can be reviewed later.
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
# httpx logs every request (including the URL with your token) - keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def plural(value, word):
    """plural(1, 'minute') -> '1 minute', plural(3, 'minute') -> '3 minutes'."""
    return f"{value:g} {word}" + ("" if value == 1 else "s")


# Messages use Telegram's HTML formatting: <b>bold</b>, <code>monospace</code>.
HELP_TEXT = (
    "👋 <b>Welcome to Price Alert Bot!</b>\n\n"
    "I'll message you when a price crosses a level you choose.\n\n"
    "<b>Commands</b>\n"
    "<code>/track BTC 82000</code> - alert when BTC crosses 82,000\n"
    "<code>/track XAUUSD 4100</code> - alert when gold crosses 4,100\n"
    "<code>/list</code> - show your alerts\n"
    "<code>/remove 1</code> - delete alert #1\n"
    "<code>/price BTC</code> - show the current price\n\n"
    f"<b>Supported:</b> {', '.join(prices.SUPPORTED_SYMBOLS)}\n"
    f"Prices are checked every {plural(CHECK_INTERVAL_MINUTES, 'minute')}."
)


def fmt(value):
    """Format a price nicely, e.g. 70000 -> '70,000.00'."""
    return f"{value:,.2f}"


# ---------------------------------------------------------------------------
# Sending messages reliably
# ---------------------------------------------------------------------------
async def send(bot, chat_id, text, html=False):
    """
    Send a message, retrying if the connection to Telegram drops.
    Raises the last error if every attempt fails, so callers can react.
    """
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            return await bot.send_message(
                chat_id, text, parse_mode="HTML" if html else None
            )
        except (BadRequest, Forbidden):
            raise  # a problem with the message or chat itself - retrying won't help
        except RetryAfter as e:
            # Telegram asked us to slow down; wait as long as it says.
            if attempt == SEND_ATTEMPTS:
                raise
            await asyncio.sleep(e.retry_after)
        except NetworkError as e:  # includes connection errors and timeouts
            if attempt == SEND_ATTEMPTS:
                raise
            logger.warning("Send failed (attempt %s/%s): %s - retrying", attempt, SEND_ATTEMPTS, e)
            await asyncio.sleep(attempt)  # wait 1s, then 2s, then 3s


async def reply(update, context, text, html=False):
    """Shortcut: send a message back to whoever sent the command."""
    return await send(context.bot, update.effective_chat.id, text, html)


# ---------------------------------------------------------------------------
# Command handlers - each function runs when a user sends that command
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start and /help - show the welcome message."""
    await reply(update, context, HELP_TEXT, html=True)


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /track SYMBOL PRICE - create a new alert.

    Safety rule: an alert is only kept if the user received the confirmation.
    If anything fails, nothing is saved and the user is told so.
    """
    chat_id = update.effective_chat.id

    # context.args holds the words after the command, e.g. ["BTC", "82000"]
    if len(context.args) != 2:
        await reply(update, context, "Usage: /track SYMBOL PRICE\nExample: /track BTC 82000")
        return

    symbol = context.args[0].upper()
    if not prices.is_supported(symbol):
        await reply(
            update, context,
            f"❌ Unknown symbol '{symbol}'.\nSupported: {', '.join(prices.SUPPORTED_SYMBOLS)}",
        )
        return

    try:
        target = float(context.args[1].replace(",", ""))  # allow "82,000"
        if target <= 0:
            raise ValueError
    except ValueError:
        await reply(update, context, "❌ The price must be a positive number, e.g. 82000")
        return

    # Step 1: get the current price. If this fails, nothing has been saved yet.
    current = await prices.get_price(symbol)
    if current is None:
        await reply(update, context, "⚠️ Couldn't fetch the price right now, so no alert was saved. Please try again.")
        return

    # Step 2: save the alert. Decide which way the price must move to "cross" it.
    direction = "above" if target > current else "below"
    try:
        num = database.add_alert(chat_id, symbol, target, direction)
    except sqlite3.Error:
        logger.exception("Could not save alert")
        await reply(update, context, "❌ Couldn't save your alert, so nothing was saved. Please try again.")
        return
    logger.info("Alert #%s saved for chat %s: %s %s %s", num, chat_id, symbol, direction, target)

    # Step 3: confirm. If the confirmation can't be delivered, undo the save,
    # so the user is never left with an alert they weren't told about.
    arrow = "📈" if direction == "above" else "📉"
    try:
        await reply(
            update, context,
            f"✅ Alert #{num} set!\n"
            f"{arrow} I'll notify you when <b>{symbol}</b> goes {direction} <b>{fmt(target)}</b>.\n"
            f"Current price: {fmt(current)}",
            html=True,
        )
    except Exception:
        database.delete_alert(chat_id, num)
        logger.exception("Confirmation for alert #%s failed - alert removed again", num)
        try:
            await reply(update, context, "❌ Connection problem: your alert was NOT saved. Please send the command again.")
        except Exception:
            pass  # still offline; the alert was removed, so nothing is half-done


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list - show this user's alerts."""
    alerts = database.get_alerts_for_chat(update.effective_chat.id)
    if not alerts:
        await reply(update, context, "You have no alerts. Add one with /track BTC 82000")
        return

    lines = ["🔔 <b>Your alerts:</b>"]
    for a in alerts:
        arrow = "📈" if a["direction"] == "above" else "📉"
        lines.append(f"#{a['user_num']}  {arrow} {a['symbol']} {a['direction']} {fmt(a['target'])}")
    lines.append("\nRemove one with /remove NUMBER")
    await reply(update, context, "\n".join(lines), html=True)


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remove NUMBER - delete one of this user's alerts."""
    if len(context.args) != 1 or not context.args[0].lstrip("#").isdigit():
        await reply(update, context, "Usage: /remove NUMBER\nSee your alert numbers with /list")
        return

    num = int(context.args[0].lstrip("#"))
    if database.delete_alert(update.effective_chat.id, num):
        await reply(update, context, f"🗑️ Alert #{num} removed.")
    else:
        await reply(update, context, f"❌ You have no alert #{num}. Check /list")


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/price SYMBOL - show the current price (handy for testing)."""
    if len(context.args) != 1:
        await reply(update, context, "Usage: /price BTC")
        return
    symbol = context.args[0].upper()
    if not prices.is_supported(symbol):
        await reply(update, context, f"❌ Unknown symbol. Supported: {', '.join(prices.SUPPORTED_SYMBOLS)}")
        return
    current = await prices.get_price(symbol)
    if current is None:
        await reply(update, context, "⚠️ Couldn't fetch the price right now.")
    else:
        await reply(update, context, f"💲 {symbol}: {fmt(current)} USD")


# ---------------------------------------------------------------------------
# Background job - runs every few minutes to check all alerts
# ---------------------------------------------------------------------------
async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    alerts = database.get_all_alerts()
    if not alerts:
        return

    # Fetch each symbol only once, even if many users track it.
    symbols = {a["symbol"] for a in alerts}
    current_prices = await prices.get_prices(symbols)
    logger.info("Checked prices: %s", current_prices)

    for a in alerts:
        current = current_prices.get(a["symbol"])
        if current is None:
            continue  # API failed for this symbol; try again next round

        hit = (a["direction"] == "above" and current >= a["target"]) or (
            a["direction"] == "below" and current <= a["target"]
        )
        if not hit:
            continue

        arrow = "📈" if a["direction"] == "above" else "📉"
        try:
            await send(
                context.bot,
                a["chat_id"],
                f"🚨 <b>Alert #{a['user_num']} triggered and removed.</b>\n"
                f"{arrow} {a['symbol']} is now {fmt(current)} "
                f"({a['direction']} your target of {fmt(a['target'])}).",
                html=True,
            )
        except Forbidden:
            # The user blocked the bot - they can't be notified, so drop the alert.
            logger.warning("Chat %s blocked the bot; removing alert #%s", a["chat_id"], a["user_num"])
        except Exception as e:
            # Keep the alert so the user is notified on the next check instead.
            logger.warning("Could not notify chat %s (will retry next check): %s", a["chat_id"], e)
            continue

        # Remove only after the notification was delivered.
        database.delete_alert(a["chat_id"], a["user_num"])
        logger.info("Alert #%s for chat %s triggered and removed", a["user_num"], a["chat_id"])


# ---------------------------------------------------------------------------
# Error handler - logs the full error instead of failing silently
# ---------------------------------------------------------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error while handling an update", exc_info=context.error)
    # Let the user know something went wrong (this may also fail if offline).
    if isinstance(update, Update) and update.effective_chat:
        try:
            await send(context.bot, update.effective_chat.id, "⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN or BOT_TOKEN == "paste-your-token-here":
        print("ERROR: No bot token found. Open the .env file and set TELEGRAM_BOT_TOKEN.")
        sys.exit(1)

    database.init_db()

    # Longer timeouts than the 5-second default, so a slow connection
    # doesn't stop replies from being delivered.
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("list", list_alerts))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("price", price))
    app.add_error_handler(on_error)

    # Run check_prices every N minutes, starting 10 seconds after launch.
    app.job_queue.run_repeating(check_prices, interval=CHECK_INTERVAL_MINUTES * 60, first=10)

    logger.info(
        "Bot is running (checking every %s). Press Ctrl+C to stop.",
        plural(CHECK_INTERVAL_MINUTES, "minute"),
    )
    app.run_polling()


if __name__ == "__main__":
    main()
