"""Helper for extracting Markdown headings used by the frontend document-sections list."""

import pathlib
import re
from typing import List, Tuple

# Match any ATX heading: # ... through ###### ...
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)", re.MULTILINE)

# Directories (relative to repo root) that are always excluded from the scan.
_EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".agents", ".cursor"}


def _iter_markdown_files(root: pathlib.Path) -> List[pathlib.Path]:
    """Yield every *.md file under *root* that is not in an excluded directory."""
    files: List[pathlib.Path] = []
    for path in root.rglob("*.md"):
        # Skip files inside excluded directories
        if any(part in _EXCLUDE_DIRS for part in path.parts):
            continue
        files.append(path)
    files.sort()
    return files


def extract_headings(root: pathlib.Path) -> List[Tuple[int, str, str]]:
    """
    Scan all Markdown files under *root* and return their headings.

    Returns a list of ``(level, title, relative_path)`` tuples where:

    * **level** – heading depth (1–6)
    * **title** – text content of the heading (hashes stripped)
    * **relative_path** – POSIX-style path relative to *root*
    """
    headings: List[Tuple[int, str, str]] = []
    for md_path in _iter_markdown_files(root):
        try:
            content = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = md_path.relative_to(root).as_posix()
        for match in HEADING_RE.finditer(content):
            level = len(match.group(1))
            title = match.group(2).strip()
            if title:
                headings.append((level, title, rel))
    return headings
