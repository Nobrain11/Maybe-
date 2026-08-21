"""
ETH Robinhood Telegram Trade Bot - full build.

Features:
  - Buy/Sell with confirmation step (button flow, no free-text amounts)
  - Portfolio view (ETH holding + USD value)
  - Order history
  - Price alerts (checked on a background loop)
  - DCA recurring buys (checked on a background loop)
  - Risk guardrails: max trade size, cooldown, daily loss limit (risk.py)
  - Kill switch: /pause and /resume
  - Dry-run mode ON by default - orders are logged but NOT sent to
    Robinhood until you explicitly turn it off with /liveon

SAFETY DEFAULTS (read before using):
  - dry_run = True until you run /liveon
  - ALLOWED_TELEGRAM_USER_ID restricts who can use the bot at all
  - Every buy/sell requires an explicit Confirm button tap
"""
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import risk
import storage
from price_feed import get_eth_price, SYMBOL
from robinhood_client import RobinhoodCryptoClient, RobinhoodAPIError

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID")
RH_API_KEY = os.getenv("ROBINHOOD_API_KEY")
RH_PRIVATE_KEY = os.getenv("ROBINHOOD_PRIVATE_KEY")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# In-memory state for multi-step flows (buy/sell amount -> confirm).
# Keyed by telegram user id. Fine for a single-user bot; for multi-user
# you'd want this to be more isolated / persisted.
pending_orders: dict[int, dict] = {}
pending_alert_input: dict[int, bool] = {}
pending_dca_input: dict[int, bool] = {}

rh_client: RobinhoodCryptoClient | None = None
if RH_API_KEY and RH_PRIVATE_KEY:
    rh_client = RobinhoodCryptoClient(api_key=RH_API_KEY, private_key_b64=RH_PRIVATE_KEY)
else:
    log.warning(
        "ROBINHOOD_API_KEY / ROBINHOOD_PRIVATE_KEY not set - bot will run in "
        "UI-only mode. Buy/sell/portfolio calls will show a config error."
    )


def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)


def require_client() -> RobinhoodCryptoClient:
    if rh_client is None:
        raise RuntimeError(
            "Robinhood API credentials are not configured. Set "
            "ROBINHOOD_API_KEY and ROBINHOOD_PRIVATE_KEY in .env."
        )
    return rh_client


# ---------- UI builders ----------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Buy", callback_data="buy"),
         InlineKeyboardButton("Sell", callback_data="sell")],
        [InlineKeyboardButton("Portfolio", callback_data="portfolio"),
         InlineKeyboardButton("Orders", callback_data="orders")],
        [InlineKeyboardButton("Alerts", callback_data="alerts"),
         InlineKeyboardButton("DCA", callback_data="dca")],
        [InlineKeyboardButton("Settings", callback_data="settings"),
         InlineKeyboardButton("Refresh", callback_data="refresh")],
    ]
    return InlineKeyboardMarkup(rows)


def back_keyboard(target: str = "back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=target)]])


async def build_menu_text() -> str:
    mode = "DRY RUN (no real orders)" if storage.is_dry_run() else "LIVE"
    paused = " | PAUSED" if storage.is_paused() else ""
    try:
        price = get_eth_price(require_client())
        price_line = (
            f"ETH  bid ${price['bid']:,.2f} / ask ${price['ask']:,.2f}"
        )
    except Exception as e:
        log.warning("Price fetch failed: %s", e)
        price_line = "ETH price unavailable"

    return f"{price_line}\n\nMode: {mode}{paused}\n"


def amount_keyboard(action: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("$25", callback_data=f"{action}_amt_25"),
            InlineKeyboardButton("$50", callback_data=f"{action}_amt_50"),
            InlineKeyboardButton("$100", callback_data=f"{action}_amt_100"),
        ],
        [InlineKeyboardButton("Back", callback_data="back")],
    ]
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm", callback_data=f"{action}_confirm"),
            InlineKeyboardButton("Cancel", callback_data="back"),
        ]
    ])


# ---------- command handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("Not authorized.")
        return
    text = await build_menu_text()
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    storage.set_paused(True)
    await update.message.reply_text("Trading PAUSED. All buy/sell blocked until /resume.")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    storage.set_paused(False)
    await update.message.reply_text("Trading resumed.")


async def liveon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    storage.set_dry_run(False)
    await update.message.reply_text(
        "LIVE MODE enabled. Orders will now be sent to Robinhood for real. "
        "Use /liveoff to go back to dry-run."
    )


async def liveoff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    storage.set_dry_run(True)
    await update.message.reply_text("Dry-run mode enabled. No real orders will be sent.")


