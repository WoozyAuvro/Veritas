# had to create a new metadata date_unix for this

from detection.loader import load_transactions
from storage.vector_store import get_collection
import pandas as pd
import re

# days before and after every bank statement this engine will search the receipt for
DATE_WINDOW_DAYS = 7

# maximum allowed difference between bank transaction amount and receipt amount 
AMOUNT_TOLERANCE = 100

# strips 'INV', punctuation, spaces and stuff so both matches invoice formats
def match_invoice_formats(inv_str):
    
    if not inv_str or pd.isna(inv_str):
        return ""
    inv_clean = str(inv_str).strip().lower()
    inv_clean = re.sub(r'^inv[-_]?', '', inv_clean)
    return re.sub(r'[^a-z0-9]', '', inv_clean)

# calls receipts only within the specified date window
def query_receipts_in_window(start_date_str, end_date_str):
    from dateutil import parser as dateparser
    
    collection = get_collection()
    # chromadb needs ts to work 
    start_ts = int(dateparser.parse(start_date_str).timestamp())
    end_ts = int(dateparser.parse(end_date_str).timestamp())
    # chromadb query
    where_filter = {
        "$and": [
            {"doc_type": {"$eq": "receipt"}},
            {"date_unix": {"$gte": start_ts}},
            {"date_unix": {"$lte": end_ts}}
        ]
    }
    
    result = collection.get(where=where_filter)
    
    receipt_records = []
    for i in range(len(result["ids"])):
        meta = result["metadatas"][i]
        formatted_invoice = match_invoice_formats(meta.get("invoice_number", ""))
        
        if formatted_invoice and formatted_invoice not in ["none", "nan"]:
            receipt_records.append({
                "receipt_id": result["ids"][i],
                "invoice_norm": formatted_invoice,
                "receipt_date": pd.to_datetime(meta.get("date")),
                "receipt_amount": meta.get("amount_bdt")
            })
            
    return pd.DataFrame(receipt_records)

# main thing
def detect_invoice_matching(df=None):
    
    if df is None:
        df = load_transactions()
    if df.empty:
        return []

    # only keeps statements with valid invoices
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    outgoing = df[df["amount"] < 0].copy()
    outgoing["amount_abs"] = outgoing["amount"].abs()
    
    tx_df = outgoing[outgoing["invoice_number"].notna()].copy()
    tx_df["invoice_norm"] = tx_df["invoice_number"].apply(match_invoice_formats)
    tx_df = tx_df[tx_df["invoice_norm"] != ""]

    flags = []

    # iterate through bank transactions and targeting queries in that specific date window
    for _, tx in tx_df.iterrows():
        tx_inv = tx["invoice_norm"]
        tx_date = tx["date"]
        tx_raw_inv = tx["invoice_number"]
        tx_amt = round(tx["amount_abs"], 2)

        # calculating the date boundaries to input in the where search of chromadb
        # +days and -days so basically twice the amount of days
        start_window = (tx_date - pd.Timedelta(days=DATE_WINDOW_DAYS)).strftime("%Y-%m-%d")
        end_window = (tx_date + pd.Timedelta(days=DATE_WINDOW_DAYS)).strftime("%Y-%m-%d")

        # then matching them
        window_receipts_df = query_receipts_in_window(start_window, end_window)
        exact_matches = window_receipts_df[window_receipts_df["invoice_norm"] == tx_inv] if not window_receipts_df.empty else pd.DataFrame()
        num_matches = len(exact_matches)

        # no matches
        if num_matches == 0:
            flags.append({
                "txn_id": str(tx["txn_id"]),
                "method": "invoice_matching",
                "vendor_name": tx["vendor_name"],
                "date": tx_date.strftime("%Y-%m-%d"),
                "amount": tx_amt,
                "reason": (
                    f"[METRIC_TYPE]: Source Document Verification Failure\n"
                    f"[CALCULATED_VALUE]: 0 string matches found inside validation window\n"
                    f"[HISTORIC_BASELINE]: Requires exactly 1 valid receipt file record\n"
                    f"[VARIANCE_SPREAD]: Target window of +/- {DATE_WINDOW_DAYS} days ({start_window} to {end_window})\n"
                    f"[CONTEXT]: Phantom Payment Risk. Funds cleared the corporate bank account referencing invoice '{tx_raw_inv}', "
                    f"but no corresponding source record matches this identifier within the allowable date window parameters."
                )
            })

        # duplicate matches
        elif num_matches > 1:
            flags.append({
                "txn_id": str(tx["txn_id"]),
                "method": "invoice_matching",
                "vendor_name": tx["vendor_name"],
                "date": tx_date.strftime("%Y-%m-%d"),
                "amount": tx_amt,
                "reason": (
                    f"[METRIC_TYPE]: Document Duplication Failure\n"
                    f"[CALCULATED_VALUE]: {num_matches} duplicate receipts found\n"
                    f"[HISTORIC_BASELINE]: Requires exactly 1 unique receipt reference mapping\n"
                    f"[VARIANCE_SPREAD]: Target window of +/- {DATE_WINDOW_DAYS} days ({start_window} to {end_window})\n"
                    f"[CONTEXT]: Double-Billing / Invoice Recycling Risk. The system uncovered multiple independent receipt records "
                    f"inside ChromaDB claiming the identical invoice field '{tx_raw_inv}' inside the active window, "
                    f"pointing to duplicate ledger entries or recycled vendor payouts."
                )
            })

        # one match but different amounts
        else:
            matched = exact_matches.iloc[0]
            receipt_amt = round(float(matched["receipt_amount"]), 2)
            amount_diff = round(abs(tx_amt - receipt_amt), 2)

            if amount_diff > AMOUNT_TOLERANCE:
                flags.append({
                    "txn_id": str(tx["txn_id"]),
                    "method": "invoice_matching",
                    "vendor_name": tx["vendor_name"],
                    "date": tx_date.strftime("%Y-%m-%d"),
                    "amount": tx_amt,
                    "reason": (
                        f"[METRIC_TYPE]: Invoice Amount Mismatch\n"
                        f"[CALCULATED_VALUE]: Bank transaction amount of {tx_amt} BDT vs receipt amount of {receipt_amt} BDT "
                        f"(discrepancy of {amount_diff} BDT)\n"
                        f"[HISTORIC_BASELINE]: Tolerance threshold of {AMOUNT_TOLERANCE} BDT\n"
                        f"[VARIANCE_SPREAD]: Target window of +/- {DATE_WINDOW_DAYS} days ({start_window} to {end_window})\n"
                        f"[CONTEXT]: Amount Inflation Risk. Invoice '{tx_raw_inv}' was matched to a valid receipt record, "
                        f"but the cleared bank amount does not agree with the source document. "
                        f"This discrepancy of {amount_diff} BDT may indicate invoice tampering, "
                        f"overbilling, or manual override of the payment amount after approval."
                    )
                })

    return flags


if __name__ == "__main__":
    import json
    print(json.dumps(detect_invoice_matching(), indent=2, default=str))