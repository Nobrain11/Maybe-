"""
Persistence layer, multi-user. Every table is keyed by telegram_user_id
so each person's credentials, trades, alerts, and settings are isolated
from everyone else's - this is what makes "anyone can use the bot" safe:
no one can see or touch another user's data or account.
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    robinhood_api_key_enc TEXT,
    robinhood_private_key_enc TEXT,
    connected_at REAL,
    dry_run INTEGER DEFAULT 1,   -- 1 = dry run (default, safe), 0 = live
    paused INTEGER DEFAULT 0,
    max_trade_usd REAL DEFAULT 100.0,
    daily_loss_limit_usd REAL DEFAULT 200.0,
    cooldown_seconds REAL DEFAULT 30
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER,
    client_order_id TEXT UNIQUE,
    robinhood_order_id TEXT,
    symbol TEXT,
    side TEXT,
    order_type TEXT,
    requested_quote_amount TEXT,
    requested_asset_quantity TEXT,
    limit_price TEXT,
    status TEXT,
    filled_price TEXT,
    filled_quantity TEXT,
    created_at REAL,
    updated_at REAL,
    raw_response TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER,
    symbol TEXT,
    direction TEXT,          -- 'above' | 'below'
    target_price REAL,
    active INTEGER DEFAULT 1,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS dca_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER,
    symbol TEXT,
    quote_amount REAL,       -- dollars per buy
    interval_hours REAL,
    next_run_at REAL,
    active INTEGER DEFAULT 1,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    telegram_user_id INTEGER,
    date TEXT,               -- YYYY-MM-DD
    realized_loss REAL DEFAULT 0,
    PRIMARY KEY (telegram_user_id, date)
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------- users / credentials ----------

def upsert_user_credentials(user_id: int, api_key_enc: str, private_key_enc: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (telegram_user_id, robinhood_api_key_enc, "
            "robinhood_private_key_enc, connected_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "robinhood_api_key_enc = excluded.robinhood_api_key_enc, "
            "robinhood_private_key_enc = excluded.robinhood_private_key_enc, "
            "connected_at = excluded.connected_at",
            (user_id, api_key_enc, private_key_enc, time.time()),
        )


def get_user(user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?", (user_id,)
        ).fetchone()


def is_connected(user_id: int) -> bool:
    u = get_user(user_id)
    return u is not None and u["robinhood_api_key_enc"] is not None


def disconnect_user(user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET robinhood_api_key_enc = NULL, "
            "robinhood_private_key_enc = NULL WHERE telegram_user_id = ?",
            (user_id,),
        )


def _ensure_user_row(conn, user_id: int):
    conn.execute(
        "INSERT INTO users (telegram_user_id) VALUES (?) "
        "ON CONFLICT(telegram_user_id) DO NOTHING",
        (user_id,),
    )


# ---------- per-user mode / pause ----------

def is_paused(user_id: int) -> bool:
    u = get_user(user_id)
    return bool(u["paused"]) if u else False


def set_paused(user_id: int, paused: bool):
    with get_conn() as conn:
        _ensure_user_row(conn, user_id)
        conn.execute(
            "UPDATE users SET paused = ? WHERE telegram_user_id = ?",
            (1 if paused else 0, user_id),
        )


def is_dry_run(user_id: int) -> bool:
    u = get_user(user_id)
    return bool(u["dry_run"]) if u else True  # default: safe


def set_dry_run(user_id: int, dry_run: bool):
    with get_conn() as conn:
        _ensure_user_row(conn, user_id)
        conn.execute(
            "UPDATE users SET dry_run = ? WHERE telegram_user_id = ?",
            (1 if dry_run else 0, user_id),
        )


def get_risk_limits(user_id: int) -> dict:
    u = get_user(user_id)
    if u is None:
        return {"max_trade_usd": 100.0, "daily_loss_limit_usd": 200.0, "cooldown_seconds": 30}
    return {
        "max_trade_usd": u["max_trade_usd"],
        "daily_loss_limit_usd": u["daily_loss_limit_usd"],
        "cooldown_seconds": u["cooldown_seconds"],
    }


def set_risk_limits(user_id: int, limits: dict):
    with get_conn() as conn:
        _ensure_user_row(conn, user_id)
        conn.execute(
            "UPDATE users SET max_trade_usd = ?, daily_loss_limit_usd = ?, "
            "cooldown_seconds = ? WHERE telegram_user_id = ?",
            (
                limits["max_trade_usd"],
                limits["daily_loss_limit_usd"],
                limits["cooldown_seconds"],
                user_id,
            ),
        )


# ---------- trades ----------

def record_trade(user_id: int, **fields) -> int:
    fields["telegram_user_id"] = user_id
    fields.setdefault("created_at", time.time())
    fields["updated_at"] = time.time()
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        return cur.lastrowid


def update_trade_status(client_order_id: str, **fields):
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE trades SET {set_clause} WHERE client_order_id = ?",
            (*fields.values(), client_order_id),
        )


def get_recent_trades(user_id: int, limit: int = 10) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE telegram_user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def get_last_trade_time(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) as t FROM trades WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()
        return row["t"] if row and row["t"] else None


# ---------- alerts ----------

def add_alert(user_id: int, symbol: str, direction: str, target_price: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (telegram_user_id, symbol, direction, "
            "target_price, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, symbol, direction, target_price, time.time()),
        )
        return cur.lastrowid


def get_active_alerts(user_id=None) -> list:
    with get_conn() as conn:
        if user_id is None:
            return conn.execute("SELECT * FROM alerts WHERE active = 1").fetchall()
        return conn.execute(
            "SELECT * FROM alerts WHERE active = 1 AND telegram_user_id = ?",
            (user_id,),
        ).fetchall()


def deactivate_alert(alert_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE alerts SET active = 0 WHERE id = ?", (alert_id,))


def delete_alert(alert_id: int, user_id: int):
    """user_id required so one user can't delete another user's alert by guessing an id."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM alerts WHERE id = ? AND telegram_user_id = ?",
            (alert_id, user_id),
        )


