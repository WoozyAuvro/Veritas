
import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_path(env_var: str, default: Path) -> Path:
    raw = os.getenv(env_var)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path
    return default


DB_PATH = _resolve_path("FRAUD_DB_PATH", PROJECT_ROOT / "data" / "fraud.sqlite3")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


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