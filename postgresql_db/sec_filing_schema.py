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


"""


async def ensure_schema() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DDL)
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
