"""Upload archive storage: S3 primary in production with local UPLOAD_FILES_DIR cache/fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.shared.storage.s3_storage_service import (
    delete_file_from_s3,
    download_file_from_s3,
    list_s3_keys,
    s3_enabled,
    s3_key_for,
    upload_file_to_s3,
)

logger = logging.getLogger(__name__)

UPLOAD_TYPES: dict[str, str] = {
    "media-bucket": "Media Bucket Images",
    "sec-pdf-import": "SEC Filing PDF Imports",
    "sec-attachment": "SEC Document Attachments",
}

_FILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UPLOAD_CATEGORY = "uploads"
_INDEX_CATEGORY = "uploads/_index"


def upload_archive_root() -> Path:
    """Resolve and ensure the writable local upload cache/fallback directory."""
    configured = os.getenv("UPLOAD_FILES_DIR", ".data/upload_files").strip() or ".data/upload_files"
    primary = Path(configured).resolve()
    try:
        primary.mkdir(parents=True, exist_ok=True)
        return primary
    except OSError as exc:
        fallback = Path(".data/upload_files").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "UPLOAD_FILES_DIR (%s) is not writable (%s); falling back to %s",
            primary,
            exc,
            fallback,
        )
        return fallback


def _strict_s3_mode() -> bool:
    override = os.getenv("UPLOAD_S3_STRICT", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return os.getenv("APP_ENV", "development").strip().lower() == "production"


def _normalize_upload_type(upload_type: Optional[str]) -> str:
    cleaned = (upload_type or "media-bucket").strip().lower().replace("_", "-")
    if cleaned not in UPLOAD_TYPES:
        allowed = ", ".join(sorted(UPLOAD_TYPES))
        raise ValueError(f"Unsupported upload_type '{cleaned}'. Allowed types: {allowed}")
    return cleaned


def _validate_file_id(file_id: str) -> str:
    cleaned = (file_id or "").strip()
    if not _FILE_ID_RE.match(cleaned) or ".." in cleaned:
        raise FileNotFoundError("Invalid upload file identifier.")
    return cleaned


def _sanitize_filename(filename: Optional[str]) -> str:
    raw = Path(filename or "upload.bin").name
    cleaned = _SAFE_NAME_RE.sub("_", raw).strip("._")
    return (cleaned or "upload.bin")[:120]


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _local_index_path(file_id: str) -> Path:
    return upload_archive_root() / "_index" / f"{file_id}.json"


def _local_payload_path(rel_path: str) -> Path:
    root = upload_archive_root()
    target = (root / rel_path).resolve()
    target.relative_to(root)
    return target


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "filename": record["filename"],
        "upload_type": record["upload_type"],
        "company_name": record.get("company_name"),
        "content_type": record["content_type"],
        "size_bytes": record["size_bytes"],
        "sha256": record["sha256"],
        "storage_backend": record.get("storage_backend", "local"),
        "s3_key": record.get("s3_key"),
        "uploaded_by": record.get("uploaded_by"),
        "created_at": record["created_at"],
    }


def archive_upload_bytes(
    *,
    data: bytes,
    filename: str,
    content_type: Optional[str] = None,
    upload_type: str = "media-bucket",
    company_name: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    strict_s3: Optional[bool] = None,
) -> dict[str, Any]:
    """Archive uploaded bytes to S3 (when configured) and local cache storage."""
    clean_type = _normalize_upload_type(upload_type)
    display_name = Path(filename or "upload.bin").name
    safe_name = _sanitize_filename(display_name)
    resolved_mime = (
        content_type
        or mimetypes.guess_type(display_name)[0]
        or "application/octet-stream"
    )
    now = datetime.now(timezone.utc)
    file_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}"
    rel_path = f"{clean_type}/{now.strftime('%Y')}/{now.strftime('%m')}/{file_id}_{safe_name}"
    s3_key = s3_key_for(_UPLOAD_CATEGORY, rel_path) if s3_enabled() else None
    meta_s3_key = s3_key_for(_INDEX_CATEGORY, f"{file_id}.json") if s3_enabled() else None
    sha256_hex = hashlib.sha256(data).hexdigest()

    record: dict[str, Any] = {
        "id": file_id,
        "filename": display_name,
        "upload_type": clean_type,
        "company_name": (company_name or "").strip() or None,
        "content_type": resolved_mime,
        "size_bytes": len(data),
        "sha256": sha256_hex,
        "rel_path": rel_path,
        "s3_key": s3_key,
        "meta_s3_key": meta_s3_key,
        "storage_backend": "local",
        "uploaded_by": uploaded_by,
        "created_at": now.isoformat(),
    }

    if s3_enabled() and s3_key and meta_s3_key:
        strict = _strict_s3_mode() if strict_s3 is None else strict_s3
        try:
            upload_file_to_s3(
                data,
                s3_key,
                content_type=resolved_mime,
                metadata={"upload-type": clean_type, "file-id": file_id},
            )
            record["storage_backend"] = "s3"
            upload_file_to_s3(
                json.dumps(record, separators=(",", ":")).encode("utf-8"),
                meta_s3_key,
                content_type="application/json",
            )
        except Exception as exc:
            if strict:
                raise
            logger.warning("S3 archive failed in non-strict mode; keeping local copy (%s): %s", file_id, exc)
            record["storage_backend"] = "local"

    try:
        _atomic_write_bytes(_local_payload_path(rel_path), data)
        _atomic_write_bytes(_local_index_path(file_id), json.dumps(record, indent=2).encode("utf-8"))
    except OSError as exc:
        if record["storage_backend"] != "s3":
            raise
        logger.warning("Failed to write local cache for S3 object %s: %s", file_id, exc)

    return _public_record(record)


def _load_record(file_id: str) -> dict[str, Any]:
    clean_id = _validate_file_id(file_id)
    local_meta = _local_index_path(clean_id)
    if local_meta.exists():
        return json.loads(local_meta.read_text(encoding="utf-8"))

    if s3_enabled():
        meta_key = s3_key_for(_INDEX_CATEGORY, f"{clean_id}.json")
        raw = download_file_from_s3(meta_key)
        record = json.loads(raw.decode("utf-8"))
        try:
            _atomic_write_bytes(local_meta, raw)
        except OSError:
            pass
        return record

    raise FileNotFoundError(f"Upload '{clean_id}' not found.")


def read_upload_bytes(file_id: str) -> tuple[dict[str, Any], bytes]:
    """Load archive metadata and payload bytes from local cache or S3."""
    record = _load_record(file_id)
    rel_path = record["rel_path"]
    local_file = _local_payload_path(rel_path)
    if local_file.exists():
        return _public_record(record), local_file.read_bytes()

    if s3_enabled() and record.get("s3_key"):
        content = download_file_from_s3(record["s3_key"])
        try:
            _atomic_write_bytes(local_file, content)
        except OSError:
            pass
        return _public_record(record), content

    raise FileNotFoundError(f"Upload content '{file_id}' not found.")


def list_uploads(
    upload_type: Optional[str] = None,
    company_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List archived uploads from S3 index (when enabled) merged with local index."""
    clean_type = _normalize_upload_type(upload_type) if upload_type else None
    company_filter = (company_name or "").strip().lower() or None
    records_by_id: dict[str, dict[str, Any]] = {}

    index_dir = upload_archive_root() / "_index"
    if index_dir.exists():
        for path in index_dir.glob("*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                records_by_id[rec["id"]] = rec
            except Exception as exc:
                logger.warning("Skipping corrupt upload index file %s: %s", path, exc)

    if s3_enabled():
        prefix = f"{s3_key_for(_INDEX_CATEGORY, '')}/"
        try:
            for key in list_s3_keys(prefix):
                if not key.endswith(".json"):
                    continue
                item_id = Path(key).stem
                if item_id in records_by_id:
                    continue
                rec = json.loads(download_file_from_s3(key).decode("utf-8"))
                records_by_id[rec["id"]] = rec
        except Exception as exc:
            logger.warning("Could not list S3 upload index; using local cache: %s", exc)

    items: list[dict[str, Any]] = []
    for rec in records_by_id.values():
        if clean_type and rec.get("upload_type") != clean_type:
            continue
        if company_filter and (rec.get("company_name") or "").strip().lower() != company_filter:
            continue
        items.append(_public_record(rec))

    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def delete_upload(file_id: str) -> bool:
    """Delete an archived upload and its metadata from S3 and local cache."""
    try:
        record = _load_record(file_id)
    except FileNotFoundError:
        return False

    clean_id = _validate_file_id(file_id)
    if s3_enabled():
        for key in (record.get("s3_key"), record.get("meta_s3_key")):
            if key:
                try:
                    delete_file_from_s3(key)
                except Exception as exc:
                    logger.warning("Failed deleting S3 key %s: %s", key, exc)

    try:
        _local_payload_path(record["rel_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    _local_index_path(clean_id).unlink(missing_ok=True)
    return True


async def archive_upload_bytes_async(**kwargs: Any) -> dict[str, Any]:
    """Run archive_upload_bytes in a worker thread."""
    return await asyncio.to_thread(archive_upload_bytes, **kwargs)


async def archive_upload_best_effort_async(**kwargs: Any) -> Optional[dict[str, Any]]:
    """Archive bytes in the background without ever raising to callers."""
    try:
        return await asyncio.to_thread(archive_upload_bytes, **kwargs, strict_s3=False)
    except Exception as exc:
        logger.warning("Best-effort upload archive failed for %s: %s", kwargs.get("filename"), exc)
        return None


async def read_upload_async(file_id: str) -> tuple[dict[str, Any], bytes]:
    """Run read_upload_bytes in a worker thread."""
    return await asyncio.to_thread(read_upload_bytes, file_id)


async def list_uploads_async(
    upload_type: Optional[str] = None,
    company_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Run list_uploads in a worker thread."""
    return await asyncio.to_thread(list_uploads, upload_type, company_name)


async def delete_upload_async(file_id: str) -> bool:
    """Run delete_upload in a worker thread."""
    return await asyncio.to_thread(delete_upload, file_id)


async def get_upload_metadata_async(file_id: str) -> dict[str, Any]:
    """Return metadata for an archived upload without reading its payload bytes."""
    record = await asyncio.to_thread(_load_record, file_id)
    return _public_record(record)
