from detection.loader import load_transactions
import pandas as pd
import numpy as np

# authority threshold
THRESHOLD = 80000 # should be around the 75th percentile of all the transactions apparently
# % below the threshold that will be considered skirting
SKIRTING_PERCENT = 0.20
ENOUGH_DAYS = 10


def detect_threshold_skirting(df=None):
    
    if df is None:
        df = load_transactions()
    if df.empty:
        return []

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    # filter for outgoing payments within the percent
    outgoing = df[df["amount"] < 0].copy()
    outgoing["amount_abs"] = outgoing["amount"].abs()
    # basically between this and the threshold
    lower_bound = THRESHOLD * (1 - SKIRTING_PERCENT)

    skirting_df = outgoing[
        (outgoing["amount_abs"] >= lower_bound) & 
        (outgoing["amount_abs"] < THRESHOLD)
    ].copy()

    # filtering out empty vendor names
    skirting_df = skirting_df[skirting_df["vendor_name"].str.strip() != ""]

    if skirting_df.empty:
        return []

    skirting_df = skirting_df.sort_values(["vendor_name", "date"])

    flags = []

    # 
    for vendor, group in skirting_df.groupby("vendor_name"):
        if len(group) < 3:
            continue  # at least 3 for it to be a cluster

        # calculate days between transactions
        days_since_last = group["date"].diff().dt.days

        # identifying where a NEW cluster starts
        # any transaction that happens more than enough days after the last one breaks the chain
        new_cluster_marker = (days_since_last > ENOUGH_DAYS) | days_since_last.isna()
        
        # create unique ids for each cluster block using cum
        group["cluster_id"] = new_cluster_marker.cumsum()

        # filtering out clusters that only have 1 transaction within that time window
        cluster_counts = group["cluster_id"].value_counts()
        valid_clusters = cluster_counts[cluster_counts >= 3].index # needs atleast 3 clusters   
        
        flagged_groups = group[group["cluster_id"].isin(valid_clusters)]

        # extracting and formatting the results
        for cid, cluster in flagged_groups.groupby("cluster_id"):
            total = cluster["amount_abs"].sum()
            dates = cluster["date"].dt.strftime("%Y-%m-%d").tolist()
            amounts = cluster["amount_abs"].tolist()
            # the txn ids for this case will be in a list
            flags.append({
                "txn_id": ", ".join(cluster["txn_id"].astype(str).tolist()),
                "method": "threshold_skirting",
                "vendor_name": vendor,
                "date": dates[0],
                "amount": round(total, 2),
                "reason": (
                    f"[METRIC_TYPE]: Policy Circumvention (Threshold Skirting)\n"
                    f"[CALCULATED_VALUE]: {len(cluster)} transactions split into small amounts\n"
                    f"[HISTORIC_BASELINE]: Static Approval Threshold is {THRESHOLD} BDT\n"
                    f"[VARIANCE_SPREAD]: Restricted Cluster Window of {ENOUGH_DAYS} Days\n"
                    f"[CONTEXT]: High Risk Smurfing. Multiple split payments were routed to {vendor} "
                    f"in close succession—each deliberately placed below the mandatory review limit. "
                    f"Individual amounts: {[round(a, 2) for a in amounts]} over dates: {dates}. "
                    f"Combined cluster exposure: {round(total, 2)} BDT."
                ),
            })

    return flags


if __name__ == "__main__":
    import json

    flags = detect_threshold_skirting()
    print(json.dumps(flags, indent=2, default=str))