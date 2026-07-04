import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from sqlalchemy import create_engine

from storage.db import DB_PATH as SQLITE_DB_PATH

try:
    from storage.vector_store import search_documents as vector_search_documents
except Exception:  # pragma: no cover
    vector_search_documents = None

try:
    from storage.vector_store import list_documents as vector_list_documents
except Exception:  # pragma: no cover
    vector_list_documents = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_path(env_var: str, default: Path) -> Path:
    raw = os.getenv(env_var)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path
    return default


engine = create_engine(f"sqlite:///{SQLITE_DB_PATH.as_posix()}")



# SQL helpers

def _normalize_sql_query(query: str) -> str:
    cleaned = str(query).strip()
    if not cleaned:
        return cleaned

    # Friendly aliases for human/LLM generated queries.
    cleaned = re.sub(r"\bvendor\b", "vendor_name", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\binvoice\b", "invoice_number", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\breference\b", "reference_number", cleaned, flags=re.IGNORECASE)
    return cleaned


def _stringify_dataframe(df: pd.DataFrame) -> str:
    if df.empty:
        return "(no rows)"
    return df.to_string(index=False)


def query_sql_ledger(query: str) -> str:
    """Execute a read-only SQL query against the fraud ledger and return a text table."""
    if not query or not str(query).strip():
        return "SQL Error: empty query"

    cleaned_query = _normalize_sql_query(query)
    lowered = cleaned_query.lower().strip()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return "SQL Error: only SELECT/WITH queries are allowed"

    try:
        return _stringify_dataframe(pd.read_sql(cleaned_query, engine))
    except Exception as exc:
        return f"SQL Error: {exc}"


def query_sql_dataframe(query: str) -> pd.DataFrame:
    """Execute a read-only SQL query and return a DataFrame."""
    cleaned_query = _normalize_sql_query(query)
    lowered = cleaned_query.lower().strip()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("only SELECT/WITH queries are allowed")
    return pd.read_sql(cleaned_query, engine)



# Generic text / document helpers

def _join_csvish(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value if str(v).strip())
    return str(value)


def _flatten_dict(prefix: str, value: Dict[str, Any], out: Dict[str, Any]) -> None:
    for key, val in value.items():
        new_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict):
            _flatten_dict(new_key, val, out)
        else:
            out[new_key] = val


def _get_any(item: Dict[str, Any], keys: Iterable[str]) -> Any:
    flattened: Dict[str, Any] = {}
    _flatten_dict("", item, flattened)
    lowered = {k.lower(): v for k, v in flattened.items()}

    for key in keys:
        key_low = key.lower()
        if key_low in lowered and lowered[key_low] not in (None, ""):
            return lowered[key_low]

    # suffix match for shapes like metadata.from, email.headers.from, etc.
    for key in keys:
        key_low = key.lower()
        for actual_key, value in lowered.items():
            if actual_key.endswith("." + key_low) and value not in (None, ""):
                return value
    return ""


def _extract_result_text(item: Dict[str, Any]) -> str:
    value = _get_any(
        item,
        [
            "body",
            "content",
            "text",
            "document",
            "page_content",
            "snippet",
            "raw_text",
            "raw_body",
            "email_body",
            "metadata.body",
            "metadata.content",
            "metadata.raw_body",
            "metadata.email_body",
        ],
    )
    return str(value or "")


def _result_title(item: Dict[str, Any], idx: int) -> str:
    return str(_get_any(item, ["filename", "source", "title", "id", "metadata.id", "metadata.source"]) or f"result_{idx}")


