# AI Fraud Detector

A Python-based financial fraud detection system that ingests bank statements, receipts,
and emails, then runs six statistical and ML detection engines to surface anomalous
transactions. All flags are saved to SQLite for the frontend to read and display.

---

## Project Structure

```
fraud_detector/
├── detection/
│   ├── benford_law.py
│   ├── invoice_matching.py
│   ├── iso_forest.py
│   ├── loader.py
│   ├── round_numbers.py
│   ├── run_engines.py
│   ├── threshold_skirting.py
│   └── z_scoring.py
├── ingestion/
│   ├── bank_statement.py
│   ├── batch_loader.py
│   ├── email_ingest.py
│   └── receipt.py
├── storage/
│   ├── db.py
│   ├── reset_db.py
│   ├── schema.sql
│   └── vector_store.py
├── data/
│   ├── fraud.sqlite3
│   └── chroma/
├── receipts_folder/
├── email_folder/
└── .env
```

---

## Setup

Make sure your `.env` file has both API keys set:

```
GROQ_API_KEY=your-groq-key
OPENROUTER_API_KEY=your-openrouter-key
```

Groq handles all LLM extraction calls. OpenRouter handles embeddings (Groq doesn't
offer an embedding model).

Initialize the database (creates both `transactions` and `flags` tables):

```powershell
python -m storage.db
```

---

## Receipts

Receipts go into **ChromaDB** (not the SQL database). Accepted file types: `.pdf`, `.txt`.

### One receipt at a time

```powershell
python -m ingestion.receipt path\to\receipt.pdf
```

### A whole folder of receipts at once

```powershell
python -m ingestion.batch_loader receipt path\to\folder
```

Processes every `.pdf`/`.txt` file in the folder and subfolders. Any other file type
is skipped. Prints `[ok]`/`[fail]`/`[skip]` per file as it goes, then a full summary.
Includes a short pause between files to avoid hitting API rate limits.

### Receipt metadata format

Each receipt is stored in ChromaDB with this metadata:

| Field | Type | Notes |
|---|---|---|
| `id` | string | Content-based hash, e.g. `receipt_f6e8058f...` — same file always produces the same id, re-ingesting won't duplicate |
| `date` | string | Human-readable date as extracted by the LLM, e.g. `"2024-03-15"` |
| `date_unix` | integer | Same date as a Unix timestamp — used by ChromaDB's date range filters (`$gte`/`$lte` only accept numbers) |
| `amount_bdt` | number | Numeric amount in BDT |
| `vendor_name` | string | |
| `content` | string | Short description of what the expenditure was for |
| `invoice_number` | string | Empty string if none found |
| `doc_type` | string | Always `"receipt"` |

The full raw receipt text is stored alongside the metadata for semantic search.

**Why two date fields?** ChromaDB's comparison operators require numeric values.
`date` is kept as a readable string so LLMs and humans can understand it.
`date_unix` is the same date as seconds-since-epoch so ChromaDB can filter by date range.

---

## Emails

Emails also go into **ChromaDB**, in the same collection as receipts (separated
internally by the `doc_type` field). Accepted file types: `.eml`, `.txt`.

### One email at a time

```powershell
python -m ingestion.email_ingest path\to\email.eml
```

### A whole folder of emails at once

```powershell
python -m ingestion.batch_loader email path\to\folder
```

Same behavior as the receipt batch loader.

**Note:** `.eml` files get their `from`/`to`/`subject`/`date` headers read directly
from the file. Plain `.txt` emails don't have real headers so the LLM infers those
fields from the body text — a `.txt` email with no date mentioned anywhere will come
back with an empty `date`.

### Email metadata format

| Field | Type | Notes |
|---|---|---|
| `id` | string | Content-based hash, e.g. `email_1076235d...` |
| `date` | string | Human-readable date from `.eml` header or inferred by LLM |
| `date_unix` | integer | Same date as Unix timestamp for ChromaDB filtering |
| `from` | string | Sender |
| `to` | string | Recipient |
| `subject` | string | |
| `parties` | string | Other companies/people mentioned besides sender and recipient, comma-separated |
| `body` | string | Email body text |
| `doc_type` | string | Always `"email"` |

---

## Bank Statements

Bank statements go into **SQLite**, not ChromaDB. Accepted file type: `.csv`.

```powershell
python -m ingestion.bank_statement path\to\statement.csv
```

Re-uploading the same statement is safe — duplicate rows are automatically skipped.

### Bank statement CSV format

The CSV needs a header row. Column names and order can vary — the system uses an LLM
to figure out the mapping. The CSV should represent these fields:

| Field | Required? | Notes |
|---|---|---|
| Date | Required | Any common date format |
| Description | Optional | Free-text narration |
| Amount | Required | Single signed column — positive = money in, negative = money out |
| Balance | Optional | Running account balance |
| Reference number | Optional | Cheque number, UTR, or similar |
| Vendor name | Optional | Used to match against receipts |
| Invoice number | Optional | Used to match against receipts |

