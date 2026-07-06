from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit, urlunsplit

import phonenumbers
from bs4 import BeautifulSoup
from phonenumbers import PhoneNumberFormat

from .models import Evidence, PageData
from .utils import (
    canonical_link,
    compact_text,
    domain_key,
    hostname,
    same_registered_domain,
)


EMAIL_RE = re.compile(
    r"(?<![\w.+-])([A-Z0-9._%+\-]{1,64}@[A-Z0-9.\-]{1,253}\.[A-Z]{2,24})(?![\w.-])",
    re.IGNORECASE,
)
OBFUSCATED_EMAIL_RE = re.compile(
    r"([A-Z0-9._%+\-]{1,64})\s*"
    r"(?:\[|\(|\{)?\s*(?:at|@)\s*(?:\]|\)|\})?\s*"
    r"([A-Z0-9\-]+(?:\s*(?:\[|\(|\{)?\s*(?:dot|\.)\s*(?:\]|\)|\})?\s*[A-Z0-9\-]+)+)",
    re.IGNORECASE,
)
PHONE_CANDIDATE_RE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s().-]*)?(?:\(?\d{2,4}\)?[\s().-]*)"
    r"\d{2,4}[\s().-]*\d{3,5}(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?(?!\w)",
    re.IGNORECASE,
)

GENERIC_PREFIXES = {
    "admin",
    "billing",
    "bookings",
    "business",
    "care",
    "commercial",
    "contact",
    "customerservice",
    "enquiries",
    "enquiry",
    "export",
    "feedback",
    "hello",
    "help",
    "hr",
    "info",
    "inquiry",
    "jobs",
    "legal",
    "mail",
    "marketing",
    "media",
    "office",
    "orders",
    "press",
    "privacy",
    "reception",
    "recruiting",
    "reservations",
    "returns",
    "sales",
    "service",
    "support",
    "team",
    "webmaster",
}
FREE_EMAIL_DOMAINS = {
    "aol.com",
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "mail.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
    "ymail.com",
}
BAD_EMAIL_PARTS = {
    "example.com",
    "domain.com",
    "email.com",
    "sentry.io",
    "sentry.wixpress.com",
    "test.com",
    "wixpress.com",
}
BAD_EMAIL_EXTENSIONS = {
    ".avif",
    ".css",
    ".gif",
    ".jpeg",
    ".jpg",
    ".js",
    ".png",
    ".svg",
    ".webp",
}

SOCIAL_HOSTS = {
    "facebook": {"facebook.com", "fb.com"},
    "instagram": {"instagram.com"},
    "linkedin": {"linkedin.com"},
    "pinterest": {"pinterest.com"},
    "threads": {"threads.net"},
    "tiktok": {"tiktok.com"},
    "twitter": {"twitter.com", "x.com"},
    "youtube": {"youtube.com", "youtu.be"},
}
SOCIAL_REJECT_PARTS = {
    "/dialog/",
    "/help",
    "/home",
    "/intent/",
    "/login",
    "/oauth",
    "/privacy",
    "/search",
    "/share",
    "/sharer",
    "/signup",
    "/status/",
    "/terms",
}


def _decode_cf_email(encoded: str) -> str:
    try:
        key = int(encoded[:2], 16)
        return "".join(
            chr(int(encoded[index : index + 2], 16) ^ key)
            for index in range(2, len(encoded), 2)
        )
    except (ValueError, IndexError):
        return ""


def _deobfuscate(text: str) -> str:
    value = text
    replacements = [
        (r"\s*(?:\[|\(|\{)\s*at\s*(?:\]|\)|\})\s*", "@"),
        (r"\s+(?:at)\s+", "@"),
        (r"\s*(?:\[|\(|\{)\s*dot\s*(?:\]|\)|\})\s*", "."),
        (r"\s+(?:dot)\s+", "."),
    ]
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value


def _valid_email(value: str) -> bool:
    email = value.strip(" \t\r\n.,;:<>[](){}\"'").lower()
    if len(email) > 254 or email.count("@") != 1:
        return False
    local, host = email.rsplit("@", 1)
    if not local or len(local) > 64 or ".." in email:
        return False
    if any(part in email for part in BAD_EMAIL_PARTS):
        return False
    if any(email.endswith(extension) for extension in BAD_EMAIL_EXTENSIONS):
        return False
    if host.startswith(("-", ".")) or host.endswith(("-", ".")):
        return False
    labels = host.split(".")
    return len(labels) >= 2 and all(
        label and len(label) <= 63 and re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    )


