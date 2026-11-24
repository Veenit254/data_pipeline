"""Integration seam for the future company data API."""

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from company_pipeline.market_data import (
    MARKET_DATA_COLUMNS,
    normalize_market_record,
)
from company_pipeline.markdown import pages_to_markdown
from company_pipeline.models import CompanyRecord
from company_pipeline.pdf import ImageOcr, PdfProcessor
from company_pipeline.storage import CompanyStore


class CompanyApiClient(Protocol):
    def fetch_companies(self) -> Iterable[Mapping[str, Any]]:
        """Fetch company objects from the configured upstream API."""


class CompanyDocumentApiClient(Protocol):
    def extract_companies(self, markdown_text: str) -> Iterable[Mapping[str, Any]]:
        """Extract structured company objects from a document's Markdown."""


class MarketDataApiClient(Protocol):
    def fetch_market_records(self) -> Iterable[Mapping[str, Any]]:
        """Fetch market data records from the configured upstream API."""


class MarketDocumentApiClient(Protocol):
    def extract_market_records(
        self, markdown_text: str, fields: Sequence[str]
    ) -> Iterable[Mapping[str, Any]]:
        """Extract only source-supported market fields from document Markdown.

        Return one mapping per company/trade-date record. Use null for fields
        that are absent from the source rather than guessing values.
        """


def ingest_api_records(
    client: CompanyApiClient, store: CompanyStore
) -> tuple[int, int]:
    """Persist API records; return (inserted_count, updated_count)."""
    inserted = updated = 0
    for raw_record in client.fetch_companies():
        if not isinstance(raw_record, Mapping):
            raise ValueError("The company API returned a record that is not an object.")
        if store.upsert(CompanyRecord.from_mapping(raw_record)):
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def extract_and_store_companies(
    markdown_text: str,
    client: CompanyDocumentApiClient,
    store: CompanyStore,
) -> tuple[int, int]:
    """Use a future API-backed extractor and persist its company records."""
    inserted = updated = 0
    for raw_record in client.extract_companies(markdown_text):
        if not isinstance(raw_record, Mapping):
            raise ValueError("The extraction API returned a record that is not an object.")
        if store.upsert(CompanyRecord.from_mapping(raw_record)):
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def ingest_market_api_records(
    client: MarketDataApiClient, store: CompanyStore
) -> tuple[int, int]:
    """Persist API market records; return (inserted_count, duplicate_count)."""
    inserted = duplicates = 0
    for raw_record in client.fetch_market_records():
        if not isinstance(raw_record, Mapping):
            raise ValueError("The market data API returned a record that is not an object.")
        if store.add_market_record(normalize_market_record(raw_record)):
            inserted += 1
        else:
            duplicates += 1
    return inserted, duplicates


def extract_market_records_from_pdf(
    pdf_path: str | Path,
    client: MarketDocumentApiClient,
    ocr: ImageOcr | None = None,
) -> list[dict[str, Any]]:
    """Extract schema-shaped market records from PDF text via an API client."""
    pages = PdfProcessor(ocr=ocr).extract_pages(pdf_path)
    markdown_text = pages_to_markdown(Path(pdf_path).stem, pages)
    records: list[dict[str, Any]] = []
    for raw_record in client.extract_market_records(markdown_text, MARKET_DATA_COLUMNS):
        if not isinstance(raw_record, Mapping):
            raise ValueError("The extraction API returned a record that is not an object.")
        records.append(normalize_market_record(raw_record))
    return records
