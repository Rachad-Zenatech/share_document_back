"""Database and event services for SEC filing mobile e-signature envelopes.

Supports real-time cross-device signing (DocuSign & Dropbox Sign SMS delivery)
under SEC Rule 302(b) of Regulation S-T.
"""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID

from postgresql_db.database import fetch_one, get_pool


def _row_to_envelope(row: Any) -> dict[str, Any]:
    env = dict(row)
    if "id" in env and isinstance(env["id"], UUID):
        env["id"] = str(env["id"])
    envelope_id = env.get("envelope_id", "")
    env["audit_trail_id"] = (
        f"SEC-AUDIT-{envelope_id.replace('ds-env-', '').replace('dbs-env-', '').replace('sec-env-', '').upper()}"
    )
    return env


async def dispatch_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO sec_signature_envelopes
                (envelope_id, document_title, document_id, block_id, officer_id,
                 signer_name, signer_title, phone_number, provider, delivery_method, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'sent_sms')
            ON CONFLICT (envelope_id) DO UPDATE
                SET document_title = EXCLUDED.document_title,
                    document_id = EXCLUDED.document_id,
                    block_id = EXCLUDED.block_id,
                    officer_id = EXCLUDED.officer_id,
                    signer_name = EXCLUDED.signer_name,
                    signer_title = EXCLUDED.signer_title,
                    phone_number = EXCLUDED.phone_number,
                    provider = EXCLUDED.provider,
                    delivery_method = EXCLUDED.delivery_method,
                    updated_at = now()
            RETURNING *
            """,
            payload["envelope_id"],
            payload["document_title"],
            payload.get("document_id"),
            payload.get("block_id"),
            payload.get("officer_id"),
            payload["signer_name"],
            payload.get("signer_title"),
            payload.get("phone_number"),
            payload.get("provider", "docusign"),
            payload.get("delivery_method", "SMS"),
        )
        return _row_to_envelope(row)


async def get_envelope(envelope_id: str) -> dict[str, Any] | None:
    row = await fetch_one(
        """
        SELECT *
        FROM sec_signature_envelopes
        WHERE envelope_id = $1
        """,
        envelope_id,
    )
    return _row_to_envelope(row) if row else None


async def complete_envelope(envelope_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.datetime.now(datetime.timezone.utc)
        row = await conn.fetchrow(
            """
            UPDATE sec_signature_envelopes
            SET status = 'signed',
                signer_name = COALESCE($2, signer_name),
                signature_text = $3,
                signature_image_url = $4,
                provider = COALESCE($5, provider),
                signed_via = $6,
                ip_address = $7,
                signed_at = $8,
                updated_at = now()
            WHERE envelope_id = $1
            RETURNING *
            """,
            envelope_id,
            payload.get("signer_name"),
            payload["signature_text"],
            payload.get("signature_image_url"),
            payload.get("provider"),
            payload.get("signed_via", "Mobile Verified SMS (SEC Rule 302(b))"),
            payload.get("ip_address"),
            now,
        )
        if not row:
            # If envelope didn't exist prior, insert it as already signed
            row = await conn.fetchrow(
                """
                INSERT INTO sec_signature_envelopes
                    (envelope_id, document_title, signer_name, provider, status,
                     signature_text, signature_image_url, signed_via, ip_address, signed_at)
                VALUES ($1, 'SEC Filing Document', COALESCE($2, 'Signer'), COALESCE($3, 'docusign'), 'signed',
                        $4, $5, $6, $7, $8)
                RETURNING *
                """,
                envelope_id,
                payload.get("signer_name"),
                payload.get("provider"),
                payload["signature_text"],
                payload.get("signature_image_url"),
                payload.get("signed_via", "Mobile Verified SMS (SEC Rule 302(b))"),
                payload.get("ip_address"),
                now,
            )
        return _row_to_envelope(row)


async def delete_envelope(envelope_id: str) -> bool:
    """Deletes an envelope so no signature data is retained in the database."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM sec_signature_envelopes
            WHERE envelope_id = $1
            """,
            envelope_id,
        )
        return result == "DELETE 1"


async def cleanup_expired_envelopes() -> None:
    """Purges transient signature envelopes older than 15 minutes."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM sec_signature_envelopes
            WHERE created_at < now() - INTERVAL '15 minutes'
            """
        )
