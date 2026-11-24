"""OCR for scanned (image-only) PDFs, using the tesseract CLI on pages rendered by pypdfium2.

  python scripts/ocr.py data/raw/master_lists/013bba5a417251a5.pdf      # print the OCR text

parse_nse_records.py calls ocr_pdf() when a PDF has no text layer. Results are cached in
data/processed/ocr/<folder>__<name>.txt, so each scanned file is read once (≈2–4 s per page).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import PROCESSED, atomic_write_text

OCR_DIR = PROCESSED / "ocr"
MAX_PAGES = 12   # delisting orders are 2–8 pages; long scanned reports are not worth the time
DPI = 300
PAGE_TIMEOUT_S = 120


def available() -> bool:
    return shutil.which("tesseract") is not None


def cache_path(pdf: Path) -> Path:
    return OCR_DIR / f"{pdf.parent.name}__{pdf.stem}.txt"


def ocr_pdf_detail(pdf: Path, max_pages: int = MAX_PAGES) -> tuple[str, list[str]]:
    """(text of the first max_pages pages, page errors). A page that fails (tesseract exit != 0, or slower than
    PAGE_TIMEOUT_S) is skipped and reported; the others are kept. Only a complete, error-free OCR is cached, so
    a failure is retried next time instead of being remembered as an empty document."""
    cached = cache_path(pdf)
    if cached.exists():
        return cached.read_text(encoding="utf-8"), []
    if not available():
        return "", []
    import pypdfium2 as pdfium

    texts, errors = [], []
    doc = pdfium.PdfDocument(str(pdf))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(min(len(doc), max_pages)):
                img = doc[i].render(scale=DPI / 72).to_pil().convert("L")
                png = Path(tmp) / f"p{i}.png"
                img.save(png)
                try:
                    r = subprocess.run(["tesseract", str(png), "stdout", "-l", "eng", "--psm", "6"],
                                       capture_output=True, text=True, timeout=PAGE_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    errors.append(f"page {i + 1}: tesseract timed out after {PAGE_TIMEOUT_S} s")
                    continue
                if r.returncode != 0:
                    errors.append(f"page {i + 1}: tesseract exit {r.returncode}: {(r.stderr or '').strip()[:120]}")
                    continue
                texts.append(r.stdout)
    finally:
        doc.close()
    text = "\n".join(texts)
    if not errors:
        OCR_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cached, text)
    return text, errors


def ocr_pdf(pdf: Path, max_pages: int = MAX_PAGES) -> str:
    """Text of the first max_pages pages ('' if tesseract is missing or nothing is readable)."""
    return ocr_pdf_detail(pdf, max_pages)[0]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    print(ocr_pdf(Path(sys.argv[1])))
