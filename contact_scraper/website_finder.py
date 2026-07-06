from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from .utils import hostname, normalize_url, registered_domain, validate_public_host

LOGGER = logging.getLogger(__name__)

DIRECTORY_HOSTS = {
    "angi.com",
    "bbb.org",
    "bloomberg.com",
    "chamberofcommerce.com",
    "clutch.co",
    "crunchbase.com",
    "datanyze.com",
    "dnb.com",
    "facebook.com",
    "find-and-update.company-information.service.gov.uk",
    "glassdoor.com",
    "google.com",
    "hotfrog.com",
    "instagram.com",
    "linkedin.com",
    "manta.com",
    "opencorporates.com",
    "rocketreach.co",
    "signalhire.com",
    "theorg.com",
    "thumbtack.com",
    "x.com",
    "yelp.com",
    "youtube.com",
    "zoominfo.com",
}

LEGAL_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "gmbh",
    "inc",
    "incorporated",
    "llc",
    "llp",
    "ltd",
    "limited",
    "lp",
    "plc",
    "pvt",
    "sa",
    "services",
    "the",
}

BUSINESS_HINTS = (
    "about",
    "contact",
    "privacy",
    "terms",
    "careers",
    "services",
)

PARKED_MARKERS = (
    "buy this domain",
    "domain for sale",
    "for sale",
    "coming soon",
    "under construction",
    "parkingcrew",
    "sedo",
    "hugedomains",
    "dan.com",
    "undeveloped",
)

SEARCH_EXCLUDED_HOSTS = (
    "bbb.org",
    "bloomberg.com",
    "chamberofcommerce.com",
    "crunchbase.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "manta.com",
    "opencorporates.com",
    "yelp.com",
    "zoominfo.com",
)

LOCATION_TLD_HINTS = {
    "australia": ("com.au", "net.au", "au"),
    "australian": ("com.au", "net.au", "au"),
    "au": ("com.au", "net.au", "au"),
    "nsw": ("com.au", "net.au", "au"),
    "vic": ("com.au", "net.au", "au"),
    "qld": ("com.au", "net.au", "au"),
    "usa": ("com", "us"),
    "united states": ("com", "us"),
    "us": ("com", "us"),
    "india": ("in", "co.in"),
    "in": ("in", "co.in"),
    "korea": ("kr", "co.kr"),
    "south korea": ("kr", "co.kr"),
    "kr": ("kr", "co.kr"),
}


@dataclass(slots=True)
class WebsiteCandidate:
    url: str
    source: str
    title: str = ""
    snippet: str = ""
    source_score: float = 0.0
    source_count: int = 1

    def key(self) -> str:
        return registered_domain(hostname(self.url))


@dataclass(slots=True)
class Verification:
    url: str
    final_url: str = ""
    title: str = ""
    description: str = ""
    text_sample: str = ""
    http_status: int | None = None
    ok: bool = False
    parked: bool = False
    error: str = ""
    elapsed_ms: int = 0
    structured_names: list[str] = field(default_factory=list)
    structured_urls: list[str] = field(default_factory=list)
    structured_same_as: list[str] = field(default_factory=list)
    structured_country: str = ""


@dataclass(slots=True)
class WebsiteMatch:
    company: str
    location: str = ""
    website: str = ""
    status: str = "not_found"
    confidence: float = 0.0
    source: str = ""
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_row_values(self) -> dict[str, str]:
        return {
            "Website": self.website,
            "website_finder_status": self.status,
            "website_confidence": f"{self.confidence:.2f}" if self.confidence else "",
            "website_source": self.source,
            "website_reason": self.reason,
            "website_top_candidates": summarize_candidate_rows(self.candidates[:5]),
        }


