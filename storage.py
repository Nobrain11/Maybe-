"""
Persistence layer. SQLite, single file, no external DB server needed.
Everything the bot needs to remember across restarts lives here:
  - trade log (every order, filled or not)
  - price alerts
  - DCA (recurring buy) schedules
  - risk state (daily loss tracking, pause flag)
  - key-value settings
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    symbol TEXT,
    direction TEXT,          -- 'above' | 'below'
    target_price REAL,
    active INTEGER DEFAULT 1,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS dca_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    quote_amount REAL,       -- dollars per buy
    interval_hours REAL,
    next_run_at REAL,
    active INTEGER DEFAULT 1,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT PRIMARY KEY,   -- YYYY-MM-DD
    realized_loss REAL DEFAULT 0
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


# ---------- settings (key-value) ----------

def get_setting(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def is_paused() -> bool:
    return get_setting("paused", "false") == "true"


def set_paused(paused: bool):
    set_setting("paused", "true" if paused else "false")


def is_dry_run() -> bool:
    # Defaults to True - real trading must be explicitly enabled.
    return get_setting("dry_run", "true") == "true"


def set_dry_run(dry_run: bool):
    set_setting("dry_run", "true" if dry_run else "false")


def get_risk_limits() -> dict:
    raw = get_setting("risk_limits")
    if raw:
        return json.loads(raw)
    return {
        "max_trade_usd": 100.0,
        "daily_loss_limit_usd": 200.0,
        "cooldown_seconds": 30,
    }


def set_risk_limits(limits: dict):
    set_setting("risk_limits", json.dumps(limits))


# ---------- trades ----------

def record_trade(**fields) -> int:
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


def get_recent_trades(limit: int = 10) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()


def get_last_trade_time():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) as t FROM trades"
        ).fetchone()
        return row["t"] if row and row["t"] else None


# ---------- alerts ----------

def add_alert(symbol: str, direction: str, target_price: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (symbol, direction, target_price, created_at) "
            "VALUES (?, ?, ?, ?)",
            (symbol, direction, target_price, time.time()),
        )
        return cur.lastrowid


def get_active_alerts() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM alerts WHERE active = 1").fetchall()


def deactivate_alert(alert_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE alerts SET active = 0 WHERE id = ?", (alert_id,))


def delete_alert(alert_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))


# ---------- DCA schedules ----------

def add_dca_schedule(symbol: str, quote_amount: float, interval_hours: float) -> int:
    next_run = time.time() + interval_hours * 3600
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO dca_schedules (symbol, quote_amount, interval_hours, "
            "next_run_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (symbol, quote_amount, interval_hours, next_run, time.time()),
        )
        return cur.lastrowid


def get_active_dca_schedules() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM dca_schedules WHERE active = 1").fetchall()


def update_dca_next_run(schedule_id: int, next_run_at: float):
    with get_conn() as conn:
        conn.execute(
            "UPDATE dca_schedules SET next_run_at = ? WHERE id = ?",
            (next_run_at, schedule_id),
        )


def deactivate_dca_schedule(schedule_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE dca_schedules SET active = 0 WHERE id = ?", (schedule_id,))


# ---------- daily loss tracking ----------

def add_realized_loss(date_str: str, amount: float):
    """amount should be positive when it represents a loss."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_pnl (date, realized_loss) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET realized_loss = realized_loss + excluded.realized_loss",
            (date_str, amount),
        )


def get_realized_loss(date_str: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT realized_loss FROM daily_pnl WHERE date = ?", (date_str,)
        ).fetchone()
        return row["realized_loss"] if row else 0.0
