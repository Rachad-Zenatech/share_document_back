"""
Performance Indexes for High-Impact Queries
Adds composite and partial indexes based on EXPLAIN ANALYZE query plans
to eliminate sequential scans, in-memory sorts, and reduce query latency.
"""
import asyncio
import logging
from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)

INDEXES_DDL = """
-- ============================================================================
-- 1. Users Table Indexes
-- ============================================================================
-- Case-insensitive email lookup for login, SSO, and token verification
CREATE INDEX IF NOT EXISTS idx_users_lower_email
    ON users (LOWER(email))
    WHERE deleted_at IS NULL;

-- Fast index-only lookup for active users in auth dependency
CREATE INDEX IF NOT EXISTS idx_users_active_live
    ON users (id, is_super_admin)
    WHERE deleted_at IS NULL AND is_active = true;

-- Optimized listing for user administration
CREATE INDEX IF NOT EXISTS idx_users_created_at
    ON users (created_at DESC)
    WHERE deleted_at IS NULL;

-- ============================================================================
-- 2. Roles Table Indexes
-- ============================================================================
-- Hierarchy navigation (recursive CTE role tree)
CREATE INDEX IF NOT EXISTS idx_roles_parent_live
    ON roles (parent_role_id)
    WHERE deleted_at IS NULL;

-- Fast active role code lookup
CREATE INDEX IF NOT EXISTS idx_roles_active_live
    ON roles (id, code)
    WHERE deleted_at IS NULL AND is_active = true;

-- ============================================================================
-- 3. User Roles Table Indexes
-- ============================================================================
-- Composite index for instant user active role resolution (Index-Only Scan)
CREATE INDEX IF NOT EXISTS idx_user_roles_user_active
    ON user_roles (user_id, role_id)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_user_roles_role_active
    ON user_roles (role_id, user_id)
    WHERE is_active = true;

-- ============================================================================
-- 4. Role Navigation & MCP Tool Permissions
-- ============================================================================
-- Covering index for granted navigation page permissions
CREATE INDEX IF NOT EXISTS idx_rnp_role_allowed
    ON role_navigation_permissions (role_id, navigation_item_id, action_id)
    WHERE is_allowed = true;

-- Covering index for granted MCP tool permissions
CREATE INDEX IF NOT EXISTS idx_rmtp_role_allowed
    ON role_mcp_tool_permissions (role_id, mcp_tool_id)
    WHERE is_allowed = true;

-- ============================================================================
-- 5. PBAC Permission Groups & Actions Foreign Keys
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_pga_group_id
    ON permission_group_actions (permission_group_id);

CREATE INDEX IF NOT EXISTS idx_pg_module_id
    ON permission_groups (module_id);

-- ============================================================================
-- 6. Login Activity Logs Indexes
-- ============================================================================
-- Partial composite index for fast heartbeat and recent login lookups without in-memory sort
CREATE INDEX IF NOT EXISTS idx_login_activity_user_success_created
    ON login_activity_logs (user_id, created_at DESC)
    WHERE success = true;

CREATE INDEX IF NOT EXISTS idx_login_activity_email_created
    ON login_activity_logs (email, created_at DESC);

-- ============================================================================
-- 7. Audit Logs Composite Indexes
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor_created
    ON audit_logs (actor_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_logs_entity_created
    ON audit_logs (entity_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_logs_action_created
    ON audit_logs (action, created_at DESC);

-- ============================================================================
-- 8. Notifications Composite & Partial Indexes
-- ============================================================================
-- User notification inbox with ordered creation date
CREATE INDEX IF NOT EXISTS idx_notifications_user_created
    ON notifications (user_id, created_at DESC);

-- Fast unread notification badge count and mark-read batch operations
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
    ON notifications (user_id)
    WHERE is_read = false;

-- ============================================================================
-- 9. SEC Filing Signatures & Spreadsheets
-- ============================================================================
-- Transient envelope expiration cleanup (runs every dispatch)
CREATE INDEX IF NOT EXISTS idx_sec_sig_env_created
    ON sec_signature_envelopes (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sec_sig_env_doc_status
    ON sec_signature_envelopes (document_id, status);
"""

async def ensure_performance_indexes() -> None:
    """Applies high-impact composite and partial indexes idempotently."""
    pool = get_pool()
    async with pool.acquire() as conn:
        logger.info("Ensuring high-impact composite and partial performance indexes...")
        await conn.execute(INDEXES_DDL)
        logger.info("Performance indexes ensured successfully.")

if __name__ == "__main__":
    from dotenv import load_dotenv
    from postgresql_db.database import create_pool, close_pool

    load_dotenv()

    async def main():
        await create_pool()
        await ensure_performance_indexes()
        await close_pool()
        print("Performance indexes applied successfully.")

    asyncio.run(main())
