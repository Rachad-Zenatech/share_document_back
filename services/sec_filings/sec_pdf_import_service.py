"""Convert an uploaded PDF into the SEC editor's document blocks.

Pipeline, per chunk of pages:
  1. Text layer: PyMuPDF for digital PDFs, Tesseract OCR for scanned ones
     (services/sec_filings/pdf_extraction_service.py, shared with the admin portal).
  2. Gemini reads the pages themselves (it does its own visual OCR) together with that
     text, and returns headings, paragraphs and financial tables as structured JSON.
  3. Verification: the AI output must keep the words and the figures of the chunk's text
     layer. Gemini can drop or invent text; a chunk that loses words or figures is
     converted straight from the PDF layout instead, and the report says so.

Without GEMINI_API_KEY, or when Gemini fails, every chunk uses the direct layout
conversion (font sizes for headings, PyMuPDF table detection for tables).

Conversions run as in-memory background jobs: a long filing takes longer than proxies
(CloudFront) allow a single request to run. The server runs one worker process
(see Dockerfile), so a job is always polled from the process that holds it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import statistics
import threading
import time
import uuid
from collections import Counter
from typing import Any, Literal, Optional
from uuid import UUID

import pymupdf
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from services.ai.gemini_service import generate_content_with_fallback, get_gemini_client
from services.sec_filings.pdf_extraction_service import extract_document_layout

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PAGES = int(os.getenv("SEC_PDF_IMPORT_MAX_PAGES", "300"))
PAGES_PER_CHUNK = max(1, int(os.getenv("SEC_PDF_IMPORT_PAGES_PER_CHUNK", "4")))
MAX_CONCURRENT_CHUNKS = max(1, int(os.getenv("SEC_PDF_IMPORT_CONCURRENCY", "3")))
# Share of the text layer's words / figures the AI output must keep. OCR text is noisy
# (and Gemini usually reads scans better than Tesseract), so scans get a lower bar.
MIN_WORD_COVERAGE = 0.92
MIN_WORD_COVERAGE_OCR = 0.70
MIN_FIGURE_COVERAGE = 0.98
MAX_INLINE_PDF_BYTES = 18 * 1024 * 1024
JOB_TTL_SECONDS = 3600


# --------------------------------------------------------------------------- AI output schema


class AiTableRow(BaseModel):
    type: Literal["header", "category_header", "data", "subtotal", "total"] = "data"
    cells: list[str] = Field(default_factory=list)
    indent: int = 0


class AiBlock(BaseModel):
    type: Literal["heading", "paragraph", "table"]
    level: Optional[int] = None
    text: Optional[str] = None
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    alignment: Optional[Literal["left", "center", "right", "justify"]] = None
    title: Optional[str] = None
    headers: Optional[list[str]] = None
    rows: Optional[list[AiTableRow]] = None


class AiChunkResult(BaseModel):
    blocks: list[AiBlock] = Field(default_factory=list)


_AI_INSTRUCTIONS = """You convert pages of an SEC filing (10-K, 10-Q, financial statements, notes) into structured document blocks.

Return JSON: {"blocks": [...]} in reading order. Each block is one of:
- {"type": "heading", "level": 1-4, "text": ..., "alignment": ...}: titles, PART / ITEM / NOTE headings, statement titles. Level 1 for the document or PART titles, 2 for ITEM or statement titles, 3 for NOTE and sub-headings, 4 for minor captions.
- {"type": "paragraph", "text": ..., "bold": ..., "italic": ..., "alignment": ...}: one block per paragraph. Join lines that were wrapped on the page into one paragraph; keep bullet or list markers at the start of the text.
- {"type": "table", "title": ..., "headers": [...], "rows": [{"type": ..., "cells": [...], "indent": 0-3}]}: every table, especially financial statements. "headers" are the column captions (first one is usually blank or "Description"). Every row has exactly one cell per header. Row types: "header" for extra caption rows (e.g. period years), "category_header" for a label row with no figures, "data", "subtotal", "total". Use "indent" for indented line items.