@dataclass(slots=True)
class WebsiteFinderSettings:
    concurrency: int = 10
    timeout_seconds: int = 12
    min_confidence: float = 0.62
    user_agent: str = (
        "Mozilla/5.0 (compatible; LightningContactScraper/1.0; "
        "+https://example.com/bot)"
    )
    google_cse_api_key: str = ""
    google_cse_id: str = ""
    brave_api_key: str = ""
    serpapi_api_key: str = ""
    searchapi_api_key: str = ""
    searxng_base_url: str = ""

    @classmethod
    def from_env(cls) -> "WebsiteFinderSettings":
        return cls(
            google_cse_api_key=os.getenv("GOOGLE_CSE_API_KEY", "").strip(),
            google_cse_id=os.getenv("GOOGLE_CSE_ID", "").strip(),
            brave_api_key=os.getenv("BRAVE_SEARCH_API_KEY", "").strip(),
            serpapi_api_key=os.getenv("SERPAPI_API_KEY", "").strip(),
            searchapi_api_key=os.getenv("SEARCHAPI_API_KEY", "").strip(),
            searxng_base_url=os.getenv("SEARXNG_BASE_URL", "").strip().rstrip("/"),
        )


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def summarize_candidate_rows(
    candidates: list[dict[str, Any]], limit: int = 3
) -> str:
    parts: list[str] = []
    for item in candidates[:limit]:
        if not isinstance(item, dict):
            continue
        url = _clean_text(str(item.get("url", "") or ""))
        source = _clean_text(str(item.get("source", "") or ""))
        title = _clean_text(str(item.get("title", "") or ""))
        score = item.get("score", "")
        summary = url
        details = []
        if score != "":
            details.append(f"{score}")
        if source:
            details.append(source)
        if details:
            summary += f" ({', '.join(details)})"
        if title:
            summary += f" - {title}"
        parts.append(summary)
    return " | ".join(parts)


def _looks_parked(title: str, description: str, text: str) -> bool:
    sample = " ".join([title, description, text[:2000]]).casefold()
    return any(marker in sample for marker in PARKED_MARKERS)


def _search_query(company: str, location: str) -> str:
    safe_company = (company or "").replace('"', " ").strip()
    parts = [f'"{safe_company}"' if safe_company else ""]
    if location:
        parts.append(location)
    parts.append('"official website"')
    parts.extend(f"-site:{host}" for host in SEARCH_EXCLUDED_HOSTS)
    return " ".join(part for part in parts if part)


def _tokens(value: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", (value or "").casefold())
    return [token for token in raw if len(token) > 1 and token not in LEGAL_SUFFIXES]


def _company_signature(company: str) -> str:
    return " ".join(_tokens(company))


def _similarity(left: str, right: str) -> float:
    left_sig = _company_signature(left)
    right_sig = _company_signature(right)
    if not left_sig or not right_sig:
        return 0.0
    left_tokens = set(left_sig.split())
    right_tokens = set(right_sig.split())
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens))
    ratio = SequenceMatcher(None, left_sig, right_sig).ratio()
    return (overlap * 0.65) + (ratio * 0.35)


def _domain_similarity(company: str, url: str) -> float:
    ordered_company_tokens = _tokens(company)
    company_tokens = set(ordered_company_tokens)
    domain = registered_domain(hostname(url)).split(".", 1)[0]
    compact_domain = re.sub(r"[^a-z0-9]", "", domain.casefold())
    domain_tokens = set(_tokens(re.sub(r"[-_]", " ", domain)))
    if not company_tokens or not domain_tokens:
        return 0.0
    if "".join(ordered_company_tokens) == compact_domain:
        return 1.0
    return len(company_tokens & domain_tokens) / max(1, len(company_tokens))


def _location_score(location: str, text: str) -> float:
    location_tokens = set(_tokens(location))
    if not location_tokens:
        return 0.0
    text_tokens = set(_tokens(text))
    return len(location_tokens & text_tokens) / max(1, len(location_tokens))


