-- while making it i named it transactions as in bank transactions and im too lazy to change it sorry
CREATE TABLE IF NOT EXISTS transactions (
    txn_id TEXT PRIMARY KEY,
    date TEXT,                  -- yyyy-mm-dd
    description TEXT,
    amount REAL,                
    balance REAL,
    reference_number TEXT,      
    vendor_name TEXT,           
    invoice_number TEXT,        
    source_filename TEXT,
    row_hash TEXT UNIQUE,       
    upload_batch_id TEXT
);

CREATE TABLE IF NOT EXISTS flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id      TEXT,
    method      TEXT,
    vendor_name TEXT,
    date        TEXT,
    amount      REAL,
    reason      TEXT
);