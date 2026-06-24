from detection import z_scoring, iso_forest, invoice_matching, threshold_skirting, round_numbers, benford_law
from storage.db import save_flags
from detection.loader import load_transactions
# all the engines currently will add more later
ENGINES = {
    "z_scoring":          z_scoring.detect_zscore_outliers,
    "iso_forest":         iso_forest.detect_isolation_forest_outliers,
    "invoice_matching":   invoice_matching.detect_invoice_matching,
    "threshold_skirting": threshold_skirting.detect_threshold_skirting,
    "round_numbers":      round_numbers.detect_round_numbers,
    "benford_law":        benford_law.detect_benfords_law,
}

def run_engines(engine_names=None):
    df = load_transactions()

    to_run = engine_names if engine_names else list(ENGINES.keys())

    all_flags = []
    for name in to_run:
        
        print(f"running {name}")
        flags = ENGINES[name](df=df)
        if flags:
            all_flags.extend(flags)
            print(f"  {len(flags)} flags found")
        else:
            print(f"  no flags")

    if all_flags:
        save_flags(all_flags)
        print(f"\ntotal flags saved: {len(all_flags)}")
    else:
        print("\nno flags to save.")

    return all_flags

if __name__ == "__main__":
    run_engines() # python -c "from detection.run_engines import run_engines; run_engines([engines to run])"