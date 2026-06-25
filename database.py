"""
database.py — SQLite schema & connection helpers for VoiceBot AI
"""
import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "voicebot.db"


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            customer_name TEXT NOT NULL,
            customer_phone TEXT NOT NULL,
            order_details TEXT NOT NULL,
            service_type TEXT DEFAULT 'all',
            language TEXT DEFAULT 'en',
            status TEXT DEFAULT 'pending',
            intent TEXT,
            speech_result TEXT,
            call_sid TEXT,
            notes TEXT,
            scheduled_at TEXT,
            scheduled_label TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            call_sid TEXT,
            event_type TEXT NOT NULL,
            event_data TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_call_logs_order ON call_logs(order_id)
    """)

    conn.commit()
    conn.close()
    logger.info("[DB] Database initialized at %s", DB_PATH)


def get_db_connection() -> sqlite3.Connection:
    """Return a new SQLite connection with Row factory enabled."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def log_call_event(order_id: str, event_type: str, event_data: str = "", call_sid: str = "") -> None:
    """Append a structured call log entry."""
    from datetime import datetime
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO call_logs (order_id, call_sid, event_type, event_data, created_at) VALUES (?, ?, ?, ?, ?)",
        (order_id, call_sid, event_type, event_data, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
