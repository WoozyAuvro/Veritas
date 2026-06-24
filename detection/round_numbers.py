from detection.loader import load_transactions
import pandas as pd

# amounts below this are ignored
MIN_ROUND_AMOUNT = 5000

# what % of them being perfectly round triggers an alert for high-volume vendors
MAX_ROUND_RATIO_THRESHOLD = 0.35

# a vendor is considered low-volume if it has fewer than this many transactions
LOW_VOLUME_THRESHOLD = 8

# low-volume vendors need at least this many round transactions to be flagged
LOW_VOLUME_MIN_ROUND = 2

def detect_round_numbers(df=None):

    if df is None:
        df = load_transactions()
    if df.empty:
        return []

    # filtering outgoing payments above the minimum
    df = df.copy()
    outgoing = df[df["amount"] < 0].copy()
    outgoing["amount_abs"] = outgoing["amount"].abs()

    large_df = outgoing[outgoing["amount_abs"] >= MIN_ROUND_AMOUNT].copy()
    large_df = large_df[large_df["vendor_name"].str.strip() != ""]

    if large_df.empty:
        return []

    # check if round catches both multiples of 1000 and 500
    large_df["is_round"] = (large_df["amount_abs"] % 1000 == 0) | (large_df["amount_abs"] % 500 == 0)

    flags = []

    # the logic basically
    for vendor, group in large_df.groupby("vendor_name"):
        total_txs = len(group)
        round_txs = group[group["is_round"]]
        num_round = len(round_txs)

        # we dont care if theres no round numbers
        if num_round == 0:
            continue

        round_ratio = num_round / total_txs

        # vendors with low number of txns are flagged if enough of them are round
        is_suspicious_low_vol = (total_txs <= LOW_VOLUME_THRESHOLD and num_round >= LOW_VOLUME_MIN_ROUND)
        # vendors with high volume get flagged depending on their ratio
        is_suspicious_high_vol = (total_txs > LOW_VOLUME_THRESHOLD and round_ratio >= MAX_ROUND_RATIO_THRESHOLD)

        if is_suspicious_low_vol or is_suspicious_high_vol:
            total_flagged_amt = round_txs["amount_abs"].sum()
            dates = pd.to_datetime(round_txs["date"]).dt.strftime("%Y-%m-%d").tolist()
            amounts = round_txs["amount_abs"].tolist()

            # determining the volume type for the flags
            vol_type = "low-volume" if total_txs <= LOW_VOLUME_THRESHOLD else "high-volume"

            flags.append({
                "txn_id": ", ".join(round_txs["txn_id"].astype(str).tolist()),
                "method": "round_numbers",
                "vendor_name": vendor,
                "date": dates[0],
                "amount": round(total_flagged_amt, 2),
                "reason": (
                    f"[METRIC_TYPE]: Currency Formatting Structure (Round Numbers)\n"
                    f"[CALCULATED_VALUE]: {num_round} round payments out of {total_txs} total ledger entries ({round(round_ratio * 100, 1)}% round ratio)\n"
                    f"[HISTORIC_BASELINE]: Anomaly threshold is {int(MAX_ROUND_RATIO_THRESHOLD * 100)}% round ratio for high-volume vendors, "
                    f"or {LOW_VOLUME_MIN_ROUND}+ round transactions for low-volume vendors (≤{LOW_VOLUME_THRESHOLD} transactions)\n"
                    f"[VARIANCE_SPREAD]: Filtered for values >= {MIN_ROUND_AMOUNT} BDT using 500 and 1000 BDT modulus checks\n"
                    f"[CONTEXT]: High risk operational profile ({vol_type}). A significant portion of outbound funds directed to "
                    f"{vendor} match exact integers with trailing zeroes. This lack of fractional distribution or standard "
                    f"itemized billing units implies arbitrary manual overrides or fabricated payment entries. "
                    f"Verify whether this is a legitimate recurring fixed payment (e.g. rent, salary) before escalating. "
                    f"Flagged round amounts: {[round(a, 2) for a in amounts]} over dates: {dates}."
                ),
            })

    return flags


if __name__ == "__main__":
    import json

    flags = detect_round_numbers()
    print(json.dumps(flags, indent=2, default=str))