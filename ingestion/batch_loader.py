
import time
from pathlib import Path

from ingestion.receipt import ingest_receipt
from ingestion.email_ingest import ingest_email

DELAY_BETWEEN_FILES = 1

# for wrong file extensions
class UnsupportedFileError(Exception):
    pass


def load_receipt_folder(folder_path):
    return load_folder(folder_path, ingest_receipt, allowed_extensions=(".pdf", ".txt"))


def load_email_folder(folder_path):
    return load_folder(folder_path, ingest_email, allowed_extensions=(".eml", ".txt"))


def load_folder(folder_path, ingest_function, allowed_extensions):

    # loads all folder AND subfolders
    # returns a summary so a frontend can show progress
    
    folder_path = Path(folder_path)
    if not folder_path.is_dir():
        raise ValueError(f"Not a folder: {folder_path}")

    files = [f for f in folder_path.rglob("*") if f.is_file()]

    results = {
        "total_files": len(files),
        "succeeded": [],
        "failed": [],
        "skipped": [],
    }

    for index, file_path in enumerate(files):
        try:
            if file_path.suffix.lower() not in allowed_extensions:
                raise UnsupportedFileError(f"Unsupported file type: {file_path.suffix}")

            metadata = ingest_function(file_path)
            results["succeeded"].append({"filename": file_path.name, "id": metadata.get("id")})
            print(f"[ok] {file_path.name}")

        except UnsupportedFileError as e:
            results["skipped"].append({"filename": file_path.name, "reason": str(e)})
            print(f"[skip] {file_path.name}: {e}")

        except Exception as e:
            results["failed"].append({"filename": file_path.name, "reason": str(e)})
            print(f"[fail] {file_path.name}: {e}")

        is_last_file = index == len(files) - 1
        if not is_last_file:
            time.sleep(DELAY_BETWEEN_FILES)

    return results


if __name__ == "__main__":
    import sys
    import json

    doc_type = sys.argv[1]   # "receipt" or "email"
    folder = sys.argv[2]

    if doc_type == "receipt":
        summary = load_receipt_folder(folder)
    elif doc_type == "email":
        summary = load_email_folder(folder)
    else:
        raise ValueError('wrong doctype')

    print(json.dumps(summary, indent=2))