def _walk_json(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _is_directory_url(url: str) -> bool:
    host = hostname(url)
    domain = registered_domain(host)
    return domain in DIRECTORY_HOSTS or host in DIRECTORY_HOSTS


def _candidate_from_url(
    raw_url: str,
    source: str,
    *,
    title: str = "",
    snippet: str = "",
    source_score: float = 0.0,
) -> WebsiteCandidate | None:
    try:
        url = normalize_url(raw_url)
    except (TypeError, ValueError):
        return None
    if _is_directory_url(url):
        return None
    return WebsiteCandidate(
        url=url,
        source=source,
        title=_clean_text(title),
        snippet=_clean_text(snippet),
        source_score=source_score,
    )


def score_candidate(
    company: str,
    location: str,
    candidate: WebsiteCandidate,
    verification: Verification,
) -> tuple[float, str]:
    evidence_text = " ".join(
        [
            candidate.title,
            candidate.snippet,
            verification.title,
            verification.description,
            verification.text_sample,
            verification.structured_country,
        ]
    )
    name_score = max(
        _similarity(company, candidate.title),
        _similarity(company, candidate.snippet),
        _similarity(company, verification.title),
        _similarity(company, verification.description),
        _similarity(company, verification.text_sample[:2000]),
        *(
            _similarity(company, name)
            for name in verification.structured_names
        ),
    )
    domain_score = _domain_similarity(company, verification.final_url or candidate.url)
    loc_score = _location_score(location, evidence_text)
    official_bonus = 0.06 if verification.ok else 0.0
    source_score = candidate.source_score

    if candidate.source == "wikidata":
        source_score = max(source_score, 0.25)
    elif candidate.source == "clearbit":
        source_score = max(source_score, 0.12)
    elif candidate.source in {"google_cse", "brave", "serpapi", "searchapi", "searxng"}:
        source_score = max(source_score, 0.16)

    agreement_bonus = min(0.12, max(0, candidate.source_count - 1) * 0.04)
    structured_bonus = 0.0
    final_url = verification.final_url or candidate.url
    if any(_domain_similarity(company, name) >= 0.75 for name in verification.structured_names):
        structured_bonus += 0.06
    if any(
        registered_domain(hostname(url)) == registered_domain(hostname(final_url))
        for url in verification.structured_urls
    ):
        structured_bonus += 0.04
    if any(
        registered_domain(hostname(url)) == registered_domain(hostname(final_url))
        for url in verification.structured_same_as
    ):
        structured_bonus += 0.02

    score = (
        (name_score * 0.42)
        + (domain_score * 0.27)
        + (loc_score * 0.11)
        + source_score
        + official_bonus
        + agreement_bonus
        + min(structured_bonus, 0.10)
    )

    if _is_directory_url(verification.final_url or candidate.url):
        score *= 0.25
    if not verification.ok:
        score *= 0.75
    if verification.parked:
        score *= 0.2
    if domain_score == 0 and name_score < 0.45:
        score *= 0.55

    reasons = [
        f"name={name_score:.2f}",
        f"domain={domain_score:.2f}",
        f"location={loc_score:.2f}",
        f"source={candidate.source}",
        f"agreement={candidate.source_count}",
    ]
    if structured_bonus:
        reasons.append(f"structured={structured_bonus:.2f}")
    return min(score, 0.99), ", ".join(reasons)


class WebsiteFinder:
    def __init__(self, settings: WebsiteFinderSettings | None = None):
        self.settings = settings or WebsiteFinderSettings.from_env()
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "WebsiteFinder":
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_seconds)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def find(self, company: str, location: str = "") -> WebsiteMatch:
        started = time.perf_counter()
        company = _clean_text(company)
        location = _clean_text(location)
        if not company:
            return WebsiteMatch(
                company=company,
                location=location,
                status="invalid_input",
                reason="Company name is empty.",
            )
        candidates = await self._collect_candidates(company, location)
        if not candidates:
            return WebsiteMatch(
                company=company,
                location=location,
                status="not_found",
                reason="No plausible website candidates found.",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        scored: list[tuple[float, str, WebsiteCandidate, Verification]] = []
        for candidate in candidates[:12]:
            verification = await self._verify(candidate.url)
            score, reason = score_candidate(company, location, candidate, verification)
            scored.append((score, reason, candidate, verification))
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_reason, best_candidate, best_verification = scored[0]
        status = "found" if best_score >= self.settings.min_confidence else "review"
        website = best_verification.final_url or best_candidate.url
        if status == "review":
            website = ""

        return WebsiteMatch(
            company=company,
            location=location,
            website=website,
            status=status,
            confidence=best_score,
            source=best_candidate.source,
            reason=best_reason,
            candidates=[
                {
                    "url": verification.final_url or candidate.url,
                    "source": candidate.source,
                    "score": round(score, 3),
                    "reason": reason,
                    "title": verification.title or candidate.title,
                    "status": verification.http_status,
                    "error": verification.error,
                }
                for score, reason, candidate, verification in scored[:5]
            ],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _collect_candidates(
        self, company: str, location: str
    ) -> list[WebsiteCandidate]:
        tasks = [
            self._from_clearbit(company),
            self._from_wikidata(company),
            self._from_duckduckgo(company, location),
            self._from_domain_guesses(company, location),
        ]
        if self.settings.google_cse_api_key and self.settings.google_cse_id:
            tasks.append(self._from_google_cse(company, location))
        if self.settings.brave_api_key:
            tasks.append(self._from_brave(company, location))
        if self.settings.serpapi_api_key:
            tasks.append(self._from_serpapi(company, location))
        if self.settings.searchapi_api_key:
            tasks.append(self._from_searchapi(company, location))
        if self.settings.searxng_base_url:
            tasks.append(self._from_searxng(company, location))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: dict[str, WebsiteCandidate] = {}
        sources_by_key: dict[str, set[str]] = {}
        for result in results:
            if isinstance(result, Exception):
                LOGGER.debug("website provider failed: %s", result)
                continue
            for candidate in result:
                key = candidate.key()
                sources_by_key.setdefault(key, set()).add(candidate.source)
                current = merged.get(key)
                if current is None or candidate.source_score > current.source_score:
                    merged[key] = candidate
        for key, candidate in merged.items():
            source_count = len(sources_by_key.get(key, {candidate.source}))
            candidate.source_count = max(1, source_count)
            candidate.source_score += min(0.12, max(0, source_count - 1) * 0.04)
        return sorted(
            merged.values(),
            key=lambda item: (
                item.source_score,
                _domain_similarity(company, item.url),
                _similarity(company, item.title + " " + item.snippet),
            ),
            reverse=True,
        )

    async def _json_get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> dict[str, Any] | list[Any]:
        if self.session is None:
            raise RuntimeError("WebsiteFinder.start() was not called")
        async with self.session.get(url, headers=headers) as response:
            if response.status >= 400:
                raise aiohttp.ClientResponseError(
                    response.request_info,
                    response.history,
                    status=response.status,
                    message=await response.text(),
                    headers=response.headers,
                )
            return await response.json(content_type=None)

    async def _from_clearbit(self, company: str) -> list[WebsiteCandidate]:
        url = (
            "https://autocomplete.clearbit.com/v1/companies/suggest?query="
            + quote(company)
        )
        data = await self._json_get(url)
        if not isinstance(data, list):
            return []
        candidates: list[WebsiteCandidate] = []
        for item in data[:8]:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain", "") or "")
            name = str(item.get("name", "") or "")
            score = 0.10 + (_similarity(company, name) * 0.12)
            candidate = _candidate_from_url(
                domain,
                "clearbit",
                title=name,
                source_score=score,
            )
            if candidate:
                candidates.append(candidate)
        return candidates

    async def _from_wikidata(self, company: str) -> list[WebsiteCandidate]:
        search_url = (
            "https://www.wikidata.org/w/api.php?"
            + urlencode(
                {
                    "action": "wbsearchentities",
                    "search": company,
                    "language": "en",
                    "format": "json",
                    "limit": "8",
                }
            )
        )
        search_data = await self._json_get(search_url)
        if not isinstance(search_data, dict):
            return []
        ids = [
            str(item.get("id"))
            for item in search_data.get("search", [])
            if isinstance(item, dict) and item.get("id")
        ]
        if not ids:
            return []
        entity_url = (
            "https://www.wikidata.org/w/api.php?"
            + urlencode(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(ids),
                    "props": "claims|labels|descriptions",
                    "languages": "en",
                    "format": "json",
                }
            )
        )
        entity_data = await self._json_get(entity_url)
        if not isinstance(entity_data, dict):
            return []
        candidates: list[WebsiteCandidate] = []
        for entity in entity_data.get("entities", {}).values():
            if not isinstance(entity, dict):
                continue
            label = entity.get("labels", {}).get("en", {}).get("value", "")
            description = entity.get("descriptions", {}).get("en", {}).get("value", "")
            for claim in entity.get("claims", {}).get("P856", []):
                mainsnak = claim.get("mainsnak", {})
                url = mainsnak.get("datavalue", {}).get("value", "")
                candidate = _candidate_from_url(
                    str(url),
                    "wikidata",
                    title=str(label),
                    snippet=str(description),
                    source_score=0.24 + (_similarity(company, str(label)) * 0.15),
                )
                if candidate:
                    candidates.append(candidate)
        return candidates

    async def _from_duckduckgo(
        self, company: str, location: str
    ) -> list[WebsiteCandidate]:
        query = _search_query(company, location)
        url = (
            "https://api.duckduckgo.com/?"
            + urlencode(
                {
                    "q": query,
                    "format": "json",
                    "no_redirect": "1",
                    "no_html": "1",
                    "skip_disambig": "1",
                }
            )
        )
        data = await self._json_get(url)
        if not isinstance(data, dict):
            return []
        candidates: list[WebsiteCandidate] = []
        abstract_url = str(data.get("AbstractURL", "") or "")
        heading = str(data.get("Heading", "") or "")
        abstract = str(data.get("AbstractText", "") or "")
        candidate = _candidate_from_url(
            abstract_url,
            "duckduckgo",
            title=heading,
            snippet=abstract,
            source_score=0.12,
        )
        if candidate:
            candidates.append(candidate)
        for item in data.get("Results", [])[:4]:
            if isinstance(item, dict):
                candidate = _candidate_from_url(
                    str(item.get("FirstURL", "") or ""),
                    "duckduckgo",
                    title=str(item.get("Text", "") or ""),
                    source_score=0.08,
                )
                if candidate:
                    candidates.append(candidate)
        return candidates

    async def _from_google_cse(
        self, company: str, location: str
    ) -> list[WebsiteCandidate]:
        query = _search_query(company, location)
        url = (
            "https://www.googleapis.com/customsearch/v1?"
            + urlencode(
                {
                    "key": self.settings.google_cse_api_key,
                    "cx": self.settings.google_cse_id,
                    "q": query,
                    "num": "5",
                }
            )
        )
        data = await self._json_get(url)
        if not isinstance(data, dict):
            return []
        return [
            candidate
            for item in data.get("items", [])
            if isinstance(item, dict)
            for candidate in [
                _candidate_from_url(
                    str(item.get("link", "") or ""),
                    "google_cse",
                    title=str(item.get("title", "") or ""),
                    snippet=str(item.get("snippet", "") or ""),
                    source_score=0.18,
                )
            ]
            if candidate
        ]

    async def _from_brave(
        self, company: str, location: str
    ) -> list[WebsiteCandidate]:
        query = _search_query(company, location)
        url = "https://api.search.brave.com/res/v1/web/search?" + urlencode(
            {"q": query, "count": "5"}
        )
        data = await self._json_get(
            url, headers={"X-Subscription-Token": self.settings.brave_api_key}
        )
        if not isinstance(data, dict):
            return []
        return [
            candidate
            for item in data.get("web", {}).get("results", [])
            if isinstance(item, dict)
            for candidate in [
                _candidate_from_url(
                    str(item.get("url", "") or ""),
                    "brave",
                    title=str(item.get("title", "") or ""),
                    snippet=str(item.get("description", "") or ""),
                    source_score=0.17,
                )
            ]
            if candidate
        ]

    async def _from_serpapi(
        self, company: str, location: str
    ) -> list[WebsiteCandidate]:
        query = _search_query(company, location)
        url = "https://serpapi.com/search.json?" + urlencode(
            {
                "engine": "google",
                "q": query,
                "api_key": self.settings.serpapi_api_key,
                "num": "5",
            }
        )
        data = await self._json_get(url)
        if not isinstance(data, dict):
            return []
        return [
            candidate
            for item in data.get("organic_results", [])
            if isinstance(item, dict)
            for candidate in [
                _candidate_from_url(
                    str(item.get("link", "") or ""),
                    "serpapi",
                    title=str(item.get("title", "") or ""),
                    snippet=str(item.get("snippet", "") or ""),
                    source_score=0.19,
                )
            ]
            if candidate
        ]

    async def _from_searchapi(
        self, company: str, location: str
    ) -> list[WebsiteCandidate]:
        query = _search_query(company, location)
        url = "https://www.searchapi.io/api/v1/search?" + urlencode(
            {
                "engine": "google",
                "q": query,
                "api_key": self.settings.searchapi_api_key,
                "num": "5",
            }
        )
        data = await self._json_get(url)
        if not isinstance(data, dict):
            return []
        return [
            candidate
            for item in data.get("organic_results", [])
            if isinstance(item, dict)
            for candidate in [
                _candidate_from_url(
                    str(item.get("link", "") or ""),
                    "searchapi",
                    title=str(item.get("title", "") or ""),
                    snippet=str(item.get("snippet", "") or ""),
                    source_score=0.18,
                )
            ]
            if candidate
        ]

    async def _from_searxng(
        self, company: str, location: str
    ) -> list[WebsiteCandidate]:
        query = _search_query(company, location)
        url = (
            f"{self.settings.searxng_base_url}/search?"
            + urlencode(
                {
                    "q": query,
                    "format": "json",
                    "language": "en-US",
                    "categories": "general",
                }
            )
        )
        data = await self._json_get(url)
        if not isinstance(data, dict):
            return []
        candidates: list[WebsiteCandidate] = []
        for index, item in enumerate(data.get("results", [])[:6]):
            if not isinstance(item, dict):
                continue
            candidate = _candidate_from_url(
                str(item.get("url", "") or ""),
                "searxng",
                title=str(item.get("title", "") or ""),
                snippet=str(item.get("content", "") or ""),
                source_score=max(0.12, 0.18 - (index * 0.015)),
            )
            if candidate:
                candidates.append(candidate)
        return candidates

    async def _from_domain_guesses(
        self, company: str, location: str = ""
    ) -> list[WebsiteCandidate]:
        tokens = _tokens(company)
        if not tokens:
            return []
        compact = "".join(tokens)
        dashed = "-".join(tokens)
        bases = [compact]
        if dashed != compact:
            bases.append(dashed)
        candidates = []
        suffixes = ["com", "net", "org", "co"]
        location_text = f" {location.casefold()} "
        for hint, hinted_suffixes in LOCATION_TLD_HINTS.items():
            if f" {hint} " in location_text:
                suffixes = list(dict.fromkeys([*hinted_suffixes, *suffixes]))
                break
        for base in bases[:2]:
            for suffix in suffixes[:7]:
                candidate = _candidate_from_url(
                    f"https://{base}.{suffix}/",
                    "domain_guess",
                    source_score=0.02,
                )
                if candidate:
                    candidates.append(candidate)
        return candidates

    async def _verify(self, url: str) -> Verification:
        started = time.perf_counter()
        if self.session is None:
            raise RuntimeError("WebsiteFinder.start() was not called")
        try:
            await validate_public_host(url)
            async with self.session.get(
                url, allow_redirects=True, max_redirects=6
            ) as response:
                body = await response.read()
                html = body[:1_000_000].decode(
                    response.charset or "utf-8", errors="replace"
                )
                title, description, text_sample, structured = self._extract_page_evidence(html)
                return Verification(
                    url=url,
                    final_url=str(response.url),
                    title=title,
                    description=description,
                    text_sample=text_sample,
                    http_status=response.status,
                    ok=200 <= response.status < 400,
                    parked=_looks_parked(title, description, text_sample),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    structured_names=structured["names"],
                    structured_urls=structured["urls"],
                    structured_same_as=structured["same_as"],
                    structured_country=structured["country"],
                )
        except Exception as exc:
            return Verification(
                url=url,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

    def _extract_page_evidence(
        self, html: str
    ) -> tuple[str, str, str, dict[str, Any]]:
        soup = BeautifulSoup(html or "", "lxml")
        title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        description = ""
        meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        if meta and meta.get("content"):
            description = _clean_text(str(meta.get("content")))
        if not description:
            og = soup.find("meta", attrs={"property": "og:description"})
            if og and og.get("content"):
                description = _clean_text(str(og.get("content")))
        structured: dict[str, Any] = {
            "names": [],
            "urls": [],
            "same_as": [],
            "country": "",
        }
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(script.string or "")
            except json.JSONDecodeError:
                continue
            for item in _walk_json(payload):
                for key in ("name", "legalName", "alternateName"):
                    value = item.get(key)
                    values = value if isinstance(value, list) else [value]
                    for name in values:
                        if isinstance(name, str):
                            structured["names"].append(_clean_text(name))
                url = item.get("url")
                if isinstance(url, str):
                    structured["urls"].append(url)
                same_as = item.get("sameAs")
                same_as_values = same_as if isinstance(same_as, list) else [same_as]
                for raw in same_as_values:
                    if isinstance(raw, str):
                        structured["same_as"].append(raw)
                address = item.get("address")
                if isinstance(address, dict) and not structured["country"]:
                    country = address.get("addressCountry")
                    if isinstance(country, str):
                        structured["country"] = _clean_text(country)
        for element in soup(["script", "style", "noscript", "template", "svg"]):
            element.decompose()
        text = _clean_text(soup.get_text(" ", strip=True))
        useful = " ".join([*structured["names"], text])
        structured["names"] = list(dict.fromkeys(structured["names"]))[:8]
        structured["urls"] = list(dict.fromkeys(structured["urls"]))[:8]
        structured["same_as"] = list(dict.fromkeys(structured["same_as"]))[:8]
        return title, description, useful[:5000], structured


def select_company_column(fieldnames: list[str], requested: str | None = None) -> str:
    if requested:
        for field in fieldnames:
            if field.casefold().strip() == requested.casefold().strip():
                return field
        raise ValueError(f"company column not found: {requested}")
    preferred = ("company", "company name", "business", "business name", "name")
    lookup = {field.casefold().strip(): field for field in fieldnames}
    for candidate in preferred:
        if candidate in lookup:
            return lookup[candidate]
    raise ValueError("Could not auto-detect company column. Use --company-column.")


def select_location_column(
    fieldnames: list[str], requested: str | None = None
) -> str | None:
    if requested:
        for field in fieldnames:
            if field.casefold().strip() == requested.casefold().strip():
                return field
        raise ValueError(f"location column not found: {requested}")
    preferred = ("location", "city", "state", "address", "country")
    lookup = {field.casefold().strip(): field for field in fieldnames}
    for candidate in preferred:
        if candidate in lookup:
            return lookup[candidate]
    return None


async def resolve_company_csv(
    input_csv: Path,
    output_csv: Path,
    *,
    company_column: str | None = None,
    location_column: str | None = None,
    settings: WebsiteFinderSettings | None = None,
) -> Path:
    settings = settings or WebsiteFinderSettings.from_env()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with input_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        fields = list(reader.fieldnames)
        company_field = select_company_column(fields, company_column)
        location_field = select_location_column(fields, location_column)
        rows = list(reader)

    semaphore = asyncio.Semaphore(max(1, settings.concurrency))
    async with WebsiteFinder(settings) as finder:
        async def resolve(row: dict[str, str]) -> WebsiteMatch:
            async with semaphore:
                return await finder.find(
                    str(row.get(company_field, "") or ""),
                    str(row.get(location_field, "") or "") if location_field else "",
                )

        matches = await asyncio.gather(*(resolve(row) for row in rows))

    added_fields = [
        "Website",
        "website_finder_status",
        "website_confidence",
        "website_source",
        "website_reason",
        "website_top_candidates",
    ]
    fieldnames = fields + [field for field in added_fields if field not in fields]
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row, match in zip(rows, matches):
            original = dict(row)
            if original.get("Website") and match.status != "found":
                original.update({key: value for key, value in match.to_row_values().items() if key != "Website"})
            else:
                original.update(match.to_row_values())
            writer.writerow(original)
    return output_csv
