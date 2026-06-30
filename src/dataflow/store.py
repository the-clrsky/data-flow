"""Tiny SQLite key/value store for app state (connections, table selection)."""
import sqlite3
import json
import os

DB_PATH = os.environ.get("DATAFLOW_DB", os.path.join(os.getcwd(), "data-flow.db"))


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    return c


def get(key, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def put(key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