def _email_category(email: str, site_domain: str) -> str:
    local, host = email.rsplit("@", 1)
    prefix = re.split(r"[._+\-]", local)[0]
    if host in FREE_EMAIL_DOMAINS:
        return "free_mail"
    if any(prefix == item or prefix.startswith(item) for item in GENERIC_PREFIXES):
        return "generic"
    if domain_key("https://" + host) == site_domain:
        return "named"
    return "external_domain"


def _email_confidence(
    email: str, source_type: str, site_domain: str, page_url: str
) -> float:
    host = email.rsplit("@", 1)[1]
    score = {
        "mailto": 0.98,
        "json_ld": 0.96,
        "cloudflare": 0.95,
        "visible_text": 0.88,
        "html": 0.72,
        "obfuscated": 0.86,
    }.get(source_type, 0.7)
    if domain_key("https://" + host) == site_domain:
        score += 0.02
    if any(word in page_url.lower() for word in ("contact", "about", "support")):
        score += 0.01
    return min(score, 0.99)


def _walk_json(value: object) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _json_ld_objects(soup: BeautifulSoup) -> list[dict]:
    objects: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        raw = raw.strip().removeprefix("<!--").removesuffix("-->")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        objects.extend(_walk_json(parsed))
    return objects


def _add_email(
    results: dict[tuple[str, str], Evidence],
    raw: str,
    page_url: str,
    source_type: str,
    site_domain: str,
) -> None:
    value = unquote(raw).split("?", 1)[0].strip().lower()
    match = EMAIL_RE.search(value)
    if not match:
        return
    email = match.group(1).strip(".,;:").lower()
    if not _valid_email(email):
        return
    evidence = Evidence(
        value=email,
        kind="email",
        source_url=page_url,
        source_type=source_type,
        confidence=_email_confidence(email, source_type, site_domain, page_url),
        category=_email_category(email, site_domain),
        raw_value=compact_text(raw, 200),
    )
    key = evidence.key()
    if key not in results or evidence.confidence > results[key].confidence:
        results[key] = evidence


def extract_emails(
    soup: BeautifulSoup, raw_html: str, page_url: str, site_domain: str
) -> list[Evidence]:
    results: dict[tuple[str, str], Evidence] = {}
    for anchor in soup.select('a[href^="mailto:" i]'):
        for address in anchor.get("href", "")[7:].split(","):
            _add_email(results, address, page_url, "mailto", site_domain)
    for element in soup.select("[data-cfemail]"):
        decoded = _decode_cf_email(element.get("data-cfemail", ""))
        _add_email(results, decoded, page_url, "cloudflare", site_domain)
    for obj in _json_ld_objects(soup):
        email = obj.get("email")
        if isinstance(email, str):
            _add_email(results, email, page_url, "json_ld", site_domain)
    visible = soup.get_text(" ", strip=True)
    for match in EMAIL_RE.finditer(visible):
        _add_email(results, match.group(1), page_url, "visible_text", site_domain)
    for match in EMAIL_RE.finditer(raw_html):
        _add_email(results, match.group(1), page_url, "html", site_domain)
    for match in OBFUSCATED_EMAIL_RE.finditer(visible):
        candidate = _deobfuscate(match.group(0))
        _add_email(results, candidate, page_url, "obfuscated", site_domain)
    return list(results.values())


def _phone_region(soup: BeautifulSoup, default_region: str) -> str:
    lang = (soup.html or {}).get("lang", "") if soup.html else ""
    if isinstance(lang, str) and "-" in lang:
        region = lang.rsplit("-", 1)[-1].upper()
        if len(region) == 2:
            return region
    return default_region


def _phone_type_label(number) -> str:
    return phonenumbers.PhoneNumberType.to_string(
        phonenumbers.number_type(number)
    )


