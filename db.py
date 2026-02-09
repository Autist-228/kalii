import sqlite3
import datetime
import os

DB_PATH = os.getenv("DB_PATH", "arb_signals.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_ticker TEXT NOT NULL,
            event_title TEXT,
            market1_ticker TEXT NOT NULL,
            market1_title TEXT,
            market1_yes_price INTEGER,
            market2_ticker TEXT NOT NULL,
            market2_title TEXT,
            market2_yes_price INTEGER,
            total_cost_cents INTEGER NOT NULL,
            arb_profit_cents INTEGER NOT NULL,
            arb_percent REAL NOT NULL,
            arb_type TEXT NOT NULL,
            executed INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_ticker TEXT NOT NULL,
            event_title TEXT,
            arb_type TEXT NOT NULL,
            leg1_ticker TEXT NOT NULL,
            leg1_side TEXT NOT NULL,
            leg1_price INTEGER NOT NULL,
            leg1_qty INTEGER NOT NULL,
            leg1_fee INTEGER NOT NULL,
            leg2_ticker TEXT NOT NULL,
            leg2_side TEXT NOT NULL,
            leg2_price INTEGER NOT NULL,
            leg2_qty INTEGER NOT NULL,
            leg2_fee INTEGER NOT NULL,
            total_cost_cents INTEGER NOT NULL,
            total_fees_cents INTEGER NOT NULL,
            net_profit_cents INTEGER NOT NULL,
            net_arb_percent REAL NOT NULL,
            contracts INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            exit_timestamp TEXT,
            exit_profit_cents INTEGER DEFAULT 0,
            exit_type TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_signal(
    event_ticker: str,
    event_title: str,
    market1_ticker: str,
    market1_title: str,
    market1_yes_price: int,
    market2_ticker: str,
    market2_title: str,
    market2_yes_price: int,
    total_cost_cents: int,
    arb_profit_cents: int,
    arb_percent: float,
    arb_type: str,
):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO arb_signals
        (timestamp, event_ticker, event_title, market1_ticker, market1_title,
         market1_yes_price, market2_ticker, market2_title, market2_yes_price,
         total_cost_cents, arb_profit_cents, arb_percent, arb_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.datetime.utcnow().isoformat(),
            event_ticker,
            event_title,
            market1_ticker,
            market1_title,
            market1_yes_price,
            market2_ticker,
            market2_title,
            market2_yes_price,
            total_cost_cents,
            arb_profit_cents,
            arb_percent,
            arb_type,
        ),
    )
    conn.commit()
    conn.close()


def get_recent_signals(limit: int = 50) -> list:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM arb_signals ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
