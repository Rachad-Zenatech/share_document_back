from services.auth_service import get_my_permissions, current_user_id_ctx
from postgresql_db.database import fetch_all

async def list_my_accessible_tools_tool():
    """
    List all MCP tools and capabilities accessible to the currently authenticated user based on active RBAC roles.

    What it does:
        Queries the system RBAC catalog to return all tools, operational capabilities, and actions permitted for the current user session.

    When to use:
        - When the user asks "What tools do I have access to?"
        - "What can you do for me?"
        - "Why don't I have permission to approve requests?"
        - When informing a user about their role-based tool limitations.

    Returns:
        str: Formatted markdown list of available tools with their names and descriptions.

    Side Effects / Confirmation:
        Read-only.
    """
    user_id = current_user_id_ctx.get()
    if not user_id:
        return "You are not authenticated in this session."

    perms = await get_my_permissions(user_id)
    if not perms:
        return "Could not retrieve user permissions."

    tool_codes = perms.get("mcp_tool_permissions", [])

    is_super_admin = perms.get("user", {}).get("is_super_admin") or any(r.get("code") == "SUPER_ADMIN" for r in perms.get("roles", []))

    if is_super_admin:
        sql = "SELECT name, description FROM mcp_tools WHERE is_active = true ORDER BY name"
        tools = await fetch_all(sql)
    else:
        if not tool_codes:
            return "You currently do not have permission to execute specialized administrative tools. You can still ask general policy and workflow questions."
        placeholders = ", ".join(f"${i+1}" for i in range(len(tool_codes)))
        sql = f"SELECT name, description FROM mcp_tools WHERE code IN ({placeholders}) AND is_active = true ORDER BY name"
        tools = await fetch_all(sql, *tool_codes)

    if not tools:
        return "No active tools found for your assigned permissions."

    result = "### Your Accessible Administrative Tools:\n"
    for i, t in enumerate(tools):
        desc = t.get('description', '') or '(No description provided)'
        result += f"{i+1}. **{t['name']}**: {desc}\n"

    return result
