import asyncio
import os
import sys
from dotenv import load_dotenv
import uuid
from postgresql_db.database import create_pool, close_pool, get_pool

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

async def setup():
    await create_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        print("Creating core RBAC & Template schema tables...")
        await conn.execute("""
            -- Extensions
            CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
            CREATE EXTENSION IF NOT EXISTS "pgcrypto";

            -- Users
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email VARCHAR(255) UNIQUE NOT NULL,
                full_name VARCHAR(255),
                department VARCHAR(255),
                job_title VARCHAR(255),
                auth_provider VARCHAR(50) DEFAULT 'microsoft',
                entra_oid VARCHAR(255) UNIQUE,
                is_active BOOLEAN DEFAULT TRUE,
                is_super_admin BOOLEAN DEFAULT FALSE,
                last_login_at TIMESTAMP WITH TIME ZONE,
                last_activity_at TIMESTAMP WITH TIME ZONE,
                last_logout_at TIMESTAMP WITH TIME ZONE,
                deleted_at TIMESTAMP WITH TIME ZONE,
                deleted_by UUID,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_by UUID
            );
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);

            -- Roles
            CREATE TABLE IF NOT EXISTS roles (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                is_system_role BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                parent_role_id UUID REFERENCES roles(id) ON DELETE SET NULL,
                display_order INT DEFAULT 0,
                department VARCHAR(255),
                deleted_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_roles_code ON roles(code);

            -- User Roles
            CREATE TABLE IF NOT EXISTS user_roles (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                is_active BOOLEAN DEFAULT TRUE,
                assigned_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                assigned_by UUID REFERENCES users(id) ON DELETE SET NULL,
                UNIQUE(user_id, role_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_roles_user_id ON user_roles(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_roles_role_id ON user_roles(role_id);

            -- Navigation Items
            CREATE TABLE IF NOT EXISTS navigation_items (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                route_path VARCHAR(255),
                parent_code VARCHAR(100),
                display_order INT DEFAULT 0,
                icon VARCHAR(100),
                is_menu_item BOOLEAN DEFAULT TRUE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Permission Actions
            CREATE TABLE IF NOT EXISTS permission_actions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Role Navigation Permissions
            CREATE TABLE IF NOT EXISTS role_navigation_permissions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                navigation_item_id UUID NOT NULL REFERENCES navigation_items(id) ON DELETE CASCADE,
                action_id UUID NOT NULL REFERENCES permission_actions(id) ON DELETE CASCADE,
                is_allowed BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(role_id, navigation_item_id, action_id)
            );

            -- PBAC Permission Modules
            CREATE TABLE IF NOT EXISTS permission_modules (
                id SERIAL PRIMARY KEY,
                code VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                sort_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- PBAC Permission Groups
            CREATE TABLE IF NOT EXISTS permission_groups (
                id SERIAL PRIMARY KEY,
                module_id INT REFERENCES permission_modules(id) ON DELETE CASCADE,
                code VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                sort_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- PBAC Permission Group Actions
            CREATE TABLE IF NOT EXISTS permission_group_actions (
                id SERIAL PRIMARY KEY,
                permission_group_id INT REFERENCES permission_groups(id) ON DELETE CASCADE,
                api_module_code VARCHAR(255) NOT NULL,
                action VARCHAR(255) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Role PBAC Group Mappings
            CREATE TABLE IF NOT EXISTS role_permission_groups (
                id SERIAL PRIMARY KEY,
                role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                permission_group_id INT REFERENCES permission_groups(id) ON DELETE CASCADE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(role_id, permission_group_id)
            );

            -- MCP Tools
            CREATE TABLE IF NOT EXISTS mcp_tools (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                server_name VARCHAR(100) NOT NULL,
                description TEXT,
                is_read_only BOOLEAN DEFAULT FALSE,
                is_sensitive BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Role MCP Tool Permissions
            CREATE TABLE IF NOT EXISTS role_mcp_tool_permissions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                mcp_tool_id UUID NOT NULL REFERENCES mcp_tools(id) ON DELETE CASCADE,
                is_allowed BOOLEAN DEFAULT TRUE,
                access_level VARCHAR(50) DEFAULT 'ALLOW',
                conditions JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(role_id, mcp_tool_id)
            );

            -- Audit Logs
            CREATE TABLE IF NOT EXISTS audit_logs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                action VARCHAR(100) NOT NULL,
                entity_type VARCHAR(100) NOT NULL,
                entity_id VARCHAR(100),
                old_value JSONB,
                new_value JSONB,
                ip_address INET,
                user_agent TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);

            -- Login Activity Logs
            CREATE TABLE IF NOT EXISTS login_activity_logs (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                success BOOLEAN NOT NULL,
                failure_reason TEXT,
                ip_address VARCHAR(100),
                user_agent TEXT,
                logout_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_login_activity_logs_user_id ON login_activity_logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_login_activity_logs_created_at ON login_activity_logs(created_at DESC);

            -- Notifications (Real-time SSE and in-app alerts)
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                type VARCHAR(100) NOT NULL,
                title VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                link_url VARCHAR(500),
                entity_type VARCHAR(100),
                entity_id VARCHAR(100),
                sender_name VARCHAR(255),
                sender_avatar VARCHAR(500),
                attachments JSONB,
                is_read BOOLEAN DEFAULT FALSE,
                read_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications(is_read);

            -- App Settings
            CREATE TABLE IF NOT EXISTS app_settings (
                key VARCHAR(100) PRIMARY KEY,
                settings_json TEXT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)

        print("Seeding permission actions...")
        actions = [
            ("VIEW", "View / Read access"),
            ("CREATE", "Create / Add access"),
            ("UPDATE", "Edit / Update access"),
            ("DELETE", "Delete access"),
            ("EXECUTE", "Execution access"),
            ("APPROVE", "Approval access"),
        ]
        for code, name in actions:
            await conn.execute("""
                INSERT INTO permission_actions (code, name)
                VALUES ($1, $2)
                ON CONFLICT (code) DO NOTHING
            """, code, name)

        print("Seeding navigation items...")
        nav_items = [
            ("DASHBOARD", "Dashboard", "/dashboard", None, 10, "LayoutDashboard", True),
            ("CONFIGURATIONS", "Configurations", "/configuration", None, 20, "Settings", True),
            ("CONFIG_USERS", "Users", "/configuration/users", "CONFIGURATIONS", 21, "Users", True),
            ("CONFIG_ROLES", "Roles & Permissions", "/configuration/roles", "CONFIGURATIONS", 22, "Shield", True),
            ("CONFIG_USER_ROLE_ASSIGNMENT", "User Role Assignment", "/configuration/user-role-assignment", "CONFIGURATIONS", 23, "UserCheck", True),
            ("CONFIG_ROLE_API_PERMISSIONS", "Role API Permissions", "/configuration/role-api-permissions", "CONFIGURATIONS", 24, "Lock", True),
            ("CONFIG_ROLE_MCP_TOOL_PERMISSIONS", "Role MCP Permissions", "/configuration/role-mcp-tool-permissions", "CONFIGURATIONS", 25, "Bot", True),
            ("LOGS", "Logs", "/log", None, 30, "FileText", True),
            ("AUDIT_LOG", "Audit Logs", "/log/audit", "LOGS", 31, "History", True),
            ("SYSTEM_LOGS", "System Logs", "/log/system-logs", "LOGS", 32, "Terminal", True),
        ]
        for code, name, path, parent, order, icon, is_menu in nav_items:
            await conn.execute("""
                INSERT INTO navigation_items (code, name, route_path, parent_code, display_order, icon, is_menu_item, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7, true)
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    route_path = EXCLUDED.route_path,
                    parent_code = EXCLUDED.parent_code,
                    display_order = EXCLUDED.display_order,
                    icon = EXCLUDED.icon,
                    is_active = true
            """, code, name, path, parent, order, icon, is_menu)

        print("Seeding permission modules and groups...")
        await conn.execute("TRUNCATE permission_modules CASCADE")

        modules = [
            ("MAIN", "Dashboard & Overview", 10),
            ("CONFIGURATIONS", "Configurations & Security", 20),
            ("LOGS", "Audit & System Logs", 30),
        ]
        for m in modules:
            await conn.execute("INSERT INTO permission_modules (code, name, sort_order) VALUES ($1, $2, $3)", *m)

        groups_data = {
            "MAIN": [
                ("MAIN_VIEW_DASHBOARD", "View Dashboard", "Access system KPI dashboards and metrics.", 10, [("DASHBOARD", "VIEW")]),
            ],
            "CONFIGURATIONS": [
                ("CONFIG_VIEW_USERS", "View Users & Roles", "View all users, roles, and permission assignments.", 10, [("CONFIG_USERS", "VIEW"), ("CONFIG_ROLES", "VIEW")]),
                ("CONFIG_MANAGE_USERS", "Manage Users", "Create, edit, activate/deactivate system users.", 20, [("CONFIG_USERS", "CREATE"), ("CONFIG_USERS", "UPDATE"), ("CONFIG_USERS", "DELETE")]),
                ("CONFIG_MANAGE_ROLES", "Manage Roles & Permissions", "Create, edit, or configure roles and permission groups.", 30, [("CONFIG_ROLES", "CREATE"), ("CONFIG_ROLES", "UPDATE"), ("CONFIG_ROLES", "DELETE")]),
                ("CONFIG_ASSIGN_ROLES", "User Role Assignment", "Assign roles and configure user permissions.", 40, [("CONFIG_USER_ROLE_ASSIGNMENT", "CREATE"), ("CONFIG_USER_ROLE_ASSIGNMENT", "UPDATE")]),
                ("CONFIG_MCP_PERMISSIONS", "MCP Tool Permissions", "Configure MCP tool access rules by role.", 50, [("CONFIG_ROLE_MCP_TOOL_PERMISSIONS", "VIEW"), ("CONFIG_ROLE_MCP_TOOL_PERMISSIONS", "UPDATE")]),
            ],
            "LOGS": [
                ("LOGS_VIEW_AUDIT", "View Audit Logs", "View system audit logs and action history.", 10, [("AUDIT_LOG", "VIEW")]),
                ("LOGS_VIEW_SYSTEM", "View System Logs", "Access system error and diagnostic logs.", 20, [("SYSTEM_LOGS", "VIEW")]),
            ],
        }

        for module_code, groups in groups_data.items():
            module_id = await conn.fetchval("SELECT id FROM permission_modules WHERE code = $1", module_code)
            for g_code, g_name, g_desc, g_sort, actions_list in groups:
                g_id = await conn.fetchval("""
                    INSERT INTO permission_groups (module_id, code, name, description, sort_order)
                    VALUES ($1, $2, $3, $4, $5) RETURNING id
                """, module_id, g_code, g_name, g_desc, g_sort)

                for nav_code, act_code in actions_list:
                    await conn.execute("""
                        INSERT INTO permission_group_actions (permission_group_id, api_module_code, action)
                        VALUES ($1, $2, $3)
                    """, g_id, nav_code, act_code)

        print("Seeding standard roles...")
        roles_data = [
            ("SUPER_ADMIN", "Super Administrator", "Full unrestricted access to all modules and configurations", True),
            ("ADMIN", "Administrator", "Administrative access to user and role configurations", True),
            ("USER", "Standard User", "Standard system user with basic dashboard access", False),
        ]
        for r_code, r_name, r_desc, is_sys in roles_data:
            await conn.execute("""
                INSERT INTO roles (code, name, description, is_system_role, is_active)
                VALUES ($1, $2, $3, $4, true)
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    is_active = true
            """, r_code, r_name, r_desc, is_sys)

        super_admin_role_id = await conn.fetchval("SELECT id FROM roles WHERE code = 'SUPER_ADMIN'")
        if super_admin_role_id:
            await conn.execute("""
                INSERT INTO role_permission_groups (role_id, permission_group_id)
                SELECT $1, id FROM permission_groups
                ON CONFLICT DO NOTHING
            """, super_admin_role_id)

        # Seed Super Admin Users
        admin_emails_env = os.getenv("INITIAL_SUPER_ADMIN_EMAILS", "admin@example.com")
        admin_emails = [e.strip().lower() for e in admin_emails_env.split(",") if e.strip()]

        for email in admin_emails:
            full_name = email.split('@')[0].replace('.', ' ').title()
            user_id = await conn.fetchval("""
                INSERT INTO users (email, full_name, is_active, is_super_admin)
                VALUES ($1, $2, true, true)
                ON CONFLICT (email) DO UPDATE SET is_super_admin = true, is_active = true
                RETURNING id
            """, email, full_name)

            if super_admin_role_id and user_id:
                await conn.execute("""
                    INSERT INTO user_roles (user_id, role_id, is_active)
                    VALUES ($1, $2, true)
                    ON CONFLICT (user_id, role_id) DO UPDATE SET is_active = true
                """, user_id, super_admin_role_id)

        print("RBAC and template schema initialization complete!")

    await close_pool()

if __name__ == "__main__":
    asyncio.run(setup())
