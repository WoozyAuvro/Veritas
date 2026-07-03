import json
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
UPLOAD_ROOT = DATA_DIR / "uploads"

import sys
sys.path.append(str(ROOT_DIR))

from ingestion.bank_statement import ingest_bank_statement
from ingestion.batch_loader import load_email_folder, load_receipt_folder
from detection.run_engines import run_engines
from agent_execution import execute_agent_investigation


class PipelineJob:
    def __init__(self, job_id: str):
        self.id = job_id
        self.status = "queued"
        self.logs: List[str] = []
        self.result: Dict[str, object] = {}
        self.error: Optional[str] = None

    def add_log(self, message: str) -> None:
        self.logs.append(message)


JOBS: Dict[str, PipelineJob] = {}


def _ensure_upload_dir() -> Path:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    return UPLOAD_ROOT


def _save_upload(file_obj, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        handle.write(file_obj.file.read())
    return destination


def _build_demo_paths() -> Dict[str, Optional[Path]]:
    return {
        "bank_statement": ROOT_DIR / "data" / "sample_bank_statement.csv",
        "receipts": ROOT_DIR / "receipts_folder",
        "emails": ROOT_DIR / "email_folder",
    }


def start_pipeline(files_by_type: Dict[str, object], use_demo: bool = False) -> PipelineJob:
    job = PipelineJob(str(uuid.uuid4()))
    JOBS[job.id] = job
    thread = threading.Thread(target=_run_pipeline, args=(job, files_by_type, use_demo), daemon=True)
    thread.start()
    return job


def _run_pipeline(job: PipelineJob, files_by_type: Dict[str, object], use_demo: bool) -> None:
    try:
        job.status = "running"
        job.add_log("Pipeline started")

        if use_demo:
            job.add_log("Using demo files from the repository sample folders")
            paths = _build_demo_paths()
        else:
            job.add_log("Saving uploaded files to the local workspace")
            upload_dir = _ensure_upload_dir()
            paths = {}
            for kind, item in files_by_type.items():
                if not item:
                    continue
                target_path = upload_dir / item["filename"]
                _save_upload(item["file"], target_path)
                paths[kind] = target_path

        bank_statement_path = paths.get("bank_statement") if "bank_statement" in paths else None
        receipts_path = paths.get("receipts") if "receipts" in paths else None
        emails_path = paths.get("emails") if "emails" in paths else None

        if bank_statement_path:
            job.add_log(f"Indexing bank statement: {bank_statement_path.name}")
            result = ingest_bank_statement(str(bank_statement_path))
            job.result["bank_statement"] = result
        else:
            job.add_log("No bank statement file supplied; skipping ingestion")

        if receipts_path:
            job.add_log(f"Indexing receipts from folder: {receipts_path}")
            summary = load_receipt_folder(str(receipts_path))
            job.result["receipts"] = summary
        else:
            job.add_log("No receipt files supplied; skipping receipt indexing")

        if emails_path:
            job.add_log(f"Indexing emails from folder: {emails_path}")
            summary = load_email_folder(str(emails_path))
            job.result["emails"] = summary
        else:
            job.add_log("No email files supplied; skipping email indexing")

        job.add_log("Running fraud detection engines")
        flags = run_engines()
        job.result["flags"] = flags
        job.add_log(f"Detection produced {len(flags)} flag(s)")

        if flags:
            job.add_log("Launching the forensic agent for the flagged cluster")
            try:
                report = execute_agent_investigation(json.dumps(flags[:5], indent=2, default=str), max_turns=2)
                job.result["agent_report"] = report
                job.add_log("Agent investigation completed")
            except Exception as exc:  # pragma: no cover - network/API fallback
                job.result["agent_report"] = f"Agent investigation could not run: {exc}"
                job.add_log(str(exc))
        else:
            job.result["agent_report"] = "No suspicious transactions were generated, so the agent did not run."
            job.add_log("No suspicious transactions were generated")

        job.status = "completed"
        job.add_log("Pipeline completed successfully")
    except Exception as exc:  # pragma: no cover - protection for runtime failures
        job.status = "failed"
        job.error = str(exc)
        job.add_log(f"Pipeline failed: {exc}")


def get_job(job_id: str) -> Optional[PipelineJob]:
    return JOBS.get(job_id)
