"""Data models shared by the pipeline."""

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class CompanyRecord:
    name: str
    website: str | None = None
    country: str | None = None
    industry: str | None = None
    description: str | None = None
    source: str | None = None
    source_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CompanyRecord":
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Company record requires a non-empty 'name'.")

        known_fields = {
            "name",
            "website",
            "country",
            "industry",
            "description",
            "source",
            "source_id",
            "attributes",
        }
        attributes = data.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise ValueError("'attributes' must be a JSON object.")

        extra_fields = {key: value for key, value in data.items() if key not in known_fields}
        return cls(
            name=name.strip(),
            website=_optional_string(data.get("website"), "website"),
            country=_optional_string(data.get("country"), "country"),
            industry=_optional_string(data.get("industry"), "industry"),
            description=_optional_string(data.get("description"), "description"),
            source=_optional_string(data.get("source"), "source"),
            source_id=_optional_string(data.get("source_id"), "source_id"),
            attributes={**attributes, **extra_fields},
        )


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"'{field_name}' must be a string or null.")
    value = value.strip()
    return value or None
