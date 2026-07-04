import json
import os
import re
from collections import Counter, defaultdict
from email.utils import parseaddr
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine

from storage.db import DB_PATH as SQLITE_DB_PATH, init_db
from storage.vector_store import GROQ_CHAT_MODEL, GROQ_URL, get_groq_api_key, post_with_retry
from tools import (
    get_vendor_documents,
    list_all_formatted_documents,
    query_sql_dataframe,
    query_sql_ledger,
    search_document_vectors,
    vendor_terms,
    verify_corporate_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
init_db()
ENGINE = create_engine(f"sqlite:///{SQLITE_DB_PATH.as_posix()}")
USE_GROQ_SYNTHESIS = os.getenv("FORENSIC_USE_GROQ", "0") == "1"
RESULTS_PATH = Path(os.getenv("FORENSIC_RESULTS_PATH", str(PROJECT_ROOT / "data" / "forensic_case_results.json")))


# General keyword groups
# They are generic audit concepts used only to classify vendor-matched documents

FRAUD_INDICATOR_TERMS = {
    "cleared before support was complete": [
        "support later", "supporting later", "documents later", "paperwork later", "receipt later",
        "clear today", "process today", "do not wait", "don't wait", "before review", "before audit",
        "outside normal batch", "manual clearance", "urgent clearance",
    ],
    "possible manual amount override": [
        "revised amount", "changed amount", "manual amount", "manual override", "adjusted amount",
        "internal amount", "different amount", "amount does not match", "source document can remain",
    ],
    "possible threshold avoidance": [
        "below threshold", "under threshold", "split approval", "separate approval", "break into",
        "split into", "avoid approval", "avoid review", "approval limit", "same range", "easier to clear",
    ],
    "possible concealment or unusual routing": [
        "keep it quiet", "do not copy", "don't copy", "same format", "keep the wording", "route through",
        "off the regular", "outside the regular", "close this before", "before compliance", "before month end review",
    ],
}

EXCULPATORY_INDICATOR_TERMS = {
    # Deliberately narrow. These are generic audit explanations
    # Avoid vague single words like "approved", "charge", "contract", or "agreement" because
    # they appear in normal finance emails and will accidentally clear real fraud.
    "contractual or recurring charge": [
        "fixed monthly", "monthly rent", "office rent", "lease payment", "lease invoice",
        "prepaid rent", "standing order", "recurring monthly", "subscription renewal",
    ],
    "clerical duplicate document": [
        "duplicate scan", "duplicate upload", "uploaded twice", "same copy", "stamped copy",
        "resubmitted copy", "re-upload", "copy only", "clerical duplicate", "not a duplicate payment",
        "scan was repeated", "second scan", "signed copy attached",
    ],
    "operational split or separate scope": [
        "separate delivery", "partial delivery", "separate work order", "separate scope",
        "different site", "different location", "separate vehicle", "weight limit", "axle limit",
        "road restriction", "bridge restriction", "truckload", "split delivery", "separate rental",
        "separate rental contract", "separate equipment", "separate crew", "separate operator",
    ],
    "legitimate adjustment or fee": [
        "weighbridge", "yard slip", "measurement adjustment", "unloading charge", "loading charge",
        "freight adjustment", "transport surcharge", "bank charge", "vat adjustment", "tax adjustment",
        "short quantity", "excess quantity", "quantity adjustment",
    ],
    "approved exception or one-off need": [
        "emergency", "urgent site need", "one-off", "one off", "change order", "shutdown",
        "weather delay", "site instruction", "special approval", "exception approval", "mobilization",
        "unplanned repair", "critical repair", "replacement required", "imported steel", "bulk pour",
    ],
}
GENERIC_PARTY_TERMS = {
    "vendor", "client", "recipient", "sender", "reviewer", "preparer", "purchaser", "buyer", "supplier",
    "accounts", "account", "finance", "unknown", "site store", "accounts review", "document participant",
}

HEADER_FIELDS = ("FROM", "TO", "CC", "BCC", "PARTIES", "SENDER", "RECIPIENT")



# Small helpers

def _clip(text: str, max_chars: int = 4000) -> str:
    text = str(text or "").strip()
    return text if len(text) <= max_chars else text[: max_chars - 80] + "\n...[truncated]..."


def _sql_escape(value: str) -> str:
    return str(value).replace("'", "''")


def _normalize_message_content(message_node):
    content = message_node.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text").strip()
    return content or ""


def _list_flag_vendors() -> List[str]:
    try:
        df = pd.read_sql(
            "SELECT DISTINCT vendor_name FROM flags WHERE vendor_name IS NOT NULL AND TRIM(vendor_name) <> '' ORDER BY vendor_name ASC",
            ENGINE,
        )
    except Exception:
        return []
    return [str(v).strip() for v in df["vendor_name"].tolist() if str(v).strip()]


def _find_vendors_in_summary(flag_cluster_summary: str) -> List[str]:
    summary_lower = str(flag_cluster_summary).lower()
    return [vendor for vendor in _list_flag_vendors() if vendor.lower() in summary_lower]


def _extract_invoice_numbers(text: str) -> List[str]:
    # Prefix length is broad enough for real invoices but avoids random hyphenated words.
    found = re.findall(r"\b[A-Z]{2,12}[-_\s]?\d{3,8}\b", str(text or ""))
    out = []
    for item in found:
        norm = re.sub(r"\s+", "", item).upper().replace("_", "-")
        if norm not in out:
            out.append(norm)
    return out[:20]


def _extract_dates(text: str) -> List[str]:
    found = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", str(text or ""))
    out = []
    for item in found:
        if item not in out:
            out.append(item)
    return out[:20]


def _extract_methods(flags_text: str) -> List[str]:
    methods = re.findall(r"\b(?:zscore|isolation_forest|invoice_matching|threshold_skirting|round_numbers|benfords_law)\b", str(flags_text))
    out = []
    for method in methods:
        if method not in out:
            out.append(method)
    return out


def _contains_any(blob: str, terms: Iterable[str]) -> bool:
    low = str(blob or "").lower()
    return any(str(term).lower() in low for term in terms if str(term).strip())


def _find_indicator_hits(blob: str, indicators: Dict[str, List[str]]) -> List[str]:
    low = str(blob or "").lower()
    hits = []
    for label, terms in indicators.items():
        if any(term.lower() in low for term in terms):
            hits.append(label)
    return hits


def _flags_blob(case_packet: Dict[str, str]) -> str:
    return "\n".join([
        case_packet.get("flags_text", ""),
        case_packet.get("transactions_text", ""),
        case_packet.get("registry_text", ""),
    ]).lower()


def _docs_blob(case_packet: Dict[str, str]) -> str:
    return "\n".join([
        case_packet.get("email_docs_text", ""),
        case_packet.get("receipt_docs_text", ""),
    ]).lower()


def _add_unique(items: List[str], value: str) -> None:
    if value and value not in items:
        items.append(value)



# Document parsing / filtering

def _split_document_blocks(text: str) -> List[str]:
    raw = str(text or "").strip()
    if not raw or raw == "(no results)":
        return []
    pieces = re.split(r"(?=\n?\[DOCUMENT\s+\d+\])", raw)
    blocks = [p.strip() for p in pieces if p.strip()]
    if len(blocks) > 1:
        return blocks
    pieces = re.split(r"(?=\n?\[RESULT\s+\d+\])", raw)
    blocks = [p.strip() for p in pieces if p.strip()]
    return blocks if blocks else [raw]


def _doc_is_vendor_consistent(block: str, vendor_name: str, invoices: List[str]) -> bool:
    low = str(block or "").lower()
    if any(inv.lower() in low for inv in invoices):
        return True
    terms = vendor_terms(vendor_name, invoices)
    return _contains_any(low, terms)


def _filter_vendor_docs(raw_docs_text: str, vendor_name: str, invoices: List[str], max_docs: int = 8) -> Tuple[str, int, int]:
    blocks = _split_document_blocks(raw_docs_text)
    kept = [b for b in blocks if _doc_is_vendor_consistent(b, vendor_name, invoices)]
    return "\n\n".join(kept[:max_docs]).strip(), len(kept), len(blocks)


def _build_search_queries(vendor_name: str, flags_text: str) -> Tuple[str, str]:
    invoices = _extract_invoice_numbers(flags_text)
    dates = _extract_dates(flags_text)
    methods = _extract_methods(flags_text)

    shared = [vendor_name, *invoices[:8], *dates[:5]]
    email_terms = ["email approval from to parties procurement finance accounts invoice support document"]
    receipt_terms = ["receipt invoice amount delivery note supporting document vendor date"]

    # Generic method-driven hints, not story answers.
    if "invoice_matching" in methods:
        email_terms.append("amount mismatch missing receipt duplicate upload revised amount source document")
    if "threshold_skirting" in methods:
        email_terms.append("approval threshold split approval separate delivery separate scope")
    if "round_numbers" in methods:
        email_terms.append("recurring contract fixed payment lease rent agreement")
    if "zscore" in methods or "isolation_forest" in methods:
        email_terms.append("one-off emergency exception approval unusual purchase")
    if "benfords_law" in methods:
        email_terms.append("structured amount same format billing pattern approval routing")

    return " | ".join(shared + email_terms), " | ".join(shared + receipt_terms)


def _vendor_case_packet(vendor_name: str) -> Dict[str, str]:
    escaped_vendor = _sql_escape(vendor_name)
    flags_text = query_sql_ledger(
        f"""
        SELECT vendor_name, date, amount, method, reason
        FROM flags
        WHERE vendor_name = '{escaped_vendor}'
        ORDER BY date ASC, method ASC;
        """
    )
    tx_text = query_sql_ledger(
        f"""
        SELECT date, amount, description, invoice_number, reference_number
        FROM transactions
        WHERE vendor_name = '{escaped_vendor}'
        ORDER BY date ASC
        LIMIT 50;
        """
    )
    registry_text = verify_corporate_registry(vendor_name)
    invoices = _extract_invoice_numbers(flags_text + "\n" + tx_text)
    email_query, receipt_query = _build_search_queries(vendor_name, flags_text + "\n" + tx_text)

    listed_email_docs = get_vendor_documents(vendor_name, invoices=invoices, doc_type="email", max_results=12)
    listed_receipt_docs = get_vendor_documents(vendor_name, invoices=invoices, doc_type="receipt", max_results=12)

    email_docs_text, email_kept, email_total = _filter_vendor_docs(listed_email_docs, vendor_name, invoices, max_docs=10)
    receipt_docs_text, receipt_kept, receipt_total = _filter_vendor_docs(listed_receipt_docs, vendor_name, invoices, max_docs=10)

    if email_kept == 0:
        raw_email_docs = search_document_vectors(email_query, n_results=20)
        email_docs_text, email_kept, email_total = _filter_vendor_docs(raw_email_docs, vendor_name, invoices, max_docs=10)
    if receipt_kept == 0:
        raw_receipt_docs = search_document_vectors(receipt_query, n_results=20)
        receipt_docs_text, receipt_kept, receipt_total = _filter_vendor_docs(raw_receipt_docs, vendor_name, invoices, max_docs=10)

    return {
        "vendor_name": vendor_name,
        "flags_text": _clip(flags_text, 8000),
        "transactions_text": _clip(tx_text, 5000),
        "registry_text": registry_text,
        "email_query": email_query,
        "receipt_query": receipt_query,
        "raw_email_result_count": str(email_total),
        "kept_email_result_count": str(email_kept),
        "raw_receipt_result_count": str(receipt_total),
        "kept_receipt_result_count": str(receipt_kept),
        "email_docs_text": _clip(email_docs_text, 9000),
        "receipt_docs_text": _clip(receipt_docs_text, 7000),
    }



# Generic people / role inference

def _extract_email_addresses(value: str) -> List[str]:
    out = []
    for email in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", str(value or "")):
        e = email.strip().strip("<>()[]{}'\" ,;").lower()
        if e and e not in out:
            out.append(e)
    return out


def _email_domain(email: str) -> str:
    email = email.lower().strip()
    return email.split("@", 1)[1] if "@" in email else ""


def _display_name_from_address(raw_value: str, email: str) -> str:
    # Try display name from RFC-ish address first.
    display, parsed_email = parseaddr(str(raw_value or ""))
    if parsed_email and parsed_email.lower() == email.lower() and display:
        clean = re.sub(r"\s+", " ", display).strip().strip('"')
        if clean and "@" not in clean:
            return f"{clean} <{email}>"

    local = email.split("@", 1)[0]
    local = re.sub(r"[^a-zA-Z]+", " ", local).strip()
    display = " ".join(part.capitalize() for part in local.split()) if local else email
    return f"{display} <{email}>"


def _header_values(block: str) -> List[Tuple[str, str]]:
    values: List[Tuple[str, str]] = []
    for line in str(block or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        match = re.match(r"^(FROM|TO|CC|BCC|PARTIES|SENDER|RECIPIENT)\s*:\s*(.+)$", clean, flags=re.IGNORECASE)
        if match:
            values.append((match.group(1).upper(), match.group(2).strip()))
            continue
        match = re.match(r"^(from|to|cc|bcc|parties|sender|recipient)\s*=\s*(.+)$", clean, flags=re.IGNORECASE)
        if match:
            values.append((match.group(1).upper(), match.group(2).strip()))
            continue

    # JSON-ish metadata fallback: "from": "..."
    pattern = r"[\"']?(from|to|cc|bcc|parties|sender|recipient)[\"']?\s*[:=]\s*[\"']([^\n\r\"']+)"
    for field, value in re.findall(pattern, str(block or ""), flags=re.IGNORECASE):
        values.append((field.upper(), value.strip()))
    return values


def _infer_internal_domains(all_email_docs: str) -> Set[str]:
    counts = Counter(_email_domain(e) for e in _extract_email_addresses(all_email_docs))
    counts = Counter({d: c for d, c in counts.items() if d})
    if not counts:
        return set()

    # Internal domains usually dominate corporate email traffic and have many unique senders/recipients.
    unique_locals: Dict[str, Set[str]] = defaultdict(set)
    for email in _extract_email_addresses(all_email_docs):
        domain = _email_domain(email)
        if domain:
            unique_locals[domain].add(email.split("@", 1)[0])

    total = sum(counts.values())
    internal = set()
    most_common_domain, most_common_count = counts.most_common(1)[0]
    internal.add(most_common_domain)

    for domain, count in counts.items():
        unique_count = len(unique_locals[domain])
        if unique_count >= 3 and count / max(total, 1) >= 0.18:
            internal.add(domain)
    return internal


def _get_corpus_email_docs() -> str:
    try:
        return list_all_formatted_documents(doc_type="email", max_results=1000)
    except Exception:
        return ""


def _role_from_email(email: str, field: str, internal_domains: Set[str], vendor_terms_set: Set[str], doc_text: str) -> str:
    local = email.split("@", 1)[0].lower()
    domain = _email_domain(email)
    is_internal = domain in internal_domains

    role_parts = []
    if is_internal:
        role_parts.append("internal")
    elif domain:
        role_parts.append("external")
    else:
        role_parts.append("document")

    if any(k in local for k in ["finance", "account", "payable", "ap", "treasury", "payment"]):
        role_parts.append("finance/accounts")
    elif any(k in local for k in ["procure", "purchase", "buyer", "sourcing", "supply"]):
        role_parts.append("procurement")
    elif any(k in local for k in ["site", "store", "warehouse", "field"]):
        role_parts.append("site/store")
    elif any(k in local for k in ["legal", "compliance", "control", "audit"]):
        role_parts.append("control/compliance")
    elif any(k in local for k in ["manager", "director", "head", "lead", "approver"]):
        role_parts.append("approver/manager")

    if field in {"FROM", "SENDER"}:
        role_parts.append("sender")
    elif field in {"TO", "RECIPIENT"}:
        role_parts.append("recipient")
    elif field in {"CC", "BCC"}:
        role_parts.append("copied")
    elif field == "PARTIES":
        role_parts.append("mentioned party")

    domain_blob = domain.replace(".", " ").replace("-", " ")
    if not is_internal and any(term in domain_blob or term in doc_text.lower() for term in vendor_terms_set):
        role_parts.append("vendor-side contact")

    return " / ".join(dict.fromkeys(role_parts))


def _looks_like_person_name(token: str) -> bool:
    clean = re.sub(r"\s+", " ", str(token or "")).strip().strip('"\'<>')
    if not clean or len(clean) < 5:
        return False
    low = clean.lower()
    if any(generic in low for generic in GENERIC_PARTY_TERMS):
        return False
    # Avoid company-ish names.
    if any(word in low for word in [" ltd", " limited", " holdings", " services", " supply", " rental", " cement", " steel", " cargo", " consult", " civil"]):
        return False
    return bool(re.search(r"\b[A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+\b", clean))


def _extract_named_parties(value: str) -> List[str]:
    # Strip emails, split common separators, and keep only human-looking names.
    without_emails = re.sub(r"<?[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}>?", " ", str(value or ""))
    candidates = re.split(r"[,;|/]", without_emails)
    out = []
    for cand in candidates:
        clean = re.sub(r"\s+", " ", cand).strip().strip('"\'<>')
        if _looks_like_person_name(clean) and clean not in out:
            out.append(clean)
    return out


def _participant_key(name_or_email: str) -> str:
    email_match = re.search(r"<([^>]+@[^>]+)>", name_or_email)
    if email_match:
        return email_match.group(1).lower()
    if "@" in name_or_email:
        return _extract_email_addresses(name_or_email)[0] if _extract_email_addresses(name_or_email) else name_or_email.lower()
    return re.sub(r"\s+", " ", name_or_email).strip().lower()


def _is_human_party_label(name_or_email: str) -> bool:
    """Keep human-looking people only. Drop companies, shared mailboxes, and role inboxes."""
    text = str(name_or_email or "").strip()
    if not text:
        return False
    display = re.sub(r"<[^>]+>", "", text).strip()
    low_display = display.lower()

    company_terms = [
        "ltd", "limited", "llc", "inc", "corp", "company", "co.", "holdings", "services",
        "supply", "rental", "cement", "steel", "cargo", "consult", "civil", "buildmart",
        "equipment", "lease", "survey", "safety", "sand", "aggregates", "hardware", "transport",
    ]
    if any(term in low_display for term in company_terms):
        return False

    role_terms = [
        "finance control", "finance", "accounts", "account", "billing", "site", "store",
        "admin", "info", "support", "payable", "ap", "treasury", "procurement desk",
    ]
    if any(term in low_display for term in role_terms):
        return False

    emails = _extract_email_addresses(text)
    if emails:
        local = emails[0].split("@", 1)[0].lower()
        role_local_tokens = ["finance", "control", "accounts", "account", "billing", "site", "store", "admin", "info", "support", "payable", "ap"]
        if any(tok in local for tok in role_local_tokens):
            return False
        if re.search(r"[a-z]+[._-][a-z]+", local):
            return True
        if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", display):
            return True
        return False

    return bool(re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", display))


def _party_display_name(name_or_email: str) -> str:
    """Return just a readable name."""
    text = str(name_or_email or "").strip()
    m = re.match(r"\s*([^<]+?)\s*<([^>]+)>\s*$", text)
    if m:
        display = re.sub(r"\s+", " ", m.group(1)).strip()
        email = m.group(2).strip().lower()
        if display and _is_human_party_label(display):
            return display
        local = email.split("@", 1)[0]
        return " ".join(part.capitalize() for part in re.split(r"[._-]+", local) if part) or email
    return re.sub(r"\s+", " ", text).strip()


def _compact_party_names(rows: List[Dict[str, object]], max_names: int = 5) -> List[str]:
    names: List[str] = []
    for row in rows:
        raw = str(row.get("name_or_email", ""))
        if not _is_human_party_label(raw):
            continue
        name = _party_display_name(raw)
        if name and name not in names:
            names.append(name)
        if len(names) >= max_names:
            break
    return names


def _extract_people_from_docs(doc_text: str, vendor_name: str, risk_verdict: str, fraud_hits: List[str]) -> Dict[str, List[Dict[str, str]]]:
    corpus_docs = _get_corpus_email_docs()
    internal_domains = _infer_internal_domains(corpus_docs or doc_text)
    vterms = set(vendor_terms(vendor_name, []))

    people: Dict[str, Dict[str, object]] = {}
    suspicious_block_count = 0

    for block in _split_document_blocks(doc_text):
        block_low = block.lower()
        block_fraud_hits = _find_indicator_hits(block_low, FRAUD_INDICATOR_TERMS)
        is_suspicious_doc = bool(block_fraud_hits)
        if is_suspicious_doc:
            suspicious_block_count += 1

        # Header/metadata fields get highest trust.
        for field, value in _header_values(block):
            emails = _extract_email_addresses(value)
            for email in emails:
                display = _display_name_from_address(value, email)
                key = _participant_key(display)
                if key not in people:
                    people[key] = {
                        "name_or_email": display,
                        "role_inference": _role_from_email(email, field, internal_domains, vterms, block),
                        "score": 0,
                        "fields": set(),
                        "suspicious_docs": 0,
                    }
                people[key]["fields"].add(field)
                people[key]["score"] = int(people[key]["score"]) + (4 if field in {"FROM", "SENDER"} else 2 if field in {"TO", "RECIPIENT", "PARTIES"} else 1)
                if is_suspicious_doc:
                    people[key]["suspicious_docs"] = int(people[key]["suspicious_docs"]) + 1
                    people[key]["score"] = int(people[key]["score"]) + 4

            # Named parties without emails, only from explicit header-ish fields.
            for name in _extract_named_parties(value):
                key = _participant_key(name)
                if key not in people:
                    people[key] = {
                        "name_or_email": name,
                        "role_inference": "named participant from email metadata/body",
                        "score": 0,
                        "fields": set(),
                        "suspicious_docs": 0,
                    }
                people[key]["fields"].add(field)
                people[key]["score"] = int(people[key]["score"]) + (3 if field == "PARTIES" else 1)
                if is_suspicious_doc:
                    people[key]["suspicious_docs"] = int(people[key]["suspicious_docs"]) + 1
                    people[key]["score"] = int(people[key]["score"]) + 3

        # Body fallback: email addresses anywhere in vendor-matched document.
        for email in _extract_email_addresses(block):
            display = _display_name_from_address(email, email)
            key = _participant_key(display)
            if key not in people:
                people[key] = {
                    "name_or_email": display,
                    "role_inference": _role_from_email(email, "BODY", internal_domains, vterms, block),
                    "score": 0,
                    "fields": set(),
                    "suspicious_docs": 0,
                }
            people[key]["fields"].add("BODY")
            people[key]["score"] = int(people[key]["score"]) + 1
            if is_suspicious_doc:
                people[key]["suspicious_docs"] = int(people[key]["suspicious_docs"]) + 1
                people[key]["score"] = int(people[key]["score"]) + 2

    # Convert and rank.
    rows = []
    for rec in people.values():
        fields = sorted(list(rec.pop("fields")))
        score = int(rec.pop("score"))
        suspicious_docs = int(rec.pop("suspicious_docs"))
        rows.append({
            "name_or_email": str(rec["name_or_email"]),
            "role_inference": str(rec["role_inference"]),
            "evidence_fields": fields,
            "involvement_score": score,
            "suspicious_email_hits": suspicious_docs,
        })
    rows.sort(key=lambda r: (r["suspicious_email_hits"], r["involvement_score"]), reverse=True)

    # Primary suspects are not just everyone in the thread. They need suspicious-doc involvement,
    # or high recurrence when the vendor is likely fraud. External vendor-side + internal processor/requester rise to top naturally.
    primary = []
    supporting = []
    for row in rows:
        role_low = row["role_inference"].lower()
        # Plain company names from PARTIES are useful context, but they are not people.
        is_plain_named_party = "<" not in row["name_or_email"]
        if is_plain_named_party:
            if len(supporting) < 8:
                supporting.append(row)
            continue

        qualifies_primary = (
            risk_verdict == "likely_fraud"
            and (
                row["suspicious_email_hits"] > 0
                or row["involvement_score"] >= 7
                or "sender" in role_low
                or "vendor-side" in role_low
                or "finance/accounts" in role_low
                or "procurement" in role_low
            )
        )
        if qualifies_primary and len(primary) < 5:
            primary.append(row)
        elif len(supporting) < 8:
            supporting.append(row)

    primary_names = _compact_party_names(primary, max_names=5)
    supporting_names = _compact_party_names(supporting, max_names=5)
    combined_names: List[str] = []
    for name in primary_names + supporting_names:
        if name not in combined_names:
            combined_names.append(name)

    return {
        "primary_suspects": primary_names,
        "supporting_participants": supporting_names,
        "people_involved": combined_names[:6],
        "people_debug": {
            "internal_domains_inferred": sorted(internal_domains),
            "suspicious_email_blocks": suspicious_block_count,
            "total_people_extracted": len(rows),
        },
    }



# Evidence scoring

def _extract_chi_square(flags_text: str) -> Optional[float]:
    text = str(flags_text or "")
    patterns = [
        r"Chi[- ]Square Stat of\s*([0-9.]+)",
        r"chi[- ]square[^0-9]{0,40}([0-9]+(?:\.[0-9]+)?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None


def _has_registry_alert(registry_text: str) -> bool:
    low = str(registry_text or "").lower()
    return "alert:" in low and "does not appear" in low


def _registry_unknown(registry_text: str) -> bool:
    return str(registry_text or "").lower().startswith("unknown")


def _block_has_any(block: str, terms: List[str]) -> bool:
    low = str(block or "").lower()
    return any(term.lower() in low for term in terms)


def _classify_evidence_context(docs_blob: str, methods: List[str]) -> Tuple[List[str], List[str]]:
    """
    Return (exculpatory_hits, fraud_hits) using narrow, method-gated indicators.

    This is still general. It does not know vendor names or story characters. The trick is
    not to treat generic words like "approved", "charge", or "contract" as magical innocence
    tokens, because that was clearing half the universe. Humanity survives another bug.
    """
    blocks = _split_document_blocks(docs_blob)
    exculp: List[str] = []
    fraud: List[str] = []

    for block in blocks:
        low = block.lower()

        # Fraud indicators can apply broadly, but still require concrete phrasing.
        for label, terms in FRAUD_INDICATOR_TERMS.items():
            if _block_has_any(low, terms):
                _add_unique(fraud, label)

        # Exculpatory indicators are method-gated, so a lease email doesn't clear a Benford-only shell pattern.
        if ("round_numbers" in methods or "invoice_matching" in methods) and _block_has_any(
            low, EXCULPATORY_INDICATOR_TERMS["contractual or recurring charge"]
        ):
            _add_unique(exculp, "contractual or recurring charge")

        if "invoice_matching" in methods and _block_has_any(
            low, EXCULPATORY_INDICATOR_TERMS["clerical duplicate document"]
        ):
            _add_unique(exculp, "clerical duplicate document")

        if "threshold_skirting" in methods and _block_has_any(
            low, EXCULPATORY_INDICATOR_TERMS["operational split or separate scope"]
        ):
            _add_unique(exculp, "operational split or separate scope")

        if "invoice_matching" in methods and _block_has_any(
            low, EXCULPATORY_INDICATOR_TERMS["legitimate adjustment or fee"]
        ):
            _add_unique(exculp, "legitimate adjustment or fee")

        if ("zscore" in methods or "isolation_forest" in methods or "invoice_matching" in methods) and _block_has_any(
            low, EXCULPATORY_INDICATOR_TERMS["approved exception or one-off need"]
        ):
            _add_unique(exculp, "approved exception or one-off need")

    return exculp, fraud



def _readable_join(items: List[str], max_items: int = 5) -> str:
    clean = [str(i).strip().rstrip('.') for i in items if str(i).strip()]
    clean = clean[:max_items]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return ", ".join(clean[:-1]) + f", and {clean[-1]}"


def _filter_display_evidence(evidence_points: List[str]) -> List[str]:
    """Remove operational notes that are useful internally but noisy in the final report."""
    noisy_prefixes = (
        "no registry source was configured",
    )
    out: List[str] = []
    for point in evidence_points:
        clean = str(point or "").strip()
        if not clean:
            continue
        if any(clean.lower().startswith(prefix) for prefix in noisy_prefixes):
            continue
        _add_unique(out, clean)
    return out


def _compose_fraud_narrative(vendor: str, verdict: str, mechanisms: List[str], fraud_hits: List[str], exculpatory_hits: List[str]) -> str:
    mechanisms_text = _readable_join(mechanisms, 4)
    fraud_text = _readable_join(fraud_hits, 3)
    exculp_text = _readable_join(exculpatory_hits, 3)

    if verdict == "likely_fraud":
        if fraud_text:
            return (
                f"The concern is that {vendor} was not just an unusual vendor, but part of a payment path that shows {mechanisms_text}. "
                f"The matched correspondence adds a behavioral signal: {fraud_text}. This combination makes the case look coordinated rather than accidental."
            )
        return (
            f"The concern is that {vendor} was used to route questionable payments through {mechanisms_text}. "
            "The transaction pattern is strong enough to escalate because the flagged behavior is not fully explained by the matched documents."
        )

    if verdict == "likely_false_positive":
        if exculp_text:
            return (
                f"The flags initially make {vendor} look suspicious because they resemble {mechanisms_text}. "
                f"However, the matched documents give a plausible business explanation: {exculp_text}. This makes the case more likely an explainable operational exception than fraud."
            )
        return (
            f"The flags around {vendor} resemble {mechanisms_text}, but the supporting documents do not show enough suspicious behavior to treat it as fraud. "
            "This should be reviewed as a false-positive candidate unless the original approvals contradict the documents."
        )

    return (
        f"The flags around {vendor} point to {mechanisms_text}, but the documents do not clearly prove or clear the concern. "
        "This case needs manual review before it can be treated as fraud or a false positive."
    )


def _compose_evidence_narrative(vendor: str, verdict: str, evidence_points: List[str], fraud_hits: List[str], exculpatory_hits: List[str]) -> str:
    visible_evidence = _filter_display_evidence(evidence_points)
    evidence_text = _readable_join(visible_evidence, 5)
    fraud_text = _readable_join(fraud_hits, 3)
    exculp_text = _readable_join(exculpatory_hits, 3)

    if verdict == "likely_fraud":
        base = f"The strongest evidence against {vendor} is that {evidence_text}." if evidence_text else f"The strongest evidence against {vendor} comes from the combined flag pattern."
        if fraud_text:
            base += f" The emails also contain language consistent with {fraud_text}, which strengthens the escalation."
        if not exculp_text:
            base += " No vendor-matched explanation was strong enough to neutralize the risk."
        return base

    if verdict == "likely_false_positive":
        base = f"The suspicious signals were real: {evidence_text}." if evidence_text else "The statistical flags were real, but weak after context review."
        if exculp_text:
            base += f" The reason the risk drops is that vendor-matched documents specifically support {exculp_text}."
        base += " The case should still be checked for document authenticity, but the current context favors explanation over misconduct."
        return base

    base = f"The available evidence shows {evidence_text}." if evidence_text else "The available evidence is incomplete."
    if fraud_text:
        base += f" Some correspondence suggests {fraud_text}."
    if exculp_text:
        base += f" Some documents also suggest {exculp_text}."
    base += " Because the evidence points in both directions, the case remains inconclusive."
    return base


def _compose_next_steps_narrative(verdict: str, next_steps: List[str]) -> str:
    clean = [str(step).strip().rstrip('.') for step in next_steps if str(step).strip()]
    if not clean:
        return "Review the source transactions, matched documents, and approval trail before closing the case."

    def lower_first(value: str) -> str:
        return value[:1].lower() + value[1:] if value else value

    if verdict == "likely_fraud":
        priority = clean[:4]
        return "Escalate this case for review. " + " Then ".join([priority[0]] + [lower_first(s) for s in priority[1:]]) + "."
    if verdict == "likely_false_positive":
        priority = clean[:3]
        return "Do a light validation before closing the alert. " + " Then ".join([priority[0]] + [lower_first(s) for s in priority[1:]]) + "."
    priority = clean[:3]
    return "Keep this case open for manual review. " + " Then ".join([priority[0]] + [lower_first(s) for s in priority[1:]]) + "."


def _score_vendor_case(case_packet: Dict[str, str]) -> Dict[str, object]:
    vendor = case_packet["vendor_name"]
    flags_blob = _flags_blob(case_packet)
    docs = _docs_blob(case_packet)
    methods = _extract_methods(case_packet.get("flags_text", ""))

    risk = 8
    confidence = 35
    fraud_mechanisms: List[str] = []
    evidence_points: List[str] = []
    next_steps: List[str] = []

    phantom = "phantom payment risk" in flags_blob or "source document verification failure" in flags_blob or "0 string matches" in flags_blob
    mismatch = "invoice amount mismatch" in flags_blob or "amount mismatch" in flags_blob
    threshold = "threshold skirting" in flags_blob or "policy circumvention" in flags_blob
    duplicate_doc = "duplicate receipts found" in flags_blob or "document duplication failure" in flags_blob or "duplicate invoice" in flags_blob
    benford = "benfords_law" in methods or "leading-digit" in flags_blob or "benford" in flags_blob
    zscore = "zscore" in methods
    iso = "isolation_forest" in methods
    round_numbers = "round_numbers" in methods
    registry_alert = _has_registry_alert(case_packet.get("registry_text", ""))
    registry_unknown = _registry_unknown(case_packet.get("registry_text", ""))

    if phantom:
        risk += 36
        confidence += 16
        _add_unique(fraud_mechanisms, "payment cleared without matching source document")
        _add_unique(evidence_points, "a bank-cleared payment has no matching receipt/source document in the allowed window")
        _add_unique(next_steps, "obtain the missing source document and approval trail")

    if mismatch:
        risk += 22
        confidence += 9
        _add_unique(fraud_mechanisms, "cleared amount differs from supporting document")
        _add_unique(evidence_points, "the cleared bank amount does not match the supporting document amount")
        _add_unique(next_steps, "compare approved amount, receipt amount, and bank-cleared amount")

    if threshold:
        risk += 22
        confidence += 8
        _add_unique(fraud_mechanisms, "payments may have been split below an approval threshold")
        _add_unique(evidence_points, "multiple near-threshold payments were clustered within a short window")
        _add_unique(next_steps, "review whether clustered payments were intentionally separated or represent separate scopes")

    if duplicate_doc:
        risk += 14
        confidence += 6
        _add_unique(fraud_mechanisms, "duplicate receipt or invoice recycling")
        _add_unique(evidence_points, "more than one receipt/source document matched the same invoice inside the active window")
        _add_unique(next_steps, "confirm whether the duplicate document was clerical or used to support payment")

    if round_numbers:
        risk += 12
        confidence += 5
        _add_unique(fraud_mechanisms, "round-number payment pattern may be manually generated")
        _add_unique(evidence_points, "a high share of payments are round numbers")
        _add_unique(next_steps, "check whether round payments are contractual recurring charges")

    benford_chi = _extract_chi_square(case_packet.get("flags_text", "")) if benford else None
    if benford:
        # Strong Benford deviations should matter even without a registry file. This is general,
        # based on the calculated statistic, not on knowing which vendor is supposed to be bad.
        bump = 18
        if benford_chi is not None and benford_chi >= 100:
            bump = 45
        elif benford_chi is not None and benford_chi >= 50:
            bump = 35
        elif benford_chi is not None and benford_chi >= 25:
            bump = 22
        risk += bump
        confidence += 12
        _add_unique(fraud_mechanisms, "amounts may be manually structured or fabricated")
        if benford_chi is not None:
            _add_unique(evidence_points, f"the vendor's leading-digit distribution strongly deviates from Benford's Law (chi-square {benford_chi:.2f})")
        else:
            _add_unique(evidence_points, "the vendor's leading-digit distribution significantly deviates from Benford's Law")
        _add_unique(next_steps, "review how invoice amounts were selected and who prepared the invoices")

    if zscore:
        risk += 10
        confidence += 4
        _add_unique(evidence_points, "one payment materially exceeded the vendor's usual payment range")
        _add_unique(next_steps, "validate the one-off amount against project scope and approval")

    if iso:
        risk += 8
        confidence += 4
        _add_unique(evidence_points, "the payment pattern looks unusual relative to the vendor's own history")

    if registry_alert:
        risk += 8
        confidence += 4
        _add_unique(evidence_points, "the vendor was not found in the configured registry snapshot")
        _add_unique(next_steps, "confirm vendor onboarding file, trade license, tax ID, and bank ownership")
    elif registry_unknown:
        _add_unique(evidence_points, "no registry source was configured, so registry status was not treated as evidence")

    exculpatory_hits, fraud_hits = _classify_evidence_context(docs, methods)

    for hit in exculpatory_hits:
        if hit == "contractual or recurring charge":
            risk -= 28
            confidence += 9
            _add_unique(evidence_points, "vendor-matched documents describe contractual, recurring, rent, or lease terms")
        elif hit == "clerical duplicate document":
            risk -= 24
            confidence += 10
            _add_unique(evidence_points, "vendor-matched documents describe a duplicate scan/upload/copy rather than a duplicate payment")
        elif hit == "operational split or separate scope":
            risk -= 24
            confidence += 9
            _add_unique(evidence_points, "vendor-matched documents describe separate deliveries, locations, work orders, or operational constraints")
        elif hit == "legitimate adjustment or fee":
            risk -= 18
            confidence += 8
            _add_unique(evidence_points, "vendor-matched documents explain tax, fee, transport, loading, or measurement adjustments")
        elif hit == "approved exception or one-off need":
            risk -= 20
            confidence += 8
            _add_unique(evidence_points, "vendor-matched documents describe an approved exception, emergency, or one-off need")

    for hit in fraud_hits:
        risk += 18
        confidence += 9
        _add_unique(fraud_mechanisms, hit)
        _add_unique(evidence_points, f"vendor-matched correspondence suggests {hit}")

    # General shell/vendor fabrication escalation. No vendor names, no known villains.
    # A severe Benford statistic can be strong by itself; mild Benford needs corroboration.
    enough_docs = int(case_packet.get("kept_email_result_count", "0") or 0) + int(case_packet.get("kept_receipt_result_count", "0") or 0)
    severe_benford = bool(benford and benford_chi is not None and benford_chi >= 75)
    corroborated_benford = bool(benford and (fraud_hits or registry_alert or enough_docs <= 2) and not exculpatory_hits)
    multi_engine_fraud = phantom or (mismatch and threshold) or (mismatch and fraud_hits) or (threshold and fraud_hits)

    if severe_benford and not exculpatory_hits:
        risk = max(risk, 72)
        confidence = max(confidence, 76)
        _add_unique(evidence_points, "very high Benford deviation without a matching business explanation indicates possible fabricated-billing or shell-vendor risk")
    elif corroborated_benford:
        risk = max(risk, 70)
        confidence = max(confidence, 72)
        _add_unique(evidence_points, "Benford deviation combined with weak/negative vendor support indicates possible shell-vendor or fabricated-billing risk")

    if multi_engine_fraud:
        risk = max(risk, 82)
        confidence = max(confidence, 80)

    risk = max(0, min(100, int(round(risk))))
    confidence = max(0, min(100, int(round(confidence))))

    if risk >= 70:
        verdict = "likely_fraud"
        confidence = max(confidence, 76)
    elif risk <= 30 and exculpatory_hits:
        verdict = "likely_false_positive"
        confidence = max(confidence, 72)
    elif risk <= 20:
        verdict = "likely_false_positive"
        confidence = max(confidence, 65)
    else:
        verdict = "inconclusive"
        confidence = max(confidence, 50)

    if verdict == "likely_false_positive":
        _add_unique(next_steps, "confirm the business explanation in the approval thread")
        _add_unique(next_steps, "mark as explainable if original documents are authentic")
    elif verdict == "likely_fraud":
        _add_unique(next_steps, "identify approver, preparer, processor, and vendor-side beneficiary from email metadata")
        _add_unique(next_steps, "compare document timestamps, uploader, and payment approval timing")
    else:
        _add_unique(next_steps, "review the full email thread and original source documents")
        _add_unique(next_steps, "confirm whether the anomaly has documented operational support")

    people_data = _extract_people_from_docs(case_packet.get("email_docs_text", ""), vendor, verdict, fraud_hits)

    if not fraud_mechanisms:
        _add_unique(fraud_mechanisms, "flag pattern requires contextual document and approval review")

    return {
        "vendor_name": vendor,
        "risk_score": risk,
        "confidence": confidence,
        "verdict": verdict,
        "case_interpretation": _compose_fraud_narrative(vendor, verdict, fraud_mechanisms, fraud_hits, exculpatory_hits),
        "evidence_summary": _compose_evidence_narrative(vendor, verdict, evidence_points, fraud_hits, exculpatory_hits),
        "people_involved": people_data["people_involved"],
        "next_steps": _compose_next_steps_narrative(verdict, next_steps),
    }


def _heuristic_vendor_summary(case_packet: Dict[str, str]) -> str:
    return json.dumps(_score_vendor_case(case_packet), indent=2, ensure_ascii=False)



def _llm_vendor_summary(case_packet: Dict[str, str], base_summary_json: str) -> str:
    if not USE_GROQ_SYNTHESIS:
        return base_summary_json

    api_key = get_groq_api_key()
    if not api_key:
        return base_summary_json

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    system_instruction = (
        "You are a concise fraud-review assistant. You receive vendor-matched flags, emails, receipts, and a draft JSON summary. "
        "Do not use outside knowledge. Do not assume named suspects unless they appear in FROM, TO, CC, BCC, PARTIES, or the email body. "
        "Do not invent vendor aliases. Clear false positives only when documents provide a direct business explanation. "
        "Return exactly one JSON object with the same keys. Keep people_involved as a list of names only. "
        "Write case_interpretation, evidence_summary, and next_steps as short plain-English paragraphs, not keyword lists. "
        "Explain what the pattern means, why it matters, and what would confirm or clear it."
    )
    user_prompt = f"""
Vendor: {case_packet['vendor_name']}
Flags:
{case_packet['flags_text']}
Transactions:
{case_packet['transactions_text']}
Registry:
{case_packet['registry_text']}
Vendor-matched Emails:
{case_packet['email_docs_text']}
Vendor-matched Receipts:
{case_packet['receipt_docs_text']}

Draft JSON:
{base_summary_json}
""".strip()
    body = {
        "model": GROQ_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": 900,
    }
    try:
        response = post_with_retry(f"{GROQ_URL}/chat/completions", headers, body, "Groq")
        response_json = response.json()
        content = _normalize_message_content(response_json["choices"][0]["message"]).strip()
        return content or base_summary_json
    except Exception:
        return base_summary_json


def _ensure_flags_present() -> int:
    """Run the detection pipeline if the flags table is empty so the agent can analyze something."""
    try:
        flag_count = int(pd.read_sql("SELECT COUNT(*) AS c FROM flags", ENGINE).iloc[0]["c"])
    except Exception as exc:
        print(f"Warning: could not inspect flags table: {exc}")
        return 0

    if flag_count > 0:
        print(f"Found {flag_count} existing flag row(s) in the database.")
        return flag_count

    print("No flags found in the database. Running the detection engines before building reports...")
    try:
        from detection.run_engines import run_engines

        new_flags = run_engines()
        if new_flags:
            print(f"Detection produced {len(new_flags)} flag(s).")
            return len(new_flags)
    except Exception as exc:
        print(f"Warning: automatic detection run failed: {exc}")

    return 0


def execute_agent_investigation(flag_cluster_summary, max_turns=5):
    """
    Public entry point preserved. Builds compact per-vendor case packets,
    filters documents by vendor/invoice, extracts people from explicit email metadata/body,
    and scores with general fraud-review rules. Groq is optional and off by default.
    """
    matched_vendors = _find_vendors_in_summary(flag_cluster_summary)
    if not matched_vendors:
        generic_packet = {
            "vendor_name": "unknown_vendor",
            "flags_text": _clip(str(flag_cluster_summary), 4000),
            "transactions_text": "",
            "registry_text": "",
            "email_docs_text": "",
            "receipt_docs_text": "",
            "kept_email_result_count": "0",
            "kept_receipt_result_count": "0",
        }
        base = _heuristic_vendor_summary(generic_packet)
        return _llm_vendor_summary(generic_packet, base)

    reports = []
    for vendor_name in matched_vendors:
        case_packet = _vendor_case_packet(vendor_name)
        base = _heuristic_vendor_summary(case_packet)
        reports.append(_llm_vendor_summary(case_packet, base))
    return "\n\n".join(reports)



# Final cross-vendor relationship summary

def _build_final_relationship_summary(reports: List[Dict[str, object]]) -> Dict[str, object]:
    fraud_reports = [r for r in reports if r.get("verdict") == "likely_fraud"]
    fraud_vendors = [str(r.get("vendor_name", "")).strip() for r in fraud_reports if str(r.get("vendor_name", "")).strip()]

    party_to_vendors: Dict[str, Set[str]] = defaultdict(set)
    for report in fraud_reports:
        vendor = str(report.get("vendor_name", "")).strip()
        for party in report.get("people_involved", []) or []:
            party_name = str(party).strip()
            if party_name:
                party_to_vendors[party_name].add(vendor)

    shared_people = [
        name for name, vendors in sorted(party_to_vendors.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if len(vendors) >= 2
    ]

    vendor_scores = [
        f"{r.get('vendor_name')} ({r.get('risk_score')}/100 risk, {r.get('verdict')})"
        for r in fraud_reports
        if r.get("vendor_name")
    ]

    if fraud_vendors and shared_people:
        summary = (
            f"The high-risk cases do not look isolated. {', '.join(fraud_vendors)} were all escalated as likely fraud, "
            f"and the same named parties appear across more than one of those vendor cases: {', '.join(shared_people[:5])}. "
            "That overlap suggests the suspicious activity may have moved through a shared internal approval or payment-processing route, rather than three unrelated vendor problems. "
            "In plain terms: Apex shows direct payment-document manipulation, while the other high-risk vendors show structured or fabricated-looking billing patterns. "
            "The repeated people across those cases are the connective tissue that makes this look coordinated."
        )
        relationship_theory = (
            "The likely relationship is a shared actor network: the same internal parties appear around multiple high-risk vendors, "
            "which points to coordination across procurement, approval, and payment handling. This does not by itself prove personal guilt, "
            "but it identifies exactly whose approval trail, inbox history, and vendor onboarding records should be reviewed first."
        )
    elif fraud_vendors:
        summary = (
            f"The high-risk vendors are {', '.join(fraud_vendors)}. The system did not find enough repeated named parties to prove a shared actor network, "
            "so these cases should be treated as separate escalations until approval-chain evidence connects them."
        )
        relationship_theory = (
            "At this stage, the common pattern is risk profile rather than proven people overlap. Compare approvers, processors, bank accounts, vendor onboarding files, and document timestamps next."
        )
    else:
        summary = "No likely-fraud vendor cluster was strong enough to build a connected-party theory."
        relationship_theory = "There is no connected-fraud theory yet because no vendor cluster crossed the fraud threshold."

    confirmation_steps = [
        "Compare approval chains across the high-risk vendors.",
        "Check whether the shared parties touched invoice approval, payment release, or vendor onboarding.",
        "Review email timestamps against payment dates and document-upload dates.",
        "Confirm vendor ownership, bank account ownership, and any personal links between vendor contacts and internal staff.",
    ]

    return {
        "high_risk_vendors": fraud_vendors,
        "vendor_risk_snapshot": vendor_scores,
        "shared_parties_across_high_risk_vendors": shared_people[:8],
        "summary": summary,
        "relationship_theory": relationship_theory,
        "recommended_confirmation_steps": confirmation_steps,
    }

if __name__ == "__main__":
    _ensure_flags_present()

    try:
        vendor_df = pd.read_sql(
            """
            SELECT vendor_name, COUNT(*) AS flag_count
            FROM flags
            WHERE vendor_name IS NOT NULL AND TRIM(vendor_name) <> ''
            GROUP BY vendor_name
            ORDER BY flag_count DESC, vendor_name ASC;
            """,
            ENGINE,
        )
    except Exception as exc:
        print(f"Unable to read flags table: {exc}")
        raise SystemExit(1)

    if vendor_df.empty:
        print("No vendor-level flags were available to analyze. The run completed without generating reports.")
        raise SystemExit(0)

    parsed_reports: List[Dict[str, object]] = []

    for _, row in vendor_df.iterrows():
        vendor_name = str(row["vendor_name"]).strip()
        summary_text = query_sql_ledger(
            f"""
            SELECT vendor_name, date, amount, method, reason
            FROM flags
            WHERE vendor_name = '{_sql_escape(vendor_name)}'
            ORDER BY date ASC, method ASC;
            """
        )
        final_verdict = execute_agent_investigation(summary_text)
        print("\n========= VENDOR FORENSIC REPORT =========")
        print(final_verdict)
        try:
            parsed_reports.append(json.loads(final_verdict))
        except Exception:
            pass

    final_connected_summary = _build_final_relationship_summary(parsed_reports)

    print("\n========= FINAL CONNECTED FRAUD SUMMARY =========")
    print(json.dumps(final_connected_summary, indent=2, ensure_ascii=False))

    # Save results so a separate follow-up Q&A process can answer later questions
    # using both the case reports and fresh email/receipt searches.
    try:
        from pathlib import Path

        results_path = RESULTS_PATH
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(
            json.dumps(
                {
                    "vendor_reports": parsed_reports,
                    "final_connected_summary": final_connected_summary,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"Warning: could not save forensic_case_results.json: {exc}")

    # Optional terminal follow-up mode. Press Enter immediately to exit.
    # Disable with: $env:FORENSIC_INTERACTIVE_QA="0"
    if os.getenv("FORENSIC_INTERACTIVE_QA", "1") == "1":
        try:
            from forensic_followup import interactive_followup_loop

            interactive_followup_loop(parsed_reports, final_connected_summary)
        except Exception as exc:
            print(f"Warning: follow-up Q&A mode could not start: {exc}")
