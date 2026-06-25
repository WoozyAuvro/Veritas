from detection.loader import load_transactions
from sklearn.ensemble import IsolationForest
import pandas as pd

# need at least this many transactions for a vendor for Isolation Forest
MIN_TRANSACTIONS_FOR_ISOFOREST = 10

# this is basically the % of rows we expect to be anomalies
CONTAMINATION = 0.03


# these are what the model scores on
FEATURES = ["amount", "days_since_last_txn", "day_of_week"] 
# amount = payment size
# days_since_last is for timing gaps
# day_of_week is for day patterns


def detect_isolation_forest_outliers(df=None):
    
    if df is None:
        df = load_transactions()
    if df.empty:
        return []

    # same as z_scoring (vendors only, no salary/ATM rows)
    vendor_df = df[df["vendor_name"].str.strip() != ""].copy()

    # day_of_week: 0=monday to 6=sunday
    vendor_df["day_of_week"] = vendor_df["date"].dt.dayofweek

    flags = []

    for vendor, group in vendor_df.groupby("vendor_name"):
        if len(group) < MIN_TRANSACTIONS_FOR_ISOFOREST:
            continue

        group = group.sort_values("date").copy()

        # days since the previous transaction with this vendor
        group["days_since_last_txn"] = (
            group["date"].diff().dt.days.fillna(0) # gotta love diff
        )

        X = group[FEATURES].values

        model = IsolationForest(contamination=CONTAMINATION, random_state=42)
        group["iso_score"] = model.fit_predict(X)
        # -1 for anomalies and 1 for normal
        group["anomaly_score"] = model.score_samples(X)  # the lower the sussier

        flagged_rows = group[group["iso_score"] == -1] 

        for _, row in flagged_rows.iterrows():
            flags.append({
                "txn_id": row["txn_id"],
                "method": "isolation_forest",
                "vendor_name": vendor,
                "date": row["date"].strftime("%Y-%m-%d"),
                "amount": row["amount"],
                "reason": (
                    f"[METRIC_TYPE]: Multi-Variable Space Isolation\n"
                    f"[CALCULATED_VALUE]: Anomaly Score of {round(row['anomaly_score'], 4)}\n"
                    f"[HISTORIC_BASELINE]: Locally trained cluster model (Contamination limit: {int(CONTAMINATION * 100)}%)\n"
                    f"[VARIANCE_SPREAD]: Multi-Feature Space matrix mapping size, timing gaps, and day intervals\n"
                    f"[CONTEXT]: The Isolation Forest isolated this specific payment from the vendor's standard operational cluster. "
                    f"The machine learning model detected an irregular multi-variable intersection: "
                    f"Transaction Amount is {row['amount']} BDT, "
                    f"executed exactly {int(row['days_since_last_txn'])} days since the previous transaction with {vendor}, "
                    f"and processed on day index {int(row['day_of_week'])} of the week (where 0=Mon, 6=Sun)."
                    f"The lower the anomaly score, the more structurally distant this point is from the vendor's "
                    f"typical payment pattern. A score near 0 suggests borderline isolation; strongly negative scores "
                ),
            })

    return flags


if __name__ == "__main__":
    import json

    flags = detect_isolation_forest_outliers()
    print(json.dumps(flags, indent=2, default=str))

    