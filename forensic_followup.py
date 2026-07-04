"""
Follow-up Q&A layer for the AI Fraud Detector.

Purpose:
- After agent_execution.py prints vendor reports and the final connected summary,
  this module can answer direct user questions like:
    "How is Rafiq related to the fraud?"
    "Why was Apex marked likely fraud?"
    "Who pushed Tariq to clear the payment?"

Design:
- Uses the already-generated vendor reports.
- Searches the stored email corpus through tools.list_all_formatted_documents().
- Searches receipts/vector results as a fallback.
- Keeps output concise and readable.
- No hardcoded demo villains, vendors, or relationships.
- Optional Groq polishing is available but OFF by default.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from storage.vector_store import GROQ_CHAT_MODEL, GROQ_URL, get_groq_api_key, post_with_retry
except Exception:  # pragma: no cover
    GROQ_CHAT_MODEL = ""
    GROQ_URL = ""
    get_groq_api_key = None
    post_with_retry = None

from dotenv import load_dotenv

from tools import list_all_formatted_documents, search_document_vectors

PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve_path(env_var: str, default: Path) -> Path:
    raw = os.getenv(env_var)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path
    return default


load_dotenv(PROJECT_ROOT / ".env")
RESULTS_PATH = _resolve_path("FORENSIC_RESULTS_PATH", PROJECT_ROOT / "data" / "forensic_case_results.json")
USE_GROQ_QA = os.getenv("FORENSIC_USE_GROQ_QA", "0") == "1"

QUESTION_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "what", "when", "where", "which", "who", "whom",
    "how", "why", "was", "were", "are", "is", "to", "of", "in", "on", "at", "by", "a", "an",
    "did", "does", "do", "it", "its", "his", "her", "their", "them", "him", "she", "he", "they",
    "fraud", "related", "relation", "connected", "connect", "connection", "involved", "payment", "vendor",
}

GENERIC_FRAUD_TERMS = [
    "clear today", "process today", "support later", "documents later", "receipt later", "do not wait",
    "don't wait", "before review", "before audit", "outside normal batch", "manual clearance",
    "revised amount", "changed amount", "manual amount", "internal amount", "amount does not match",
    "below threshold", "under threshold", "split approval", "separate approval", "avoid review",
    "same format", "keep the wording", "route through", "close this before", "before compliance",
]


GENERIC_VENDOR_TOKENS = {
    "company", "co", "co.", "ltd", "llc", "inc", "limited", "holdings", "services", "service",
    "bd", "bangladesh", "group", "trading", "supply", "supplies", "corporation", "corp",
    "equipment", "rental", "cement", "steel", "safety", "survey", "lease", "cargo", "consult",
    "sand", "aggregates", "buildmart", "hardware", "transport", "office", "electrical", "gear",
}

QUERY_EXPANSIONS = {
    "innocent": ["false positive", "legitimate", "explain", "supporting document", "business explanation"],
    "legit": ["legitimate", "business explanation", "supporting document"],
    "clean": ["legitimate", "business explanation", "false positive"],
    "false": ["false positive", "duplicate upload", "clerical", "business explanation"],
    "id": ["vendor setup", "vendor master", "setup", "created", "requested", "confirmation"],
    "diya": ["vendor setup", "vendor master", "requested", "created", "confirmation"],
    "dilo": ["vendor setup", "vendor master", "requested", "created", "confirmation"],
    "dise": ["vendor setup", "vendor master", "requested", "created", "confirmation"],
    "about": ["case", "summary", "evidence", "explanation"],
}

# Single generic words like "clear" should not trigger a broad evidence dump.
# These terms are useful only after the question resolves to a vendor/person/invoice
# or after the user asks a fuller domain-specific question.
AMBIGUOUS_SINGLE_WORDS = {
    "clear", "process", "payment", "invoice", "receipt", "fraud", "case", "evidence",
    "approval", "approved", "support", "document", "vendor", "amount", "risk", "score",
}

DOMAIN_QUERY_TERMS = {
    "threshold", "skirting", "split", "duplicate", "receipt", "invoice", "missing",
    "phantom", "mismatch", "benford", "round", "zscore", "isolation", "forest",
    "approval", "approved", "clearance", "cleared", "supporting", "document",
    "vendor", "master", "setup", "payment", "amount", "risk", "score",
}

SETUP_QUERY_TERMS = {"id", "diya", "dilo", "dise", "setup", "created", "requested", "confirmation", "master"}

HEADER_FIELDS = ["FROM", "TO", "CC", "BCC", "PARTIES", "SUBJECT", "DATE", "BODY", "DOC_TYPE", "VENDOR_NAME", "INVOICE_NUMBER"]


def _load_saved_results(path: Path = RESULTS_PATH) -> Dict[str, object]:
    if not path.exists():
        return {"vendor_reports": [], "final_connected_summary": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"vendor_reports": [], "final_connected_summary": {}}


def _clean_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Convert "Name <email@domain>" -> "Name". If no name exists, keep email.
    m = re.match(r"\s*([^<]+?)\s*<[^>]+>\s*$", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().strip('"')
    # Remove accidental role suffixes.
    text = re.sub(r"\s+\([^)]*\)$", "", text).strip()
    return text


def _name_tokens(name: str) -> List[str]:
    base = _clean_name(name).lower()
    tokens = [t for t in re.split(r"[^a-z0-9@.]+", base) if len(t) >= 3 and t not in QUESTION_STOPWORDS]
    if "@" in base:
        tokens.append(base)
        local = base.split("@", 1)[0].replace(".", " ")
        tokens.extend(t for t in local.split() if len(t) >= 3)
    if base and "@" not in base:
        tokens.append(base)
    # de-duplicate while preserving order
    out = []
    for t in tokens:
        if t and t not in out:
            out.append(t)
    return out[:8]


def _question_keywords(question: str) -> List[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9@._-]+", str(question or ""))]
    kept = []
    for w in words:
        if len(w) < 2:
            continue
        if len(w) >= 3 and w not in QUESTION_STOPWORDS and w not in kept:
            kept.append(w)
        for extra in QUERY_EXPANSIONS.get(w, []):
            if extra not in kept:
                kept.append(extra)
    return kept[:28]

def _split_document_blocks(text: str) -> List[str]:
    raw = str(text or "").strip()
    if not raw or raw == "(no results)":
        return []
    pieces = re.split(r"(?=\n?\[DOCUMENT\s+\d+\])", raw)
    return [p.strip() for p in pieces if p.strip()]


def _field(block: str, field: str) -> str:
    pattern = rf"^{re.escape(field)}:\s*(.*)$"
    m = re.search(pattern, block, flags=re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else ""


def _body(block: str) -> str:
    m = re.search(r"^BODY:\s*\n?(.*)$", block, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _block_blob(block: str) -> str:
    return re.sub(r"\s+", " ", str(block or "")).strip()


def _extract_people_from_question(question: str, reports: Sequence[Dict[str, object]], final_summary: Dict[str, object]) -> List[str]:
    q_low = str(question or "").lower()
    known_people = []
    for report in reports:
        for person in report.get("people_involved", []) or []:
            name = _clean_name(str(person))
            if name and name not in known_people:
                known_people.append(name)
    for person in final_summary.get("shared_parties_across_high_risk_vendors", []) or []:
        name = _clean_name(str(person))
        if name and name not in known_people:
            known_people.append(name)

    matched = []
    for name in known_people:
        tokens = _name_tokens(name)
        # Match full name or strong token from the name.
        if name.lower() in q_low or any(tok in q_low for tok in tokens if len(tok) >= 4):
            matched.append(name)

    # If the user asks "he", "that person", etc., use the shared parties from final summary.
    pronounish = re.search(r"\b(he|him|his|she|her|that person|that guy|that man|that woman|they|them)\b", q_low)
    if not matched and pronounish:
        for person in final_summary.get("shared_parties_across_high_risk_vendors", []) or []:
            name = _clean_name(str(person))
            if name and name not in matched:
                matched.append(name)

    return matched[:6]


def _vendor_match_terms(vendor: str) -> List[str]:
    """Generate reusable, non-hardcoded matching terms for a vendor name."""
    raw = str(vendor or "").strip()
    low = raw.lower()
    terms = []
    if low:
        terms.append(low)
    words = [w for w in re.split(r"[^a-z0-9]+", low) if len(w) >= 3]
    meaningful = [w for w in words if w not in GENERIC_VENDOR_TOKENS and w not in QUESTION_STOPWORDS]
    # Partial questions like "meghna" or "apex" should resolve.
    terms.extend(meaningful)
    # Also allow distinctive first token if it is not generic.
    if words and words[0] not in GENERIC_VENDOR_TOKENS:
        terms.append(words[0])
    # Acronym support: Meghna Sand & Aggregates -> msa.
    acronym = "".join(w[0] for w in words if w and w not in GENERIC_VENDOR_TOKENS)
    if len(acronym) >= 2:
        terms.append(acronym)
    # De-duplicate while preserving order.
    out = []
    for t in terms:
        t = t.strip().lower()
        if t and t not in out:
            out.append(t)
    return out[:10]


def _contains_vendor(block_or_question: str, vendor: str) -> bool:
    low = str(block_or_question or "").lower()
    for term in _vendor_match_terms(vendor):
        if not term:
            continue
        if " " in term:
            if term in low:
                return True
        else:
            if re.search(rf"\b{re.escape(term)}\b", low):
                return True
    return False


def _extract_vendors_from_question(question: str, reports: Sequence[Dict[str, object]], final_summary: Dict[str, object]) -> List[str]:
    q_low = str(question or "").lower()
    vendors = []

    for report in reports:
        vendor = str(report.get("vendor_name", "")).strip()
        if vendor and _contains_vendor(q_low, vendor):
            vendors.append(vendor)

    # If the user says "this fraud", "the fraud", or "the scheme" with no vendor,
    # then use only high-risk vendors. Do NOT do this for random unresolved questions,
    # or every weird prompt turns into Apex/Swift/Strata evidence soup.
    if not vendors and re.search(r"\b(high[- ]risk|fraud network|fraud scheme|the fraud|that fraud|scheme|overall fraud|connected fraud)\b", q_low):
        for vendor in final_summary.get("high_risk_vendors", []) or []:
            if vendor and vendor not in vendors:
                vendors.append(str(vendor))

    return vendors[:8]

def _reports_relevant_to_question(
    question: str,
    reports: Sequence[Dict[str, object]],
    people: Sequence[str],
    vendors: Sequence[str],
) -> List[Dict[str, object]]:
    q_low = str(question or "").lower()
    selected = []
    for report in reports:
        vendor = str(report.get("vendor_name", "")).strip()
        report_people = [_clean_name(str(p)) for p in report.get("people_involved", []) or []]
        if vendor in vendors:
            selected.append(report)
            continue
        if any(p in report_people for p in people):
            selected.append(report)
            continue
        if vendor and vendor.lower() in q_low:
            selected.append(report)
            continue
    return selected[:8]


def _score_email_block(
    block: str,
    question_keywords: Sequence[str],
    people: Sequence[str],
    vendors: Sequence[str],
    high_risk_vendors: Sequence[str],
    require_vendor: bool = False,
) -> int:
    low = block.lower()
    score = 0

    if require_vendor and vendors and not any(_contains_vendor(block, vendor) for vendor in vendors):
        return 0

    for person in people:
        for tok in _name_tokens(person):
            if tok and tok in low:
                score += 12 if " " in tok or "@" in tok else 6

    for vendor in vendors:
        if _contains_vendor(block, vendor):
            score += 18

    # Only use high-risk vendors as a light ranking signal for truly broad fraud questions.
    # Never let this override a specific vendor/person question.
    if not vendors and not people:
        for vendor in high_risk_vendors:
            if vendor.lower() in low:
                score += 4

    for kw in question_keywords:
        kw_low = str(kw).lower()
        if not kw_low:
            continue
        if " " in kw_low:
            if kw_low in low:
                score += 4
        elif re.search(rf"\b{re.escape(kw_low)}\b", low):
            score += 2

    for term in GENERIC_FRAUD_TERMS:
        if term in low:
            score += 5

    if _field(block, "DOC_TYPE").lower() == "email":
        score += 2
    return score

def _select_relevant_emails(
    question: str,
    reports: Sequence[Dict[str, object]],
    final_summary: Dict[str, object],
    max_results: int = 8,
) -> List[str]:
    people = _extract_people_from_question(question, reports, final_summary)
    vendors = _extract_vendors_from_question(question, reports, final_summary)
    unresolved_terms = _possible_unresolved_entity_terms(question, people, vendors)
    keywords = _question_keywords(question)
    high_risk_vendors = [str(v) for v in final_summary.get("high_risk_vendors", []) or []]

    q_low = str(question or "").lower()
    broad_fraud_question = bool(re.search(r"\b(high[- ]risk|fraud network|fraud scheme|the fraud|that fraud|scheme|overall fraud|connected fraud)\b", q_low))
    require_vendor = bool(vendors)

    # Direct corpus scan gives reliable FROM/TO/PARTIES/BODY fields.
    email_text = list_all_formatted_documents("email", max_results=2000)
    blocks = _split_document_blocks(email_text)

    scored = []
    for block in blocks:
        if unresolved_terms:
            if _block_contains_all_terms(block, unresolved_terms):
                scored.append((50, block))
            continue
        score = _score_email_block(
            block,
            keywords,
            people,
            vendors,
            high_risk_vendors if broad_fraud_question else [],
            require_vendor=require_vendor,
        )
        if score > 0:
            scored.append((score, block))

    # If direct scan is sparse, semantic search acts as fallback. The query is now built
    # around the resolved entity first, not blindly around high-risk vendors.
    if len(scored) < 3:
        query_bits = [question, *people, *vendors]
        if unresolved_terms:
            query_bits.extend(unresolved_terms)
        if broad_fraud_question and not vendors and not people and not unresolved_terms:
            query_bits.extend(high_risk_vendors)
        query_bits.extend(["from to parties body approval payment invoice vendor setup document"])
        vector_text = search_document_vectors(" | ".join([b for b in query_bits if b]), n_results=12)
        for block in _split_document_blocks(vector_text):
            if unresolved_terms:
                if _block_contains_all_terms(block, unresolved_terms):
                    scored.append((45, block))
                continue
            score = _score_email_block(
                block,
                keywords,
                people,
                vendors,
                high_risk_vendors if broad_fraud_question else [],
                require_vendor=require_vendor,
            )
            if score > 0:
                scored.append((score, block))

    scored.sort(key=lambda x: x[0], reverse=True)

    # De-duplicate by subject+date+from.
    out = []
    seen = set()
    for _, block in scored:
        key = (
            _field(block, "DATE").lower(),
            _field(block, "FROM").lower(),
            _field(block, "TO").lower(),
            _field(block, "SUBJECT").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(block)
        if len(out) >= max_results:
            break
    return out

def _best_snippet(block: str, query_terms: Sequence[str], max_chars: int = 420) -> str:
    body = _body(block) or _block_blob(block)
    low = body.lower()
    terms = [t.lower() for t in list(query_terms) + GENERIC_FRAUD_TERMS if t]
    pos = -1
    for term in terms:
        idx = low.find(term)
        if idx >= 0:
            pos = idx
            break
    if pos < 0:
        return re.sub(r"\s+", " ", body[:max_chars]).strip()
    start = max(0, pos - max_chars // 3)
    end = min(len(body), start + max_chars)
    snippet = body[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(body):
        snippet += "..."
    return re.sub(r"\s+", " ", snippet).strip()


def _summarize_email_block(block: str, question_terms: Sequence[str]) -> Dict[str, str]:
    sender = _field(block, "FROM")
    to = _field(block, "TO")
    parties = _field(block, "PARTIES")
    subject = _field(block, "SUBJECT")
    date = _field(block, "DATE")
    body = _body(block)
    snippet = _best_snippet(block, question_terms)

    direction = ""
    if sender or to:
        direction = f"{sender or 'Unknown sender'} -> {to or 'Unknown recipient'}"

    return {
        "date": date,
        "from_to": direction,
        "subject": subject,
        "parties": parties,
        "relevant_excerpt": snippet,
    }


def _question_intent(question: str) -> str:
    q = str(question or "").lower()
    if any(t in q for t in ["innocent", "not fraud", "isn't fraud", "isnt fraud", "false positive", "legit", "legitimate", "clean"]):
        return "challenge_verdict"
    if any(t in q for t in ["tell me more", "more about", "about ", "details", "explain"]):
        return "explain_case"
    if re.search(r"\bwho\b", q):
        return "who"
    if any(t in q for t in ["related", "relation", "connected", "connect", "involved"]):
        return "relationship"
    return "general"



def _possible_unresolved_entity_terms(question: str, people: Sequence[str], vendors: Sequence[str]) -> List[str]:
    """Return user-supplied entity terms when no saved person/vendor was resolved.

    This prevents a made-up name like "Afnan Maheem" from triggering generic
    high-risk fraud emails just because those emails contain suspicious words.
    """
    if people or vendors:
        return []

    q = str(question or "").strip()
    q_low = q.lower()
    candidates = []

    patterns = [
        r"\bwho\s+(?:is|are|was|were)\s+(.+)$",
        r"\btell\s+me\s+(?:more\s+)?about\s+(.+)$",
        r"\bwhat\s+about\s+(.+)$",
        r"\bhow\s+(?:is|was|are|were)\s+(.+?)\s+(?:related|involved|connected)\b",
    ]
    for pat in patterns:
        m = re.search(pat, q_low)
        if m:
            candidates.append(m.group(1))

    if not candidates:
        return []

    raw = candidates[0]
    raw = re.sub(r"\b(to|with|in|on|for|about|the|this|that|fraud|case|payment|scheme|vendor)\b", " ", raw)
    raw = re.sub(r"[^a-z0-9@._ -]+", " ", raw)
    tokens = [t for t in re.split(r"\s+", raw.strip()) if len(t) >= 3 and t not in QUESTION_STOPWORDS]

    # Avoid treating vague phrases like "more details" as an unknown person.
    vague = {"more", "details", "case", "summary", "evidence", "fraud", "payment", "vendor"}
    tokens = [t for t in tokens if t not in vague]
    return list(dict.fromkeys(tokens))[:5]


def _block_contains_all_terms(block: str, terms: Sequence[str]) -> bool:
    low = str(block or "").lower()
    useful = [t.lower() for t in terms if len(str(t)) >= 3]
    if not useful:
        return False
    return all(re.search(rf"\b{re.escape(t)}\b", low) for t in useful)

def _sentence_join(items: Sequence[str], max_items: int = 5) -> str:
    vals = [str(x).strip() for x in items if str(x).strip()]
    vals = list(dict.fromkeys(vals))[:max_items]
    if not vals:
        return ""
    if len(vals) == 1:
        return vals[0]
    return ", ".join(vals[:-1]) + " and " + vals[-1]


def _relationship_answer_from_evidence(
    question: str,
    reports: Sequence[Dict[str, object]],
    final_summary: Dict[str, object],
    email_blocks: Sequence[str],
) -> Dict[str, object]:
    people = _extract_people_from_question(question, reports, final_summary)
    vendors = _extract_vendors_from_question(question, reports, final_summary)
    unresolved_terms = _possible_unresolved_entity_terms(question, people, vendors)
    relevant_reports = _reports_relevant_to_question(question, reports, people, vendors)
    keywords = _question_keywords(question)
    intent = _question_intent(question)

    vendors_from_reports = [str(r.get("vendor_name", "")).strip() for r in relevant_reports if r.get("vendor_name")]
    high_risk_set = set(final_summary.get("high_risk_vendors", []) or [])
    high_risk_overlap = [v for v in vendors_from_reports if v in high_risk_set]

    evidence_items = [_summarize_email_block(block, keywords + list(people) + list(vendors)) for block in email_blocks[:6]]

    named_people = list(people or [])
    if not named_people and intent in {"relationship", "general"} and final_summary.get("shared_parties_across_high_risk_vendors") and not vendors:
        named_people = [str(p) for p in final_summary.get("shared_parties_across_high_risk_vendors", [])[:3]]

    # Specific vendor questions should answer from that vendor's report first, even if it is a false positive.
    if vendors and relevant_reports:
        r = relevant_reports[0]
        vendor = str(r.get("vendor_name", vendors[0]))
        verdict = str(r.get("verdict", "unknown"))
        risk = r.get("risk_score", "unknown")
        interp = str(r.get("case_interpretation", "")).strip()
        summary = str(r.get("evidence_summary", "")).strip()
        report_people = [str(p) for p in r.get("people_involved", []) or []]

        if intent == "challenge_verdict":
            if verdict == "likely_fraud":
                direct = (
                    f"Based on the current evidence, {vendor} is not cleared as innocent. It is marked {verdict} with a risk score of {risk}. "
                    f"The main reason is: {interp or summary}. To overturn that, you would need clean source documents, approval records, and a business explanation that directly addresses the flagged payments."
                )
            elif verdict == "likely_false_positive":
                direct = (
                    f"Your instinct may be right for {vendor}. It is currently marked {verdict} with a risk score of {risk}, meaning the alert looked suspicious at first but the matched documents give a plausible explanation. "
                    f"The case interpretation says: {interp or summary}"
                )
            else:
                direct = (
                    f"{vendor} is currently {verdict} with a risk score of {risk}. The evidence is not clean enough to close either way. "
                    f"The main interpretation is: {interp or summary}"
                )
        elif intent == "explain_case":
            people_text = _sentence_join(report_people, max_items=4)
            direct = (
                f"{vendor} is marked {verdict} with a risk score of {risk}. {interp or summary}"
            )
            if people_text:
                direct += f" The main parties mentioned around this case are {people_text}."
        else:
            direct = (
                f"The question points to {vendor}. It is marked {verdict} with a risk score of {risk}. "
                f"{interp or summary}"
            )
    elif named_people and high_risk_overlap:
        direct = (
            f"{', '.join(named_people)} appear in the evidence around the high-risk vendor cases: {', '.join(high_risk_overlap)}. "
            "That makes them important because the same names show up around multiple flagged vendors, which suggests the issue may have moved through a shared approval, procurement, or payment-processing path rather than one isolated invoice."
        )
    elif named_people and vendors_from_reports:
        direct = (
            f"{', '.join(named_people)} appear in the case evidence for {', '.join(vendors_from_reports)}. "
            "The available reports and matched emails show involvement in the communication trail, but the specific role should be confirmed from approval logs and payment-release records."
        )
    elif unresolved_terms and evidence_items:
        direct = (
            f"I do not see {' '.join(unresolved_terms).title()} in the saved vendor reports or people lists, but the exact name appears in the email/document corpus. "
            "The excerpts below are the only matched evidence I found, so treat this as a search hit rather than a confirmed case relationship."
        )
    elif unresolved_terms:
        direct = (
            f"I could not find {' '.join(unresolved_terms).title()} in the saved case reports, people lists, or matched email evidence. "
            "Based on the available investigation data, that name is not connected to the fraud case."
        )
    elif evidence_items:
        direct = (
            "I could not confidently map the question to a saved vendor or person, but I found related email evidence. "
            "Use the excerpts below to identify the sender, recipient, and payment context."
        )
    else:
        direct = (
            "I could not confidently resolve a specific person or vendor from the question. "
            "Try naming the vendor or person directly, for example: 'Tell me more about Meghna' or 'How is Tariq related to Apex?'"
        )

    if evidence_items:
        direct += " The most relevant email evidence is listed below."
    elif unresolved_terms:
        direct += " No email excerpt was returned because there was no exact evidence match for that name."
    else:
        direct += " I did not find a strong matching email excerpt, so this answer relies mainly on the saved case reports."

    return {
        "question": question,
        "direct_answer": direct,
        "related_vendors": list(dict.fromkeys(vendors_from_reports or vendors)),
        "related_people": list(dict.fromkeys(named_people)),
        "email_evidence": evidence_items,
        "case_report_context": [
            {
                "vendor_name": r.get("vendor_name"),
                "verdict": r.get("verdict"),
                "risk_score": r.get("risk_score"),
                "case_interpretation": r.get("case_interpretation"),
                "people_involved": r.get("people_involved", []),
            }
            for r in relevant_reports[:5]
        ],
        "next_check": [
            "Open the full email threads for the listed excerpts.",
            "Compare those email timestamps against the flagged payment dates.",
            "Check approval logs to confirm who requested, approved, and released the payments.",
        ],
    }


def _is_broad_fraud_question(question: str) -> bool:
    q_low = str(question or "").lower()
    return bool(re.search(r"\b(high[- ]risk|fraud network|fraud scheme|the fraud|that fraud|scheme|overall fraud|connected fraud)\b", q_low))


def _is_setup_or_vendor_master_question(question: str) -> bool:
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9@._-]+", str(question or ""))}
    return bool(words & SETUP_QUERY_TERMS)


def _is_domain_specific_question(question: str, keywords: Sequence[str]) -> bool:
    q_low = str(question or "").lower()
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9@._-]+", q_low)}

    # Needs at least two useful words unless it is a known multi-word forensic phrase.
    useful = [w for w in words if len(w) >= 3 and w not in QUESTION_STOPWORDS]
    has_domain_term = bool(words & DOMAIN_QUERY_TERMS or any(str(k).lower() in DOMAIN_QUERY_TERMS for k in keywords))
    has_multiword_forensic_phrase = bool(re.search(
        r"\b(missing receipt|duplicate receipt|invoice mismatch|threshold skirting|vendor master|vendor setup|benford|round numbers|approval trail|payment release)\b",
        q_low,
    ))
    return has_multiword_forensic_phrase or (has_domain_term and len(useful) >= 2)


def _is_too_ambiguous_for_evidence_search(
    question: str,
    people: Sequence[str],
    vendors: Sequence[str],
    unresolved_terms: Sequence[str],
    keywords: Sequence[str],
) -> bool:
    """Block random one-word prompts from returning generic fraud evidence.

    If the question does not resolve to a person/vendor/unknown entity and is not a
    clear broad-fraud or domain-specific query, the safest answer is no match.
    """
    if people or vendors or unresolved_terms:
        return False
    if _is_broad_fraud_question(question) or _is_setup_or_vendor_master_question(question):
        return False
    if _is_domain_specific_question(question, keywords):
        return False

    raw_words = [w.lower() for w in re.findall(r"[A-Za-z0-9@._-]+", str(question or ""))]
    useful = [w for w in raw_words if len(w) >= 3 and w not in QUESTION_STOPWORDS]
    if len(useful) <= 1:
        return True
    if all(w in AMBIGUOUS_SINGLE_WORDS for w in useful):
        return True
    return True


def _no_match_answer(question: str) -> Dict[str, object]:
    return {
        "question": question,
        "direct_answer": (
            "I could not find a specific matching person, vendor, invoice, or case topic for that question. "
            "I did not return email excerpts because the prompt is too vague and would risk pulling unrelated evidence. "
            "Ask with a name, vendor, invoice number, or a specific topic such as 'Meghna', 'Tariq', 'APEX-26018', or 'missing receipt'."
        ),
        "related_vendors": [],
        "related_people": [],
        "email_evidence": [],
        "case_report_context": [],
        "next_check": [],
    }

def _try_groq_polish(question: str, draft: Dict[str, object]) -> Dict[str, object]:
    if not USE_GROQ_QA or not get_groq_api_key or not post_with_retry:
        return draft
    api_key = get_groq_api_key()
    if not api_key:
        return draft

    system = (
        "You answer follow-up questions for a fraud investigation. Use only the supplied JSON evidence. "
        "Do not invent names, emails, quotes, or relationships. Keep the same JSON keys. "
        "Make direct_answer clearer and more specific. Keep email_evidence short."
    )
    body = {
        "model": GROQ_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"question": question, "draft": draft}, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        response = post_with_retry(f"{GROQ_URL}/chat/completions", headers, body, "Groq follow-up QA")
        content = response.json()["choices"][0]["message"].get("content", "")
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else draft
    except Exception:
        return draft


def answer_followup_question(
    question: str,
    reports: Optional[Sequence[Dict[str, object]]] = None,
    final_summary: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Answer a direct follow-up question using saved reports + matched email evidence."""
    saved = _load_saved_results()
    reports = list(reports if reports is not None else saved.get("vendor_reports", []) or [])
    final_summary = dict(final_summary if final_summary is not None else saved.get("final_connected_summary", {}) or {})

    people = _extract_people_from_question(question, reports, final_summary)
    vendors = _extract_vendors_from_question(question, reports, final_summary)
    unresolved_terms = _possible_unresolved_entity_terms(question, people, vendors)
    keywords = _question_keywords(question)

    if _is_too_ambiguous_for_evidence_search(question, people, vendors, unresolved_terms, keywords):
        return _no_match_answer(question)

    email_blocks = _select_relevant_emails(question, reports, final_summary, max_results=8)
    draft = _relationship_answer_from_evidence(question, reports, final_summary, email_blocks)
    return _try_groq_polish(question, draft)


def interactive_followup_loop(
    reports: Optional[Sequence[Dict[str, object]]] = None,
    final_summary: Optional[Dict[str, object]] = None,
) -> None:
    print("\n========= ASK FOLLOW-UP QUESTIONS =========")
    print("Type a direct question like: How is Rafiq related to the fraud? Press Enter to exit.")
    while True:
        try:
            question = input("\nFollow-up question> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            break
        answer = answer_followup_question(question, reports=reports, final_summary=final_summary)
        print("\n========= FOLLOW-UP ANSWER =========")
        print(json.dumps(answer, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    interactive_followup_loop()
