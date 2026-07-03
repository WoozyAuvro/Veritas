
import hashlib
import json
import uuid # for the uploaded batch's id
from pathlib import Path

import pandas as pd

from storage.db import get_connection, init_db
from storage.vector_store import ask_llm_for_json


def ingest_bank_statement(csv_path, upload_batch_id=None):
    csv_path = Path(csv_path)
    upload_batch_id = upload_batch_id or str(uuid.uuid4())

    init_db()
    raw_df = pd.read_csv(csv_path, dtype=str)

    headers = list(raw_df.columns)
    sample_rows = raw_df.head(5).fillna("").to_dict(orient="records")
    mapping = get_column_mapping(headers, sample_rows)

    normalized_df = apply_mapping(raw_df, mapping)
    rows_loaded = save_transactions(normalized_df, csv_path.name, upload_batch_id)

    return {
        "upload_batch_id": upload_batch_id,
        "rows_loaded": rows_loaded,
        "column_mapping_used": mapping,
    }

# sends the first 5 rows to llm and maps the columns
def get_column_mapping(headers, sample_rows):
    
    system_prompt = (
        "You map unpredictable bank statement CSV columns to a fixed target schema. "
        "Return only raw JSON, no markdown or commentary. Use null when no source column exists."
    )
    user_prompt = (
        "Map these CSV headers to this exact JSON shape:\n"
        "{\n"
        '  "date_column": "<header name>",\n'
        '  "description_column": "<header name or null>",\n'
        '  "amount_column": "<header name>",\n'
        '  "balance_column": "<header name or null>",\n'
        '  "reference_column": "<header name or null>",\n'
        '  "vendor_column": "<header name or null>",\n'
        '  "invoice_column": "<header name or null>"\n'
        "}\n\n"
        "amount_column is a single signed amount column (positive = money in, "
        "negative = money out). This CSV always has exactly one amount column, "
        "never separate debit/credit columns.\n"
        "reference_column is any cheque number, transaction reference, or UTR/UTR-like "
        "identifier column, if present — null is a normal answer.\n"
        "vendor_column and invoice_column are only present if the CSV explicitly "
        "has columns for them — null is a normal answer for either.\n\n"
        f"Headers: {headers}\n\nSample rows:\n{json.dumps(sample_rows, indent=2)}"
    )

    mapping = ask_llm_for_json(system_prompt, user_prompt)
    check_mapping_is_valid(mapping, headers)
    return mapping

# checks if the mapping was done properly. lets hope we never have to use it lol
def check_mapping_is_valid(mapping, headers):
    required_keys = {"date_column", "description_column", "amount_column", "balance_column",
                      "reference_column", "vendor_column", "invoice_column"}
    missing = required_keys - set(mapping)
    if missing:
        raise ValueError(f"LLM mapping is missing keys: {missing}")

    if not mapping.get("amount_column"):
        raise ValueError("mapping must include amount_column")

    for key, value in mapping.items():
        if value is not None and value not in headers:
            raise ValueError(f"LLM mapped {key} to a column that doesn't exist: {value}")

# turns it into a proper dataframe
def apply_mapping(df, mapping):
    
    out = pd.DataFrame() # day_first is False
    out["date"] = pd.to_datetime(df[mapping["date_column"]], errors="coerce", dayfirst=False).dt.strftime("%Y-%m-%d")
    out["description"] = df[mapping["description_column"]].fillna("") if mapping.get("description_column") else ""
    out["amount"] = parse_money(df[mapping["amount_column"]])
    out["balance"] = parse_money(df[mapping["balance_column"]]) if mapping.get("balance_column") else None
    out["reference_number"] = text_column(df, mapping.get("reference_column"))
    out["vendor_name"] = text_column(df, mapping.get("vendor_column"))
    out["invoice_number"] = text_column(df, mapping.get("invoice_column"))
    return out

# fills missing values
def text_column(df, column_name):
    if column_name:
        return df[column_name].fillna("")
    return ""

# strips comma, currency symbol and handles negative
def parse_money(series):
    text = series.fillna("").astype(str).str.strip()
    is_negative = text.str.startswith("(") & text.str.endswith(")")
    cleaned = text.str.replace(r"[^\d.\-]", "", regex=True)
    numbers = pd.to_numeric(cleaned, errors="coerce")
    return numbers.mask(is_negative, -numbers.abs())

# creates the table for all the transactions in the bank statement
def save_transactions(df, source_filename, upload_batch_id):
    conn = get_connection()
    rows_loaded = 0

    for row in df.to_dict(orient="records"):
        row_hash = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
        cursor = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (txn_id, date, description, amount, balance, reference_number,
                vendor_name, invoice_number, source_filename, row_hash, upload_batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), row["date"], row["description"], row["amount"], row["balance"],
             row["reference_number"], row["vendor_name"], row["invoice_number"],
             source_filename, row_hash, upload_batch_id),
        )
        rows_loaded += cursor.rowcount

    conn.commit()
    conn.close()
    return rows_loaded


if __name__ == "__main__":
    import sys

    result = ingest_bank_statement(sys.argv[1])
    print(json.dumps(result, indent=2))