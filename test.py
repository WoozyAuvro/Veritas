from tools import query_sql_ledger
count = 5
#print(query_sql_ledger(f"SELECT reason, date FROM flags ORDER BY date ASC LIMIT 5 OFFSET {count};"))

query = "SELECT COUNT(*) AS total_rows FROM flags;"
print(int(query_sql_ledger(query)[20:])+5)

