from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str = ""
    status: int | None = None
    html: str = ""
    tier: str = "direct"
    error: str = ""
    elapsed_ms: int = 0
    blocked: bool = False
    rendered: bool = False
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.html) and self.status is not None and 200 <= self.status < 400


@dataclass(slots=True)
class Evidence:
    value: str
    kind: str
    source_url: str
    source_type: str
    confidence: float
    category: str = ""
    raw_value: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        return self.kind, self.value.casefold()


@dataclass(slots=True)
class PageData:
    url: str
    title: str = ""
    contacts: list[Evidence] = field(default_factory=list)
    address: str = ""
    description: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class SiteResult:
    input_url: str
    normalized_url: str
    final_url: str = ""
    domain: str = ""
    status: str = "failed"
    fetch_tier: str = ""
    http_status: int | None = None
    contacts: list[Evidence] = field(default_factory=list)
    address: str = ""
    description: str = ""
    title: str = ""
    pages_scraped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contacts"] = [asdict(item) for item in self.contacts]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SiteResult":
        data = dict(payload)
        data["contacts"] = [Evidence(**item) for item in data.get("contacts", [])]
        return cls(**data)
