"""PDF Import routes for SEC filing documents, with best-effort S3 archival."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile

from services.sec_filings.documents.sec_pdf_import_service import (
    MAX_PDF_BYTES,
    get_pdf_import,
    start_pdf_import,
)
from services.shared.auth.auth_service import get_current_user_id_dependency
from services.shared.storage.upload_archive_service import archive_upload_best_effort_async

router = APIRouter()


@router.post("/import/pdf", status_code=202)
async def start_pdf_import_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """Accept an uploaded PDF, start background conversion, and archive the file best-effort."""
    data = await file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB.",
        )
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="This file is not a PDF.")

    file_name = file.filename or "document.pdf"
    background_tasks.add_task(
        archive_upload_best_effort_async,
        data=data,
        filename=file_name,
        content_type="application/pdf",
        upload_type="sec-pdf-import",
        uploaded_by=str(user_id),
    )
    return {"jobId": start_pdf_import(user_id, data, file_name)}


@router.get("/import/pdf/{job_id}")
async def get_pdf_import_endpoint(
    job_id: str,
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """Return status and converted blocks for an active PDF import job."""
    job = get_pdf_import(user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import not found or expired.")
    return job
