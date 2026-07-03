from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.pipeline_service import get_job, start_pipeline

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

app = FastAPI(title="Veritas Fraud Analysis API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/run-pipeline")
async def run_pipeline(
    bank_statement: Optional[UploadFile] = File(default=None),
    receipts: Optional[List[UploadFile]] = File(default=None),
    emails: Optional[List[UploadFile]] = File(default=None),
    use_demo: bool = Form(default=False),
):
    files_by_type: Dict[str, object] = {}

    if bank_statement is not None:
        files_by_type["bank_statement"] = {"filename": bank_statement.filename, "file": bank_statement}

    if receipts:
        files_by_type["receipts"] = {"filename": receipts[0].filename, "file": receipts[0]}

    if emails:
        files_by_type["emails"] = {"filename": emails[0].filename, "file": emails[0]}

    job = start_pipeline(files_by_type, use_demo=use_demo)
    return {"job_id": job.id, "status": job.status}


@app.get("/api/jobs/{job_id}")
def get_pipeline_job(job_id: str):
    job = get_job(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"detail": "job not found"})

    return {
        "job_id": job.id,
        "status": job.status,
        "logs": job.logs,
        "result": job.result,
        "error": job.error,
    }
