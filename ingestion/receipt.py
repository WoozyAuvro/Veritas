
import hashlib
from pathlib import Path 
import pdfplumber

from storage.vector_store import ask_llm_for_json, save_document
 
 
def ingest_receipt(file_path):
    from dateutil import parser as dateparser

    file_path = Path(file_path)
    text = read_receipt_text(file_path)
    metadata = extract_receipt_info(text, file_path)

    # date saves the date properly into smth we can read
    # date_unix is what we need invoice_matching.py

    if metadata.get("date"):
        try:
            metadata["date_unix"] = int(dateparser.parse(str(metadata["date"])).timestamp())
        except:
            metadata["date_unix"] = 0

    doc_id = make_id("receipt", file_path, text)
    metadata["id"] = doc_id
    metadata["doc_type"] = "receipt"

    save_document(doc_id, text, metadata)
    return metadata


def read_receipt_text(file_path):
    if file_path.suffix == ".txt":
        return file_path.read_text(encoding="utf-8", errors="replace")
 
    if file_path.suffix == ".pdf":
        with pdfplumber.open(file_path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n".join(pages).strip()
 
    raise ValueError(f"can't read this file type: {file_path.suffix}")
 
 
def extract_receipt_info(text, file_path):
    
    system_prompt = (
        "Extract receipt metadata for fraud analysis. Return strict JSON only — "
        "a single JSON object, never a list or array. "
        "Use null for unknown values. Amount must be numeric BDT when possible."
    )
    user_prompt = (
        "Extract this receipt into a single JSON object with exactly these keys: "
        "date (YYYY-MM-DD format), amount_bdt, vendor_name, content, invoice_number. "
        "Return one JSON object, not a list.\n\n"
        f"Filename: {file_path.name}\n\nReceipt text:\n{text[:12000]}"
    )
 
    info = ask_llm_for_json(system_prompt, user_prompt)
 
    # unwrap if it  returns a list
    if isinstance(info, list):
        if len(info) == 0:
            raise ValueError("LLM returned empty list instead of JSON")
        info = info[0]
 
    return clean_metadata(info)
 
# same id will generate for the same file always, no duplicates
def make_id(prefix, file_path, text):
    
    digest = hashlib.sha256(f"{prefix}:{file_path.name}:{text}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"
 
# sometimes it returned it weirdly
def clean_metadata(metadata):
    
    cleaned = {}
    for key, value in metadata.items():
        if value is None:
            cleaned[key] = ""
        elif isinstance(value, list):
            
            cleaned[key] = ", ".join(str(item) for item in value)
        elif isinstance(value, dict):
            cleaned[key] = str(value)
        else:
            cleaned[key] = value
    return cleaned
 
 
if __name__ == "__main__":
    import sys
    import json
 
    result = ingest_receipt(sys.argv[1])
    print(json.dumps(result, indent=2))