def _normalize_phone(raw: str, region: str) -> tuple[str, str, str] | None:
    candidate = re.sub(r"\s+", " ", unquote(raw)).strip(" \t\r\n.,;")
    try:
        number = phonenumbers.parse(candidate, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(number):
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    digits = str(number.national_number)
    if re.fullmatch(r"(\d)\1{6,}", digits):
        return None
    return (
        phonenumbers.format_number(number, PhoneNumberFormat.E164),
        phonenumbers.region_code_for_number(number) or "",
        _phone_type_label(number),
    )


def _add_phone(
    results: dict[tuple[str, str], Evidence],
    raw: str,
    page_url: str,
    source_type: str,
    region: str,
) -> None:
    normalized = _normalize_phone(raw, region)
    if not normalized:
        return
    value, detected_region, phone_type = normalized
    confidence = {
        "tel": 0.99,
        "json_ld": 0.96,
        "visible_text": 0.80,
    }.get(source_type, 0.72)
    if any(word in page_url.lower() for word in ("contact", "support")):
        confidence += 0.01
    evidence = Evidence(
        value=value,
        kind="phone",
        source_url=page_url,
        source_type=source_type,
        confidence=min(confidence, 0.99),
        raw_value=compact_text(raw, 100),
        meta={"region": detected_region, "type": phone_type},
    )
    key = evidence.key()
    if key not in results or evidence.confidence > results[key].confidence:
        results[key] = evidence


def extract_phones(
    soup: BeautifulSoup, page_url: str, default_region: str
) -> list[Evidence]:
    results: dict[tuple[str, str], Evidence] = {}
    region = _phone_region(soup, default_region)
    for anchor in soup.select('a[href^="tel:" i]'):
        _add_phone(results, anchor.get("href", "")[4:], page_url, "tel", region)
    for obj in _json_ld_objects(soup):
        for key in ("telephone", "phone", "mobile", "faxNumber"):
            value = obj.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str):
                    _add_phone(results, item, page_url, "json_ld", region)
    visible = soup.get_text(" ", strip=True)
    for match in PHONE_CANDIDATE_RE.finditer(visible):
        _add_phone(
            results, match.group(0), page_url, "visible_text", region
        )
    return list(results.values())


def _social_platform(url: str) -> str | None:
    host = hostname(url)
    for platform, hosts in SOCIAL_HOSTS.items():
        if any(host == item or host.endswith("." + item) for item in hosts):
            return platform
    return None


def _canonical_social(url: str, platform: str) -> str | None:
    parts = urlsplit(url)
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    lower = path.lower()
    if not path or path == "/" or any(part in lower for part in SOCIAL_REJECT_PARTS):
        return None
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if not segments:
        return None
    first = segments[0]
    first_lower = first.lower()
    if platform == "linkedin":
        if first_lower not in {"company", "in", "school", "showcase"} or len(segments) < 2:
            return None
        path = "/" + "/".join(segments[:2])
    elif platform == "youtube":
        if first.startswith("@"):
            path = "/" + first
        elif first_lower in {"channel", "c", "user"} and len(segments) >= 2:
            path = "/" + "/".join(segments[:2])
        else:
            return None
    elif platform == "facebook":
        if first_lower in {
            "events",
            "groups",
            "marketplace",
            "photo",
            "photos",
            "posts",
            "reel",
            "reels",
            "videos",
            "watch",
        }:
            return None
        if first_lower == "pages":
            if len(segments) < 3:
                return None
            path = "/" + "/".join(segments[:3])
        elif first_lower == "profile.php":
            return None
        else:
            path = "/" + first
    elif platform == "instagram":
        if first_lower in {"about", "accounts", "developer", "explore", "p", "reel", "reels", "search", "stories"}:
            return None
        path = "/" + first
    elif platform == "twitter":
        if first_lower in {"about", "accounts", "developer", "explore", "hashtag", "home", "i", "intent", "legal", "search", "share"}:
            return None
        path = "/" + first
    elif platform == "tiktok":
        if not first.startswith("@"):
            return None
        path = "/" + first
    elif platform == "threads":
        if first_lower in {"about", "accounts", "discover", "explore", "legal", "privacy", "search", "terms"}:
            return None
        path = "/" + first
    elif platform == "pinterest":
        if first_lower in {"ideas", "pin", "search"}:
            return None
        path = "/" + first
    host = {
        "twitter": "x.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "linkedin": "linkedin.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
        "pinterest": "pinterest.com",
        "threads": "threads.net",
    }.get(platform, parts.netloc.lower())
    return urlunsplit(("https", host, path, "", ""))


