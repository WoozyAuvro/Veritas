# Forensic Reasoning Layer: `tools.py`, `agent_execution.py`, and `forensic_followup.py`

This document explains the final AI reasoning layer used after the statistical fraud engines have already created flags. The goal of this layer is not to re-run the detection engines. Its job is to read the existing flags, pull the relevant emails/receipts, produce vendor-level forensic summaries, connect related fraud cases, and answer follow-up questions from the user.

The three files work together like this:

```text
Detection engines
    ↓
SQLite flags table + transactions table
    ↓
tools.py
    ↓
agent_execution.py
    ↓
data/forensic_case_results.json
    ↓
forensic_followup.py
```

---

## 1. Where this layer fits in the project

The core fraud system already has three important data sources:

1. **SQLite**
   - Stores bank transactions.
   - Stores fraud flags created by the six detection engines.

2. **ChromaDB**
   - Stores receipts.
   - Stores emails.
   - Email metadata includes fields like `from`, `to`, `subject`, `parties`, and `body`.

3. **Detection flags**
   - Each flag has a vendor, method, date, amount, and structured `reason`.
   - The `reason` field already explains why the engine flagged the transaction.

The forensic layer starts after `python -m detection.run_engines` has already populated the `flags` table.

---

## 2. `tools.py`

### Purpose

`tools.py` is the shared utility layer. It does not decide whether something is fraud. It only provides safe ways for the reasoning code to retrieve data from the ledger, ChromaDB, and optional registry sources.

Think of it as the evidence-fetching layer. Boring, necessary, and thankfully not pretending to be Sherlock Holmes.

---

### Main responsibilities

### A. Query the SQLite ledger safely

The file exposes SQL helpers such as:

```python
query_sql_ledger(query: str) -> str
query_sql_dataframe(query: str) -> pd.DataFrame
```

These functions:

- connect to `data/fraud.sqlite3`
- allow only `SELECT` or `WITH` queries
- block write operations like `DELETE`, `UPDATE`, or `DROP`
- normalize friendly field names like `vendor`, `invoice`, and `reference`
- return either a readable text table or a pandas DataFrame

This is used by `agent_execution.py` to pull:

- flags grouped by vendor
- transactions for a vendor
- invoice numbers
- dates and methods from the flags table

---

### B. Search documents through ChromaDB

The file exposes:

```python
search_document_vectors(query_text: str, n_results: int = 5) -> str
search_documents(query_text: str, n_results: int = 5) -> str
```

These wrap the project’s vector search function from `storage.vector_store`.

This is useful when the agent needs semantic search, such as:

```text
Apex Buildmart APEX-26018 supporting note payment clearance
```

Vector search is useful, but it can be noisy. So the final system does **not** trust vector search blindly. It uses it as a fallback or secondary evidence source.

---

### C. List and format all ChromaDB documents

The final version also uses helpers like:

```python
list_all_formatted_documents()
```

This reads the stored ChromaDB documents directly, formats the metadata, and exposes the useful parts clearly:

```text
DOC_TYPE: email
DATE: ...
FROM: ...
TO: ...
SUBJECT: ...
PARTIES: ...
BODY: ...
```

This matters because follow-up questions need to answer things like:

```text
Who asked Tariq to clear the payment?
```

That requires direct access to email headers and body text, not just vague vector snippets.

---

### D. Build dynamic vendor matching terms

`tools.py` has vendor helper logic such as:

```python
vendor_terms(vendor_name: str, invoice_numbers: Optional[Sequence[str]] = None)
```

It dynamically creates search anchors from:

- the vendor name
- important vendor tokens
- invoice numbers
- invoice prefixes

For example, if the vendor is:

```text
Meghna Sand & Aggregates
```

it can generate useful matching terms from:

```text
meghna
meghna sand
meghna sand aggregates
MSA-26003
MSA
```

This avoids hardcoding vendor names. If a new dataset has totally different vendors, the system can still build search terms from the actual data.

---

### E. Retrieve vendor-specific documents

A key helper is:

```python
get_vendor_documents(...)
```

This pulls documents that match the vendor or its invoices, then filters and ranks them.

It tries to avoid evidence contamination. For example:

- Apex should not get OfficeLease rent explanations.
- Meghna should not get Apex payment-clearance emails.
- Swift should not get random clean-vendor explanations.

