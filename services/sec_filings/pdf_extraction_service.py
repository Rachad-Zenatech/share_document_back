"""PDF text and layout extraction, with Tesseract OCR for scanned PDFs (ported from the admin portal)."""

import os
import sys
import io
import logging
from typing import Any, Dict, List, Tuple
import pymupdf as fitz
from PIL import Image

logger = logging.getLogger(__name__)


def detect_pdf_type(pdf_bytes: bytes) -> Tuple[bool, int, str]:
    """
    Inspects PDF to determine if it has a usable digital text layer.
    Extracts text using PyMuPDF and falls back to pypdf.
    Returns: (is_digital, page_count, sample_text)
    """
    page_count = 1
    total_chars = 0
    sample_text_parts = []

    # 1. First attempt: PyMuPDF digital text extraction
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = len(doc)
        for page in doc:
            text = page.get_text("text") or ""
            total_chars += len(text.strip())
            if len(sample_text_parts) < 5 and text.strip():
                sample_text_parts.append(text.strip())
    except Exception as e:
        logger.warning(f"PyMuPDF detect_pdf_type extraction encountered error: {e}")

    # 2. Second attempt: pypdf fallback if PyMuPDF extracted minimal/no text
    if total_chars < 15:
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            page_count = len(reader.pages)
            pypdf_chars = 0
            pypdf_parts = []
            for page in reader.pages:
                text = page.extract_text() or ""
                pypdf_chars += len(text.strip())
                if len(pypdf_parts) < 5 and text.strip():
                    pypdf_parts.append(text.strip())
            if pypdf_chars > total_chars:
                total_chars = pypdf_chars
                sample_text_parts = pypdf_parts
        except Exception as e:
            logger.debug(f"pypdf detection fallback encountered error: {e}")

    sample_text = "\n---\n".join(sample_text_parts)
    # If the PDF contains at least 15 characters of extracted text, it has a digital text layer
    # and does NOT require Tesseract OCR.
    is_digital = total_chars >= 15

    logger.info(
        f"PDF Type Detection: pages={page_count}, total_chars={total_chars}, is_digital={is_digital}"
    )
    return is_digital, page_count, sample_text


def extract_digital_layout(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Extracts text, blocks, and layout coordinates from a digital PDF using PyMuPDF.
    Falls back to pypdf if PyMuPDF layout extraction fails.
    Preserves bounding boxes, page structure, and spatial orientation.
    """
    pages_data = []

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page_idx, page in enumerate(doc):
            rect = page.rect
            page_width = float(rect.width)
            page_height = float(rect.height)

            # Extract structured text blocks (x0, y0, x1, y1, text, block_no, block_type)
            blocks = page.get_text("blocks")
            elements = []
            raw_text_lines = []

            for b in blocks:
                x0, y0, x1, y1, text, block_no, block_type = b
                clean_text = text.strip()
                if not clean_text:
                    continue

                raw_text_lines.append(clean_text)
                elements.append({
                    "text": clean_text,
                    "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                    "block_no": block_no,
                    "confidence": 1.0,
                    "source": "digital",
                })

            # Sort elements spatially by top (y0) then left (x0)
            elements.sort(key=lambda el: (round(el["bbox"][1] / 10) * 10, el["bbox"][0]))

            pages_data.append({
                "page_number": page_idx + 1,
                "width": page_width,
                "height": page_height,
                "raw_text": "\n".join(raw_text_lines),
                "elements": elements,
            })
    except Exception as mupdf_err:
        logger.warning(f"PyMuPDF extract_digital_layout error: {mupdf_err}. Trying pypdf fallback...")
        pages_data = []

    # Fallback to pypdf if PyMuPDF produced no pages or text
    if not pages_data or all(not p.get("raw_text", "").strip() for p in pages_data):
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            fallback_pages = []
            for idx, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                w = float(page.mediabox.width) if page.mediabox else 612.0
                h = float(page.mediabox.height) if page.mediabox else 792.0
                fallback_pages.append({
                    "page_number": idx + 1,
                    "width": w,
                    "height": h,
                    "raw_text": text.strip(),
                    "elements": [{
                        "text": text.strip(),
                        "bbox": [0.0, 0.0, w, h],
                        "block_no": 0,
                        "confidence": 1.0,
                        "source": "pypdf",
                    }] if text.strip() else [],
                })
            if fallback_pages:
                pages_data = fallback_pages
        except Exception as pypdf_err:
            logger.error(f"pypdf layout fallback failed: {pypdf_err}")

    return {
        "page_count": len(pages_data),
        "ocr_used": False,
        "pages": pages_data,
    }


def extract_scanned_ocr_layout(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Renders PDF pages to high-resolution images and applies OCR with bounding box extraction.
    Uses PyMuPDF rendering + local OCR fallback (pytesseract or paddleocr).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_data = []

    # Check if pytesseract is available with a valid binary on system PATH
    pytesseract_available = False
    try:
        import pytesseract
        import shutil
        tess_cmd = os.getenv("TESSERACT_CMD") or shutil.which("tesseract") or ("/usr/bin/tesseract" if sys.platform != "win32" else None)
        if tess_cmd and os.path.exists(tess_cmd):
            pytesseract.pytesseract.tesseract_cmd = tess_cmd
            pytesseract_available = True
        else:
            pytesseract_available = False
    except (ImportError, Exception) as e:
        pytesseract_available = False
        logger.debug(f"pytesseract binary check: {e}")

    for page_idx, page in enumerate(doc):
        rect = page.rect
        page_width = float(rect.width)
        page_height = float(rect.height)

        # Render page to high-res image (200 DPI)
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))

        elements = []
        raw_text = ""

        if pytesseract_available:
            try:
                import pytesseract
                # Get bounding box data
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                n_boxes = len(data["text"])
                line_words = []
                current_line = -1

                for i in range(n_boxes):
                    text = data["text"][i].strip()
                    conf = float(data["conf"][i])
                    if not text or conf < 0:
                        continue

                    # Rescale coordinates back to PDF point dimensions
                    scale_x = page_width / img.width
                    scale_y = page_height / img.height
                    x = data["left"][i] * scale_x
                    y = data["top"][i] * scale_y
                    w = data["width"][i] * scale_x
                    h = data["height"][i] * scale_y

                    elements.append({
                        "text": text,
                        "bbox": [round(x, 2), round(y, 2), round(x + w, 2), round(y + h, 2)],
                        "confidence": round(conf / 100.0, 2),
                        "source": "ocr",
                    })

                raw_text = pytesseract.image_to_string(img)
            except Exception as ocr_err:
                logger.warning(f"pytesseract extraction error on page {page_idx + 1}: {ocr_err}")
                raw_text = page.get_text("text") or ""
        else:
            raw_text = page.get_text("text") or ""

        pages_data.append({
            "page_number": page_idx + 1,
            "width": page_width,
            "height": page_height,
            "raw_text": raw_text,
            "elements": elements,
        })

    return {
        "page_count": len(pages_data),
        "ocr_used": True,
        "pages": pages_data,
    }


def extract_document_layout(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Main entry point: detects PDF type and extracts layout-aware representation.
    """
    is_digital, page_count, _ = detect_pdf_type(pdf_bytes)
    if is_digital:
        return extract_digital_layout(pdf_bytes)
    else:
        return extract_scanned_ocr_layout(pdf_bytes)