def extract_socials(soup: BeautifulSoup, page_url: str) -> list[Evidence]:
    results: dict[tuple[str, str], Evidence] = {}
    for anchor in soup.select("a[href]"):
        url = canonical_link(page_url, anchor.get("href", ""))
        if not url:
            continue
        platform = _social_platform(url)
        if not platform:
            continue
        value = _canonical_social(url, platform)
        if not value:
            continue
        evidence = Evidence(
            value=value,
            kind="social",
            source_url=page_url,
            source_type="anchor",
            confidence=0.94,
            category=platform,
            raw_value=compact_text(anchor.get_text(" ", strip=True), 100),
        )
        results[evidence.key()] = evidence
    for obj in _json_ld_objects(soup):
        same_as = obj.get("sameAs")
        values = same_as if isinstance(same_as, list) else [same_as]
        for raw in values:
            if not isinstance(raw, str):
                continue
            platform = _social_platform(raw)
            if not platform:
                continue
            value = _canonical_social(raw, platform)
            if not value:
                continue
            evidence = Evidence(
                value=value,
                kind="social",
                source_url=page_url,
                source_type="json_ld",
                confidence=0.98,
                category=platform,
                raw_value=raw,
            )
            results[evidence.key()] = evidence
    return list(results.values())


def extract_address(soup: BeautifulSoup) -> str:
    for obj in _json_ld_objects(soup):
        address = obj.get("address")
        if isinstance(address, str) and len(address.strip()) >= 12:
            return compact_text(address, 300)
        if isinstance(address, dict):
            parts = [
                address.get("streetAddress"),
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("postalCode"),
                address.get("addressCountry"),
            ]
            value = ", ".join(str(part).strip() for part in parts if part)
            if len(value) >= 12:
                return compact_text(value, 300)
    tag = soup.find("address")
    if tag:
        value = compact_text(tag.get_text(" ", strip=True), 300)
        if len(value) >= 12:
            return value
    for selector in (
        '[itemprop="address"]',
        '[class*="address" i]',
        '[id*="address" i]',
    ):
        element = soup.select_one(selector)
        if element:
            value = compact_text(element.get_text(" ", strip=True), 300)
            if len(value) >= 12 and any(char.isdigit() for char in value):
                return value
    return ""


def extract_description(soup: BeautifulSoup) -> str:
    selectors = (
        'meta[name="description" i]',
        'meta[property="og:description" i]',
        'meta[name="twitter:description" i]',
    )
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag:
            value = compact_text(tag.get("content", ""), 600)
            if 50 <= len(value) <= 600:
                return value
    for obj in _json_ld_objects(soup):
        value = obj.get("description")
        if isinstance(value, str):
            value = compact_text(value, 600)
            if len(value) >= 50:
                return value
    candidates: list[tuple[int, str]] = []
    for paragraph in soup.select("main p, article p, [class*='about' i] p, p"):
        value = compact_text(paragraph.get_text(" ", strip=True), 600)
        if not 100 <= len(value) <= 600:
            continue
        lower = value.lower()
        if any(
            bad in lower
            for bad in ("cookie", "all rights reserved", "privacy policy")
        ):
            continue
        score = len(value)
        if any(
            good in lower
            for good in (
                "we are",
                "founded",
                "established",
                "specialize",
                "our company",
                "our mission",
            )
        ):
            score += 300
        candidates.append((score, value))
    return max(candidates, default=(0, ""))[1]


def discover_links(soup: BeautifulSoup, page_url: str) -> list[tuple[str, str]]:
    links: dict[str, str] = {}
    for anchor in soup.select("a[href]"):
        url = canonical_link(page_url, anchor.get("href", ""))
        if not url or not same_registered_domain(page_url, url):
            continue
        text = compact_text(anchor.get_text(" ", strip=True), 100)
        links.setdefault(url, text)
    return list(links.items())


