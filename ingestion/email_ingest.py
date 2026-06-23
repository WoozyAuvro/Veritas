
from email import policy
from email.parser import BytesParser
from pathlib import Path
 
from ingestion.receipt import make_id, clean_metadata  # reuse the same helpers
from storage.vector_store import ask_llm_for_json, save_document
 
 
def ingest_email(file_path):
    from dateutil import parser as dateparser

    file_path = Path(file_path)
    text, headers = read_email_text(file_path)
    metadata = extract_email_info(text, file_path, headers)

    # date saves the date properly into smth we can read
    # date_unix is what we need invoice_matching.py because chroma cant compute dates otherwise

    if metadata.get("date"):
        try:
            metadata["date_unix"] = int(dateparser.parse(str(metadata["date"])).timestamp())
        except: # this exists if date somehow cant be parsed the entire thing wont crash
            metadata["date_unix"] = 0

    doc_id = make_id("email", file_path, text)
    metadata["id"] = doc_id
    metadata["doc_type"] = "email"

    save_document(doc_id, text, metadata)
    return metadata
 
 
def read_email_text(file_path):
    
    if file_path.suffix == ".txt":
        return file_path.read_text(encoding="utf-8", errors="replace"), {}
 
    if file_path.suffix == ".eml":
        with file_path.open("rb") as f:
            message = BytesParser(policy=policy.default).parse(f)
 
        headers = {
            "date": str(message.get("date", "")),
            "from": str(message.get("from", "")),
            "to": str(message.get("to", "")),
            "subject": str(message.get("subject", "")),
        }
        return get_email_body(message), headers
 
    raise ValueError(f"Can't read this file type: {file_path.suffix}")
 
def get_email_body(message):
    # automatically finds the 'text/plain' body
    body_part = message.get_body(preferencelist=("plain", "html"))   
    if body_part:
        return body_part.get_content().strip()
    
    return ""
 
 
def extract_email_info(text, file_path, headers):
    
    system_prompt = (
        "Extract email metadata for fraud analysis. Return strict JSON only — "
        "a single JSON object, never a list or array. "
        "Use null for unknown values. Keep body concise but faithful."
    )
    user_prompt = (
        "Extract this email into a single JSON object with exactly these keys: "
        "date, from, to, subject, parties, body. "
        "Parties means other companies or people mentioned besides the sender and recipient. "
        "Return one JSON object, not a list.\n\n"
        f"Parsed headers (use these if the email text doesn't repeat them): {headers}\n\n"
        f"Filename: {file_path.name}\n\nEmail text:\n{text[:12000]}"
    )
 
    info = ask_llm_for_json(system_prompt, user_prompt)
 
    # unwrap if it returns a list for some reason
    if isinstance(info, list):
        if len(info) == 0:
            raise ValueError("LLM returned an empty list instead of a JSON object")
        info = info[0]
    # gets header
    for key, value in headers.items():
        info.setdefault(key, value)
 
    return clean_metadata(info)
 
 
if __name__ == "__main__":
    import sys
    import json
 
    result = ingest_email(sys.argv[1])
    print(json.dumps(result, indent=2))