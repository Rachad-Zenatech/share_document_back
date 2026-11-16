"""
RDS PostgreSQL Role-Based Access Control & Row-Level Security (RLS) Setup.

Implements two complementary database security controls on AWS RDS PostgreSQL:
1. Role-based access (`NOLOGIN` permission roles granted to `app_backend`):
   - `notification_reader`, `notification_writer`
   - `audit_reader`, `audit_writer`
2. Row-Level Security (RLS) policies driven by verified transaction-scoped context
   variables (`current_setting('app.user_id', true)` and `current_setting('app.is_super_admin', true)`).

Policies are permissive when no user context is bound (startup, background jobs, public
token endpoints) unless `app.rls_strict` is true. Reference/master data and company-wide
shared tables (users, roles, permissions, SEC filing templates/spreadsheets/envelopes,
app_settings) intentionally have no owner-based RLS.
"""

import asyncio
import logging
import os
from postgresql_db.database import close_pool, create_pool, get_pool

logger = logging.getLogger(__name__)


RLS_ROLES_AND_GRANTS_SQL = """
DO $$
BEGIN
    -- 1. Create database NOLOGIN permission roles
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'notification_reader') THEN
        CREATE ROLE notification_reader NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'notification_writer') THEN
        CREATE ROLE notification_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audit_reader') THEN
        CREATE ROLE audit_reader NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audit_writer') THEN
        CREATE ROLE audit_writer NOLOGIN;
    END IF;
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE NOTICE 'Skipping CREATE ROLE (account lacks CREATEROLE privilege on RDS); continuing with RLS policies.';
END $$;

-- Grant schema usage and table privileges to permission roles
DO $$
BEGIN
    GRANT USAGE ON SCHEMA public TO
        notification_reader, notification_writer,
        audit_reader, audit_writer;

    IF to_regclass('public.notifications') IS NOT NULL THEN
        GRANT SELECT ON public.notifications TO notification_reader;
        GRANT INSERT, UPDATE, DELETE ON public.notifications TO notification_writer;
    END IF;
    IF to_regclass('public.notifications_id_seq') IS NOT NULL THEN
        GRANT USAGE, SELECT ON SEQUENCE public.notifications_id_seq TO notification_writer;
    END IF;

    IF to_regclass('public.audit_logs') IS NOT NULL THEN
        GRANT SELECT ON public.audit_logs TO audit_reader;
        GRANT INSERT ON public.audit_logs TO audit_writer;
    END IF;
    IF to_regclass('public.login_activity_logs') IS NOT NULL THEN
        GRANT SELECT ON public.login_activity_logs TO audit_reader;
        GRANT INSERT, UPDATE ON public.login_activity_logs TO audit_writer;
    END IF;
    IF to_regclass('public.login_activity_logs_id_seq') IS NOT NULL THEN
        GRANT USAGE, SELECT ON SEQUENCE public.login_activity_logs_id_seq TO audit_writer;
    END IF;

    -- 2. Assign permission roles to app_backend (if present) and ensure NOBYPASSRLS
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_backend') THEN
        GRANT notification_reader, notification_writer,
              audit_reader, audit_writer
        TO app_backend;
        BEGIN
            ALTER ROLE app_backend NOBYPASSRLS;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'Skipping ALTER ROLE app_backend NOBYPASSRLS (requires rds_superuser).';
        END;
    END IF;
EXCEPTION
    WHEN undefined_object OR insufficient_privilege THEN
        RAISE NOTICE 'Skipping role grants due to role/privilege constraints on current connection.';
END $$;
"""


