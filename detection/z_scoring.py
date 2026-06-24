from detection.loader import load_transactions


# atleast this many is needed
MIN_TRANSACTIONS_FOR_ZSCORE = 8

# z score of above 3 is flagged (generally used in stats)
ZSCORE_THRESHOLD = 3.0


def detect_zscore_outliers(df=None):
    
    if df is None:
        df = load_transactions()
    if df.empty:
        return []

    # only consider transactions that actually have a vendor 
    # salary, ATM withdrawals and etc have no vendor
    vendor_df = df[df["vendor_name"].str.strip() != ""].copy()

    flags = []

    for vendor, group in vendor_df.groupby("vendor_name"):
        if len(group) < MIN_TRANSACTIONS_FOR_ZSCORE:
            continue

        mean_amount = group["amount"].mean()
        std_amount = group["amount"].std()

        # if std = 0 we dont care
        # std = std happens if the transaction amount for that specific vector is always the same
        if std_amount == 0 or std_amount != std_amount: # NaN against NaN is not equal 
            continue

        for _, row in group.iterrows():
            z = (row["amount"] - mean_amount) / std_amount

            # these will be the common columns for all the engines
            if abs(z) >= ZSCORE_THRESHOLD:
                flags.append({
                    "txn_id": row["txn_id"],
                    "method": "zscore",
                    "vendor_name": vendor,
                    "date": row["date"].strftime("%Y-%m-%d"),
                    "amount": row["amount"],
                    "reason": (
                        f"[METRIC_TYPE]: Statistical Deviation (Z-Score)\n"
                        f"[CALCULATED_VALUE]: {abs(round(z, 2))} Standard Deviations\n"
                        f"[HISTORIC_BASELINE]: {round(mean_amount, 2)} BDT (Typical Vendor Average)\n"
                        f"[VARIANCE_SPREAD]: Standard Deviation is {round(std_amount, 2)}\n"
                        f"[CONTEXT]: Outgoing transaction of {row['amount']} BDT severely breaches standard operational boundaries calculated for {vendor}."
                    ),
                })

    return flags


if __name__ == "__main__":
    import json

    flags = detect_zscore_outliers()
    print(json.dumps(flags, indent=2, default=str))