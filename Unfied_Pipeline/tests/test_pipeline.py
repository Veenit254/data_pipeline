import tempfile
import unittest
from unittest import mock
import csv
from pathlib import Path

from company_pipeline.filtering import CompanyFilter, filter_companies
from company_pipeline.models import CompanyRecord
from company_pipeline.storage import CompanyStore
from company_pipeline.api import (
    extract_and_store_companies,
    extract_market_records_from_pdf,
    ingest_api_records,
)
from company_pipeline.market_data import (
    MARKET_DATA_COLUMNS,
    filter_market_records,
    write_market_csv,
)
from company_pipeline.pdf import PdfImageDetector, PdfProcessor


class CompanyStoreTests(unittest.TestCase):
    def test_upsert_does_not_duplicate_same_website(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with CompanyStore(Path(directory) / "companies.sqlite3") as store:
                first = CompanyRecord(
                    name="Example Co",
                    website="https://www.example.com/about",
                    country="India",
                    industry="Metals",
                )
                updated = CompanyRecord(
                    name="Example Company",
                    website="http://example.com",
                    country="India",
                    industry="Materials",
                )

                self.assertTrue(store.upsert(first))
                self.assertFalse(store.upsert(updated))
                companies = store.list_companies()

        self.assertEqual(len(companies), 1)
        self.assertEqual(companies[0].name, "Example Company")
        self.assertEqual(companies[0].industry, "Materials")

    def test_api_records_update_existing_company_by_source_id(self) -> None:
        class FakeApiClient:
            def fetch_companies(self) -> list[dict[str, str]]:
                return [
                    {
                        "name": "Example Co",
                        "source": "directory-api",
                        "source_id": "42",
                    },
                    {
                        "name": "Example Company",
                        "industry": "Metals",
                        "source": "directory-api",
                        "source_id": "42",
                    },
                ]

        with tempfile.TemporaryDirectory() as directory:
            with CompanyStore(Path(directory) / "companies.sqlite3") as store:
                counts = ingest_api_records(FakeApiClient(), store)
                companies = store.list_companies()

        self.assertEqual(counts, (1, 1))
        self.assertEqual(len(companies), 1)
        self.assertEqual(companies[0].industry, "Metals")

    def test_document_extraction_records_are_persisted(self) -> None:
        class FakeDocumentApi:
            def extract_companies(self, markdown_text: str) -> list[dict[str, str]]:
                self.markdown_text = markdown_text
                return [{"name": "Example Co", "country": "India"}]

        api = FakeDocumentApi()
        with tempfile.TemporaryDirectory() as directory:
            with CompanyStore(Path(directory) / "companies.sqlite3") as store:
                counts = extract_and_store_companies("# Page 1\nExample Co", api, store)
                companies = store.list_companies()

        self.assertEqual(api.markdown_text, "# Page 1\nExample Co")
        self.assertEqual(counts, (1, 0))
        self.assertEqual(companies[0].name, "Example Co")

    def test_filter_matches_country_industry_and_name(self) -> None:
        companies = [
            CompanyRecord(name="North Metals", country="India", industry="Metals"),
            CompanyRecord(name="South Energy", country="India", industry="Energy"),
        ]

        result = filter_companies(
            companies,
            CompanyFilter(country="india", industry="metals", name_contains="north"),
        )

        self.assertEqual([company.name for company in result], ["North Metals"])

    def test_market_records_deduplicate_exact_rows_and_keep_distinct_dates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with CompanyStore(Path(directory) / "companies.sqlite3") as store:
                first = {
                    "isin": "INE000A01001",
                    "nse_symbol": "EXAMPLE",
                    "trade_date": "2026-11-20",
                    "close": 120.5,
                }
                next_day = {**first, "trade_date": "2026-11-21", "close": 121.0}

                self.assertTrue(store.add_market_record(first))
                self.assertFalse(store.add_market_record(first))
                self.assertTrue(store.add_market_record(next_day))
                records = store.list_market_records()

        self.assertEqual(len(records), 2)

    def test_market_csv_has_exact_schema_and_can_filter(self) -> None:
        records = [
            {"isin": "IN1", "sector": "Technology", "close": 10},
            {"isin": "IN2", "sector": "Banking", "close": 20},
        ]
        filtered = filter_market_records(records, ["sector=technology"])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "filtered.csv"
            write_market_csv(output, filtered)
            with output.open(encoding="utf-8-sig", newline="") as csv_file:
                reader = csv.DictReader(csv_file)
                rows = list(reader)
                headers = reader.fieldnames

        self.assertEqual(headers, list(MARKET_DATA_COLUMNS))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["isin"], "IN1")
        self.assertEqual(rows[0]["close"], "10")
        self.assertEqual(rows[0]["announcement_body_md"], "")

    def test_pdf_image_detector_identifies_image_regions(self) -> None:
        detector = PdfImageDetector()

        class FakePage:
            def __init__(self, blocks: list[dict[str, object]]) -> None:
                self.blocks = blocks

            def get_text(self, _kind: str) -> dict[str, list[dict[str, object]]]:
                return {"blocks": self.blocks}

        self.assertTrue(
            detector.has_images(
                FakePage([{"type": 1, "bbox": (1, 2, 3, 4)}])
            )
        )
        self.assertFalse(detector.has_images(FakePage([])))

    def test_pdf_processor_automatically_ocr_image_regions(self) -> None:
        class FakeRectangle:
            def __init__(self, bbox: tuple[float, ...]) -> None:
                self.bbox = bbox

        class FakePixmap:
            def tobytes(self, _format: str) -> bytes:
                return b"image"

        class FakePage:
            rect = FakeRectangle((0, 0, 10, 10))

            def get_text(self, _kind: str) -> dict[str, list[dict[str, object]]]:
                return {"blocks": [{"type": 1, "bbox": (1, 2, 3, 4)}]}

            def get_pixmap(self, **_kwargs: object) -> FakePixmap:
                return FakePixmap()

        class FakeOcr:
            def extract_text(self, image_bytes: bytes) -> str:
                self.image_bytes = image_bytes
                return "OCR result"

        ocr = FakeOcr()
        processor = PdfProcessor(ocr=ocr)

        self.assertEqual(processor._extract_image_text(FakePage()), ["OCR result"])
        self.assertEqual(ocr.image_bytes, b"image")

    def test_pdf_market_extraction_requests_all_fields_and_normalizes_output(self) -> None:
        class FakePdfProcessor:
            def extract_pages(self, _pdf_path: str) -> list[str]:
                return ["Extracted financial report text"]

        class FakeDocumentApi:
            def extract_market_records(
                self, markdown_text: str, fields: tuple[str, ...]
            ) -> list[dict[str, object]]:
                self.markdown_text = markdown_text
                self.fields = fields
                return [{"isin": "IN1", "close": 100.0}]

        api = FakeDocumentApi()
        with mock.patch(
            "company_pipeline.api.PdfProcessor", return_value=FakePdfProcessor()
        ):
            records = extract_market_records_from_pdf("report.pdf", api)

        self.assertIn("Extracted financial report text", api.markdown_text)
        self.assertEqual(api.fields, MARKET_DATA_COLUMNS)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["isin"], "IN1")
        self.assertEqual(records[0]["close"], 100.0)
        self.assertIsNone(records[0]["industry"])


if __name__ == "__main__":
    unittest.main()
