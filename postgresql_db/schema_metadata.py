"""
SQLAlchemy Core schema mirror used ONLY for Alembic autogenerate.
Contains core RBAC, Auth, Audit, and Notification tables for the template.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

app_settings = sa.Table(
    "app_settings", metadata,
    sa.Column("key", sa.VARCHAR(length=100), primary_key=True),
    sa.Column("settings_json", sa.TEXT()),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("email", sa.VARCHAR(length=255), nullable=False),
    sa.Column("full_name", sa.VARCHAR(length=255)),
    sa.Column("department", sa.VARCHAR(length=255)),
    sa.Column("job_title", sa.VARCHAR(length=255)),
    sa.Column("auth_provider", sa.VARCHAR(length=50), server_default=sa.text("'microsoft'::character varying")),
    sa.Column("entra_oid", sa.VARCHAR(length=255)),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("is_super_admin", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("last_login_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("last_activity_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("last_logout_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("deleted_by", sa.UUID()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_by", sa.UUID()),
    sa.UniqueConstraint("email", name="users_email_key"),
    sa.UniqueConstraint("entra_oid", name="users_entra_oid_key"),
)

roles = sa.Table(
    "roles", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("is_system_role", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("parent_role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="SET NULL")),
    sa.Column("display_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("department", sa.VARCHAR(length=255)),
    sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="roles_code_key"),
)

user_roles = sa.Table(
    "user_roles", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("assigned_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("assigned_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.UniqueConstraint("user_id", "role_id", name="user_roles_user_id_role_id_key"),
)

navigation_items = sa.Table(
    "navigation_items", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("route_path", sa.VARCHAR(length=255)),
    sa.Column("parent_code", sa.VARCHAR(length=100)),
    sa.Column("display_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("icon", sa.VARCHAR(length=100)),
    sa.Column("is_menu_item", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="navigation_items_code_key"),
)

permission_actions = sa.Table(
    "permission_actions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_actions_code_key"),
)

role_navigation_permissions = sa.Table(
    "role_navigation_permissions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("navigation_item_id", sa.UUID(), sa.ForeignKey("navigation_items.id", ondelete="CASCADE"), nullable=False),
    sa.Column("action_id", sa.UUID(), sa.ForeignKey("permission_actions.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_allowed", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "navigation_item_id", "action_id", name="role_navigation_permissions_role_id_navigation_item_id_act_key"),
)

permission_modules = sa.Table(
    "permission_modules", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_modules_id_seq'::regclass)")),
    sa.Column("code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("sort_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_modules_code_key"),
)

permission_groups = sa.Table(
    "permission_groups", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_groups_id_seq'::regclass)")),
    sa.Column("module_id", sa.INTEGER(), sa.ForeignKey("permission_modules.id", ondelete="CASCADE")),
    sa.Column("code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("sort_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_groups_code_key"),
)

permission_group_actions = sa.Table(
    "permission_group_actions", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_group_actions_id_seq'::regclass)")),
    sa.Column("permission_group_id", sa.INTEGER(), sa.ForeignKey("permission_groups.id", ondelete="CASCADE")),
    sa.Column("api_module_code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("action", sa.VARCHAR(length=255), nullable=False),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

role_permission_groups = sa.Table(
    "role_permission_groups", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('role_permission_groups_id_seq'::regclass)")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("permission_group_id", sa.INTEGER(), sa.ForeignKey("permission_groups.id", ondelete="CASCADE"), nullable=False),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "permission_group_id", name="role_permission_groups_role_id_permission_group_id_key"),
)

mcp_tools = sa.Table(
    "mcp_tools", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("server_name", sa.VARCHAR(length=100), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("is_read_only", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_sensitive", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="mcp_tools_code_key"),
)

role_mcp_tool_permissions = sa.Table(
    "role_mcp_tool_permissions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("mcp_tool_id", sa.UUID(), sa.ForeignKey("mcp_tools.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_allowed", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("access_level", sa.VARCHAR(length=50), server_default=sa.text("'ALLOW'::character varying")),
    sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "mcp_tool_id", name="role_mcp_tool_permissions_role_id_mcp_tool_id_key"),
)

audit_logs = sa.Table(
    "audit_logs", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("actor_user_id", sa.UUID(), sa.ForeignKey("users.id")),
    sa.Column("action", sa.VARCHAR(length=100), nullable=False),
    sa.Column("entity_type", sa.VARCHAR(length=100), nullable=False),
    sa.Column("entity_id", sa.VARCHAR(length=100)),
    sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("ip_address", postgresql.INET()),
    sa.Column("user_agent", sa.TEXT()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
)

login_activity_logs = sa.Table(
    "login_activity_logs", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('login_activity_logs_id_seq'::regclass)")),
    sa.Column("email", sa.VARCHAR(length=255), nullable=False),
    sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id")),
    sa.Column("success", sa.BOOLEAN(), nullable=False),
    sa.Column("failure_reason", sa.TEXT()),
    sa.Column("ip_address", sa.VARCHAR(length=100)),
    sa.Column("user_agent", sa.TEXT()),
    sa.Column("logout_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

notifications = sa.Table(
    "notifications", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('notifications_id_seq'::regclass)")),
    sa.Column("user_id", sa.VARCHAR(length=255), nullable=False),
    sa.Column("type", sa.VARCHAR(length=100), nullable=False),
    sa.Column("title", sa.VARCHAR(length=255), nullable=False),
    sa.Column("message", sa.TEXT(), nullable=False),
    sa.Column("link_url", sa.VARCHAR(length=500)),
    sa.Column("entity_type", sa.VARCHAR(length=100)),
    sa.Column("entity_id", sa.VARCHAR(length=100)),
    sa.Column("sender_name", sa.VARCHAR(length=255)),
    sa.Column("sender_avatar", sa.VARCHAR(length=500)),
    sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("is_read", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("read_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)
