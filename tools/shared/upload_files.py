"""Uploaded files & media bucket endpoints backed by S3 and local cache storage."""

from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile

from services.shared.auth.auth_service import get_current_user_id_dependency
from services.shared.storage.s3_storage_service import generate_presigned_url, s3_enabled
from services.shared.storage.upload_archive_service import (
    UPLOAD_TYPES,
    archive_upload_bytes_async,
    delete_upload_async,
    get_upload_metadata_async,
    list_uploads_async,
    read_upload_async,
)
from services.shared.storage.upload_security_service import (
    SAFE_IMAGE_EXTENSIONS,
    read_upload_limited,
)

router = APIRouter()

_INLINE_SAFE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "application/pdf",
    }
)


def _content_disposition(disposition: str, filename: str) -> str:
    fallback = filename.replace('"', "").replace("\r", "").replace("\n", "")
    encoded = quote(filename)
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


@router.get("")
async def list_uploaded_files(
    upload_type: Optional[str] = Query(None),
    company_name: Optional[str] = Query(None),
):
    """List archived uploaded files, optionally filtered by upload_type or company_name."""
    try:
        types = [{"value": value, "label": label} for value, label in UPLOAD_TYPES.items()]
        files = await list_uploads_async(upload_type, company_name)
        return {"upload_types": types, "files": files}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/media", status_code=201)
async def upload_media_asset(
    file: UploadFile = File(...),
    company_name: str = Form("General Corporate"),
    upload_type: str = Form("media-bucket"),
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """Upload a media image for the Company Media Bucket to S3 (with local cache)."""
    data = await read_upload_limited(file, allowed_suffixes=SAFE_IMAGE_EXTENSIONS)
    try:
        item = await archive_upload_bytes_async(
            data=data,
            filename=file.filename or "media-asset.png",
            content_type=file.content_type,
            upload_type=upload_type,
            company_name=company_name,
            uploaded_by=str(user_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Failed to store uploaded media asset.") from exc

    return {
        **item,
        "view_path": f"/api/upload-files/{item['id']}/view",
        "download_path": f"/api/upload-files/{item['id']}/download",
    }


@router.get("/{file_id}/view")
async def view_uploaded_file(file_id: str):
    """Stream an uploaded file inline (or sandboxed/attachment for unsafe content types)."""
    try:
        item, content = await read_upload_async(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    media_type = (item.get("content_type") or "application/octet-stream").split(";")[0].strip().lower()
    disposition = "inline" if (media_type in _INLINE_SAFE_MIME_TYPES or media_type == "image/svg+xml") else "attachment"
    headers = {
        "Content-Disposition": _content_disposition(disposition, item.get("filename") or "upload"),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600",
    }
    if media_type == "image/svg+xml":
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"

    return Response(content=content, media_type=media_type, headers=headers)


@router.get("/{file_id}/download")
async def download_uploaded_file(file_id: str):
    """Download an archived upload as an attachment."""
    try:
        item, content = await read_upload_async(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return Response(
        content=content,
        media_type=item.get("content_type") or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition("attachment", item.get("filename") or "upload"),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{file_id}/presigned-url")
async def get_uploaded_file_presigned_url(
    file_id: str,
    expires_in: int = Query(3600, ge=60, le=86400),
):
    """Return a short-lived pre-signed S3 download URL for an S3-backed upload."""
    if not s3_enabled():
        raise HTTPException(status_code=400, detail="S3 storage is not configured in this environment.")
    try:
        item = await get_upload_metadata_async(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    s3_key = item.get("s3_key")
    if not s3_key:
        raise HTTPException(status_code=404, detail="Upload has no associated S3 object key.")

    url = await asyncio.to_thread(generate_presigned_url, s3_key, expires_in)
    return {"id": item["id"], "url": url, "expires_in": expires_in}


@router.delete("/{file_id}", status_code=204)
async def delete_uploaded_file(file_id: str):
    """Delete an archived upload from S3 and local cache."""
    if not await delete_upload_async(file_id):
        raise HTTPException(status_code=404, detail="Upload not found.")
    return None
