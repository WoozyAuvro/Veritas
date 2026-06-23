from storage.db import get_connection

def drop_flags():
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS flags")
    conn.commit()
    conn.close()
    print("flags table dropped.")

def drop_transactions():
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS transactions")
    conn.commit()
    conn.close()
    print("transactions table dropped.")

def drop_all():
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS flags")
    conn.execute("DROP TABLE IF EXISTS transactions")
    conn.commit()
    conn.close()
    print("all tables dropped.")

if __name__ == "__main__":
    drop_flags()