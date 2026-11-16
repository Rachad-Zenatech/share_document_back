"""Schema for the Social Media Script review workflow: one review per script document, its collaborators (invited
reviewers, with a role and an EDIT / VIEW capacity), their check-ins per round, and a history of what happened."""

import asyncio

from postgresql_db.database import get_pool

DDL = """
CREATE TABLE IF NOT EXISTS sec_script_reviews (
    document_id VARCHAR PRIMARY KEY REFERENCES sec_documents(id) ON DELETE CASCADE,
    status VARCHAR NOT NULL DEFAULT 'DRAFT',
    round INTEGER NOT NULL DEFAULT 1,
    creator_id UUID NOT NULL REFERENCES users(id),
    page_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision_note TEXT,
    decided_by UUID REFERENCES users(id),
    decided_at TIMESTAMPTZ,
    post_requested_at TIMESTAMPTZ,
    post_requested_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sec_script_collaborators (
    document_id VARCHAR NOT NULL REFERENCES sec_documents(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id),
    role VARCHAR NOT NULL,
    capacity VARCHAR NOT NULL DEFAULT 'EDIT',
    position INTEGER NOT NULL DEFAULT 0,
    invited_by UUID REFERENCES users(id),
    invited_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, user_id)
);

CREATE TABLE IF NOT EXISTS sec_script_checkins (
    document_id VARCHAR NOT NULL REFERENCES sec_documents(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id),
    vote VARCHAR NOT NULL,
    note TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, round, user_id)
);

CREATE TABLE IF NOT EXISTS sec_script_events (
    id BIGSERIAL PRIMARY KEY,
    document_id VARCHAR NOT NULL REFERENCES sec_documents(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    user_id UUID REFERENCES users(id),
    kind VARCHAR NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sec_script_events_doc_idx ON sec_script_events (document_id, id);
"""

_ensured = False
_lock = asyncio.Lock()


async def ensure_schema() -> None:
    global _ensured
    if _ensured:
        return
    async with _lock:
        if _ensured:
            return
        from postgresql_db.schemas.sec_filing_schema import ensure_schema as ensure_documents

        await ensure_documents()  # the tables above reference sec_documents
        async with get_pool().acquire() as conn:
            await conn.execute(DDL)
        _ensured = True
