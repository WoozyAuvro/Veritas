from storage.db import get_connection

def clear_flags():
    conn = get_connection()
    conn.execute("DELETE FROM flags")
    conn.commit()
    conn.close()
    print("wiped all flags")

def clear_transactions():
    conn = get_connection()
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    print("wiped all transactions")

def drop_all():
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS flags")
    conn.execute("DROP TABLE IF EXISTS transactions")
    conn.commit()
    conn.close()
    print("all tables dropped. need to reinitialize storage.db")

if __name__ == "__main__":
    drop_all()