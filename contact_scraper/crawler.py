from __future__ import annotations

import asyncio
import logging
import time
import re

import phonenumbers

from .accuracy import CLEAN_SOCIAL_FIELDS, CleanExportOptions, kinds_for_clean_fields
from .config import Settings
from .extractors import extract_page, merge_contacts, rank_subpages
from .fetchers import FetchManager
from .models import Evidence, PageData, SiteResult
from .utils import domain_key, origin

LOGGER = logging.getLogger(__name__)
ALL_KINDS = {"email", "phone", "social", "address", "description"}


def fulfilled_kinds(pages: list[PageData], wanted: set[str]) -> set[str]:
    fulfilled: set[str] = set()
    contacts = merge_contacts(pages)
    if "email" in wanted and any(item.kind == "email" for item in contacts):
        fulfilled.add("email")
    if "phone" in wanted and any(item.kind == "phone" for item in contacts):
        fulfilled.add("phone")
    # One valid company social profile is enough to stop social-only searching.
    if "social" in wanted and any(item.kind == "social" for item in contacts):
        fulfilled.add("social")
    if "address" in wanted and any(page.address for page in pages):
        fulfilled.add("address")
    if "description" in wanted and any(page.description for page in pages):
        fulfilled.add("description")
    return fulfilled


def _contact_meets_quality(contact: Evidence, options: CleanExportOptions) -> bool:
    return contact.confidence >= options.fast_quality_threshold


def _phone_matches_options(
    contact: Evidence,
    settings: Settings,
    options: CleanExportOptions,
) -> bool:
    if contact.kind != "phone" or not _contact_meets_quality(contact, options):
        return False
    target_region = None if options.phone_region == "AUTO" else options.phone_region
    try:
        number = phonenumbers.parse(
            contact.value,
            target_region or settings.default_phone_region or None,
        )
    except phonenumbers.NumberParseException:
        return False
    if not phonenumbers.is_valid_number(number):
        return False
    region = phonenumbers.region_code_for_number(number) or ""
    return not target_region or region == target_region


def fulfilled_clean_fields(
    pages: list[PageData],
    settings: Settings,
    options: CleanExportOptions,
) -> set[str]:
    fulfilled: set[str] = set()
    contacts = merge_contacts(pages)
    for field in options.fields or ():
        if field == "website":
            fulfilled.add(field)
        elif field == "email" and any(
            item.kind == "email" and _contact_meets_quality(item, options)
            for item in contacts
        ):
            fulfilled.add(field)
        elif field == "phone" and any(
            _phone_matches_options(item, settings, options) for item in contacts
        ):
            fulfilled.add(field)
        elif field in CLEAN_SOCIAL_FIELDS and any(
            item.kind == "social"
            and item.category == CLEAN_SOCIAL_FIELDS[field]
            and _contact_meets_quality(item, options)
            for item in contacts
        ):
            fulfilled.add(field)
        elif field == "address" and any(page.address for page in pages):
            fulfilled.add(field)
        elif field == "description" and any(page.description for page in pages):
            fulfilled.add(field)
    return fulfilled


def _needed_kinds_for_fast(
    selected_kinds: set[str],
    remaining_fields: set[str],
    options: CleanExportOptions | None,
) -> set[str]:
    if not options:
        return selected_kinds
    if not remaining_fields:
        return set()
    needed = kinds_for_clean_fields(tuple(remaining_fields))
    return (needed or selected_kinds) & selected_kinds


