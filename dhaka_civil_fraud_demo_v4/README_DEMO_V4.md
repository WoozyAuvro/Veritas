# Dhaka Civil Works Fraud Demo v4

Synthetic dataset for the AI Fraud Detector demo.

## Files to ingest

- `bank_statement_dhaka_civil_demo_v4.csv`
- all files inside `receipts_folder/`
- all files inside `email_folder/`

## Files **not** to ingest

- `ground_truth_anomalies_v4.csv`
- `DEMO_STORYLINE_V4.md`
- `README_DEMO_V4.md`
- `manifest.json`

## Dataset summary

- Bank transactions: 359
- Receipt TXT files: 276
- Email EML files: 269
- Timeline: 2026-01-03 to 2026-08-30
- Threshold skirting tuned for: 100,000 BDT

## Intended behavior

This version is tuned to produce a mix of:

- true fraud around Apex Buildmart, Swift Cargo BD, and Strata Consult BD
- explainable false positives around Northstar Survey Services, Meghna Sand & Aggregates, OfficeLease Holdings, Rahman Steel & Iron, Padma Equipment Rental, Bengal Safety Gear, and Bismillah Cement Co.
- a few noisy statistical flags so the LLM reasoning layer has real work to do

### Core fraud narrative

- **Apex Buildmart** is the main operational fraud vendor
  - staged payments under the threshold
  - revised bank amount not matching receipt
  - final large payment with no receipt
- **Swift Cargo BD** and **Strata Consult BD** are shell vendors with manually chosen amount patterns that should violate Benford's Law
- Fraud emails are subtle, not explicit. They use payment pressure, routing language, and document timing rather than cartoonish admissions.

### Core false-positive narrative

- **Northstar Survey Services** has a legitimate emergency drone-survey outlier and an intentional duplicate rescan
- **Meghna Sand & Aggregates** looks like threshold skirting but is explained by separate truckloads
- **OfficeLease Holdings** is fixed monthly rent and includes one duplicate lease packet
- **Rahman Steel & Iron** includes one legitimate 500 BDT adjustment and one large but explainable imported beam purchase
- **Padma Equipment Rental** has three near-threshold payments that are actually separate excavator/operator packages, plus one signed duplicate receipt
- **Bismillah Cement Co.** includes a seasonal bulk stock-up and a duplicate stamped copy
- **Bengal Safety Gear** remains a borderline statistical false positive

## Suggested run steps

```powershell
python -m storage.db
python -m ingestion.bank_statement bank_statement_dhaka_civil_demo_v4.csv
python -m ingestion.batch_loader receipt receipts_folder
python -m ingestion.batch_loader email email_folder
python -m detection.run_engines
```

## Notes

The project stores bank statements in SQLite and receipts/emails in ChromaDB, with the six engines running over the transactions and writing to the flags table. This package keeps to that structure. 
