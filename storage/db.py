
import sqlite3
import os
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "fraud.sqlite3"
DB_PATH = Path(os.getenv("DB_PATH", DEFAULT_DB_PATH)).expanduser()
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    conn.close()


def save_flags(flags):
    rows = [
        (f["txn_id"], f["method"], f["vendor_name"], f["date"], f["amount"], f["reason"])
        for f in flags
    ]
    conn = get_connection()
    conn.executemany(
        "INSERT INTO flags (txn_id, method, vendor_name, date, amount, reason) VALUES (?,?,?,?,?,?)",
        rows
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"database ready at {DB_PATH}")
