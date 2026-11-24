"""Simple, composable company record filters."""

from dataclasses import dataclass

from company_pipeline.models import CompanyRecord


@dataclass(frozen=True)
class CompanyFilter:
    country: str | None = None
    industry: str | None = None
    name_contains: str | None = None

    def matches(self, company: CompanyRecord) -> bool:
        if self.country and (company.country or "").casefold() != self.country.casefold():
            return False
        if self.industry and (company.industry or "").casefold() != self.industry.casefold():
            return False
        if self.name_contains and self.name_contains.casefold() not in company.name.casefold():
            return False
        return True


def filter_companies(
    companies: list[CompanyRecord], criteria: CompanyFilter
) -> list[CompanyRecord]:
    return [company for company in companies if criteria.matches(company)]
