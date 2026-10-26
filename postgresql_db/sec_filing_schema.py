"""Schema for SEC filing financial statement table templates.

Templates were previously hardcoded in the frontend
(`src/data/financialTableTemplates.ts`), so changing one meant a code change and
a deploy. They live here instead so they can be created and edited at runtime.

Templates are global: there is no company column. Adding one later is an
additive ALTER, so nothing here forecloses per-company scoping.
"""

import asyncio

from postgresql_db.database import get_pool

DDL = """
CREATE TABLE IF NOT EXISTS sec_financial_table_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Stable key so the built-in seed is idempotent and the frontend can refer
    -- to a known template (e.g. 'balance_sheet') without hardcoding a UUID.
    template_key VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    description TEXT,
    badge VARCHAR,
    -- Icon name the frontend maps to a lucide component, e.g. 'scale'.
    icon VARCHAR NOT NULL DEFAULT 'table',
    color VARCHAR,
    sort_order INTEGER NOT NULL DEFAULT 0,
    -- Seeded templates cannot be deleted, only edited or superseded.
    is_builtin BOOLEAN NOT NULL DEFAULT false,
    -- { title, headers, columnAlignments, columnWidths, rows[], footnotes[] }
    block JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID REFERENCES users(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by UUID REFERENCES users(id),
    deleted_at TIMESTAMPTZ,
    deleted_by UUID REFERENCES users(id)
);

ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS template_key VARCHAR;
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS badge VARCHAR;
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS icon VARCHAR NOT NULL DEFAULT 'table';
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS color VARCHAR;
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS is_builtin BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id);
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS updated_by UUID REFERENCES users(id);
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE sec_financial_table_templates ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id);

-- One live template per key; soft-deleted rows are excluded so a key can be reused.
CREATE UNIQUE INDEX IF NOT EXISTS sec_financial_table_templates_key_live_idx
    ON sec_financial_table_templates (template_key)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS sec_financial_table_templates_listing_idx
    ON sec_financial_table_templates (deleted_at, sort_order, name);

-- Register the page so its permissions can be granted through the existing Role
-- Page Permissions screen. Without this row, require_permission('SEC_FILINGS_*')
-- would only ever pass for super admins.
INSERT INTO navigation_items (name, code, route_path, parent_code, display_order, icon, is_menu_item, is_active)
SELECT 'SEC Filings', 'SEC_FILINGS', '/sec-filings', NULL, 95, 'FileText', true, true
WHERE NOT EXISTS (SELECT 1 FROM navigation_items WHERE code = 'SEC_FILINGS');

-- Mobile E-Signature Envelopes for Cross-Device Real-Time Signing & Rule 302(b) Audit Trail
CREATE TABLE IF NOT EXISTS sec_signature_envelopes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    envelope_id VARCHAR NOT NULL UNIQUE,
    document_title VARCHAR NOT NULL,
    document_id VARCHAR,
    block_id VARCHAR,
    officer_id VARCHAR,
    signer_name VARCHAR NOT NULL,
    signer_title VARCHAR,
    phone_number VARCHAR,
    provider VARCHAR NOT NULL DEFAULT 'docusign',
    delivery_method VARCHAR NOT NULL DEFAULT 'SMS',
    status VARCHAR NOT NULL DEFAULT 'sent_sms',
    signature_text VARCHAR,
    signature_image_url TEXT,
    signed_via VARCHAR,
    signed_at TIMESTAMPTZ,
    ip_address VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sec_signature_envelopes_env_idx
    ON sec_signature_envelopes (envelope_id);

CREATE INDEX IF NOT EXISTS idx_sec_sig_env_created
    ON sec_signature_envelopes (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sec_sig_env_doc_status
    ON sec_signature_envelopes (document_id, status);

-- Attached Dynamic Spreadsheets for Document Variable Linking
CREATE TABLE IF NOT EXISTS sec_attached_spreadsheets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id VARCHAR NOT NULL,
    name VARCHAR NOT NULL DEFAULT 'Spreadsheet',
    sheet_name VARCHAR NOT NULL DEFAULT 'Sheet1',
    total_rows INTEGER NOT NULL DEFAULT 20,
    total_columns INTEGER NOT NULL DEFAULT 10,
    columns JSONB NOT NULL DEFAULT '[]'::jsonb,
    cells JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS sec_attached_spreadsheets_doc_idx
    ON sec_attached_spreadsheets (document_id);

    -- Multi-tab support for attached spreadsheets
    ALTER TABLE sec_attached_spreadsheets ADD COLUMN IF NOT EXISTS tabs JSONB NOT NULL DEFAULT '[]'::jsonb;
    ALTER TABLE sec_attached_spreadsheets ADD COLUMN IF NOT EXISTS active_tab_id VARCHAR;
    -- Spreadsheet Hub workbook id, so a linked sheet maps back to its frontend registry entry
    ALTER TABLE sec_attached_spreadsheets ADD COLUMN IF NOT EXISTS source_sheet_id VARCHAR;

-- Reporting workflows (e.g. "FS Notes V2"): an ordered list of steps, each with
-- who it is assigned to. A contributor invite picks one step as its branch title.
CREATE TABLE IF NOT EXISTS sec_workflows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR NOT NULL,
    description TEXT,
    -- [{ step, description, assignedTo }]
    steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID REFERENCES users(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by UUID REFERENCES users(id),
    deleted_at TIMESTAMPTZ,
    deleted_by UUID REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS sec_workflows_listing_idx
    ON sec_workflows (deleted_at, name);

-- One workflow per document. Documents are identified by the frontend's string id.
CREATE TABLE IF NOT EXISTS sec_document_workflow_assignments (
    document_id VARCHAR PRIMARY KEY,
    workflow_id UUID NOT NULL REFERENCES sec_workflows(id) ON DELETE CASCADE,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_by UUID REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS sec_document_workflow_assignments_workflow_idx
    ON sec_document_workflow_assignments (workflow_id);

-- Filing documents. These used to live only in each browser's localStorage, so
-- nobody else could see them and clearing site data deleted them. `id` keeps the
-- frontend's string ids so existing links, workflow and spreadsheet assignments still match.
CREATE TABLE IF NOT EXISTS sec_documents (
    id VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL DEFAULT 'Untitled Document',
    form_type VARCHAR,
    period VARCHAR,
    template_type VARCHAR,
    owner_id UUID REFERENCES users(id),
    owner_email VARCHAR,
    owner_name VARCHAR,
    -- Lower-cased emails / user id strings, mirrored from the document for access checks.
    invited_emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    invited_user_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The full SecFilingDocument as the editor uses it (blocks, version, status...).
    content JSONB NOT NULL,
    -- Bumped on every save; a save based on an older revision is rejected (409).
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID REFERENCES users(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by UUID REFERENCES users(id),
    deleted_at TIMESTAMPTZ,
    deleted_by UUID REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS sec_documents_owner_idx ON sec_documents (owner_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS sec_documents_invited_emails_idx ON sec_documents USING gin (invited_emails);
CREATE INDEX IF NOT EXISTS sec_documents_listing_idx ON sec_documents (deleted_at, updated_at DESC);

-- Change proposals (contributor branches) of a document.
CREATE TABLE IF NOT EXISTS sec_document_proposals (
    id VARCHAR PRIMARY KEY,
    document_id VARCHAR NOT NULL REFERENCES sec_documents(id) ON DELETE CASCADE,
    status VARCHAR NOT NULL DEFAULT 'draft',
    author_email VARCHAR,
    -- The full SecChangeProposal (blocks, spreadsheet copy, review fields...).
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID REFERENCES users(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by UUID REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS sec_document_proposals_doc_idx ON sec_document_proposals (document_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS sec_document_proposals_author_idx ON sec_document_proposals (author_email);

-- Version snapshots of a document (merges, restores). Append-only.
CREATE TABLE IF NOT EXISTS sec_document_versions (
    id VARCHAR PRIMARY KEY,
    document_id VARCHAR NOT NULL REFERENCES sec_documents(id) ON DELETE CASCADE,
    version_number INTEGER,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS sec_document_versions_doc_idx ON sec_document_versions (document_id, created_at);

-- No-login guest access to exactly one change proposal (external contributors).
-- Registered when a signed-in user with access to the document saves the proposal
-- with a guest invite token; one live token per proposal, so a new token revokes
-- the old link. Looked up by SHA-256 hash; expiry is set here, never by the client.
CREATE TABLE IF NOT EXISTS sec_proposal_guest_invites (
    proposal_id VARCHAR PRIMARY KEY REFERENCES sec_document_proposals(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID REFERENCES users(id),
    last_used_at TIMESTAMPTZ
);


"""


_schema_ensured = False
_schema_lock = asyncio.Lock()


async def ensure_schema() -> None:
    global _schema_ensured
    if _schema_ensured:
        return
    async with _schema_lock:
        if _schema_ensured:
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(DDL)
        _schema_ensured = True
        print("SEC filing template schema ensured.")


if __name__ == "__main__":
    from dotenv import load_dotenv

    from postgresql_db.database import close_pool, create_pool

    load_dotenv()

    async def main():
        await create_pool()
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(DDL)
        await close_pool()
        print("SEC filing template schema applied successfully.")

    asyncio.run(main())
