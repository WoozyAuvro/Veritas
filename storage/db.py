
import sqlite3
from pathlib import Path
 
DB_PATH = Path(__file__).parent.parent / "data" / "fraud.sqlite3"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
 
 
def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
 
# creates the tables
def init_db():
    
    conn = get_connection()
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    conn.close()
 
# does it work tho
if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DB_PATH}")