Example header row:
```
date,description,amount,balance,vendor_name,invoice_number,reference_number
```

What gets stored in the `transactions` table:

| Column | Type | Notes |
|---|---|---|
| `txn_id` | TEXT | Generated UUID |
| `date` | TEXT | Normalized to `YYYY-MM-DD` |
| `description` | TEXT | |
| `amount` | REAL | Signed |
| `balance` | REAL | NULL if not provided |
| `reference_number` | TEXT | Empty string if not provided |
| `vendor_name` | TEXT | Empty string if not provided |
| `invoice_number` | TEXT | Empty string if not provided |
| `source_filename` | TEXT | |
| `row_hash` | TEXT | Prevents duplicate inserts |
| `upload_batch_id` | TEXT | Groups rows from the same ingestion run |

---

## Running the Detection Engines

All six engines are orchestrated by `run_engines.py`. Run all of them at once:

```powershell
python -m detection.run_engines
```

This loads the transactions DataFrame once, passes it to every engine, collects all
flags, and bulk-inserts them into the `flags` table in SQLite.

The frontend can also select specific engines by passing a list of engine name strings
to `run_engines()`:

```python
from detection.run_engines import run_engines

run_engines(["z_scoring", "benford_law"])        # only these two
run_engines()                                     # all six
```

Valid engine name strings:
`"z_scoring"`, `"iso_forest"`, `"invoice_matching"`, `"threshold_skirting"`,
`"round_numbers"`, `"benford_law"`

---

## The Six Detection Engines

Each statistical engine accepts a DataFrame via `df=None` and returns a list of flag dicts.
No engine has a `main()` — the orchestrator calls each engine's detection function
directly.

### 1. Z-Scoring (`z_scoring.py` → `detect_zscore_outliers`)

Flags transactions that are statistically too far from a vendor's normal payment range.
Computes Z-Score per transaction per vendor. Anything above 3.0 standard deviations
is flagged.

Skips: vendors with fewer than 3 transactions, vendors where all amounts are identical,
rows with no vendor name.

Key parameters: `MIN_TRANSACTIONS_FOR_ZSCORE = 8`, `ZSCORE_THRESHOLD = 3.0`

### 2. Isolation Forest (`iso_forest.py` → `detect_isolation_forest_outliers`)

ML-based anomaly detection trained per vendor on three features: payment amount,
days since last transaction with that vendor, and day of week. Flags the bottom 3%
most anomalous transactions as determined by the model.

Skips: vendors with fewer than 10 transactions, rows with no vendor name.

Key parameters: `CONTAMINATION = 0.035`, `MIN_TRANSACTIONS_FOR_ISOFOREST = 10`

### 3. Invoice Matching (`invoice_matching.py` → `detect_invoice_matching`)

Cross-references every outgoing transaction that has an invoice number against receipt
documents in ChromaDB. Flags transactions where:
- 0 receipt matches found → Phantom Payment risk
- 2+ receipt matches found → Duplicate Invoice / double billing risk
- 1 receipt match found but amount mismatch → overbilling / fabrication / manual override

Invoice numbers are normalized on both sides before matching (strips "INV-" prefixes,
punctuation, and spaces) so format differences between the bank and receipt don't
cause false misses.

Receipt queries use `date_unix` for ChromaDB's date range filter within a ±7 day
window around the transaction date.

Skips: incoming transactions, transactions with no invoice number.

Key parameters: `DATE_WINDOW_DAYS = 7`, `AMOUNT_TOLERANCE = 100`

### 4. Threshold Skirting (`threshold_skirting.py` → `detect_threshold_skirting`)

Detects smurfing — splitting payments into smaller amounts to stay below a
mandatory approval threshold. Groups transactions per vendor into time-based
clusters and flags any cluster of 3+ payments that fall within the 20% window
just below the threshold, all occurring within 10 days of each other.

Skips: rows with no vendor name, vendors with fewer than 3 candidate transactions total.

Key parameters: `THRESHOLD = 100000`, `SKIRTING_PERCENT = 0.20`, `ENOUGH_DAYS = 10`

### 5. Round Numbers (`round_numbers.py` → `detect_round_numbers`)

Flags vendors where an suspicious proportion of payments are perfectly round
multiples of 500 or 1,000 BDT. Real business transactions rarely end in exact
round figures due to taxes, itemized billing, and fractional pricing. Uses
different rules for low vs high volume vendors:

- Low-volume (3 or fewer transactions): flagged if 2+ transactions are round
- High-volume (4+ transactions): flagged if 35%+ of transactions are round

Skips: transactions below 10,000 BDT, rows with no vendor name.

Key parameters: `MIN_ROUND_AMOUNT = 10000`, `LOW_VOLUME_THRESHOLD = 3`,
`LOW_VOLUME_MIN_ROUND = 2`, `MAX_ROUND_RATIO_THRESHOLD = 0.35`

