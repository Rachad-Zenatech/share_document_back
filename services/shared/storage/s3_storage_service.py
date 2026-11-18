"""AWS S3 storage helper with hardened defaults, presigned URLs, and local-cache hydration."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

# Cached thread-safe boto3 S3 client so requests don't re-run EC2 IMDSv2 credential lookups.
_s3_client = None
_s3_client_lock = threading.Lock()


def s3_enabled() -> bool:
    """Return True when an S3 upload bucket is configured."""
    return bool((os.getenv("AWS_S3_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME") or "").strip())


def _bucket_name() -> str:
    bucket = (os.getenv("AWS_S3_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME") or "").strip()
    if not bucket:
        raise ValueError("AWS_S3_BUCKET_NAME environment variable is not configured.")
    return bucket


def _aws_region() -> str:
    return (os.getenv("AWS_S3_REGION") or os.getenv("AWS_REGION") or "ca-central-1").strip() or "ca-central-1"


def _s3_prefix() -> str:
    return os.getenv("UPLOAD_S3_PREFIX", "data").strip().strip("/")


def s3_key_for(category: str, rel_path: str) -> str:
    """Build a normalized S3 object key under UPLOAD_S3_PREFIX."""
    parts = [p for p in (_s3_prefix(), category.strip("/"), rel_path.replace("\\", "/").lstrip("/")) if p]
    return "/".join(parts)


def _is_flag_enabled(value: Optional[str], *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _botocore_config() -> Config:
    return Config(
        connect_timeout=int(os.getenv("AWS_S3_CONNECT_TIMEOUT", "5")),
        read_timeout=int(os.getenv("AWS_S3_READ_TIMEOUT", "30")),
        retries={
            "max_attempts": int(os.getenv("AWS_S3_MAX_ATTEMPTS", "2")),
            "mode": "standard",
        },
    )


def _request_security_args() -> dict[str, Any]:
    expected_owner = os.getenv("AWS_S3_EXPECTED_BUCKET_OWNER", "").strip()
    return {"ExpectedBucketOwner": expected_owner} if expected_owner else {}


def _upload_security_args() -> dict[str, Any]:
    algorithm = os.getenv("AWS_S3_SERVER_SIDE_ENCRYPTION", "AES256").strip()
    if algorithm not in {"AES256", "aws:kms"}:
        raise ValueError("AWS_S3_SERVER_SIDE_ENCRYPTION must be 'AES256' or 'aws:kms'.")

    args: dict[str, Any] = {
        "ServerSideEncryption": algorithm,
        "ChecksumAlgorithm": "SHA256",
        "CacheControl": "private, no-store",
        **_request_security_args(),
    }
    if algorithm == "aws:kms":
        kms_key_id = os.getenv("AWS_S3_KMS_KEY_ID", "").strip()
        if kms_key_id:
            args["SSEKMSKeyId"] = kms_key_id
        args["BucketKeyEnabled"] = _is_flag_enabled(
            os.getenv("AWS_S3_BUCKET_KEY_ENABLED"),
            default=True,
        )
    return args


def reset_s3_client() -> None:
    """Clear the cached S3 client (useful after credential/config changes in tests)."""
    global _s3_client
    with _s3_client_lock:
        _s3_client = None


def get_s3_client():
    """Return the shared thread-safe boto3 S3 client, initializing it on first call."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    with _s3_client_lock:
        if _s3_client is not None:
            return _s3_client

        access_key = (os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY") or "").strip()
        secret_key = (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()

        # Strip empty env vars so boto3's EnvProvider does not shadow EC2 IAM role credentials.
        if "AWS_ACCESS_KEY_ID" in os.environ and not os.environ["AWS_ACCESS_KEY_ID"].strip():
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
        if "AWS_SECRET_ACCESS_KEY" in os.environ and not os.environ["AWS_SECRET_ACCESS_KEY"].strip():
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)

        kwargs: dict[str, Any] = {
            "region_name": _aws_region(),
            "use_ssl": True,
            "config": _botocore_config(),
        }
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key

        _s3_client = boto3.client("s3", **kwargs)
        return _s3_client


def _sanitize_metadata(metadata: Optional[dict[str, str]]) -> dict[str, str]:
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for k, v in metadata.items():
        if v is None:
            continue
        ascii_val = str(v).encode("ascii", "ignore").decode("ascii").strip()
        if ascii_val:
            cleaned[str(k)] = ascii_val[:512]
    return cleaned


def upload_file_to_s3(
    file_bytes: bytes,
    s3_key: str,
    content_type: Optional[str] = None,
    metadata: Optional[dict[str, str]] = None,
) -> str:
    """Upload bytes to S3 and return the object HTTPS URI."""
    bucket = _bucket_name()
    client = get_s3_client()
    extra_args = _upload_security_args()
    if content_type:
        extra_args["ContentType"] = content_type
    clean_meta = _sanitize_metadata(metadata)
    if clean_meta:
        extra_args["Metadata"] = clean_meta

    try:
        client.put_object(Bucket=bucket, Key=s3_key, Body=file_bytes, **extra_args)
    except ClientError as exc:
        # Retry plain put_object if a bucket/IAM policy restricts optional checksum/SSE headers.
        try:
            simple_args: dict[str, Any] = {**_request_security_args()}
            if content_type:
                simple_args["ContentType"] = content_type
            client.put_object(Bucket=bucket, Key=s3_key, Body=file_bytes, **simple_args)
        except Exception:
            logger.error("Failed to upload %s to S3 bucket %s: %s", s3_key, bucket, exc)
            raise RuntimeError(f"S3 upload error: {exc}") from exc
    except BotoCoreError as exc:
        logger.error("Failed to upload %s to S3 bucket %s: %s", s3_key, bucket, exc)
        raise RuntimeError(f"S3 upload error: {exc}") from exc

    logger.info("Uploaded object to S3: %s", s3_key)
    return f"https://{bucket}.s3.{_aws_region()}.amazonaws.com/{s3_key}"


def download_file_from_s3(s3_key: str) -> bytes:
    """Download an object from S3 as raw bytes, raising FileNotFoundError if absent."""
    bucket = _bucket_name()
    client = get_s3_client()
    try:
        response = client.get_object(Bucket=bucket, Key=s3_key, **_request_security_args())
        return response["Body"].read()
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise FileNotFoundError(f"S3 object not found: {s3_key}") from exc
        logger.error("Failed to download %s from S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 download error: {exc}") from exc
    except BotoCoreError as exc:
        logger.error("Failed to download %s from S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 download error: {exc}") from exc


def generate_presigned_url(s3_key: str, expiration: int = 3600) -> str:
    """Generate a temporary pre-signed GET URL for an S3 object."""
    bucket = _bucket_name()
    client = get_s3_client()
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": s3_key, **_request_security_args()},
            ExpiresIn=expiration,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to generate presigned URL for %s: %s", s3_key, exc)
        raise RuntimeError(f"S3 presigned URL error: {exc}") from exc


def delete_file_from_s3(s3_key: str) -> bool:
    """Delete an object from S3."""
    bucket = _bucket_name()
    client = get_s3_client()
    try:
        client.delete_object(Bucket=bucket, Key=s3_key, **_request_security_args())
        logger.info("Deleted object from S3: %s", s3_key)
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to delete %s from S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 delete error: {exc}") from exc


def list_s3_keys(prefix: str) -> list[str]:
    """List object keys in the configured bucket under the given prefix."""
    bucket = _bucket_name()
    client = get_s3_client()
    keys: list[str] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, **_request_security_args()):
            for item in page.get("Contents", []):
                key = item.get("Key")
                if key:
                    keys.append(key)
        return keys
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to list S3 keys under %s: %s", prefix, exc)
        raise RuntimeError(f"S3 list error: {exc}") from exc


def ensure_local_file(path: Path, category: str, rel_path: str) -> Path:
    """Return local cached file if present; otherwise hydrate it from S3."""
    if path.exists():
        return path
    if not s3_enabled():
        raise FileNotFoundError(f"Local file missing and S3 is disabled: {path}")

    data = download_file_from_s3(s3_key_for(category, rel_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path