# ---------- callback (button) handler ----------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(update):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    action = query.data
    user_id = update.effective_user.id

    try:
        if action == "refresh" or action == "back":
            pending_orders.pop(user_id, None)
            text = await build_menu_text()
            await query.edit_message_text(text, reply_markup=main_menu_keyboard())

        elif action in ("buy", "sell"):
            await query.edit_message_text(
                f"{action.capitalize()} ETH - choose an amount:",
                reply_markup=amount_keyboard(action),
            )

        elif action.startswith(("buy_amt_", "sell_amt_")):
            side, _, amt_str = action.partition("_amt_")
            usd_amount = float(amt_str)
            await handle_amount_selected(query, user_id, side, usd_amount)

        elif action in ("buy_confirm", "sell_confirm"):
            side = action.split("_")[0]
            await handle_order_confirm(query, user_id, side)

        elif action == "portfolio":
            await show_portfolio(query)

        elif action == "orders":
            await show_orders(query)

        elif action == "alerts":
            await show_alerts_menu(query)

        elif action == "alert_add":
            pending_alert_input[user_id] = True
            await query.edit_message_text(
                "Send the alert as a message, e.g.:\n"
                "`above 3500` or `below 2800`\n\n"
                "(Send as plain text, then I'll confirm it.)",
                parse_mode="Markdown",
                reply_markup=back_keyboard("alerts"),
            )

        elif action.startswith("alert_del_"):
            alert_id = int(action.replace("alert_del_", ""))
            storage.delete_alert(alert_id)
            await show_alerts_menu(query)

        elif action == "dca":
            await show_dca_menu(query)

        elif action == "dca_add":
            pending_dca_input[user_id] = True
            await query.edit_message_text(
                "Send the DCA schedule as a message, e.g.:\n"
                "`50 24` = buy $50 of ETH every 24 hours\n\n"
                "(amount_usd interval_hours)",
                parse_mode="Markdown",
                reply_markup=back_keyboard("dca"),
            )

        elif action.startswith("dca_del_"):
            schedule_id = int(action.replace("dca_del_", ""))
            storage.deactivate_dca_schedule(schedule_id)
            await show_dca_menu(query)

        elif action == "settings":
            await show_settings(query)

        else:
            await query.edit_message_text(
                f"Unhandled action: {action}", reply_markup=back_keyboard()
            )

    except RuntimeError as e:
        await query.edit_message_text(str(e), reply_markup=back_keyboard())
    except RobinhoodAPIError as e:
        log.exception("Robinhood API error")
        await query.edit_message_text(
            f"Robinhood API error: {e}", reply_markup=back_keyboard()
        )
    except Exception:
        log.exception("Unexpected error in button_handler")
        await query.edit_message_text(
            "Something went wrong. Check the bot logs.", reply_markup=back_keyboard()
        )


async def handle_amount_selected(query, user_id: int, side: str, usd_amount: float):
    client = require_client()
    price = get_eth_price(client)
    ref_price = price["ask"] if side == "buy" else price["bid"]
    est_quantity = usd_amount / ref_price

    pending_orders[user_id] = {
        "side": side,
        "usd_amount": usd_amount,
        "ref_price": ref_price,
        "est_quantity": est_quantity,
        "client_order_id": str(uuid.uuid4()),
    }

    mode = "DRY RUN" if storage.is_dry_run() else "LIVE"
    text = (
        f"Confirm {side.upper()} [{mode}]\n\n"
        f"~{est_quantity:.6f} ETH @ ~${ref_price:,.2f}\n"
        f"Total: ~${usd_amount:,.2f}\n\n"
        f"Actual fill price may differ slightly (market order)."
    )
    await query.edit_message_text(text, reply_markup=confirm_keyboard(side))


