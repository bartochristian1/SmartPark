"""
database.py
-----------
Handles the "Dynamic Database" requirement of the brief.

Why SQLite, and why this schema is "dynamic":
- TOTAL_SLOTS is a config value, not hard-coded row-by-row logic. Increasing
  it (e.g. the client adds a new parking level) only means changing one
  number and re-running init_db() - it will INSERT the extra slot rows
  without touching existing data (see ensure_slot_count()).
- The `sessions` table is an append-only log: every arrival creates a new
  row, every departure updates that same row (exit_time, fee, status).
  So the database grows dynamically with real traffic instead of using a
  fixed-size structure - this is what lets us produce history / reports.
- Foreign key (slot_id) ties a session to a physical slot so we can always
  answer "which car is in slot 7 right now?" with a single indexed query.
- payment_status / checkout_request_id / mpesa_receipt / phone_number
  support the Safaricom Daraja (M-Pesa STK Push) payment flow: a session
  moves from 'pending' -> 'paid' (or 'failed') as Daraja calls back, so
  the car park never opens the barrier before money has actually landed.
"""

import sqlite3
from contextlib import contextmanager

DB_PATH = "parking.db"

# Total number of physical slots the client currently operates.
# Change this single value to scale the car park up or down.
TOTAL_SLOTS = 50


@contextmanager
def get_conn():
    """Context-managed SQLite connection so callers never forget to close it."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they do not exist, then make sure the slot count
    matches TOTAL_SLOTS (this is the "dynamic" part - it can grow later)."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_number TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'available'  -- 'available' | 'occupied'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_number TEXT NOT NULL,
                slot_id INTEGER NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                fee REAL,
                status TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'completed'
                FOREIGN KEY (slot_id) REFERENCES slots (id)
            )
        """)
        # M-Pesa / Daraja payment tracking columns (added on top of the
        # original schema so existing databases upgrade in place).
        _ensure_column(conn, "sessions", "phone_number", "TEXT")
        _ensure_column(conn, "sessions", "checkout_request_id", "TEXT")
        _ensure_column(conn, "sessions", "payment_status", "TEXT")  # pending|paid|failed|NULL(free)
        _ensure_column(conn, "sessions", "mpesa_receipt", "TEXT")

        ensure_slot_count(conn, TOTAL_SLOTS)


def _ensure_column(conn, table, column, coltype):
    """Adds `column` to `table` if it doesn't already exist (safe to re-run)."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def ensure_slot_count(conn, target_count):
    """Grow the slots table to target_count without disturbing existing rows.
    This is what makes the schema 'dynamic' rather than a fixed array."""
    current = conn.execute("SELECT COUNT(*) AS c FROM slots").fetchone()["c"]
    for i in range(current + 1, target_count + 1):
        conn.execute(
            "INSERT INTO slots (slot_number, status) VALUES (?, 'available')",
            (f"S{i:02d}",),
        )