### 6. Benford's Law (`benford_law.py` → `detect_benfords_law`)

Runs a per-vendor Chi-Square test against Benford's logarithmic leading-digit
distribution to detect fabricated amounts in low-volume vendors — those with
10 to 50 outgoing transactions. Vendors above this range are already covered
by the statistical engines (z-score, isolation forest). Vendors below it don't
have enough data for a meaningful Chi-Square.

For each qualifying vendor, the first digit of every transaction amount is
extracted and its observed frequency is compared against Benford's expected
distribution. If the Chi-Square stat exceeds 15.51 (95% confidence, 8 degrees
of freedom), that vendor is flagged and the two most over-represented digits
are reported.

Skips: vendors outside the 10–50 transaction window, rows with no vendor name.

Key parameters: `MIN_TRANSACTIONS = 10`, `MAX_TRANSACTIONS = 50`, `CRITICAL_CHI2 = 15.51`

---

## The Flags Table

All engines write to the same `flags` table in SQLite using the same six-field schema:

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Auto-incremented primary key |
| `txn_id` | TEXT | Transaction ID from the bank statement. Group-based engines (Benford, Round Numbers, Threshold Skirting) store comma-separated IDs here |
| `method` | TEXT | Which engine produced this flag: `zscore`, `isolation_forest`, `invoice_matching`, `threshold_skirting`, `round_numbers`, `benfords_law` |
| `vendor_name` | TEXT | |
| `date` | TEXT | Date of the flagged transaction in `YYYY-MM-DD` format |
| `amount` | REAL | Transaction amount |
| `reason` | TEXT | Structured explanation of why this was flagged (see below) |

### The `reason` field

Every engine formats its `reason` field using the same five structured tags so that
any LLM can parse and understand a flag without additional context:

```
[METRIC_TYPE]: What kind of analysis was used
[CALCULATED_VALUE]: The specific number or score that triggered the flag
[HISTORIC_BASELINE]: What the normal/expected value is
[VARIANCE_SPREAD]: The range or threshold parameters used
[CONTEXT]: Plain English explanation of why this is suspicious
```

This structure was intentionally designed so LLMs can read the flags directly and
reason about them — making it straightforward to have models debate, score, or
summarize flags without needing to know how each engine works internally.

---

## Checking What's in the Database

**Check flags:**

```python
from storage.db import get_connection

conn = get_connection()
rows = conn.execute("SELECT * FROM flags LIMIT 10").fetchall()
for row in rows:
    print(tuple(row))
print(conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0], "total flags")
```

**Check transactions:**

```python
from storage.db import get_connection

conn = get_connection()
count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
print(f"Total rows: {count}")
```

**Check ChromaDB (receipts + emails):**

```python
from storage.vector_store import list_documents

docs = list_documents()           # everything
# docs = list_documents("receipt")    # just receipts
# docs = list_documents("email")      # just emails

print(f"Total: {len(docs)}")
for doc in docs:
    print(doc["metadata"])
```

---

## Clearing Data

**Drop only the flags table (keeps transactions):**

```powershell
python -c "from storage.reset_db import drop_flags; drop_flags()"
python -m storage.db
```

**Drop only the transactions table (keeps flags):**

```powershell
python -c "from storage.reset_db import drop_transactions; drop_transactions()"
python -m storage.db
```

**Drop both tables:**

```powershell
python -c "from storage.reset_db import drop_all; drop_all()"
python -m storage.db
```

**Clear ChromaDB (receipts + emails):**

```powershell
python -c "from storage.vector_store import delete_all_documents; print(delete_all_documents(), 'docs deleted')"
```

Or full reset by deleting the folder:

```powershell
Remove-Item -Recurse -Force data\chroma
```

The collection is recreated automatically next time anything tries to use it.

**Delete one specific document from ChromaDB:**

```python
from storage.vector_store import delete_document
delete_document("receipt_abc123...")
```

---

## Quick Reference

| Task | Command |
|---|---|
| Initialize database | `python -m storage.db` |
| One receipt | `python -m ingestion.receipt <file>` |
| Folder of receipts | `python -m ingestion.batch_loader receipt <folder>` |
| One email | `python -m ingestion.email_ingest <file>` |
| Folder of emails | `python -m ingestion.batch_loader email <folder>` |
| One bank statement | `python -m ingestion.bank_statement <file.csv>` |
| Run all engines | `python -m detection.run_engines` |
| Run specific engines | `python -c "from detection.run_engines import run_engines; run_engines(['z_scoring', 'benford_law'])"` |
| Clear flags rows | `python -c "from storage.reset_db import clear_flags; clear_flags()"` |
| Clear transactions rows | `python -c "from storage.reset_db import clear_transactions; clear_transactions()"` |
| Drop all tables | `python -c "from storage.reset_db import drop_all; drop_all()"` |
| Clear ChromaDB | `python -c "from storage.vector_store import delete_all_documents; print(delete_all_documents(), 'docs deleted')"` |