def _format_single_result(item: Dict[str, Any], idx: int) -> str:
    """Format a document as a rigid metadata block, regardless of its original wrapper shape."""
    doc_type = _get_any(item, ["doc_type", "metadata.doc_type"]) or "unknown"
    vendor = _get_any(item, ["vendor_name", "metadata.vendor_name"])
    invoice = _get_any(item, ["invoice_number", "metadata.invoice_number"])
    date = _get_any(item, ["date", "metadata.date"])
    sender = _get_any(item, ["from", "sender", "metadata.from", "metadata.sender"])
    to = _get_any(item, ["to", "recipient", "metadata.to", "metadata.recipient"])
    cc = _get_any(item, ["cc", "metadata.cc"])
    bcc = _get_any(item, ["bcc", "metadata.bcc"])
    parties = _get_any(item, ["parties", "metadata.parties"])
    subject = _get_any(item, ["subject", "metadata.subject"])
    amount = _get_any(item, ["amount_bdt", "amount", "metadata.amount_bdt", "metadata.amount"])
    text = _extract_result_text(item)

    lines = [
        f"[DOCUMENT {idx}]",
        f"TITLE: {_result_title(item, idx)}",
        f"DOC_TYPE: {doc_type}",
    ]
    if vendor:
        lines.append(f"VENDOR_NAME: {_join_csvish(vendor)}")
    if invoice:
        lines.append(f"INVOICE_NUMBER: {_join_csvish(invoice)}")
    if amount:
        lines.append(f"AMOUNT_BDT: {_join_csvish(amount)}")
    if date:
        lines.append(f"DATE: {_join_csvish(date)}")
    if sender:
        lines.append(f"FROM: {_join_csvish(sender)}")
    if to:
        lines.append(f"TO: {_join_csvish(to)}")
    if cc:
        lines.append(f"CC: {_join_csvish(cc)}")
    if bcc:
        lines.append(f"BCC: {_join_csvish(bcc)}")
    if parties:
        lines.append(f"PARTIES: {_join_csvish(parties)}")
    if subject:
        lines.append(f"SUBJECT: {_join_csvish(subject)}")
    if text:
        lines.append("BODY:")
        lines.append(str(text))
    return "\n".join(lines).strip()


