"""Shared SQLite data layer for the hotel reservation system.

Usage: python tools/reservation_db.py init
"""
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / ".tmp" / "hotel.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK (type IN ('single', 'double', 'suite')),
    capacity INTEGER NOT NULL,
    nightly_rate REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    guest_name TEXT NOT NULL,
    contact TEXT NOT NULL,
    check_in TEXT NOT NULL,
    check_out TEXT NOT NULL CHECK (check_out > check_in),
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_res_room_dates ON reservations(room_id, check_in, check_out);
"""

SEED_ROOMS = [
    ("101", "single", 1, 80.0), ("102", "single", 1, 80.0),
    ("201", "double", 2, 120.0), ("202", "double", 2, 120.0), ("203", "double", 3, 140.0),
    ("301", "suite", 4, 250.0),
]


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO rooms (number, type, capacity, nightly_rate) VALUES (?, ?, ?, ?)",
                SEED_ROOMS,
            )
        return conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] != "init":
        print("usage: reservation_db.py init", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"db": str(DB_PATH), "rooms": init()}))
