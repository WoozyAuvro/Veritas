from detection.loader import load_transactions
import pandas as pd
import numpy as np

# only test vendors with transactions in this range
MIN_TRANSACTIONS = 10       # below this chi-square is statistically meaningless
MAX_TRANSACTIONS = 50       # above this other engines (z-score, isolation forest) handle it

NUMBER_OF_LARGEST_DIGITS = 2
CRITICAL_CHI2 = 15.51       # 8 degrees of freedom (digits 1-9)

def detect_benfords_law(df=None):

    if df is None:
        df = load_transactions()
    if df.empty:
        return []

    df = df.copy()
    outgoing = df[df["amount"] < 0].copy()
    outgoing["amount_abs"] = outgoing["amount"].abs()
    outgoing = outgoing[outgoing["vendor_name"].str.strip() != ""]
                            # formula to find the first digit of ANY number ever
    outgoing["first_digit"] = outgoing["amount_abs"] // (10 ** np.floor(np.log10(outgoing["amount_abs"])))
    outgoing["first_digit"] = outgoing["first_digit"].astype(int)
            # formula to find the probabilities for each digit in dictionary
    benford_probs = {d: np.log10(1 + 1/d) for d in range(1, 10)}

    flags = []
    # checking for each vendor individually
    for vendor, group in outgoing.groupby("vendor_name"):
        # skip vendors outside the volume window
        if len(group) < MIN_TRANSACTIONS or len(group) > MAX_TRANSACTIONS:
            continue

        observed_counts = group["first_digit"].value_counts().reindex(range(1, 10), fill_value=0)
        expected_counts = pd.Series({d: benford_probs[d] * len(group) for d in range(1, 10)})
                            # chi square test formula
        chi_square_stat = ((observed_counts - expected_counts) ** 2 / expected_counts).sum()
        #skips if it isnt too much of a deviation
        if chi_square_stat <= CRITICAL_CHI2:
            continue

        deviation = observed_counts - expected_counts
        # only the top numbers that deviated the most, could be changed easily
        suspicious_digits = deviation.nlargest(NUMBER_OF_LARGEST_DIGITS).index.tolist()

        flagged_txns = group[group["first_digit"].isin(suspicious_digits)]

        amounts = flagged_txns["amount_abs"].tolist()
        dates = pd.to_datetime(flagged_txns["date"]).dt.strftime("%Y-%m-%d").tolist()
        total = flagged_txns["amount_abs"].sum()

        flags.append({
            "txn_id": ", ".join(flagged_txns["txn_id"].astype(str).tolist()),
            "method": "benfords_law",
            "vendor_name": vendor,
            "date": dates[0],
            "amount": round(total, 2),
            "reason": (
                f"[METRIC_TYPE]: Logarithmic Leading-Digit Frequency Deviation\n"
                f"[CALCULATED_VALUE]: Vendor-Level Chi-Square Stat of {round(chi_square_stat, 2)}\n"
                f"[HISTORIC_BASELINE]: Critical Chi-Square Threshold is {CRITICAL_CHI2} (95% Confidence)\n"
                f"[VARIANCE_SPREAD]: Highest positive deviations on digits {suspicious_digits}\n"
                f"[CONTEXT]: Low-volume vendor ({len(group)} transactions) whose outbound payment "
                f"leading-digit distribution significantly breaches Benford's Law. Vendor {vendor} "
                f"over-uses digits {suspicious_digits}, suggesting human-invented or structured amounts. "
                f"Flagged amounts: {[round(a, 2) for a in amounts]} on dates: {dates}."
            ),
        })

    return flags


if __name__ == "__main__":
    import json
    flags = detect_benfords_law()
    print(json.dumps(flags, indent=2, default=str))