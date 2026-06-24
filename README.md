# AI Fraud Detector — Ingestion Guide

This covers how to load data into the system and how to clear it out. All commands
are run from the `fraud_detector` project folder.

## Setup

Make sure your `.env` file has both API keys set:

```
GROQ_API_KEY=your-groq-key
OPENROUTER_API_KEY=your-openrouter-key
```

Groq handles all LLM extraction calls. OpenRouter handles embeddings (Groq doesn't
offer an embedding model).

---

## Receipts

Receipts go into **ChromaDB** (not the SQL database). Accepted file types: `.pdf`, `.txt`.

### One receipt at a time

```powershell
python -m ingestion.receipt path\to\receipt.pdf
```

Prints the extracted metadata (vendor, amount, date, invoice number, etc.) once it's done.

### A whole folder of receipts at once

```powershell
python -m ingestion.batch_loader receipt path\to\folder
```

Processes every `.pdf`/`.txt` file in the folder (and subfolders). Any other file
type in that folder is skipped, not treated as an error — the rest of the batch
still completes. Prints `[ok]`/`[fail]`/`[skip]` per file as it goes, then a summary
showing what succeeded, failed, and was skipped.

Use the single-file command if you're only adding one or two receipts. Use the
batch command for anything larger — it includes a short pause between files to
avoid hitting API rate limits.

### Receipt metadata format

Each receipt is stored in ChromaDB with this metadata:

| Field | Type | Notes |
|---|---|---|
| `id` | string | Content-based hash, e.g. `receipt_f6e8058f...` — same file content always produces the same id, so re-ingesting doesn't duplicate |
| `date` | string | YYYY-MM-DD format |
| `date_utix`| int | this is what chromadb uses to measure dates |
| `amount_bdt` | number | Numeric amount in BDT |
| `vendor_name` | string | |
| `content` | string | Short description of what the expenditure was for |
| `invoice_number` | string | Empty string if none was found on the receipt |
| `doc_type` | string | Always `"receipt"` |

The full raw receipt text is also stored alongside the metadata (used for the embedding/semantic search).

---

## Emails

Emails also go into **ChromaDB**, in the same collection as receipts (kept separate
internally by a `doc_type` field). Accepted file types: `.eml`, `.txt`.

### One email at a time

```powershell
python -m ingestion.email_ingest path\to\email.eml
```

### A whole folder of emails at once

```powershell
python -m ingestion.batch_loader email path\to\folder
```

Same behavior as the receipt batch loader — processes every `.eml`/`.txt` file,
skips anything else, keeps going if one file fails, prints a summary at the end.

**Note:** `.eml` files get their `from`/`to`/`subject`/`date` headers read directly
from the file. Plain `.txt` emails don't have real headers, so the LLM has to infer
those fields from the email's text content instead — this usually works fine, but a
`.txt` email with no date mentioned anywhere will come back with an empty `date`.

### Email metadata format

Each email is stored in ChromaDB with this metadata:

| Field | Type | Notes |
|---|---|---|
| `id` | string | Content-based hash, e.g. `email_1076235d...` |
| `date` | string | From the `.eml` header if present, otherwise inferred by the LLM (YYYY-MM-DD) — can be empty if no date appears anywhere |
| `date_utix`| int | this is what chromadb uses to measure dates |
| `from` | string | Sender |
| `to` | string | Recipient |
| `subject` | string | |
| `parties` | string | Other companies/people mentioned besides sender and recipient, comma-separated (e.g. `"Rahim Chowdhury, Zenith Traders"`) — empty string if none |
| `body` | string | The email body text |
| `doc_type` | string | Always `"email"` |

The full raw email text is also stored alongside the metadata.

---

## Clearing ChromaDB (receipts + emails)

Two options, since both receipts and emails live in the same collection.

**Delete everything:**

```powershell
python -c "from storage.vector_store import delete_all_documents; print('Deleted', delete_all_documents(), 'documents')"
```

**Full reset (delete the folder entirely):**

```powershell
Remove-Item -Recurse -Force data\chroma
```

The collection gets recreated automatically, empty, next time anything tries to use it.

**Check what's currently stored**, useful before deciding whether to clear anything:

```python
from storage.vector_store import list_documents

docs = list_documents()          # everything
# docs = list_documents("receipt")   # just receipts
# docs = list_documents("email")     # just emails

print(f"Total: {len(docs)}")
for doc in docs:
    print(doc["metadata"])
```

