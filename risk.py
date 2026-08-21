"""
Risk guardrails, per-user. Every trade must pass through
check_trade_allowed(user_id, amount) before an order is sent to
Robinhood. Single choke point - add new rules here, not elsewhere,
so nothing can bypass them.
"""
import datetime as dt
import time

import storage


class RiskViolation(Exception):
    pass


def _today_str() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d")


def check_trade_allowed(user_id: int, quote_amount_usd: float):
    """Raises RiskViolation with a human-readable reason if the trade
    should be blocked. Returns None if OK."""

    if storage.is_paused(user_id):
        raise RiskViolation(
            "Trading is paused (kill switch is on). Use /resume to re-enable."
        )

    limits = storage.get_risk_limits(user_id)

    if quote_amount_usd > limits["max_trade_usd"]:
        raise RiskViolation(
            f"Trade of ${quote_amount_usd:.2f} exceeds your max trade size "
            f"of ${limits['max_trade_usd']:.2f}. Adjust in /settings if intended."
        )

    last_trade_time = storage.get_last_trade_time(user_id)
    if last_trade_time is not None:
        elapsed = time.time() - last_trade_time
        cooldown = limits["cooldown_seconds"]
        if elapsed < cooldown:
            wait = cooldown - elapsed
            raise RiskViolation(
                f"Cooldown active - wait {wait:.0f}s before your next trade "
                f"(prevents accidental rapid-fire orders)."
            )

    today_loss = storage.get_realized_loss(user_id, _today_str())
    if today_loss >= limits["daily_loss_limit_usd"]:
        raise RiskViolation(
            f"Daily loss limit reached (${today_loss:.2f} / "
            f"${limits['daily_loss_limit_usd']:.2f}). Trading auto-paused "
            f"until tomorrow (UTC) or until you raise the limit in /settings."
        )


def record_realized_pnl(user_id: int, pnl_usd: float):
    """Call after a sell fills. Pass a NEGATIVE number for a loss.
    Only losses accumulate against the daily limit."""
    if pnl_usd < 0:
        storage.add_realized_loss(user_id, _today_str(), abs(pnl_usd))
