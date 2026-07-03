import re

import pandas as pd
from sqlalchemy import create_engine

from storage.vector_store import search_documents as vector_search_documents

engine = create_engine("sqlite:///data/fraud.sqlite3")


def _normalize_sql_query(query: str) -> str:
    cleaned = str(query).strip()
    if not cleaned:
        return cleaned

    cleaned = re.sub(r"\bvendor\b", "vendor_name", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\binvoice\b", "invoice_number", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\breference\b", "reference_number", cleaned, flags=re.IGNORECASE)
    return cleaned


def query_sql_ledger(query: str) -> str:
    """Execute a read-only SQL query against the financial ledger and return the result table."""
    if not query or not str(query).strip():
        return "SQL Error: empty query"

    cleaned_query = _normalize_sql_query(query)
    if not (cleaned_query.lower().startswith("select") or cleaned_query.lower().startswith("with")):
        return "SQL Error: only SELECT/WITH queries are allowed"

    try:
        df = pd.read_sql(cleaned_query, engine)
        return df.to_string(index=False)
    except Exception as exc:
        return f"SQL Error: {exc}"


def search_document_vectors(semantic_query: str, n_results: int = 2) -> str:
    """Search the document index for semantically relevant emails and receipts."""
    try:
        return vector_search_documents(query_text=semantic_query, n_results=n_results)
    except Exception as exc:
        return f"Vector DB Error: {exc}"


def verify_corporate_registry(vendor_name: str) -> str:
    """Check whether the supplier appears in a local registry of verified corporations."""
    verified_vendors = ["apex supply co", "global logistics corp", "steakhouse inc"]
    if vendor_name and vendor_name.lower() in verified_vendors:
        return f"SUCCESS: '{vendor_name}' is officially registered as an active corporation."
    return f"ALERT: '{vendor_name}' could not be found in the registry. High risk of shell or lookalike company."


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
                        "description": "A SQL SELECT or WITH query to run against the transactions table.",
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
                    "query_text": {
                        "type": "string",
                        "description": "Natural-language search query for emails and receipts.",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "How many matching documents to return.",
                        "default": 3,
                    },
                },
                "required": ["query_text"],
            },
        },
    },
]