Save as a `.py` file and run it — avoids PowerShell quoting issues with inline `-c` commands.

**Delete one specific document** (e.g. from a frontend's delete button):

```python
from storage.vector_store import delete_document
delete_document("receipt_abc123...")  # use the exact id
```

---

## Bank Statements

Bank statements go into the **SQL database** (SQLite), not ChromaDB. Accepted file
type: `.csv`. Column order and naming can vary — the system uses an LLM to figure
out the mapping automatically each time.

```powershell
python -m ingestion.bank_statement path\to\statement.csv
```

Prints a summary: how many rows were loaded and the column mapping the system
figured out.

Bank statements don't have a folder-batch loader — each CSV is ingested individually,
since CSVs typically already contain many transaction rows in one file rather than
needing many separate files combined.

Re-uploading the exact same statement twice is safe — duplicate rows are
automatically skipped, not double-counted.

### Bank statement CSV format

The CSV needs a header row. Column **names** can vary (the LLM maps whatever the
bank actually calls them) and column **order** doesn't matter, but the CSV should
represent these fields:

| Field | Required? | Notes |
|---|---|---|
| Date | Required | Any common date format works (`pd.to_datetime` with `dayfirst=True` parses it) |
| Description | Optional | Free-text narration of the transaction |
| Amount | Required | A single signed column — positive = money in, negative = money out. Commas, currency symbols, and parenthesized negatives like `(1,234.56)` are all handled automatically. **No separate debit/credit columns** — combine them into one signed amount before uploading |
| Balance | Optional | Running account balance after the transaction |
| Reference number | Optional | Cheque number, UTR, or similar transaction reference |
| Vendor name | Optional | Used later to match against receipts |
| Invoice number | Optional | Used later to match against receipts — for clean matching, keep this as plain digits (e.g. `4521`, not `INV4521`), since receipt invoice numbers are extracted by an LLM and aren't guaranteed to include the same prefix |

Example header row:
```
date,description,amount,balance,vendor_name,invoice_number,reference_number
```

What gets stored in the `transactions` table after ingestion:

| Column | Type | Notes |
|---|---|---|
| `txn_id` | TEXT | Generated UUID |
| `date` | TEXT | Normalized to `YYYY-MM-DD` |
| `description` | TEXT | |
| `amount` | REAL | Signed |
| `balance` | REAL | `NULL` if not provided |
| `reference_number` | TEXT | Empty string if not provided |
| `vendor_name` | TEXT | Empty string if not provided |
| `invoice_number` | TEXT | Empty string if not provided |
| `source_filename` | TEXT | |
| `row_hash` | TEXT | Used to prevent duplicate inserts on re-upload |
| `upload_batch_id` | TEXT | Groups rows from the same ingestion run |

---

## Clearing the SQL Database (bank statements)

**Delete everything in the transactions table, keep the database file:**

```python
from storage.db import get_connection

conn = get_connection()
conn.execute("DELETE FROM transactions")
conn.commit()
print("Cleared all transactions")
```

**Full reset (delete the database file entirely):**

```powershell
Remove-Item data\fraud.sqlite3
python -m storage.db
```

Do this if you've changed `schema.sql` (added/removed a column) — `CREATE TABLE IF
NOT EXISTS` won't update an existing table's structure, so after a schema change the
old file needs to be deleted and rebuilt fresh.

**Check what's currently stored:**

```python
from storage.db import get_connection

conn = get_connection()
count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
print(f"Total rows: {count}")

rows = conn.execute(
    """SELECT date, description, amount, balance, vendor_name, invoice_number, reference_number
       FROM transactions ORDER BY date"""
).fetchall()
for row in rows:
    print(dict(row))


## Quick Reference

| Task | Command |
|---|---|
| One receipt | `python -m ingestion.receipt <file>` |
| Folder of receipts | `python -m ingestion.batch_loader receipt <folder>` |
| One email | `python -m ingestion.email_ingest <file>` |
| Folder of emails | `python -m ingestion.batch_loader email <folder>` |
| One bank statement | `python -m ingestion.bank_statement <file.csv>` |
| Run z-score detection | `python -m detection.z_score` |
| Run Isolation Forest detection | `python -m detection.isolation_forest` |
| Clear ChromaDB | `Remove-Item -Recurse -Force data\chroma` |
| Clear SQL database | `Remove-Item data\fraud.sqlite3` then `python -m storage.db` |
