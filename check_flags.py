from storage.db import get_connection

conn = get_connection()
rows = conn.execute("SELECT * FROM flags LIMIT 10").fetchall()

for row in rows:
    print(tuple(row))

print(f"\nTotal flags: {conn.execute('SELECT COUNT(*) FROM flags').fetchone()[0]}")