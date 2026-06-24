
# USE THIS FOR ALL THE DETECTION ENGINES

import pandas as pd
 
from storage.db import get_connection
 
# creates the df for the detecction engines
def load_transactions():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM transactions", conn)
    conn.close()
 
    df["date"] = pd.to_datetime(df["date"])
    return df