"""Stratum database layer — SQLite init plus simple CRUD helpers."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Optional

from config import DATABASE_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    commence_time TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS odds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    bookmaker TEXT NOT NULL,
    home_american REAL,
    away_american REAL,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    side TEXT NOT NULL,
    american_odds REAL NOT NULL,
    fair_probability REAL,
    kelly_stake REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a SQLite connection with row access by name and FK support."""
    conn = sqlite3.connect(db_path or DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Create all tables if they don't already exist."""
    conn = get_connection(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --- Generic CRUD helpers --------------------------------------------------

def insert(conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> int:
    """Insert a row; returns the new row id."""
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", tuple(data.values()))
    conn.commit()
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, table: str, row_id: int, data: dict[str, Any]) -> int:
    """Update a row by id; returns number of rows affected."""
    assignments = ", ".join(f"{k} = ?" for k in data)
    cur = conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?", (*data.values(), row_id)
    )
    conn.commit()
    return cur.rowcount


def delete(conn: sqlite3.Connection, table: str, row_id: int) -> int:
    """Delete a row by id; returns number of rows affected."""
    cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    conn.commit()
    return cur.rowcount


def fetch_all(conn: sqlite3.Connection, table: str, where: str = "", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    """Select rows, optionally filtered by a WHERE clause fragment."""
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql, tuple(params)).fetchall()


def fetch_one(conn: sqlite3.Connection, table: str, row_id: int) -> Optional[sqlite3.Row]:
    """Select a single row by id, or None."""
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