def extract_page(
    raw_html: str,
    page_url: str,
    site_domain: str,
    default_phone_region: str,
    selected_kinds: set[str] | None = None,
) -> PageData:
    wanted = selected_kinds or {
        "email",
        "phone",
        "social",
        "address",
        "description",
    }
    soup = BeautifulSoup(raw_html, "lxml")
    for element in soup(["script", "style", "noscript", "template", "svg"]):
        if element.name != "script" or element.get("type") != "application/ld+json":
            element.decompose()
    title = compact_text(soup.title.get_text(" ", strip=True), 200) if soup.title else ""
    contacts: list[Evidence] = []
    if "email" in wanted:
        contacts.extend(extract_emails(soup, raw_html, page_url, site_domain))
    if "phone" in wanted:
        contacts.extend(extract_phones(soup, page_url, default_phone_region))
    if "social" in wanted:
        contacts.extend(extract_socials(soup, page_url))
    return PageData(
        url=page_url,
        title=title,
        contacts=contacts,
        address=extract_address(soup) if "address" in wanted else "",
        description=extract_description(soup) if "description" in wanted else "",
        links=discover_links(soup, page_url),
    )


def rank_subpages(
    links: list[tuple[str, str]],
    base_url: str,
    limit: int,
    needed_kinds: set[str] | None = None,
) -> list[str]:
    scores: dict[str, int] = {}
    wanted = needed_kinds or {
        "email",
        "phone",
        "social",
        "address",
        "description",
    }
    keywords: dict[str, int] = {
        "contact": 70,
        "contact-us": 70,
        "about": 55,
        "about-us": 60,
        "support": 45,
        "help": 35,
        "team": 35,
        "company": 35,
        "location": 35,
        "locations": 35,
        "leadership": 30,
        "staff": 25,
        "people": 20,
        "privacy": 12,
        "legal": 12,
    }
    if wanted & {"email", "phone", "address"}:
        keywords.update(
            {
                "contact": 220,
                "contact-us": 220,
                "support": 90,
                "help": 65,
                "location": 110,
                "locations": 110,
                "office": 75,
            }
        )
    if "description" in wanted:
        keywords.update(
            {
                "about": 190,
                "about-us": 200,
                "company": 130,
                "our-story": 120,
                "who-we-are": 120,
                "mission": 80,
                "team": 65,
                "leadership": 55,
            }
        )
    if "social" in wanted:
        keywords.update(
            {
                "contact": max(keywords.get("contact", 0), 120),
                "about": max(keywords.get("about", 0), 90),
                "team": max(keywords.get("team", 0), 75),
                "community": 70,
                "connect": 100,
            }
        )
    negative = {
        "blog",
        "cart",
        "category",
        "checkout",
        "login",
        "news",
        "product",
        "shop",
        "signup",
        "tag",
    }
    for url, text in links:
        parts = urlsplit(url)
        haystack = f"{parts.path} {text}".lower()
        score = max(
            (weight for word, weight in keywords.items() if word in haystack),
            default=0,
        )
        score -= sum(80 for word in negative if word in haystack)
        depth = len([part for part in parts.path.split("/") if part])
        score -= depth * 3
        if score > 0:
            scores[url] = max(scores.get(url, 0), score)

    root = base_url.rstrip("/")
    common: list[tuple[str, int]] = []
    if wanted & {"email", "phone", "social", "address"}:
        common.extend(
            [
                ("/contact", 180),
                ("/contact-us", 179),
                ("/support", 70),
            ]
        )
    if wanted & {"description", "social"}:
        common.extend(
            [
                ("/about", 150),
                ("/about-us", 149),
                ("/company", 100),
                ("/team", 80),
            ]
        )
    if not common:
        common = [("/contact", 90), ("/about", 60)]
    for path, score in common:
        url = root + path
        scores[url] = max(scores.get(url, 0), score)
    return [
        url
        for url, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[
            :limit
        ]
    ]


def merge_contacts(pages: list[PageData]) -> list[Evidence]:
    merged: dict[tuple[str, str], Evidence] = {}
    for page in pages:
        for evidence in page.contacts:
            key = evidence.key()
            current = merged.get(key)
            if current is None or evidence.confidence > current.confidence:
                merged[key] = evidence
    return sorted(
        merged.values(),
        key=lambda item: (
            item.kind,
            item.category,
            -item.confidence,
            item.value,
        ),
    )