That post-filtering is important because vector search can return semantically similar but vendor-wrong results. Because naturally, software sees the word “payment” and thinks everything is everyone’s problem.

---

### F. Optional corporate registry check

`tools.py` includes:

```python
verify_corporate_registry(vendor_name: str) -> str
```

In the final general version, this should not use a fake hardcoded registry. It checks for an actual registry file if configured, such as:

```text
VENDOR_REGISTRY_CSV
```

or a local CSV like:

```text
data/vendor_registry.csv
vendor_registry.csv
```

If no registry exists, it returns an unknown status instead of pretending the vendor is unregistered.

---

## 3. `agent_execution.py`

### Purpose

`agent_execution.py` is the main forensic report generator.

It reads the detection flags, groups them by vendor, retrieves vendor-specific documents, scores the case, produces readable JSON reports, builds the final connected fraud summary, and optionally starts the follow-up Q&A loop.

This is the main file you run:

```powershell
python -m agent_execution
```

---

### High-level flow

```text
1. Read vendors from the flags table.
2. For each flagged vendor:
   - Load that vendor's flags.
   - Load nearby/vendor-related transactions.
   - Extract invoice numbers and dates.
   - Retrieve matching emails and receipts.
   - Classify evidence as suspicious or explanatory.
   - Score the vendor case.
   - Produce a vendor forensic report.
3. Build one final connected fraud summary across high-risk vendors.
4. Save all reports to data/forensic_case_results.json.
5. Start follow-up Q&A if enabled.
```

---

### A. Load vendors from the flags table

The agent first asks SQLite which vendors have flags.

It does not investigate every vendor in the bank statement. It investigates vendors that the detection engines already flagged.

This keeps the reasoning layer focused and cheaper.

---

### B. Build a vendor case packet

For each vendor, `agent_execution.py` builds a compact evidence packet.

The packet contains:

- vendor name
- all flags for that vendor
- methods that flagged it
- relevant invoice numbers
- relevant dates
- vendor-specific email documents
- vendor-specific receipt documents
- optional registry result

This packet is the raw material used to score the case.

---

### C. Classify evidence into fraud indicators and explanations

The agent uses general audit-pattern groups, not hardcoded demo answers.

Suspicious indicators include concepts like:

- cleared before support was complete
- possible manual amount override
- possible threshold avoidance
- possible concealment or unusual routing

Exculpatory indicators include concepts like:

- clerical duplicate document
- contractual or recurring charge
- operational split or separate scope
- legitimate adjustment or fee
- approved exception or one-off need

Important: these are general evidence categories, not fixed names or fixed vendors. The system should not contain an answer key like “Rafiq = fraud person” or “Apex = bad vendor.” It must infer from the flags and matched documents.

---

### D. Score each vendor case

The agent assigns a risk score and confidence score.

Risk is increased by strong fraud signals such as:

- missing receipt/source document
- invoice amount mismatch
- threshold skirting
- very high Benford deviation
- suspicious email wording
- unexplained unusual transaction pattern

Risk is reduced by vendor-matched explanations such as:

- duplicate scan, not duplicate payment
- emergency or one-off scope
- split delivery due to operational constraints
- rent or recurring contractual payment
- small adjustment with supporting explanation

The final report classifies the vendor as one of:

```text
likely_fraud
likely_false_positive
inconclusive
```

---

### E. Generate the vendor forensic report

Each vendor report is printed as JSON.

Final report fields:

```json
{
  "vendor_name": "...",
  "risk_score": 0,
  "confidence": 0,
  "verdict": "likely_fraud | likely_false_positive | inconclusive",
  "case_interpretation": "...",
  "evidence_summary": "...",
  "people_involved": ["..."],
  "next_steps": "..."
}
```

#### `case_interpretation`

Explains what the case means in plain English.

For fraud vendors, it explains how the fraud may have worked.

For false positives, it explains why the flags looked suspicious but were likely explainable.

#### `evidence_summary`

Explains the strongest evidence and whether matched documents strengthened or weakened the case.

#### `people_involved`

Only includes names. No role scores, no evidence fields, no noisy metadata.

Example:

```json
"people_involved": [
  "Rafiq Hossain",
  "Tariq Molla"
]
```

This is intentionally simple because the user only needs to see the relevant parties, not an autopsy of every email header.

