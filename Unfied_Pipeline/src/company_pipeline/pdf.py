"""PDF text extraction with OCR for embedded image regions."""

from pathlib import Path
from typing import Any, Protocol


class ImageOcr(Protocol):
    def extract_text(self, image_bytes: bytes) -> str:
        """Return text recognized in a PNG image."""


class OcrUnavailableError(RuntimeError):
    pass


class TesseractOcr:
    def extract_text(self, image_bytes: bytes) -> str:
        try:
            from PIL import Image
            import pytesseract
        except ImportError as error:
            raise OcrUnavailableError(
                "Image OCR requires the optional dependencies. "
                'Install them with: python -m pip install -e ".[ocr]"'
            ) from error

        import io

        image = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(image)


class PdfImageDetector:
    """Detect image regions on a PDF page."""

    def image_regions(self, page: Any) -> list[tuple[float, float, float, float]]:
        return [
            tuple(block["bbox"])
            for block in page.get_text("dict")["blocks"]
            if block.get("type") == 1
        ]

    def has_images(self, page: Any) -> bool:
        return bool(self.image_regions(page))


class PdfProcessor:
    def __init__(
        self,
        ocr: ImageOcr | None = None,
        image_detector: PdfImageDetector | None = None,
    ) -> None:
        self.ocr = ocr if ocr is not None else TesseractOcr()
        self.image_detector = image_detector or PdfImageDetector()

    def extract_pages(self, pdf_path: str | Path) -> list[str]:
        try:
            import fitz
        except ImportError as error:
            raise RuntimeError(
                "PDF processing requires PyMuPDF. Install project dependencies first."
            ) from error

        pages: list[str] = []
        with fitz.open(pdf_path) as document:
            for page in document:
                page_text = page.get_text("text").strip()
                image_text = self._extract_image_text(page)
                combined = _combine_text(page_text, image_text)
                pages.append(combined)
        return pages

    def _extract_image_text(self, page: Any) -> list[str]:
        image_regions = self.image_detector.image_regions(page)
        if not image_regions:
            return []

        recognized: list[str] = []
        for bbox in image_regions:
            rectangle = page.rect.__class__(bbox)
            pixmap = page.get_pixmap(clip=rectangle, alpha=False)
            text = self.ocr.extract_text(pixmap.tobytes("png")).strip()
            if text:
                recognized.append(text)
        return recognized


def _combine_text(page_text: str, image_text: list[str]) -> str:
    combined = page_text
    for text in image_text:
        normalized_text = " ".join(text.casefold().split())
        normalized_combined = " ".join(combined.casefold().split())
        if normalized_text and normalized_text not in normalized_combined:
            combined = f"{combined}\n\n{text}" if combined else text
    return combined
