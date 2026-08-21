"""
ETH Robinhood Telegram Trade Bot - multi-user build.

Anyone can start this bot. Each person connects THEIR OWN Robinhood
Crypto API credentials with /connect, and every trade they make goes
through their own account only - never anyone else's. Credentials are
encrypted at rest (see crypto_util.py) and isolated per Telegram user
ID in the database (see storage.py).

Flow for a new user:
  /start    -> menu, prompts to /connect if not connected yet
  /connect  -> bot asks for API key, then private key (as two separate
               messages so neither ends up sitting in the same line of
               chat history), stores them encrypted
  then normal use: Buy / Sell / Portfolio / Alerts / DCA / Settings,
  all scoped to their own account.

SAFETY DEFAULTS:
  - dry_run = True per-user until they run /liveon
  - Every buy/sell requires an explicit Confirm button tap
  - /disconnect wipes a user's stored credentials immediately

IMPORTANT: read the README section on Robinhood's API terms before
distributing this bot to other people - hosting other users' brokerage
API credentials on your own server is a meaningfully different
liability than a personal single-user bot, and may be restricted by
Robinhood's terms of service. Verify before going wide with this.
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

import crypto_util
import risk
import storage
from price_feed import get_eth_price, SYMBOL
from robinhood_client import RobinhoodCryptoClient, RobinhoodAPIError

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# In-memory per-user flow state. Fine for a modest number of concurrent
# users on a single process; move to Redis/DB if you outgrow this.
pending_orders: dict[int, dict] = {}
pending_alert_input: dict[int, bool] = {}
pending_dca_input: dict[int, bool] = {}
pending_connect_step: dict[int, str] = {}   # "await_api_key" | "await_private_key"
pending_connect_api_key: dict[int, str] = {}

# Cache of live clients so we don't decrypt credentials on every single call.
_client_cache: dict[int, RobinhoodCryptoClient] = {}


def get_client(user_id: int) -> RobinhoodCryptoClient | None:
    if user_id in _client_cache:
        return _client_cache[user_id]
    user = storage.get_user(user_id)
    if not user or not user["robinhood_api_key_enc"]:
        return None
    api_key = crypto_util.decrypt(user["robinhood_api_key_enc"])
    private_key = crypto_util.decrypt(user["robinhood_private_key_enc"])
    client = RobinhoodCryptoClient(api_key=api_key, private_key_b64=private_key)
    _client_cache[user_id] = client
    return client


def require_client(user_id: int) -> RobinhoodCryptoClient:
    client = get_client(user_id)
    if client is None:
        raise RuntimeError(
            "You haven't connected a Robinhood account yet. Send /connect to link one."
        )
    return client


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


def connect_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Connect Robinhood", callback_data="connect_start")]])


async def build_menu_text(user_id: int) -> str:
    if not storage.is_connected(user_id):
        return "You haven't connected a Robinhood account yet.\n"

    mode = "DRY RUN (no real orders)" if storage.is_dry_run(user_id) else "LIVE"
    paused = " | PAUSED" if storage.is_paused(user_id) else ""
    try:
        client = require_client(user_id)
        price = get_eth_price(client)
        price_line = f"ETH  bid ${price['bid']:,.2f} / ask ${price['ask']:,.2f}"
    except Exception as e:
        log.warning("Price fetch failed for user %s: %s", user_id, e)
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
    user_id = update.effective_user.id
    if not storage.is_connected(user_id):
        await update.message.reply_text(
            "Welcome. This bot trades ETH on YOUR OWN Robinhood account - "
            "nobody else's. To use it, connect your Robinhood Crypto API "
            "credentials first.\n\n"
            "Your credentials are encrypted before being stored, and only "
            "ever used to place trades on your behalf.",
            reply_markup=connect_prompt_keyboard(),
        )
        return
    text = await build_menu_text(user_id)
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await begin_connect_flow(update.effective_user.id, update.message.reply_text)


async def begin_connect_flow(user_id: int, reply_fn):
    pending_connect_step[user_id] = "await_api_key"
    await reply_fn(
        "Let's connect your Robinhood account.\n\n"
        "Step 1/2: Send your Robinhood Crypto API key (starts with an "
        "identifier from your Robinhood API settings page).\n\n"
        "Get one at: Robinhood web (classic) -> Account -> Crypto -> API"
    )


async def disconnect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    storage.disconnect_user(user_id)
    _client_cache.pop(user_id, None)
    await update.message.reply_text(
        "Disconnected. Your stored Robinhood credentials have been removed."
    )


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    storage.set_paused(user_id, True)
    await update.message.reply_text("Trading PAUSED. All buy/sell blocked until /resume.")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    storage.set_paused(user_id, False)
    await update.message.reply_text("Trading resumed.")


async def liveon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not storage.is_connected(user_id):
        await update.message.reply_text("Connect a Robinhood account first with /connect.")
        return
    storage.set_dry_run(user_id, False)
    await update.message.reply_text(
        "LIVE MODE enabled. Orders will now be sent to Robinhood for real. "
        "Use /liveoff to go back to dry-run."
    )


async def liveoff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    storage.set_dry_run(user_id, True)
    await update.message.reply_text("Dry-run mode enabled. No real orders will be sent.")


# ---------- callback (button) handler ----------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = query.data
    user_id = update.effective_user.id

    try:
        if action == "connect_start":
            await begin_connect_flow(user_id, query.edit_message_text)
            return

        if not storage.is_connected(user_id) and action not in ("back", "refresh"):
            await query.edit_message_text(
                "Connect a Robinhood account first.", reply_markup=connect_prompt_keyboard()
            )
            return

        if action == "refresh" or action == "back":
            pending_orders.pop(user_id, None)
            text = await build_menu_text(user_id)
            kb = main_menu_keyboard() if storage.is_connected(user_id) else connect_prompt_keyboard()
            await query.edit_message_text(text, reply_markup=kb)

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
            await show_portfolio(query, user_id)

        elif action == "orders":
            await show_orders(query, user_id)

        elif action == "alerts":
            await show_alerts_menu(query, user_id)

        elif action == "alert_add":
            pending_alert_input[user_id] = True
            await query.edit_message_text(
                "Send the alert as a message, e.g.:\n"
                "`above 3500` or `below 2800`",
                parse_mode="Markdown",
                reply_markup=back_keyboard("alerts"),
            )

        elif action.startswith("alert_del_"):
            alert_id = int(action.replace("alert_del_", ""))
            storage.delete_alert(alert_id, user_id)
            await show_alerts_menu(query, user_id)

        elif action == "dca":
            await show_dca_menu(query, user_id)

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
            storage.deactivate_dca_schedule(schedule_id, user_id)
            await show_dca_menu(query, user_id)

        elif action == "settings":
            await show_settings(query, user_id)

        else:
            await query.edit_message_text(
                f"Unhandled action: {action}", reply_markup=back_keyboard()
            )

    except RuntimeError as e:
        await query.edit_message_text(str(e), reply_markup=connect_prompt_keyboard())
    except RobinhoodAPIError as e:
        log.exception("Robinhood API error for user %s", user_id)
        await query.edit_message_text(
            f"Robinhood API error: {e}", reply_markup=back_keyboard()
        )
    except Exception:
        log.exception("Unexpected error in button_handler for user %s", user_id)
        await query.edit_message_text(
            "Something went wrong. Check the bot logs.", reply_markup=back_keyboard()
        )


async def handle_amount_selected(query, user_id: int, side: str, usd_amount: float):
    client = require_client(user_id)
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

    mode = "DRY RUN" if storage.is_dry_run(user_id) else "LIVE"
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

    try:
        risk.check_trade_allowed(user_id, usd_amount)
    except risk.RiskViolation as e:
        await query.edit_message_text(f"Blocked: {e}", reply_markup=back_keyboard())
        return

    client = require_client(user_id)
    client_order_id = order["client_order_id"]
    dry_run = storage.is_dry_run(user_id)

    storage.record_trade(
        user_id,
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


async def show_portfolio(query, user_id: int):
    client = require_client(user_id)
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


async def show_orders(query, user_id: int):
    rows = storage.get_recent_trades(user_id, limit=10)
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


async def show_alerts_menu(query, user_id: int):
    alerts = storage.get_active_alerts(user_id)
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


async def show_dca_menu(query, user_id: int):
    schedules = storage.get_active_dca_schedules(user_id)
    lines = ["Active DCA schedules:\n"] if schedules else ["No active DCA schedules.\n"]
    rows = []
    for s in schedules:
        lines.append(f"- ${s['quote_amount']:.2f} every {s['interval_hours']:.1f}h")
        rows.append([
            InlineKeyboardButton(
                f"Delete: ${s['quote_amount']:.0f}/{s['interval_hours']:.0f}h",
                callback_data=f"dca_del_{s['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("+ Add DCA", callback_data="dca_add")])
    rows.append([InlineKeyboardButton("Back", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def show_settings(query, user_id: int):
    limits = storage.get_risk_limits(user_id)
    mode = "DRY RUN" if storage.is_dry_run(user_id) else "LIVE"
    paused = "yes" if storage.is_paused(user_id) else "no"
    text = (
        f"Mode: {mode}  (/liveon /liveoff)\n"
        f"Paused: {paused}  (/pause /resume)\n"
        f"Connected: yes  (/disconnect to remove your credentials)\n\n"
        f"Risk limits:\n"
        f"  Max trade size: ${limits['max_trade_usd']:.2f}\n"
        f"  Daily loss limit: ${limits['daily_loss_limit_usd']:.2f}\n"
        f"  Cooldown: {limits['cooldown_seconds']}s between trades\n\n"
        f"To change limits, use storage.set_risk_limits(user_id, {{...}}) "
        f"or extend this menu with input handlers (same pattern as Alerts/DCA)."
    )
    await query.edit_message_text(text, reply_markup=back_keyboard())


# ---------- free-text input handler (connect / alert / DCA entry) ----------

async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # --- connect flow: collects API key, then private key ---
    step = pending_connect_step.get(user_id)
    if step == "await_api_key":
        pending_connect_api_key[user_id] = text
        pending_connect_step[user_id] = "await_private_key"
        await update.message.reply_text(
            "Got it. Step 2/2: now send your Robinhood API private key "
            "(the base64 string Robinhood showed you when you created the key).\n\n"
            "This message will be processed and I'd recommend deleting it "
            "from the chat afterward for your own security."
        )
        return

    if step == "await_private_key":
        api_key = pending_connect_api_key.pop(user_id, None)
        pending_connect_step.pop(user_id, None)
        if not api_key:
            await update.message.reply_text("Something went wrong - send /connect to try again.")
            return
        try:
            # Validate the credentials actually work before saving them.
            test_client = RobinhoodCryptoClient(api_key=api_key, private_key_b64=text)
            test_client.get_account()
        except Exception as e:
            await update.message.reply_text(
                f"Couldn't verify those credentials with Robinhood: {e}\n"
                f"Send /connect to try again."
            )
            return

        api_key_enc = crypto_util.encrypt(api_key)
        private_key_enc = crypto_util.encrypt(text)
        storage.upsert_user_credentials(user_id, api_key_enc, private_key_enc)
        _client_cache.pop(user_id, None)
        await update.message.reply_text(
            "Connected. Your credentials are encrypted and stored.\n"
            "Trading starts in DRY RUN mode - use /liveon when you're ready "
            "to place real orders.\n\n"
            "Send /start to open the menu."
        )
        return

    # --- alert entry ---
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
        storage.add_alert(user_id, SYMBOL, direction, target)
        await update.message.reply_text(f"Alert added: {SYMBOL} {direction} ${target:,.2f}")
        return

    # --- DCA entry ---
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
        storage.add_dca_schedule(user_id, SYMBOL, amount, interval)
        await update.message.reply_text(f"DCA schedule added: ${amount:.2f} every {interval:.1f}h")
        return

    await update.message.reply_text("Use /start to open the menu.")


# ---------- background loop: alerts + DCA, across ALL users ----------

async def check_alerts_and_dca(context: ContextTypes.DEFAULT_TYPE):
    """Runs periodically. Iterates every connected user and checks their
    alerts and DCA schedules against their own account. One user's data
    never touches another's here - each pass is scoped by user_id."""
    all_alerts = storage.get_active_alerts()
    all_dca = storage.get_active_dca_schedules()
    if not all_alerts and not all_dca:
        return

    user_ids = {a["telegram_user_id"] for a in all_alerts} | {d["telegram_user_id"] for d in all_dca}

    for user_id in user_ids:
        client = get_client(user_id)
        if client is None:
            continue
        try:
            price = get_eth_price(client)
        except Exception as e:
            log.warning("Background price fetch failed for user %s: %s", user_id, e)
            continue
        mid = price["mid"]

        for alert in [a for a in all_alerts if a["telegram_user_id"] == user_id]:
            target = alert["target_price"]
            triggered = (
                (alert["direction"] == "above" and mid >= target)
                or (alert["direction"] == "below" and mid <= target)
            )
            if triggered:
                storage.deactivate_alert(alert["id"])
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"ALERT: {alert['symbol']} is {alert['direction']} "
                        f"${target:,.2f} (currently ${mid:,.2f})"
                    ),
                )

        if storage.is_paused(user_id):
            continue  # kill switch also halts this user's automated DCA buys

        now = time.time()
        for sched in [d for d in all_dca if d["telegram_user_id"] == user_id]:
            if sched["next_run_at"] > now:
                continue

            usd_amount = sched["quote_amount"]
            try:
                risk.check_trade_allowed(user_id, usd_amount)
            except risk.RiskViolation as e:
                log.info("DCA buy skipped for user %s by risk check: %s", user_id, e)
                storage.update_dca_next_run(sched["id"], now + sched["interval_hours"] * 3600)
                continue

            client_order_id = str(uuid.uuid4())
            dry_run = storage.is_dry_run(user_id)
            storage.record_trade(
                user_id,
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
                    result = client.place_order(
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
            await context.bot.send_message(chat_id=user_id, text=msg)


# ---------- entrypoint ----------

def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    if not os.getenv("BOT_MASTER_KEY"):
        raise SystemExit(
            "BOT_MASTER_KEY is not set. This is required to encrypt user "
            "credentials in a multi-user bot. Generate one with:\n"
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )

    storage.init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect_cmd))
    app.add_handler(CommandHandler("disconnect", disconnect_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("liveon", liveon_cmd))
    app.add_handler(CommandHandler("liveoff", liveoff_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    app.job_queue.run_repeating(check_alerts_and_dca, interval=60, first=10)

    log.info("Multi-user bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
