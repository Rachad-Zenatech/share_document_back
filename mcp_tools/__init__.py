# mcp_tools/__init__.py
# Central registry for all MCP tools and prompts. Each tool/prompt is a plain
# module-level function; `register_all` attaches tools via `mcp.add_tool` and
# prompts via `mcp.add_prompt`, deriving the name and description from the
# function name and docstring.
import os
import re
import inspect
import asyncio
import json
import logging

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors
from fastmcp.prompts import Prompt

from mcp_tools.prompts import ALL_PROMPTS
from mcp_tools.permissions import list_my_accessible_tools_tool
from services.auth_service import current_user_id_ctx, user_can_use_mcp_tool, get_my_permissions

load_dotenv()
logger = logging.getLogger(__name__)

# Registered MCP tools for this template
ALL_TOOLS = [
    list_my_accessible_tools_tool,
]

MCP_TOOL_CODE_MAP = {
    "list_my_accessible_tools_tool": "PERMISSIONS_VIEW",
}

# Setup Gemini Client if API key is provided
gemini_key = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=gemini_key) if gemini_key else None


def _normalize_ai_model_name(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _ai_model_candidates(model: str = None) -> list:
    if model:
        return [_normalize_ai_model_name(model)]

    configured = os.getenv("GEMINI_AVAILABLE_MODELS", "gemini-2.5-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-3.1-pro-preview")
    candidates = [_normalize_ai_model_name(item) for item in configured.split(",") if item.strip()]

    deprecated = {
        "gemini-2.0-pro", "gemini-2.0-flash", "gemini-2.0-flash-lite",
        "gemini-2-flash", "gemini-2-flash-lite", "gemini-2",
        "gemini-1.5-flash", "gemini-1.5-pro",
    }

    unique_candidates = [c for c in candidates if c not in deprecated]
    return unique_candidates or ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]


async def generate_with_fallback(contents, config):
    """Call Gemini with retries and progressive model fallback."""
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    models = _ai_model_candidates()
    last_error = None

    for model in models:
        for attempt in range(2):
            try:
                return await gemini_client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
            except errors.ServerError as e:
                last_error = e
                await asyncio.sleep(0.5)
            except errors.APIError as e:
                last_error = e
                break
            except Exception as e:
                last_error = e
                break

    raise last_error or RuntimeError("All Gemini model attempts failed.")


async def generate_stream_with_fallback(contents, config):
    """Stream Gemini response with fallback handling."""
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    models = _ai_model_candidates()
    last_error = None

    for model in models:
        try:
            stream = await gemini_client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
            async for chunk in stream:
                yield chunk
            return
        except Exception as e:
            last_error = e
            logger.warning(f"Streaming failed on model {model}: {e}")
            continue

    raise last_error or RuntimeError("Streaming failed on all configured models.")


def register_all(mcp):
    """Register every tool and prompt with the given FastMCP instance."""
    for tool_fn in ALL_TOOLS:
        mcp.add_tool(tool_fn)
    for prompt_fn in ALL_PROMPTS:
        mcp.add_prompt(prompt_fn)


async def ask_gemini_stream(message: str, history: list = None, file_data: str = None, mime_type: str = None):
    """Stream response from Gemini model for AI Chat interface."""
    if not gemini_client:
        yield f"data: {json.dumps({'type': 'error', 'message': 'GEMINI_API_KEY is not configured.'})}\n\n"
        return

    config = types.GenerateContentConfig(
        temperature=0.3,
        system_instruction="You are an enterprise AI assistant helping the user navigate the administrative portal and manage RBAC roles and permissions.",
    )

    contents = []
    if history:
        for h in history:
            role = "user" if h.role == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h.content)]))

    user_parts = [types.Part.from_text(text=message)]
    contents.append(types.Content(role="user", parts=user_parts))

    try:
        async for chunk in generate_stream_with_fallback(contents=contents, config=config):
            if chunk.candidates and chunk.candidates[0].content:
                for p in (chunk.candidates[0].content.parts or []):
                    if getattr(p, "text", None):
                        yield f"data: {json.dumps({'type': 'text', 'content': p.text})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


async def ask_gemini(message: str, file_path: str = None) -> str:
    """Convenience synchronous-like wrapper for non-streaming Gemini calls."""
    if not gemini_client:
        return "GEMINI_API_KEY is not configured."
    res = await generate_with_fallback(contents=message, config=None)
    return res.text if res else "No response generated."


async def sync_mcp_tools_to_vector_db():
    """Sync registered tools to PostgreSQL mcp_tools table."""
    from postgresql_db.database import get_pool
    try:
        pool = get_pool()
        if pool is not None:
            async with pool.acquire() as conn:
                await conn.execute('UPDATE mcp_tools SET is_active = false')
                for tool in ALL_TOOLS:
                    name = tool.__name__
                    desc = inspect.getdoc(tool) or name
                    code = MCP_TOOL_CODE_MAP.get(name, name)
                    friendly_name = name.replace('_tool', '').replace('_', ' ').title()

                    await conn.execute('''
                        INSERT INTO mcp_tools (name, code, server_name, description, is_read_only, is_sensitive, is_active)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (code) DO UPDATE SET
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            is_active = true
                    ''', friendly_name, code, "template-mcp", desc, True, False, True)
    except Exception as e:
        logger.warning(f"MCP tool sync skipped or errored: {e}")
