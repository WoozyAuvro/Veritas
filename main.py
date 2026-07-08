import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from io import TextIOBase
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ingestion.bank_statement import ingest_bank_statement
from ingestion.batch_loader import load_email_folder, load_receipt_folder
from detection.run_engines import run_engines
from agent_execution import configure_analysis_storage, run_full_analysis
from fastapi.middleware.cors import CORSMiddleware
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://veritas-frontend-xo0w.onrender.com",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
#app.include_router(...)
PROJECT_ROOT = Path(__file__).resolve().parent


def _results_path() -> Path:
    return Path(os.getenv("FORENSIC_RESULTS_PATH", str(PROJECT_ROOT / "data" / "forensic_case_results.json")))


JOB_EXECUTOR = ThreadPoolExecutor(max_workers=8)
JOB_REGISTRY: Dict[str, Dict[str, Any]] = {}
JOB_LOCK = Lock()
CURRENT_JOB_CHANNEL: ContextVar[Optional["JobLogBuffer"]] = ContextVar("current_job_channel", default=None)


class JobLogBuffer:
    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._lock = Lock()
        self._closed = False

    def emit(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)

    def pop_chunk(self) -> str:
        with self._lock:
            if not self._chunks:
                return ""
            chunk = "".join(self._chunks)
            self._chunks.clear()
            return chunk

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._chunks)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def is_closed(self) -> bool:
        with self._lock:
            return self._closed


class TeeStream(TextIOBase):
    def write(self, data: str) -> int:
        channel = CURRENT_JOB_CHANNEL.get()
        if channel is not None and data:
            channel.emit(data)
        return len(data)

    def flush(self) -> None:
        return None


def _register_job(job_type: str) -> Dict[str, Any]:
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "job_type": job_type,
        "status": "queued",
        "result": None,
        "error": None,
        "done": Event(),
        "log_buffer": JobLogBuffer(),
    }
    with JOB_LOCK:
        JOB_REGISTRY[job_id] = job
    return job


def _run_job(job_id: str, job_type: str, fn, *args, **kwargs) -> None:
    job = JOB_REGISTRY[job_id]
    old_stdout, old_stderr = sys.stdout, sys.stderr
    token = None
    try:
        job["status"] = "running"
        sys.stdout = TeeStream()
        sys.stderr = TeeStream()
        token = CURRENT_JOB_CHANNEL.set(job["log_buffer"])
        print(f"[{job_type}] job {job_id} started")
        result = fn(*args, **kwargs)
        job["result"] = result
        job["status"] = "completed"
        print(f"[{job_type}] job {job_id} finished")
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        print(f"[{job_type}] job {job_id} failed: {exc}")
    finally:
        if token is not None:
            CURRENT_JOB_CHANNEL.reset(token)
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        job["done"].set()
        job["log_buffer"].close()


def _start_job(job_type: str, fn, *args, **kwargs) -> str:
    job = _register_job(job_type)
    job_id = job["job_id"]
    JOB_EXECUTOR.submit(_run_job, job_id, job_type, fn, *args, **kwargs)
    return job_id


def _ensure_runtime_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_temp_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(_ensure_runtime_dir(PROJECT_ROOT / "data" / "tmp"))))


def _run_agent_job(demo: bool = True) -> Dict[str, Any]:
    print("[forensic-agent] Starting forensic agent analysis...")
    result = run_full_analysis(demo=demo, interactive_followup=False)
    print("[forensic-agent] Forensic agent analysis completed")
    return result


def _run_analysis_workflow(demo: bool = True) -> Dict[str, Any]:
    configure_analysis_storage(demo)

    print("[analysis-workflow] Running fraud detection engines...")
    flags = run_engines()
    print(f"[analysis-workflow] Detection finished with {len(flags)} flag(s).")

    print("[analysis-workflow] Starting forensic agent job...")
    agent_job_id = _start_job("forensic-agent", _run_agent_job, demo)
    agent_job = JOB_REGISTRY[agent_job_id]
    agent_job["done"].wait(timeout=3600)

    if agent_job["status"] != "completed":
        raise RuntimeError(agent_job.get("error") or "Forensic agent job did not complete successfully")

    return {
        "status": "completed",
        "flags_generated": len(flags),
        "agent_job_id": agent_job_id,
        "agent_result": agent_job.get("result"),
    }