RLS_FUNCTIONS_AND_POLICIES_SQL = """
-- Helper: safely parse current_setting('app.user_id', true) into UUID without throwing on empty/'*'
CREATE OR REPLACE FUNCTION public.current_app_user_id()
RETURNS uuid
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $func$
DECLARE
    raw_val text;
BEGIN
    raw_val := NULLIF(btrim(current_setting('app.user_id', true)), '');
    IF raw_val IS NULL OR raw_val = '*' THEN
        RETURN NULL;
    END IF;
    RETURN raw_val::uuid;
EXCEPTION
    WHEN invalid_text_representation THEN
        RETURN NULL;
END;
$func$;

-- Helper: evaluate whether the current context or user is a super-admin / admin role / background worker
CREATE OR REPLACE FUNCTION public.is_rls_exempt_or_elevated_user(p_user_id uuid)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $func$
DECLARE
    raw_user text;
    strict_mode boolean;
    super_ctx boolean;
    has_elevated boolean;
BEGIN
    raw_user := NULLIF(btrim(current_setting('app.user_id', true)), '');
    strict_mode := COALESCE(NULLIF(btrim(current_setting('app.rls_strict', true)), '')::boolean, false);
    super_ctx := COALESCE(NULLIF(btrim(current_setting('app.is_super_admin', true)), '')::boolean, false);

    IF super_ctx THEN
        RETURN true;
    END IF;

    -- Allow internal background loops/migrations when no request user context is active and strict mode is off
    IF raw_user IS NULL AND NOT strict_mode THEN
        RETURN true;
    END IF;

    IF p_user_id IS NULL THEN
        RETURN false;
    END IF;

    -- Check super admin flag on users table
    SELECT EXISTS (
        SELECT 1 FROM public.users u
        WHERE u.id = p_user_id
          AND u.is_active = true
          AND u.deleted_at IS NULL
          AND u.is_super_admin = true
    ) INTO has_elevated;

    IF has_elevated THEN
        RETURN true;
    END IF;

    -- Check administrative RBAC roles
    SELECT EXISTS (
        SELECT 1
        FROM public.user_roles ur
        JOIN public.roles r ON r.id = ur.role_id
        WHERE ur.user_id = p_user_id
          AND ur.is_active = true
          AND r.is_active = true
          AND r.deleted_at IS NULL
          AND r.code IN ('SUPER_ADMIN', 'ADMIN')
    ) INTO has_elevated;

    RETURN COALESCE(has_elevated, false);
END;
$func$;

-- 1. Row-Level Security on public.notifications (user_id is VARCHAR; '*' marks broadcast rows)
DO $$
BEGIN
    IF to_regclass('public.notifications') IS NOT NULL THEN
        ALTER TABLE public.notifications ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.notifications FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS read_received_notifications ON public.notifications;
        CREATE POLICY read_received_notifications
        ON public.notifications
        FOR SELECT
        TO PUBLIC
        USING (
            user_id = '*'
            OR user_id = NULLIF(current_setting('app.user_id', true), '')
            OR public.is_rls_exempt_or_elevated_user(public.current_app_user_id())
        );

        DROP POLICY IF EXISTS insert_notifications ON public.notifications;
        CREATE POLICY insert_notifications
        ON public.notifications
        FOR INSERT
        TO PUBLIC
        WITH CHECK (
            user_id IS NOT NULL AND btrim(user_id) <> ''
        );

        DROP POLICY IF EXISTS update_received_notifications ON public.notifications;
        CREATE POLICY update_received_notifications
        ON public.notifications
        FOR UPDATE
        TO PUBLIC
        USING (
            user_id = '*'
            OR user_id = NULLIF(current_setting('app.user_id', true), '')
            OR public.is_rls_exempt_or_elevated_user(public.current_app_user_id())
        )
        WITH CHECK (
            user_id = '*'
            OR user_id = NULLIF(current_setting('app.user_id', true), '')
            OR public.is_rls_exempt_or_elevated_user(public.current_app_user_id())
        );

        DROP POLICY IF EXISTS delete_received_notifications ON public.notifications;
        CREATE POLICY delete_received_notifications
        ON public.notifications
        FOR DELETE
        TO PUBLIC
        USING (
            user_id = '*'
            OR user_id = NULLIF(current_setting('app.user_id', true), '')
            OR public.is_rls_exempt_or_elevated_user(public.current_app_user_id())
        );
    END IF;
END $$;

-- 2. Row-Level Security on public.audit_logs (owner: actor_user_id; inserts open so any action can be audited)
DO $$
BEGIN
    IF to_regclass('public.audit_logs') IS NOT NULL THEN
        ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.audit_logs FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS read_audit_logs ON public.audit_logs;
        CREATE POLICY read_audit_logs
        ON public.audit_logs
        FOR SELECT
        TO PUBLIC
        USING (
            actor_user_id = public.current_app_user_id()
            OR public.is_rls_exempt_or_elevated_user(public.current_app_user_id())
        );

        DROP POLICY IF EXISTS insert_audit_logs ON public.audit_logs;
        CREATE POLICY insert_audit_logs
        ON public.audit_logs
        FOR INSERT
        TO PUBLIC
        WITH CHECK (true);

        DROP POLICY IF EXISTS update_audit_logs ON public.audit_logs;
        CREATE POLICY update_audit_logs
        ON public.audit_logs
        FOR UPDATE
        TO PUBLIC
        USING (public.is_rls_exempt_or_elevated_user(public.current_app_user_id()))
        WITH CHECK (public.is_rls_exempt_or_elevated_user(public.current_app_user_id()));

        DROP POLICY IF EXISTS delete_audit_logs ON public.audit_logs;
        CREATE POLICY delete_audit_logs
        ON public.audit_logs
        FOR DELETE
        TO PUBLIC
        USING (public.is_rls_exempt_or_elevated_user(public.current_app_user_id()));
    END IF;
END $$;

-- 3. Row-Level Security on public.login_activity_logs (owner: user_id; failed logins have no user context)
DO $$
BEGIN
    IF to_regclass('public.login_activity_logs') IS NOT NULL THEN
        ALTER TABLE public.login_activity_logs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.login_activity_logs FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS read_login_activity_logs ON public.login_activity_logs;
        CREATE POLICY read_login_activity_logs
        ON public.login_activity_logs
        FOR SELECT
        TO PUBLIC
        USING (
            user_id = public.current_app_user_id()
            OR public.is_rls_exempt_or_elevated_user(public.current_app_user_id())
        );

        DROP POLICY IF EXISTS insert_login_activity_logs ON public.login_activity_logs;
        CREATE POLICY insert_login_activity_logs
        ON public.login_activity_logs
        FOR INSERT
        TO PUBLIC
        WITH CHECK (true);

        DROP POLICY IF EXISTS update_login_activity_logs ON public.login_activity_logs;
        CREATE POLICY update_login_activity_logs
        ON public.login_activity_logs
        FOR UPDATE
        TO PUBLIC
        USING (
            user_id = public.current_app_user_id()
            OR public.is_rls_exempt_or_elevated_user(public.current_app_user_id())
        )
        WITH CHECK (
            user_id = public.current_app_user_id()
            OR public.is_rls_exempt_or_elevated_user(public.current_app_user_id())
        );

        DROP POLICY IF EXISTS delete_login_activity_logs ON public.login_activity_logs;
        CREATE POLICY delete_login_activity_logs
        ON public.login_activity_logs
        FOR DELETE
        TO PUBLIC
        USING (public.is_rls_exempt_or_elevated_user(public.current_app_user_id()));
    END IF;
END $$;
"""


async def ensure_rls_security(conn=None) -> None:
    """Apply RDS PostgreSQL permission roles and Row-Level Security (RLS) policies idempotently."""
    if conn is not None:
        await conn.execute(RLS_ROLES_AND_GRANTS_SQL)
        await conn.execute(RLS_FUNCTIONS_AND_POLICIES_SQL)
        return

    pool = get_pool()
    async with pool.acquire() as acquired_conn:
        await acquired_conn.execute(RLS_ROLES_AND_GRANTS_SQL)
        await acquired_conn.execute(RLS_FUNCTIONS_AND_POLICIES_SQL)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local"), override=True)

    async def _main():
        await create_pool()
        try:
            await ensure_rls_security()
            print("RDS PostgreSQL permission roles and RLS policies applied successfully.")
        finally:
            await close_pool()

    asyncio.run(_main())