Rules:
1. Copy text EXACTLY. Never summarize, paraphrase, correct, translate or invent anything. Every word and every figure on the pages must appear once in the output.
2. Figures: keep them exactly as printed, including $, commas, parentheses for negatives, %, and dashes for zero (e.g. "(1,234)", "—").
3. Leave out running page headers, page footers and page numbers that repeat on every page.
4. A table that continues from a previous page: output its rows with the same headers.
5. If a page is a scan, read it visually; the extracted text below may contain OCR mistakes, so trust the page image for spelling and figures."""


# --------------------------------------------------------------------------- verification

_TOKEN_RE = re.compile(r"[A-Za-z]+|\d[\d,]*(?:\.\d+)?")


def _tokens(text: str) -> tuple[Counter, Counter]:
    """Lower-cased words and normalised figures (commas removed) of a text."""
    words: Counter = Counter()
    figures: Counter = Counter()
    for tok in _TOKEN_RE.findall(text or ""):
        if tok[0].isdigit():
            fig = tok.replace(",", "")
            # Page numbers and list markers are not figures worth checking.
            if len(fig.replace(".", "")) >= 3:
                figures[fig] += 1
        elif len(tok) > 1:
            words[tok.lower()] += 1
    return words, figures


def _coverage(source: Counter, output: Counter) -> float:
    total = sum(source.values())
    if total == 0:
        return 1.0
    return sum(min(n, output.get(k, 0)) for k, n in source.items()) / total


def _blocks_text(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for b in blocks:
        if b.get("text"):
            parts.append(b["text"])
        if b.get("title"):
            parts.append(b["title"])
        parts.extend(b.get("headers") or [])
        for row in b.get("rows") or []:
            parts.extend(row.get("cells") or [])
    return "\n".join(parts)


def _verify(source_text: str, blocks: list[dict[str, Any]], ocr: bool) -> dict[str, Any]:
    src_words, src_figures = _tokens(source_text)
    out_words, out_figures = _tokens(_blocks_text(blocks))
    word_cov = _coverage(src_words, out_words)
    fig_cov = _coverage(src_figures, out_figures)
    missing_figures = [f for f in src_figures if out_figures.get(f, 0) < src_figures[f]]
    # Figures the AI produced that the page does not contain (possible hallucination).
    extra_figures = [f for f in out_figures if out_figures[f] > src_figures.get(f, 0)]
    min_words = MIN_WORD_COVERAGE_OCR if ocr else MIN_WORD_COVERAGE
    # On a digital PDF the text layer is exact, so a figure that is not on the page was
    # invented. OCR misreads figures, so for scans that is reported but not rejected.
    passed = word_cov >= min_words and (ocr or (fig_cov >= MIN_FIGURE_COVERAGE and not extra_figures))
    return {
        "wordCoverage": round(word_cov, 4),
        "figureCoverage": round(fig_cov, 4),
        "missingFigures": missing_figures[:20],
        "extraFigures": extra_figures[:20],
        "passed": passed,
    }


# --------------------------------------------------------------------------- direct layout conversion


def _is_page_number(text: str) -> bool:
    return bool(re.fullmatch(r"(page\s+)?\d{1,4}(\s+of\s+\d{1,4})?|[-–—]\s*\d{1,4}\s*[-–—]", text.strip(), re.I))


def _is_figure(text: str) -> bool:
    t = text.strip()
    return bool(t) and bool(re.fullmatch(r"[\s$€£(]*[-–—]?[\d,]+(\.\d+)?\s*%?\)?|[-–—]+", t))


def _table_block(rows: list[list[Optional[str]]]) -> Optional[dict[str, Any]]:
    clean = [[(c or "").replace("\n", " ").strip() for c in r] for r in rows]
    clean = [r for r in clean if any(r)]
    if len(clean) < 2:
        return None
    width = max(len(r) for r in clean)
    clean = [r + [""] * (width - len(r)) for r in clean]

    # Leading rows with no figures are captions (e.g. "2025  2024").
    header_rows = 0
    for r in clean:
        if any(_is_figure(c) for c in r[1:]) and not all(re.fullmatch(r"(19|20)\d{2}", c) for c in r[1:] if c):
            break
        header_rows += 1
        if header_rows >= 3:
            break
    header_rows = max(1, min(header_rows, len(clean) - 1))
    headers = [" ".join(r[i] for r in clean[:header_rows] if r[i]).strip() for i in range(width)]

    out_rows = []
    for r in clean[header_rows:]:
        label = r[0].lower()
        has_figures = any(c for c in r[1:])
        if label.startswith("total"):
            row_type = "total"
        elif "subtotal" in label:
            row_type = "subtotal"
        elif not has_figures:
            row_type = "category_header"
        else:
            row_type = "data"
        out_rows.append({"type": row_type, "cells": r, "indent": 0})
    return {"type": "table", "title": headers[0] or None, "headers": headers, "rows": out_rows}


def _cell_text(page: pymupdf.Page, bbox: Any) -> str:
    if not bbox:
        return ""
    words = page.get_text("words", clip=pymupdf.Rect(bbox))
    words.sort(key=lambda w: (round(w[1]), w[0]))
    return " ".join(w[4] for w in words)


def _layout_blocks_digital(page: pymupdf.Page, body_size: float) -> list[dict[str, Any]]:
    """Headings, paragraphs and tables of one digital page, from fonts and positions."""
    items: list[tuple[float, dict[str, Any]]] = []
    table_rects: list[pymupdf.Rect] = []
    try:
        for tab in page.find_tables().tables:
            # Read each cell's words from the page: Table.extract() drops spaces and
            # moves thousands separators in some PDFs.
            block = _table_block([[_cell_text(page, cell) for cell in row.cells] for row in tab.rows])
            if block:
                table_rects.append(pymupdf.Rect(tab.bbox))
                items.append((tab.bbox[1], block))
    except Exception as e:  # table detection is best effort
        logger.debug("find_tables failed on page %s: %s", page.number + 1, e)

    width = page.rect.width
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        rect = pymupdf.Rect(blk["bbox"])
        if any(rect.intersects(t) for t in table_rects):
            continue
        spans = [s for line in blk.get("lines", []) for s in line.get("spans", []) if s.get("text", "").strip()]
        if not spans:
            continue
        text = " ".join(
            " ".join(s["text"].strip() for s in line.get("spans", []) if s.get("text", "").strip())
            for line in blk.get("lines", [])
        )
        text = re.sub(r"\s+", " ", text).strip()
        if not text or _is_page_number(text):
            continue
        size = max(s.get("size", body_size) for s in spans)
        bold = all((s.get("flags", 0) & 16) or "bold" in s.get("font", "").lower() for s in spans)
        italic = all((s.get("flags", 0) & 2) or "italic" in s.get("font", "").lower() for s in spans)
        center = (rect.x0 + rect.x1) / 2
        if abs(center - width / 2) < width * 0.06 and rect.width < width * 0.75:
            alignment = "center"
        elif rect.x0 > width * 0.55:
            alignment = "right"
        else:
            alignment = "left"

        ratio = size / body_size if body_size else 1.0
        short = len(text) < 140 and not text.endswith((".", ";", ","))
        if ratio >= 1.6:
            level = 1
        elif ratio >= 1.3:
            level = 2
        elif ratio >= 1.12 and short:
            level = 3
        elif bold and short and len(text) < 100:
            level = 3 if re.match(r"(part|item|note)\b", text, re.I) else 4
        else:
            level = None

        if level:
            block = {"type": "heading", "level": level, "text": text, "alignment": "left" if alignment == "right" else alignment}
        else:
            block = {"type": "paragraph", "text": text, "bold": bold or None, "italic": italic or None, "alignment": alignment}
        items.append((rect.y0, block))

    items.sort(key=lambda it: it[0])
    return [b for _, b in items]


def _layout_blocks_ocr(raw_text: str) -> list[dict[str, Any]]:
    """Paragraphs from OCR text (blank lines separate paragraphs)."""
    blocks = []
    for para in re.split(r"\n\s*\n", raw_text or ""):
        text = re.sub(r"\s+", " ", para).strip()
        if not text or _is_page_number(text):
            continue
        is_heading = len(text) < 90 and (text.isupper() or re.match(r"(part|item|note)\s+[\divx]+\b", text, re.I))
        if is_heading:
            blocks.append({"type": "heading", "level": 3, "text": text, "alignment": "left"})
        else:
            blocks.append({"type": "paragraph", "text": text, "alignment": "left"})
    return blocks


def _body_font_size(doc: pymupdf.Document) -> float:
    sizes: list[float] = []
    for page in doc:
        for blk in page.get_text("dict").get("blocks", []):
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    sizes.extend([round(span.get("size", 10), 1)] * len(span.get("text", "").strip()))
            if len(sizes) > 200_000:
                break
    return statistics.median(sizes) if sizes else 10.0


# --------------------------------------------------------------------------- AI conversion


def _chunk_pdf_bytes(doc: pymupdf.Document, first: int, last: int) -> bytes:
    sub = pymupdf.open()
    sub.insert_pdf(doc, from_page=first, to_page=last)
    data = sub.tobytes(garbage=3, deflate=True)
    sub.close()
    return data


def _page_images(doc: pymupdf.Document, first: int, last: int) -> list[types.Part]:
    parts = []
    for i in range(first, last + 1):
        pix = doc[i].get_pixmap(dpi=150)
        parts.append(types.Part.from_bytes(data=pix.tobytes("jpeg"), mime_type="image/jpeg"))
    return parts


async def _ai_blocks(client, on_doc, first: int, last: int, source_text: str, ocr: bool) -> list[dict[str, Any]]:
    pdf_bytes = await on_doc(_chunk_pdf_bytes, first, last)
    contents: list[Any] = [_AI_INSTRUCTIONS]
    if len(pdf_bytes) <= MAX_INLINE_PDF_BYTES:
        contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
    else:
        contents.extend(await on_doc(_page_images, first, last))
    label = "OCR text (may contain misreads)" if ocr else "Extracted text layer"
    contents.append(f"Pages {first + 1}-{last + 1}. {label}:\n\n{source_text[:60_000]}")

    response = await generate_content_with_fallback(
        client=client,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AiChunkResult,
            temperature=0.0,
            max_output_tokens=60_000,
        ),
    )
    raw = (response.text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw)
    result = AiChunkResult(**json.loads(raw))
    return [b.model_dump(exclude_none=True) for b in result.blocks if _has_content(b)]


def _has_content(b: AiBlock) -> bool:
    if b.type == "table":
        return bool(b.rows)
    return bool((b.text or "").strip())


# --------------------------------------------------------------------------- conversion


async def convert_pdf(pdf_bytes: bytes, progress: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Blocks plus a per-chunk report of how each part was converted and checked."""
    progress = progress if progress is not None else {}
    progress.update(stage="Reading the PDF")

    doc = await asyncio.to_thread(pymupdf.open, stream=pdf_bytes, filetype="pdf")
    try:
        if doc.needs_pass:
            raise ValueError("This PDF is password protected. Remove the password and upload it again.")
        page_count = len(doc)
        if page_count == 0:
            raise ValueError("This PDF has no pages.")
        if page_count > MAX_PAGES:
            raise ValueError(f"This PDF has {page_count} pages; the limit is {MAX_PAGES}.")
        progress.update(totalPages=page_count, donePages=0)

        progress.update(stage="Extracting text (OCR for scanned pages)")
        layout = await asyncio.to_thread(extract_document_layout, pdf_bytes)
        ocr = bool(layout.get("ocr_used"))
        page_texts = [p.get("raw_text", "") for p in layout.get("pages", [])]
        page_texts += [""] * (page_count - len(page_texts))
        body_size = 10.0 if ocr else await asyncio.to_thread(_body_font_size, doc)

        # A PyMuPDF document is not thread-safe; chunks run in parallel, so every access
        # to it goes through this lock (the slow part, Gemini, runs outside it).
        doc_lock = threading.Lock()

        def _locked(fn, *args):
            with doc_lock:
                return fn(doc, *args)

        async def on_doc(fn, *args):
            return await asyncio.to_thread(_locked, fn, *args)

        def _layout_pages(d: pymupdf.Document, first: int, last: int) -> list[dict[str, Any]]:
            return [b for i in range(first, last + 1) for b in _layout_blocks_digital(d[i], body_size)]

        client = get_gemini_client()
        chunks = [(s, min(s + PAGES_PER_CHUNK, page_count) - 1) for s in range(0, page_count, PAGES_PER_CHUNK)]
        results: list[Optional[dict[str, Any]]] = [None] * len(chunks)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHUNKS)
        progress.update(stage="Converting with Gemini AI" if client else "Converting from the PDF layout")

        async def run(idx: int, first: int, last: int) -> None:
            source = "\n\n".join(page_texts[first:last + 1])
            pages = f"{first + 1}" if first == last else f"{first + 1}-{last + 1}"
            report: dict[str, Any] = {"pages": pages, "method": "layout"}
            blocks: list[dict[str, Any]] = []
            if client:
                async with semaphore:
                    try:
                        ai = await _ai_blocks(client, on_doc, first, last, source, ocr)
                        has_source_text = bool(_tokens(source)[0])
                        check = _verify(source, ai, ocr) if has_source_text else None
                        if check is None or check["passed"]:
                            blocks, report["method"] = ai, "ai"
                            report["verification"] = check
                            if check is None:
                                report["warning"] = "No text layer or OCR text to check the AI conversion against."
                        else:
                            report["verification"] = check
                            invented = (
                                f" and added figures not on the page ({', '.join(check['extraFigures'][:5])})"
                                if check["extraFigures"]
                                else ""
                            )
                            report["warning"] = (
                                f"AI kept {check['wordCoverage']:.0%} of the words and {check['figureCoverage']:.0%} "
                                f"of the figures{invented}, so these pages were converted directly from the PDF layout instead."
                            )
                        if check and check["passed"] and ocr and check["extraFigures"]:
                            report["warning"] = (
                                "Scanned pages: check these figures against the PDF, the AI and OCR read them "
                                f"differently: {', '.join(check['extraFigures'][:8])}."
                            )
                    except (json.JSONDecodeError, ValidationError) as e:
                        report["warning"] = f"The AI response could not be read ({e.__class__.__name__}); used the PDF layout."
                    except Exception as e:
                        logger.warning("Gemini conversion failed for pages %s: %s", pages, e)
                        report["warning"] = "Gemini AI was unavailable for these pages; used the PDF layout."
            if report["method"] == "layout":
                if ocr:
                    blocks = _layout_blocks_ocr(source)
                else:
                    blocks = await on_doc(_layout_pages, first, last)
            results[idx] = {"blocks": blocks, "report": report}
            progress["donePages"] = progress.get("donePages", 0) + (last - first + 1)

        await asyncio.gather(*(run(i, s, e) for i, (s, e) in enumerate(chunks)))
    finally:
        doc.close()

    blocks = [b for r in results if r for b in r["blocks"]]
    reports = [r["report"] for r in results if r]
    return {
        "blocks": blocks,
        "pageCount": page_count,
        "ocrUsed": ocr,
        "aiUsed": any(r["method"] == "ai" for r in reports),
        "aiConfigured": client is not None,
        "chunks": reports,
        "warnings": [
            f"{'Pages' if '-' in r['pages'] else 'Page'} {r['pages']}: {r['warning']}" for r in reports if r.get("warning")
        ],
    }