@app.get("/results")
def index():
    with _results_path().open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    job = JOB_REGISTRY.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "job_type": job.get("job_type"),
        "status": job.get("status"),
        "done": job["done"].is_set(),
        "result": job.get("result"),
        "error": job.get("error"),
    }


@app.get("/jobs/{job_id}/logs")
def stream_job_logs(job_id: str):
    job = JOB_REGISTRY.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    def generate() -> Any:
        while True:
            chunk = job["log_buffer"].pop_chunk()
            if chunk:
                yield chunk
                continue
            if job["done"].is_set():
                break
            time.sleep(0.1)

    return StreamingResponse(generate(), media_type="text/plain")

@app.post("/start-analysis")
async def start_agent(demo: bool = True):
    job_id = _start_job("analysis-workflow", _run_analysis_workflow, demo)
    return {
        "job_id": job_id,
        "status": "started",
        "logs_url": f"/jobs/{job_id}/logs",
        "status_url": f"/jobs/{job_id}",
    }


@app.post("/ingest/bank-statements")
async def ingest_bank_statements(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    upload_batch_id = str(uuid.uuid4())
    temp_dir = _make_temp_dir("bank_upload_")
    saved_files: list[Path] = []

    try:
        for file in files:
            if not file.filename or not file.filename.lower().endswith(".csv"):
                raise HTTPException(status_code=400, detail="Only CSV files are supported.")

            destination = temp_dir / file.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            saved_files.append(destination)

        job_id = _start_job(
            "bank-statement",
            _run_bank_statement_job,
            saved_files,
            upload_batch_id,
            temp_dir,
        )
        return {
            "job_id": job_id,
            "status": "started",
            "logs_url": f"/jobs/{job_id}/logs",
            "status_url": f"/jobs/{job_id}",
        }
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/ingest/emails")
async def ingest_emails(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    temp_dir = _make_temp_dir("email_upload_")
    saved_files: list[Path] = []

    try:
        for file in files:
            destination = temp_dir / file.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            saved_files.append(destination)

        job_id = _start_job("email-ingest", _run_email_job, saved_files, temp_dir)
        return {
            "job_id": job_id,
            "status": "started",
            "logs_url": f"/jobs/{job_id}/logs",
            "status_url": f"/jobs/{job_id}",
        }
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/ingest/receipts")
async def ingest_receipts(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    temp_dir = _make_temp_dir("receipt_upload_")
    saved_files: list[Path] = []

    try:
        for file in files:
            destination = temp_dir / file.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            saved_files.append(destination)

        job_id = _start_job("receipt-ingest", _run_receipt_job, saved_files, temp_dir)
        return {
            "job_id": job_id,
            "status": "started",
            "logs_url": f"/jobs/{job_id}/logs",
            "status_url": f"/jobs/{job_id}",
        }
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _run_bank_statement_job(file_paths: list[Path], upload_batch_id: str, temp_dir: Path) -> Dict[str, Any]:
    summary = {
        "upload_batch_id": upload_batch_id,
        "total_files": len(file_paths),
        "succeeded": [],
        "failed": [],
    }

    try:
        for file_path in file_paths:
            try:
                result = ingest_bank_statement(file_path, upload_batch_id=upload_batch_id)
                summary["succeeded"].append({
                    "filename": file_path.name,
                    "rows_loaded": result["rows_loaded"],
                    "column_mapping_used": result["column_mapping_used"],
                })
            except Exception as exc:
                summary["failed"].append({"filename": file_path.name, "reason": str(exc)})
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return summary


def _run_email_job(file_paths: list[Path], temp_dir: Path) -> Dict[str, Any]:
    try:
        return load_email_folder(temp_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _run_receipt_job(file_paths: list[Path], temp_dir: Path) -> Dict[str, Any]:
    try:
        return load_receipt_folder(temp_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