class SiteCrawler:
    def __init__(
        self,
        settings: Settings,
        fetcher: FetchManager,
        selected_kinds: set[str] | None = None,
        crawl_mode: str = "full",
        clean_options: CleanExportOptions | None = None,
    ):
        self.settings = settings
        self.fetcher = fetcher
        self.selected_kinds = set(selected_kinds or ALL_KINDS)
        self.crawl_mode = crawl_mode if crawl_mode in {"fast", "full"} else "full"
        self.clean_options = clean_options

    async def _discover_sitemap_pages(
        self,
        site_origin: str,
        needed_kinds: set[str],
    ) -> list[str]:
        sitemap_urls = {
            site_origin + "/sitemap.xml",
            site_origin + "/sitemap_index.xml",
        }
        try:
            robots = await self.fetcher.fetch(
                site_origin + "/robots.txt",
                homepage=False,
                allow_browser=False,
            )
            if robots.ok and robots.html:
                for line in robots.html.splitlines():
                    match = re.match(r"\s*sitemap\s*:\s*(\S+)", line, re.I)
                    if match:
                        sitemap_urls.add(match.group(1).strip())
        except Exception:
            pass

        discovered: list[tuple[str, str]] = []
        for sitemap_url in list(sitemap_urls)[:4]:
            try:
                result = await self.fetcher.fetch(
                    sitemap_url,
                    homepage=False,
                    allow_browser=False,
                )
            except Exception:
                continue
            if not result.ok or not result.html:
                continue
            urls = [
                re.sub(r"\s+", "", match)
                for match in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", result.html, re.I)
            ]
            for url in urls[:500]:
                if not url.startswith(site_origin):
                    continue
                discovered.append((url, url.rsplit("/", 1)[-1]))
        return rank_subpages(
            discovered,
            site_origin,
            max(self.settings.max_pages * 4, 20),
            needed_kinds=needed_kinds,
        )

    async def crawl(self, input_url: str, normalized_url: str) -> SiteResult:
        started = time.perf_counter()
        site_domain = domain_key(normalized_url)
        result = SiteResult(
            input_url=input_url,
            normalized_url=normalized_url,
            domain=site_domain,
        )
        try:
            homepage = await self.fetcher.fetch(
                normalized_url, homepage=True, allow_browser=True
            )
        except Exception as exc:
            result.errors.append(f"{type(exc).__name__}: {exc}")
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return result

        result.fetch_tier = homepage.tier
        result.http_status = homepage.status
        result.final_url = homepage.final_url or normalized_url
        if not homepage.ok:
            result.errors.append(
                homepage.error
                or f"homepage fetch failed with status {homepage.status}"
            )
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return result

        pages: list[PageData] = []
        try:
            home_data = extract_page(
                homepage.html,
                result.final_url,
                site_domain,
                self.settings.default_phone_region,
                self.selected_kinds,
            )
            pages.append(home_data)
        except Exception as exc:
            result.errors.append(f"homepage parse: {type(exc).__name__}: {exc}")
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return result

        max_pages = max(1, self.settings.max_pages)
        site_origin = origin(result.final_url)
        seen = {home_data.url.rstrip("/")}
        queued: set[str] = set()
        candidates: list[str] = []
        attempted_pages = 1
        semaphore = asyncio.Semaphore(self.settings.per_site_concurrency)

        def add_candidates(page: PageData, needed: set[str]) -> None:
            if not needed:
                return
            ranked = rank_subpages(
                page.links,
                site_origin,
                max(max_pages * 4, 20),
                needed_kinds=needed,
            )
            for url in ranked:
                key = url.rstrip("/")
                if key in seen or key in queued:
                    continue
                queued.add(key)
                candidates.append(url)

        sitemap_loaded = False

        async def add_sitemap_candidates(needed: set[str]) -> None:
            nonlocal sitemap_loaded
            if sitemap_loaded or not needed:
                return
            sitemap_loaded = True
            for url in await self._discover_sitemap_pages(site_origin, needed):
                key = url.rstrip("/")
                if key in seen or key in queued:
                    continue
                queued.add(key)
                candidates.append(url)

        async def fetch_subpage(
            url: str, extract_kinds: set[str]
        ) -> PageData | None:
            async with semaphore:
                try:
                    fetched = await self.fetcher.fetch(
                        url,
                        homepage=False,
                        allow_browser=False,
                    )
                    if not fetched.ok:
                        if fetched.error and "robots.txt" not in fetched.error:
                            result.errors.append(
                                f"{url}: {fetched.error or fetched.status}"
                            )
                        return None
                    if fetched.tier not in result.fetch_tier.split("+"):
                        result.fetch_tier = (
                            f"{result.fetch_tier}+{fetched.tier}"
                            if result.fetch_tier
                            else fetched.tier
                        )
                    return extract_page(
                        fetched.html,
                        fetched.final_url or url,
                        site_domain,
                        self.settings.default_phone_region,
                        extract_kinds,
                    )
                except Exception as exc:
                    result.errors.append(f"{url}: {type(exc).__name__}: {exc}")
                    return None

        if self.clean_options:
            remaining_fields = set(self.clean_options.fields or ()) - fulfilled_clean_fields(
                pages, self.settings, self.clean_options
            )
            remaining = _needed_kinds_for_fast(
                self.selected_kinds, remaining_fields, self.clean_options
            )
        else:
            remaining_fields = set()
            remaining = self.selected_kinds - fulfilled_kinds(
                pages, self.selected_kinds
            )
        if self.crawl_mode == "fast":
            add_candidates(home_data, remaining)
            if not candidates:
                await add_sitemap_candidates(remaining)
            while candidates and attempted_pages < max_pages and remaining:
                url = candidates.pop(0)
                queued.discard(url.rstrip("/"))
                seen.add(url.rstrip("/"))
                attempted_pages += 1
                page = await fetch_subpage(url, remaining)
                if page is None:
                    continue
                pages.append(page)
                if self.clean_options:
                    remaining_fields = set(self.clean_options.fields or ()) - fulfilled_clean_fields(
                        pages, self.settings, self.clean_options
                    )
                    remaining = _needed_kinds_for_fast(
                        self.selected_kinds, remaining_fields, self.clean_options
                    )
                else:
                    remaining = self.selected_kinds - fulfilled_kinds(
                        pages, self.selected_kinds
                    )
                if remaining:
                    add_candidates(page, remaining)
                    if not candidates:
                        await add_sitemap_candidates(remaining)
        else:
            add_candidates(home_data, self.selected_kinds)
            while candidates and attempted_pages < max_pages:
                batch_size = min(
                    self.settings.per_site_concurrency,
                    max_pages - attempted_pages,
                    len(candidates),
                )
                batch = [candidates.pop(0) for _ in range(batch_size)]
                attempted_pages += len(batch)
                for url in batch:
                    queued.discard(url.rstrip("/"))
                    seen.add(url.rstrip("/"))
                subpages = await asyncio.gather(
                    *(
                        fetch_subpage(url, self.selected_kinds)
                        for url in batch
                    )
                )
                for page in subpages:
                    if page is None:
                        continue
                    pages.append(page)
                    add_candidates(page, self.selected_kinds)

        result.contacts = merge_contacts(pages)
        result.pages_scraped = [page.url for page in pages]
        result.title = next((page.title for page in pages if page.title), "")
        addresses = [page.address for page in pages if page.address]
        descriptions = [page.description for page in pages if page.description]
        if self.crawl_mode == "full":
            result.address = max(addresses, key=len, default="")
            result.description = max(descriptions, key=len, default="")
        else:
            result.address = addresses[0] if addresses else ""
            result.description = descriptions[0] if descriptions else ""
        found_any = bool(result.contacts or result.address or result.description)
        result.status = "ok" if found_any else "partial"
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        LOGGER.info(
            "scraped %s: %d contacts from %d pages",
            site_domain,
            len(result.contacts),
            len(result.pages_scraped),
            extra={
                "domain": site_domain,
                "url": result.final_url,
                "tier": result.fetch_tier,
                "status": result.status,
                "elapsed_ms": result.elapsed_ms,
            },
        )
        return result