async def handle_order_confirm(query, user_id: int, side: str):
    order = pending_orders.pop(user_id, None)
    if not order:
        await query.edit_message_text(
            "No pending order found - it may have expired. Start again.",
            reply_markup=back_keyboard(),
        )
        return

    usd_amount = order["usd_amount"]

    # Risk check - single choke point, cannot be bypassed.
    try:
        risk.check_trade_allowed(usd_amount)
    except risk.RiskViolation as e:
        await query.edit_message_text(f"Blocked: {e}", reply_markup=back_keyboard())
        return

    client = require_client()
    client_order_id = order["client_order_id"]
    dry_run = storage.is_dry_run()

    storage.record_trade(
        client_order_id=client_order_id,
        symbol=SYMBOL,
        side=side,
        order_type="market",
        requested_quote_amount=str(usd_amount),
        requested_asset_quantity=None,
        limit_price=None,
        status="dry_run" if dry_run else "submitted",
        raw_response=None,
    )

    if dry_run:
        await query.edit_message_text(
            f"[DRY RUN] {side.upper()} order logged, not sent to Robinhood.\n"
            f"~${usd_amount:,.2f} of ETH.\n\n"
            f"Use /liveon to enable real trading.",
            reply_markup=back_keyboard(),
        )
        return

    try:
        if side == "buy":
            result = client.place_order(
                symbol=SYMBOL, side="buy", order_type="market",
                quote_amount=str(usd_amount), client_order_id=client_order_id,
            )
        else:
            # For sell, Robinhood market orders take asset_quantity, not quote_amount.
            price = get_eth_price(client)
            est_quantity = usd_amount / price["bid"]
            result = client.place_order(
                symbol=SYMBOL, side="sell", order_type="market",
                asset_quantity=f"{est_quantity:.8f}", client_order_id=client_order_id,
            )

        storage.update_trade_status(
            client_order_id,
            robinhood_order_id=result.get("id"),
            status=result.get("state", "submitted"),
            raw_response=str(result),
        )
        await query.edit_message_text(
            f"Order submitted.\nOrder ID: {result.get('id', 'unknown')}\n"
            f"Status: {result.get('state', 'submitted')}\n\n"
            f"Check /orders shortly for fill confirmation.",
            reply_markup=back_keyboard(),
        )
    except RobinhoodAPIError as e:
        storage.update_trade_status(client_order_id, status="error", raw_response=str(e))
        raise


async def show_portfolio(query):
    client = require_client()
    holdings = client.get_holdings(asset_codes=["ETH"])
    price = get_eth_price(client)

    results = holdings.get("results", [])
    if not results:
        text = "No ETH holding found (balance is 0, or account not yet funded)."
    else:
        h = results[0]
        qty = float(h.get("total_quantity", 0))
        value = qty * price["mid"]
        text = (
            f"ETH holding: {qty:.6f}\n"
            f"Approx value: ${value:,.2f} (at mid ${price['mid']:,.2f})\n"
        )
    await query.edit_message_text(text, reply_markup=back_keyboard())


async def show_orders(query):
    rows = storage.get_recent_trades(limit=10)
    if not rows:
        text = "No trades yet."
    else:
        lines = ["Recent trades:\n"]
        for r in rows:
            ts = datetime.fromtimestamp(r["created_at"], tz=timezone.utc).strftime("%m-%d %H:%M UTC")
            lines.append(
                f"{ts}  {r['side'].upper()}  ${r['requested_quote_amount']}  [{r['status']}]"
            )
        text = "\n".join(lines)
    await query.edit_message_text(text, reply_markup=back_keyboard())


