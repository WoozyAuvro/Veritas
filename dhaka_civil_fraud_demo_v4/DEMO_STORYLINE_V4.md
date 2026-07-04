# Dhaka Civil Works Fraud Demo v4 Storyline

## Company

**Dhaka Civil Works Ltd.**  
A mid-size civil construction contractor. Domain: `@dhakacivil.com.bd`

## Main people

- **Rafiq Hossain** — Senior Procurement Officer
- **Tariq Molla** — Junior Accounts Officer
- **Salim Chowdhury** — Apex Buildmart contact
- **Noman Uddin** — Swift Cargo BD contact
- **Farhan Iqbal** — Strata Consult BD contact

## Story arc

### Months 1-2: Vendor setup and normal pattern building
Apex Buildmart is introduced through urgent procurement and starts with mostly ordinary supply invoices. Several additional routine Apex invoices are present in v4 so the vendor looks less mathematically artificial in early-stage statistical checks.

### Months 3-4: Controlled escalation
Apex begins receiving slightly larger payments, while Swift Cargo BD appears with tightly controlled route-movement amounts. Meghna Sand and Padma both create threshold-like patterns that are actually explainable by logistics and separate equipment packages.

### Months 5-6: Fraud network becomes visible
Apex enters the 80k-100k threshold window, then moves into large-value invoices. Swift and Strata keep repeating suspicious amount bands. False positives also begin to stack up: Northstar emergency survey, Bismillah bulk stock-up, Bengal Safety bundle pricing.

### Months 7-8: Closeout pressure
Apex invoice `APEX-26017` is cleared at a higher bank amount than the supporting receipt. Then `APEX-26018` is paid with no receipt present. The subtle fraud emails show pressure to keep things moving and to attach supporting notes later. The activity profile should look like someone trying to finish a vendor line before review.

## Retrieval design

This version is built for **flag-centered retrieval**:

- Vendor names appear clearly in subjects and bodies
- Key invoice numbers appear in emails and receipts
- Large or suspicious amounts are often written in the email body
- Duplicate-receipt false positives have direct explanation emails saying the packet was re-uploaded, stamped, or resent

## Intended investigation outcome

### Likely fraud after context review
- Apex Buildmart
- Swift Cargo BD
- Strata Consult BD

### Likely explainable alerts after context review
- Northstar Survey Services
- Meghna Sand & Aggregates
- OfficeLease Holdings
- Rahman Steel & Iron
- Padma Equipment Rental
- Bengal Safety Gear
- Bismillah Cement Co.

## Tuning note

v4 intentionally adds five routine Apex transactions with different leading digits so Benford attention stays focused on Swift and Strata instead of overwhelming Apex with every engine at once.