# --------------------------------------------------------------------------- jobs

_jobs: dict[str, dict[str, Any]] = {}


def _prune_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    for job_id in [j for j, v in _jobs.items() if v["createdAt"] < cutoff]:
        _jobs.pop(job_id, None)


def start_pdf_import(user_id: UUID, pdf_bytes: bytes, file_name: str) -> str:
    _prune_jobs()
    job_id = uuid.uuid4().hex
    job: dict[str, Any] = {
        "userId": str(user_id),
        "fileName": file_name,
        "status": "running",
        "progress": {"stage": "Queued"},
        "result": None,
        "error": None,
        "createdAt": time.time(),
    }
    _jobs[job_id] = job

    async def run() -> None:
        try:
            job["result"] = await convert_pdf(pdf_bytes, job["progress"])
            job["status"] = "done"
        except ValueError as e:
            job["status"], job["error"] = "error", str(e)
        except Exception as e:
            logger.exception("PDF import failed: %s", e)
            job["status"], job["error"] = "error", "The PDF could not be converted."

    job["task"] = asyncio.create_task(run())
    return job_id


def get_pdf_import(user_id: UUID, job_id: str) -> Optional[dict[str, Any]]:
    job = _jobs.get(job_id)
    if not job or job["userId"] != str(user_id):
        return None
    return {
        "jobId": job_id,
        "fileName": job["fileName"],
        "status": job["status"],
        "progress": job["progress"],
        "result": job["result"],
        "error": job["error"],
    }