#### `next_steps`

Gives practical confirmation steps, such as:

- compare approved amount vs receipt amount vs bank amount
- review approval trail
- check vendor onboarding records
- compare email timestamps with payment dates

---

### F. Build the final connected fraud summary

After all vendor reports are created, `agent_execution.py` builds one final summary.

It looks at all vendors marked:

```text
likely_fraud
```

Then it checks whether the same people appear across multiple high-risk vendors.

The final summary contains:

```json
{
  "high_risk_vendors": [],
  "vendor_risk_snapshot": [],
  "shared_parties_across_high_risk_vendors": [],
  "summary": "...",
  "relationship_theory": "...",
  "recommended_confirmation_steps": []
}
```

This is where the system connects cases like:

```text
Apex Buildmart + Strata Consult BD + Swift Cargo BD
```

If the same people appear across those high-risk vendors, the summary explains that the fraud may not be isolated by vendor. It may be moving through a shared procurement, approval, or payment-processing path.

---

### G. Save the report results

The agent saves the output to:

```text
data/forensic_case_results.json
```

This file is important because `forensic_followup.py` uses it later to answer user questions.

The saved file includes:

- all vendor reports
- final connected fraud summary
- enough context for follow-up Q&A

---

### H. Start interactive follow-up mode

At the end, if interactive mode is enabled, `agent_execution.py` imports and starts:

```python
from forensic_followup import interactive_followup_loop
```

The user can then ask direct questions after the report is generated.

Example:

```text
Follow-up question> how is Tariq related?
```

To exit the loop, press Enter on an empty line.

To disable follow-up mode:

```powershell
$env:FORENSIC_INTERACTIVE_QA="0"
python -m agent_execution
```

---

## 4. `forensic_followup.py`

### Purpose

`forensic_followup.py` is the interactive Q&A layer.

It answers direct user questions after the main forensic reports are generated.

It does **not** replace `agent_execution.py`. It depends on the saved reports from `agent_execution.py`.

---

### High-level flow

```text
1. Load data/forensic_case_results.json.
2. Read the user's question.
3. Resolve the question to a known vendor, person, invoice, or case topic.
4. Search saved reports and email evidence.
5. Return a focused answer.
6. If the question is too vague or unsupported, say so and stop.
```

---

### A. Load saved case reports

The follow-up layer starts by loading:

```text
data/forensic_case_results.json
```

That gives it access to:

- vendor names
- verdicts
- risk scores
- case interpretations
- evidence summaries
- people involved
- final connected fraud summary

So when the user asks:

```text
tell me more about Meghna
```

it can resolve that to the saved report for:

```text
Meghna Sand & Aggregates
```

---

### B. Resolve user questions

The follow-up layer tries to map the question to one or more of these:

1. **Vendor**
   - exact vendor name
   - partial vendor name
   - useful vendor tokens

2. **Person**
   - names from `people_involved`
   - names found in email metadata/body

3. **Invoice or transaction clue**
   - invoice-like strings such as `APEX-26018`
   - vendor setup / vendor master terms

4. **Broad fraud-network question**
   - questions like “how are the fraud vendors connected?”

This lets it answer specific questions without always falling back to Apex/Strata/Swift evidence.

---

### C. Search relevant email evidence

When the question resolves to a vendor or person, it searches formatted ChromaDB email documents.

For each matching email, it can return:

```json
{
  "date": "...",
  "from_to": "sender -> recipient",
  "subject": "...",
  "parties": "...",
  "relevant_excerpt": "..."
}
```

This allows specific answers like:

```text
Rafiq emailed Tariq asking him to process APEX-26018 before the supporting note was finalized.
```

This is the whole point of the follow-up layer. Not “AI says connected,” but “here is the email that shows the connection.” Revolutionary, apparently.

---

### D. Strict no-hallucination behavior

The final follow-up version has guardrails for vague or made-up questions.

If the user asks something like:

```text
who is Afnan Maheem?
```

and that name does not exist in the reports or email evidence, it should answer:

```text
I could not find Afnan Maheem in the saved case reports, people lists, or matched email evidence.
```

It should not return random Apex emails.

If the user enters a random vague word like:

```text
clear
```

it should say the prompt is too vague and avoid returning unrelated evidence.

This prevents the system from hallucinating connections just because one word appears in suspicious emails.

