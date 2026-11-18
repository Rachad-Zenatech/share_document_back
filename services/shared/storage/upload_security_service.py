"""Upload validation helpers: extension allowlists and bounded streaming reads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Collection, Optional

from fastapi import HTTPException, UploadFile

DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024

SAFE_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}
)


def max_upload_bytes() -> int:
    """Return the configured maximum upload size in bytes."""
    raw = os.getenv("MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)).strip()
    try:
        parsed = int(raw or str(DEFAULT_MAX_UPLOAD_BYTES))
    except ValueError:
        parsed = DEFAULT_MAX_UPLOAD_BYTES
    return max(UPLOAD_READ_CHUNK_BYTES, parsed)


async def read_upload_limited(
    upload: UploadFile,
    *,
    allowed_suffixes: Optional[Collection[str]] = None,
    maximum_bytes: Optional[int] = None,
) -> bytes:
    """Read an uploaded file in chunks while enforcing extension and byte limits."""
    filename = Path(upload.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    normalized_suffixes = {
        value.lower() if value.startswith(".") else f".{value.lower()}"
        for value in (allowed_suffixes or [])
    }
    if normalized_suffixes and suffix not in normalized_suffixes:
        allowed = ", ".join(sorted(normalized_suffixes))
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported upload type. Allowed extensions: {allowed}",
        )

    limit = maximum_bytes or max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {limit // (1024 * 1024)} MB limit.",
            )
        chunks.append(chunk)

    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")
    return data
