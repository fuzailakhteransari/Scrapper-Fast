from __future__ import annotations

import html
import ipaddress
import re
import socket
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import tldextract


TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}
TRACKING_PREFIXES = ("utm_",)
BAD_SCHEMES = {"mailto", "tel", "javascript", "data", "file", "ftp"}


def normalize_url(raw: str) -> str:
    value = html.unescape(str(raw or "")).strip().strip("\"'")
    if not value:
        raise ValueError("empty website value")
    if value.startswith("//"):
        value = "https:" + value
    explicit_scheme = re.match(r"^([a-z][a-z0-9+.-]*):", value, re.IGNORECASE)
    if explicit_scheme and explicit_scheme.group(1).lower() not in {"http", "https"}:
        raise ValueError(
            f"unsupported URL scheme: {explicit_scheme.group(1).lower()}"
        )
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme: {parts.scheme}")
    host = (parts.hostname or "").strip(".").lower()
    if not host:
        raise ValueError("URL has no hostname")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid international hostname") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in labels
        ):
            raise ValueError("invalid hostname")
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in TRACKING_KEYS
            and not key.lower().startswith(TRACKING_PREFIXES)
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


@lru_cache(maxsize=10_000)
def registered_domain(host: str) -> str:
    extracted = tldextract.extract(host)
    return extracted.top_domain_under_public_suffix or host


def domain_key(url: str) -> str:
    return registered_domain(hostname(url))


def site_key(url: str) -> str:
    """Key one website host while treating www and bare host as equivalent."""
    return hostname(url)


def same_registered_domain(left: str, right: str) -> bool:
    return domain_key(left) == domain_key(right)


def canonical_link(base_url: str, href: str) -> str | None:
    value = html.unescape((href or "").strip())
    if not value or value.startswith(("#", "{", "[")):
        return None
    scheme = urlsplit(value).scheme.lower()
    if scheme in BAD_SCHEMES:
        return None
    try:
        return normalize_url(urljoin(base_url, value))
    except (ValueError, TypeError):
        return None


def url_variants(url: str) -> list[str]:
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    hosts = [host]
    if host.startswith("www."):
        hosts.append(host[4:])
    else:
        hosts.append("www." + host)
    schemes = [parts.scheme, "https", "http"]
    variants: list[str] = []
    for scheme in schemes:
        for candidate_host in hosts:
            candidate = urlunsplit(
                (scheme, candidate_host, parts.path or "/", parts.query, "")
            )
            if candidate not in variants:
                variants.append(candidate)
    return variants


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def validate_public_host(url: str) -> None:
    host = urlsplit(url).hostname or ""
    if host in {"localhost"} or host.endswith(".local"):
        raise ValueError("local hostnames are not allowed")
    try:
        ipaddress.ip_address(host)
        if not is_public_ip(host):
            raise ValueError("private or reserved IP addresses are not allowed")
        return
    except ValueError as exc:
        if "not allowed" in str(exc):
            raise
    loop = __import__("asyncio").get_running_loop()
    try:
        records = await loop.getaddrinfo(
            host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ValueError(f"DNS lookup failed for {host}") from exc
    addresses = {record[4][0] for record in records}
    if not addresses or any(not is_public_ip(address) for address in addresses):
        raise ValueError("hostname resolves to a private or reserved address")


def compact_text(value: str, limit: int = 500) -> str:
    cleaned = re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    return cleaned[:limit]