# ---------- DCA schedules ----------

def add_dca_schedule(user_id: int, symbol: str, quote_amount: float, interval_hours: float) -> int:
    next_run = time.time() + interval_hours * 3600
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO dca_schedules (telegram_user_id, symbol, quote_amount, "
            "interval_hours, next_run_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, symbol, quote_amount, interval_hours, next_run, time.time()),
        )
        return cur.lastrowid


def get_active_dca_schedules(user_id=None) -> list:
    with get_conn() as conn:
        if user_id is None:
            return conn.execute("SELECT * FROM dca_schedules WHERE active = 1").fetchall()
        return conn.execute(
            "SELECT * FROM dca_schedules WHERE active = 1 AND telegram_user_id = ?",
            (user_id,),
        ).fetchall()


def update_dca_next_run(schedule_id: int, next_run_at: float):
    with get_conn() as conn:
        conn.execute(
            "UPDATE dca_schedules SET next_run_at = ? WHERE id = ?",
            (next_run_at, schedule_id),
        )


def deactivate_dca_schedule(schedule_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE dca_schedules SET active = 0 WHERE id = ? AND telegram_user_id = ?",
            (schedule_id, user_id),
        )


# ---------- daily loss tracking ----------

def add_realized_loss(user_id: int, date_str: str, amount: float):
    """amount should be positive when it represents a loss."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_pnl (telegram_user_id, date, realized_loss) "
            "VALUES (?, ?, ?) ON CONFLICT(telegram_user_id, date) "
            "DO UPDATE SET realized_loss = realized_loss + excluded.realized_loss",
            (user_id, date_str, amount),
        )


def get_realized_loss(user_id: int, date_str: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT realized_loss FROM daily_pnl WHERE telegram_user_id = ? AND date = ?",
            (user_id, date_str),
        ).fetchone()
        return row["realized_loss"] if row else 0.0
