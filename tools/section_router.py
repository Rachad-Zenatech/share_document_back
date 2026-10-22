"""FastAPI router that exposes document sections (Markdown headings) to the frontend."""

import pathlib

from fastapi import APIRouter

from tools.section_utils import extract_headings

router = APIRouter()

# Repository root is two levels above this file:  tools/ -> repo root
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@router.get(
    "/sections",
    tags=["Document Sections"],
    summary="List all document section headings",
    response_description="Array of heading objects (level, title, file).",
)
def list_sections():
    """
    Return every Markdown heading found across the repository's ``.md`` files.

    The frontend uses this endpoint to populate the **Document Sections** panel.
    New headings are picked up automatically — just add them to any ``.md`` file
    inside the repository and they will appear here on the next request.

    Each item in the response contains:

    * ``level``  – heading depth 1–6 (``#`` = 1, ``##`` = 2, …)
    * ``title``  – heading text with hashes stripped
    * ``file``   – POSIX-style path relative to the repository root
    """
    headings = extract_headings(_REPO_ROOT)
    return [
        {"level": level, "title": title, "file": file}
        for level, title, file in headings
    ]