async def show_alerts_menu(query):
    alerts = storage.get_active_alerts()
    lines = ["Active alerts:\n"] if alerts else ["No active alerts.\n"]
    rows = []
    for a in alerts:
        lines.append(f"- {a['symbol']} {a['direction']} ${a['target_price']:,.2f}")
        rows.append([
            InlineKeyboardButton(
                f"Delete: {a['direction']} ${a['target_price']:.0f}",
                callback_data=f"alert_del_{a['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("+ Add Alert", callback_data="alert_add")])
    rows.append([InlineKeyboardButton("Back", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def show_dca_menu(query):
    schedules = storage.get_active_dca_schedules()
    lines = ["Active DCA schedules:\n"] if schedules else ["No active DCA schedules.\n"]
    rows = []
    for s in schedules:
        lines.append(
            f"- ${s['quote_amount']:.2f} every {s['interval_hours']:.1f}h"
        )
        rows.append([
            InlineKeyboardButton(
                f"Delete: ${s['quote_amount']:.0f}/{s['interval_hours']:.0f}h",
                callback_data=f"dca_del_{s['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("+ Add DCA", callback_data="dca_add")])
    rows.append([InlineKeyboardButton("Back", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def show_settings(query):
    limits = storage.get_risk_limits()
    mode = "DRY RUN" if storage.is_dry_run() else "LIVE"
    paused = "yes" if storage.is_paused() else "no"
    text = (
        f"Mode: {mode}  (/liveon /liveoff)\n"
        f"Paused: {paused}  (/pause /resume)\n\n"
        f"Risk limits:\n"
        f"  Max trade size: ${limits['max_trade_usd']:.2f}\n"
        f"  Daily loss limit: ${limits['daily_loss_limit_usd']:.2f}\n"
        f"  Cooldown: {limits['cooldown_seconds']}s between trades\n\n"
        f"To change limits, edit them in bot.db settings or extend this menu "
        f"with input handlers (same pattern as alerts/DCA add)."
    )
    await query.edit_message_text(text, reply_markup=back_keyboard())


# ---------- free-text input handler (for alert / DCA entry) ----------

async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if pending_alert_input.pop(user_id, False):
        parts = text.lower().split()
        if len(parts) != 2 or parts[0] not in ("above", "below"):
            await update.message.reply_text(
                "Format: `above 3500` or `below 2800`", parse_mode="Markdown"
            )
            pending_alert_input[user_id] = True
            return
        direction, price_str = parts
        try:
            target = float(price_str)
        except ValueError:
            await update.message.reply_text("Price must be a number.")
            pending_alert_input[user_id] = True
            return
        storage.add_alert(SYMBOL, direction, target)
        await update.message.reply_text(
            f"Alert added: {SYMBOL} {direction} ${target:,.2f}"
        )
        return

    if pending_dca_input.pop(user_id, False):
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Format: `amount_usd interval_hours`, e.g. `50 24`")
            pending_dca_input[user_id] = True
            return
        try:
            amount = float(parts[0])
            interval = float(parts[1])
        except ValueError:
            await update.message.reply_text("Both values must be numbers.")
            pending_dca_input[user_id] = True
            return
        storage.add_dca_schedule(SYMBOL, amount, interval)
        await update.message.reply_text(
            f"DCA schedule added: ${amount:.2f} every {interval:.1f}h"
        )
        return

    # Not in an input flow - point them at /start
    await update.message.reply_text("Use /start to open the menu.")


# ---------- background loops: alerts + DCA ----------

async def check_alerts_and_dca(context: ContextTypes.DEFAULT_TYPE):
    """Runs periodically via the job queue. Checks price alerts and fires
    any due DCA buys. Sends Telegram messages for anything triggered."""
    if rh_client is None:
        return

    try:
        price = get_eth_price(rh_client)
    except Exception as e:
        log.warning("Background price fetch failed: %s", e)
        return

    mid = price["mid"]
    chat_id = ALLOWED_USER_ID

    # --- alerts ---
    for alert in storage.get_active_alerts():
        target = alert["target_price"]
        triggered = (
            (alert["direction"] == "above" and mid >= target)
            or (alert["direction"] == "below" and mid <= target)
        )
        if triggered:
            storage.deactivate_alert(alert["id"])
            if chat_id:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"ALERT: {alert['symbol']} is {alert['direction']} "
                        f"${target:,.2f} (currently ${mid:,.2f})"
                    ),
                )

    # --- DCA ---
    if storage.is_paused():
        return  # kill switch also halts automated DCA buys

    now = time.time()
    for sched in storage.get_active_dca_schedules():
        if sched["next_run_at"] > now:
            continue

        usd_amount = sched["quote_amount"]
        try:
            risk.check_trade_allowed(usd_amount)
        except risk.RiskViolation as e:
            log.info("DCA buy skipped by risk check: %s", e)
            storage.update_dca_next_run(sched["id"], now + sched["interval_hours"] * 3600)
            continue

        client_order_id = str(uuid.uuid4())
        dry_run = storage.is_dry_run()
        storage.record_trade(
            client_order_id=client_order_id,
            symbol=sched["symbol"],
            side="buy",
            order_type="market",
            requested_quote_amount=str(usd_amount),
            requested_asset_quantity=None,
            limit_price=None,
            status="dry_run" if dry_run else "submitted",
            raw_response=None,
        )

        msg = f"DCA buy: ${usd_amount:.2f} of {sched['symbol']}"
        if dry_run:
            msg += " [DRY RUN - not sent]"
        else:
            try:
                result = rh_client.place_order(
                    symbol=sched["symbol"], side="buy", order_type="market",
                    quote_amount=str(usd_amount), client_order_id=client_order_id,
                )
                storage.update_trade_status(
                    client_order_id,
                    robinhood_order_id=result.get("id"),
                    status=result.get("state", "submitted"),
                    raw_response=str(result),
                )
                msg += f" - order {result.get('id', 'unknown')} submitted"
            except RobinhoodAPIError as e:
                storage.update_trade_status(client_order_id, status="error", raw_response=str(e))
                msg += f" - FAILED: {e}"

        storage.update_dca_next_run(sched["id"], now + sched["interval_hours"] * 3600)
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=msg)


# ---------- entrypoint ----------

def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    storage.init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("liveon", liveon_cmd))
    app.add_handler(CommandHandler("liveoff", liveoff_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    # Background loop: checks alerts + DCA every 60 seconds.
    app.job_queue.run_repeating(check_alerts_and_dca, interval=60, first=10)

    log.info("Bot starting (dry_run=%s, paused=%s)...", storage.is_dry_run(), storage.is_paused())
    app.run_polling()


if __name__ == "__main__":
    main()