---

### E. Examples of good follow-up questions

```text
How is Tariq related to the fraud?
Tell me more about Meghna.
Why is Apex marked likely fraud?
Was Northstar actually suspicious?
Who requested vendor setup changes?
How are Apex, Swift, and Strata connected?
What happened with APEX-26018?
```

---

### F. Example follow-up response structure

```json
{
  "question": "how is Tariq related?",
  "direct_answer": "Tariq Molla appears in multiple high-risk vendor cases and is repeatedly shown as a recipient or processor around payment-related emails.",
  "related_vendors": ["Apex Buildmart", "Swift Cargo BD", "Strata Consult BD"],
  "related_people": ["Tariq Molla"],
  "email_evidence": [
    {
      "date": "...",
      "from_to": "rafiq.hossain@... -> tariq.molla@...",
      "subject": "...",
      "parties": "...",
      "relevant_excerpt": "..."
    }
  ],
  "case_report_context": [],
  "next_check": []
}
```

---

## 5. How the three files work together

### Step-by-step system flow

### Step 1: Detection engines create flags

The detection layer runs first:

```powershell
python -m detection.run_engines
```

This writes all statistical/ML flags into SQLite.

---

### Step 2: `tools.py` provides access to evidence

`tools.py` gives the reasoning layer safe access to:

- `flags`
- `transactions`
- ChromaDB emails
- ChromaDB receipts
- optional registry CSV

---

### Step 3: `agent_execution.py` builds vendor reports

For each flagged vendor, it:

1. gets the vendor flags
2. finds invoice numbers and dates
3. retrieves vendor-matched documents
4. classifies suspicious vs explanatory evidence
5. scores the case
6. prints a vendor forensic report

---

### Step 4: `agent_execution.py` builds the connected fraud summary

After individual reports, it finds shared people across high-risk vendors.

This is where the system explains whether multiple vendors appear connected through the same people or approval path.

---

### Step 5: `agent_execution.py` saves results

The full output is saved to:

```text
data/forensic_case_results.json
```

---

### Step 6: `forensic_followup.py` answers direct questions

The user can then ask specific questions.

The follow-up file uses:

- saved reports
- final connected summary
- email evidence
- vector search fallback when appropriate

It answers the question and cites relevant email snippets.

---

## 6. Why this design is better than the first agent version

The first approach used a multi-turn LLM tool-calling agent. That caused two problems:

1. It used too many tokens.
2. It often treated every flag as fraud instead of using documents to clear false positives.

The final design is better because:

- the core report generation is deterministic
- Groq is optional, not required
- vendor evidence is filtered before scoring
- false positives can be cleared by actual documents
- high-risk vendors can be connected through shared parties
- follow-up answers are grounded in reports and emails
- vague questions do not trigger hallucinated evidence dumps

Basically, the AI stopped free-styling and started doing the job. A rare and fragile victory.

---

## 7. Environment flags

### Disable interactive follow-up mode

```powershell
$env:FORENSIC_INTERACTIVE_QA="0"
python -m agent_execution
```

### Enable optional Groq vendor-summary polish

```powershell
$env:FORENSIC_USE_GROQ="1"
python -m agent_execution
```

Default should usually stay:

```powershell
$env:FORENSIC_USE_GROQ="0"
```

### Enable optional Groq follow-up polish

```powershell
$env:FORENSIC_USE_GROQ_QA="1"
python -m forensic_followup
```

Default should usually stay:

```powershell
$env:FORENSIC_USE_GROQ_QA="0"
```

---

## 8. Expected final demo behavior

A good run should show:

1. Individual vendor reports.
2. False-positive vendors explained and downgraded.
3. High-risk vendors escalated.
4. A final connected fraud summary.
5. Interactive follow-up questions that can search reports and emails.
6. Safe refusal/no-result behavior for made-up names or vague prompts.

Example final story:

```text
The detection engines produced suspicious flags.
The forensic agent reviewed vendor-specific receipts and emails.
It cleared operational false positives.
It escalated the truly high-risk vendors.
It found that the same parties appeared across multiple high-risk cases.
It then allowed the user to ask direct questions and retrieve specific email evidence.
```

That is the whole forensic reasoning layer. It turns raw flags into an investigation narrative without needing the LLM to burn through tokens like it discovered capitalism.
