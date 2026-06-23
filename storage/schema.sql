--while making it i named it transactions as in bank transactions and im too lazy to change it sorry
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
    upload_batch_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_vendor ON transactions(vendor_name);
CREATE INDEX IF NOT EXISTS idx_txn_batch ON transactions(upload_batch_id);
CREATE INDEX IF NOT EXISTS idx_txn_reference ON transactions(reference_number);
CREATE INDEX IF NOT EXISTS idx_txn_invoice ON transactions(invoice_number);

CREATE TABLE IF NOT EXISTS flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id      TEXT,
    method      TEXT,
    vendor_name TEXT,
    date        TEXT,
    amount      REAL,
    reason      TEXT
);