def _unwrap_vector_payload(payload: Any) -> List[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "matches", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value

        # Chroma-style parallel arrays.
        docs = payload.get("documents")
        metas = payload.get("metadatas") or payload.get("metadata")
        ids = payload.get("ids")
        if isinstance(docs, list):
            if docs and isinstance(docs[0], list):
                docs = docs[0]
            if isinstance(metas, list) and metas and isinstance(metas[0], list):
                metas = metas[0]
            if isinstance(ids, list) and ids and isinstance(ids[0], list):
                ids = ids[0]
            rows = []
            for i, doc in enumerate(docs):
                meta = metas[i] if isinstance(metas, list) and i < len(metas) and isinstance(metas[i], dict) else {}
                row = dict(meta)
                row["body"] = doc
                if isinstance(ids, list) and i < len(ids):
                    row["id"] = ids[i]
                rows.append(row)
            return rows
    return [payload]


def stringify_vector_results(payload: Any) -> str:
    if isinstance(payload, str):
        return payload

    rows = _unwrap_vector_payload(payload)
    if not rows:
        return "(no results)"

    formatted = []
    for idx, row in enumerate(rows, start=1):
        if isinstance(row, dict):
            formatted.append(_format_single_result(row, idx))
        else:
            formatted.append(f"[DOCUMENT {idx}]\nBODY:\n{row}")
    return "\n\n".join(formatted).strip()


def search_document_vectors(semantic_query: str, n_results: int = 8) -> str:
    """Search the document index for semantically relevant emails and receipts."""
    if vector_search_documents is None:
        return "Vector DB Error: search_documents is not available from storage.vector_store"
    try:
        payload = vector_search_documents(query_text=semantic_query, n_results=n_results)
        return stringify_vector_results(payload)
    except Exception as exc:
        return f"Vector DB Error: {exc}"


def search_documents(query_text: str, n_results: int = 8) -> str:
    return search_document_vectors(query_text, n_results=n_results)



# Direct Chroma/list_documents helpers

LEGAL_OR_GENERIC_VENDOR_WORDS = {
    "company", "co", "corp", "corporation", "inc", "llc", "ltd", "limited", "holdings",
    "services", "service", "trading", "supply", "supplies", "enterprise", "enterprises",
    "group", "bd", "bangladesh", "the", "and", "for", "with", "vendor", "invoice",
    # common industry/category words that are too broad as standalone vendor anchors
    "cargo", "consult", "consulting", "rental", "equipment", "cement", "steel", "safety",
    "gear", "sand", "aggregates", "survey", "office", "lease", "electrical", "hardware",
    "transport", "stationery", "printing", "security", "labour", "labor", "tiles", "timber",
}


def _normalize_blob(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _significant_vendor_tokens(vendor_name: str) -> List[str]:
    base = str(vendor_name or "").lower()
    raw_tokens = re.split(r"[^a-z0-9]+", base)
    tokens = [t for t in raw_tokens if len(t) >= 4 and t not in LEGAL_OR_GENERIC_VENDOR_WORDS]
    return list(dict.fromkeys(tokens))


def vendor_terms(vendor_name: str, invoice_numbers: Optional[Iterable[str]] = None) -> List[str]:
    """Derive reusable vendor anchors from the vendor name and invoice prefixes. No dataset-specific aliases."""
    base = str(vendor_name or "").strip().lower()
    terms = []
    if base:
        terms.append(base)
        terms.append(base.replace("&", "and"))
        compact = re.sub(r"[^a-z0-9]+", "", base)
        if len(compact) >= 6:
            terms.append(compact)
    terms.extend(_significant_vendor_tokens(vendor_name)[:4])

    # Invoice prefixes are often the strongest general anchor: ABC-26001 -> abc-
    for inv in invoice_numbers or []:
        m = re.match(r"\s*([A-Za-z]{2,10})[-_\s]*\d+", str(inv))
        if m:
            terms.append(m.group(1).lower() + "-")
            terms.append(m.group(1).lower())

    # De-duplicate and remove dangerously broad terms.
    clean = []
    for term in terms:
        t = str(term).strip().lower()
        if not t or t in LEGAL_OR_GENERIC_VENDOR_WORDS:
            continue
        if len(t) < 3:
            continue
        if t not in clean:
            clean.append(t)
    return clean


def _document_doc_type(item: Dict[str, Any]) -> str:
    return str(_get_any(item, ["doc_type", "metadata.doc_type"]) or "").strip().lower()


def _document_search_blob(item: Dict[str, Any]) -> str:
    flattened: Dict[str, Any] = {}
    _flatten_dict("", item, flattened)
    parts = []
    for key, value in flattened.items():
        if key.lower() in {"embedding", "embeddings"}:
            continue
        parts.append(str(value))
    return "\n".join(parts)


def _coerce_list_document(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        row = dict(item)
        meta = row.get("metadata")
        if isinstance(meta, dict):
            merged = dict(meta)
            for key in ("id", "document", "text", "body", "content", "page_content", "raw_text", "source", "filename"):
                if key in row and key not in merged:
                    merged[key] = row[key]
            merged["metadata"] = meta
            return merged
        return row
    return {"body": str(item)}


def _doc_matches_vendor_or_invoice(item: Dict[str, Any], vendor_name: str, invoices: Optional[Iterable[str]] = None) -> bool:
    blob = _normalize_blob(_document_search_blob(item))
    invs = [str(i).strip().lower() for i in (invoices or []) if str(i).strip()]
    if any(inv in blob for inv in invs):
        return True
    terms = vendor_terms(vendor_name, invs)
    # Require full vendor or at least one significant token. Exact invoices are preferred by score.
    return any(term in blob for term in terms)


def _score_doc_relevance(item: Dict[str, Any], vendor_name: str, invoices: Optional[Iterable[str]] = None) -> int:
    blob = _normalize_blob(_document_search_blob(item))
    score = 0
    invs = [str(i).strip().lower() for i in (invoices or []) if str(i).strip()]
    for inv in invs:
        if inv in blob:
            score += 12
    terms = vendor_terms(vendor_name, invs)
    for term in terms:
        if term in blob:
            score += 4 if len(term) >= 5 else 2
    if _document_doc_type(item) == "email":
        score += 1
    for field, bonus in (("from", 3), ("to", 2), ("parties", 2), ("subject", 1)):
        if _get_any(item, [field, f"metadata.{field}"]):
            score += bonus
    return score


def get_vendor_documents(vendor_name: str, invoices: Optional[Iterable[str]] = None, doc_type: Optional[str] = None, max_results: int = 10) -> str:
    """
    Pull documents directly from list_documents() and format metadata. This is generic:
    matching uses vendor name tokens and invoice numbers, not hand-written aliases.
    """
    if vector_list_documents is None:
        return "(list_documents unavailable)"

    try:
        try:
            raw_docs = vector_list_documents(doc_type) if doc_type else vector_list_documents()
        except TypeError:
            raw_docs = vector_list_documents()
    except Exception as exc:
        return f"Document Listing Error: {exc}"

    rows = [_coerce_list_document(doc) for doc in (raw_docs or [])]
    wanted_type = str(doc_type or "").strip().lower()
    if wanted_type:
        rows = [row for row in rows if _document_doc_type(row) == wanted_type]

    matched = [row for row in rows if _doc_matches_vendor_or_invoice(row, vendor_name, invoices)]
    matched.sort(key=lambda row: _score_doc_relevance(row, vendor_name, invoices), reverse=True)

    if not matched:
        return "(no results)"
    return "\n\n".join(_format_single_result(row, idx) for idx, row in enumerate(matched[:max_results], start=1)).strip()


def list_all_formatted_documents(doc_type: Optional[str] = None, max_results: int = 1000) -> str:
    """Return formatted documents for corpus-level inference, capped to avoid printing the universe."""
    if vector_list_documents is None:
        return "(list_documents unavailable)"
    try:
        try:
            raw_docs = vector_list_documents(doc_type) if doc_type else vector_list_documents()
        except TypeError:
            raw_docs = vector_list_documents()
    except Exception as exc:
        return f"Document Listing Error: {exc}"

    rows = [_coerce_list_document(doc) for doc in (raw_docs or [])]
    wanted_type = str(doc_type or "").strip().lower()
    if wanted_type:
        rows = [row for row in rows if _document_doc_type(row) == wanted_type]
    return "\n\n".join(_format_single_result(row, idx) for idx, row in enumerate(rows[:max_results], start=1)).strip() or "(no results)"



# Optional registry support

def _load_registry_rows() -> List[str]:
    paths = []
    env_path = os.getenv("VENDOR_REGISTRY_CSV")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        paths.append(path)
    paths.extend([PROJECT_ROOT / "data" / "vendor_registry.csv", PROJECT_ROOT / "vendor_registry.csv"])

    for path in paths:
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if df.empty:
            continue
        name_col = None
        for col in df.columns:
            if str(col).strip().lower() in {"vendor", "vendor_name", "name", "company", "supplier"}:
                name_col = col
                break
        if name_col is None:
            name_col = df.columns[0]
        return [str(v).strip().lower() for v in df[name_col].dropna().tolist() if str(v).strip()]
    return []


def verify_corporate_registry(vendor_name: str) -> str:
    """
    Generic registry check. Uses VENDOR_REGISTRY_CSV or data/vendor_registry.csv when present.
    If no registry file exists, returns UNKNOWN instead of pretending we have a magic list.
    """
    registry = _load_registry_rows()
    if not registry:
        return "UNKNOWN: no vendor registry source configured. Registry status was not used as proof."

    target = str(vendor_name or "").strip().lower()
    if target in registry:
        return f"SUCCESS: '{vendor_name}' appears in the configured vendor registry."
    return f"ALERT: '{vendor_name}' does not appear in the configured vendor registry snapshot. This alone is not proof of fraud."


FORENSIC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_sql_ledger",
            "description": "Execute a read-only SQL query against the fraud ledger.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A SQL SELECT or WITH query to run against the transactions or flags tables.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search internal emails and receipts using semantic similarity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {"type": "string", "description": "Natural-language search query for emails and receipts."},
                    "n_results": {"type": "integer", "description": "How many matching documents to return.", "default": 8},
                },
                "required": ["query_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_corporate_registry",
            "description": "Check whether a vendor appears in a configured vendor registry CSV, if available.",
            "parameters": {
                "type": "object",
                "properties": {"vendor_name": {"type": "string", "description": "Vendor name to validate."}},
                "required": ["vendor_name"],
            },
        },
    },
]
