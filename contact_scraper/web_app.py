from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from .accuracy import CleanExportOptions, clean_field_to_kind
from .config import Settings
from .exporters import export_csvs
from .logging_utils import configure_logging
from .models import SiteResult
from .runner import RunControl, run_workers
from .storage import Storage
from .website_finder import (
    WebsiteFinderSettings,
    resolve_company_csv,
    select_company_column,
    select_location_column,
)

LOGGER = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {".csv"}
WEBSITE_KIND = "website"
CONTACT_KINDS = {"email", "phone", "social", "address", "description"}
ALL_KINDS = CONTACT_KINDS | {WEBSITE_KIND}
PASTE_LIMIT = 100_000
CRAWL_MODES = {"fast", "full"}
BRIGHT_SETTINGS_FILE = "brightdata_settings.json"
MODE_KINDS = {
    "all": CONTACT_KINDS,
    "email": {"email"},
    "phone": {"phone"},
    "social": {"social"},
    "details": {"address", "description"},
}
JobRunner = Callable[..., Awaitable[dict[str, int]]]


def _ui_build_signature(package_dir: Path) -> str:
    digest = hashlib.sha256()
    relative_paths = (
        "web_app.py",
        "templates/scraper.html",
        "static/scraper.css",
        "static/scraper.js",
    )
    for relative_path in relative_paths:
        digest.update(f"contact_scraper/{relative_path}".encode("utf-8"))
        digest.update((package_dir / relative_path).read_bytes())
    return digest.hexdigest()[:16]


def _bright_fallback_ready(settings: Settings) -> bool:
    return bool(
        (settings.use_web_unlocker and settings.web_unlocker_ready)
        or (settings.use_browser and settings.browser_ready)
    )


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    value = int(seconds)
    if value < 60:
        return f"{value}s"
    minutes, secs = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _mode_display(selected_kinds: set[str]) -> str:
    labels = []
    if WEBSITE_KIND in selected_kinds:
        labels.append("Websites")
    if "email" in selected_kinds:
        labels.append("Emails")
    if "phone" in selected_kinds:
        labels.append("Phones")
    if "social" in selected_kinds:
        labels.append("Socials")
    if "address" in selected_kinds or "description" in selected_kinds:
        labels.append("Details")
    return ", ".join(labels) if labels else "Custom"


@dataclass(slots=True)
class UploadRecord:
    id: str
    path: Path
    original_name: str
    row_count: int
    valid_count: int
    unique_count: int
    invalid_count: int
    website_column: str
    headers: list[str]
    input_type: str = "website"
    company_column: str = ""
    location_column: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "originalName": self.original_name,
            "rowCount": self.row_count,
            "validCount": self.valid_count,
            "uniqueCount": self.unique_count,
            "invalidCount": self.invalid_count,
            "websiteColumn": self.website_column,
            "inputType": self.input_type,
            "companyColumn": self.company_column,
            "locationColumn": self.location_column,
            "headers": self.headers,
        }


@dataclass(slots=True)
class JobRecord:
    id: str
    upload: UploadRecord
    output_dir: Path
    database_path: Path
    mode: str
    selected_kinds: set[str]
    clean_options: CleanExportOptions
    crawl_mode: str
    max_pages: int
    workers: int
    status: str = "queued"
    message: str = "Queued"
    total: int = 0
    completed: int = 0
    success: int = 0
    failed: int = 0
    extracted: int = 0
    emails: int = 0
    phones: int = 0
    socials: int = 0
    websites: int = 0
    lookup_failed: int = 0
    active_domains: list[str] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    paused_at: float | None = None
    paused_seconds: float = 0.0
    error: str = ""
    logs: list[str] = field(default_factory=list)
    downloads: list[dict[str, str]] = field(default_factory=list)
    control: RunControl = field(default_factory=RunControl)
    lock: threading.RLock = field(default_factory=threading.RLock)
    stage: str = "initial"
    recovered: int = 0
    scrape_input_path: Path | None = None


    def add_log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{stamp}] {message}")
            if len(self.logs) > 200:
                self.logs = self.logs[-200:]

    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at or time.time()
        paused = self.paused_seconds
        if self.paused_at is not None:
            paused += max(0.0, time.time() - self.paused_at)
        return max(0.0, end - self.started_at - paused)

    def eta_seconds(self) -> float | None:
        if self.completed <= 0 or self.total <= self.completed:
            return 0.0 if self.total and self.completed >= self.total else None
        return (self.elapsed_seconds() / self.completed) * (
            self.total - self.completed
        )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            display_total = self.total
            display_completed = self.completed
            display_failed = self.failed
            if self.upload.input_type == "company" and (
                self.lookup_failed or self.total
            ):
                display_total = self.total + self.lookup_failed
                display_completed = min(
                    display_total, self.completed + self.lookup_failed
                )
                display_failed = self.failed + self.lookup_failed
            progress = (display_completed / display_total * 100) if display_total else 0
            current = self.active_domains[0] if self.active_domains else ""
            return {
                "id": self.id,
                "status": self.status,
                "message": self.message,
                "mode": self.mode,
                "crawlMode": self.crawl_mode,
                "maxPages": self.max_pages,
                "total": display_total,
                "completed": display_completed,
                "success": self.success,
                "failed": self.failed,
                "failedTotal": display_failed,
                "extracted": self.extracted,
                "emails": self.emails,
                "phones": self.phones,
                "socials": self.socials,
                "websites": self.websites,
                "lookupFailed": self.lookup_failed,
                "progress": round(progress, 1),
                "current": current,
                "activeDomains": list(self.active_domains),
                "elapsed": _format_duration(self.elapsed_seconds()),
                "eta": _format_duration(self.eta_seconds()),
                "error": self.error,
                "logs": list(self.logs[-80:]),
                "downloads": list(self.downloads),
                "stage": self.stage,
                "recovered": self.recovered,
            }


def _inspect_csv(path: Path, requested_column: str | None = None) -> dict[str, Any]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise ValueError("CSV has no header row")
                try:
                    website_column = Storage._select_website_column(
                        reader.fieldnames, requested_column
                    )
                    input_type = "website"
                    company_column = ""
                    location_column = ""
                except ValueError:
                    website_column = ""
                    input_type = "company"
                    company_column = select_company_column(reader.fieldnames)
                    location_column = select_location_column(reader.fieldnames) or ""
                rows = 0
                valid = 0
                invalid = 0
                unique: set[str] = set()
                from .utils import normalize_url, site_key

                for row in reader:
                    rows += 1
                    if input_type == "website":
                        try:
                            key = site_key(
                                normalize_url(str(row.get(website_column, "") or ""))
                            )
                            valid += 1
                            unique.add(key)
                        except (ValueError, TypeError):
                            invalid += 1
                    else:
                        company = str(row.get(company_column, "") or "").strip()
                        location = (
                            str(row.get(location_column, "") or "").strip()
                            if location_column
                            else ""
                        )
                        if company:
                            valid += 1
                            unique.add(f"{company.casefold()}|{location.casefold()}")
                        else:
                            invalid += 1
                return {
                    "row_count": rows,
                    "valid_count": valid,
                    "unique_count": len(unique),
                    "invalid_count": invalid,
                    "website_column": website_column,
                    "input_type": input_type,
                    "company_column": company_column,
                    "location_column": location_column,
                    "headers": list(reader.fieldnames),
                }
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not read CSV: {last_error or 'unknown encoding'}")


def _parse_pasted_website_values(text: str) -> list[str]:
    websites: list[str] = []
    header_names = {"website", "website url", "website_url", "url", "domain", "site"}
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        values = next(csv.reader([cleaned], skipinitialspace=True))
        tokens: list[str] = []
        for value in values:
            tokens.extend(part.strip() for part in value.split("\t"))
        for token in tokens:
            value = token.strip().strip("\"'")
            if not value or value.casefold() in header_names:
                continue
            websites.append(value)
            if len(websites) > PASTE_LIMIT:
                raise ValueError(
                    f"Paste lists are limited to {PASTE_LIMIT:,} entries."
                )
    return websites


def _parse_pasted_companies_with_header(text: str) -> list[dict[str, str]]:
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    header = next(reader, None)
    if not header:
        return []
    if len(header) == 1 and "\t" in header[0]:
        header = [part.strip() for part in header[0].split("\t")]
    normalized = [str(item or "").strip().casefold() for item in header]
    company_headers = {"company", "company name", "business", "business name", "name"}
    location_headers = {"location", "city", "state", "address", "country"}
    company_index = next(
        (index for index, value in enumerate(normalized) if value in company_headers),
        None,
    )
    if company_index is None:
        return []
    location_index = next(
        (index for index, value in enumerate(normalized) if value in location_headers),
        None,
    )
    companies: list[dict[str, str]] = []
    for row in reader:
        if len(row) == 1 and "\t" in row[0]:
            row = [part.strip() for part in row[0].split("\t")]
        if not any(str(cell or "").strip() for cell in row):
            continue
        company = str(row[company_index] if company_index < len(row) else "").strip()
        location = (
            str(row[location_index] if location_index is not None and location_index < len(row) else "").strip()
        )
        if not company:
            continue
        companies.append({"Company": company, "Location": location})
        if len(companies) > PASTE_LIMIT:
            raise ValueError(
                f"Paste lists are limited to {PASTE_LIMIT:,} entries."
            )
    return companies


def _parse_pasted_websites(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Paste at least one website.")
    if len(text) > 5_000_000:
        raise ValueError("Pasted input is too large.")
    websites = _parse_pasted_website_values(text)
    if not websites:
        raise ValueError("No website values were detected.")
    return websites


def _parse_pasted_companies(raw: str) -> list[dict[str, str]]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Paste at least one company name.")
    if len(text) > 5_000_000:
        raise ValueError("Pasted input is too large.")

    companies = _parse_pasted_companies_with_header(text)
    if companies:
        return companies

    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        cleaned = line.strip().strip("\"'")
        if not cleaned:
            continue
        if "\t" in cleaned:
            parts = [part.strip() for part in cleaned.split("\t")]
            company = parts[0] if parts else ""
            location = parts[1] if len(parts) > 1 else ""
        else:
            company = cleaned
            location = ""
        if not company:
            continue
        rows.append({"Company": company, "Location": location})
        if len(rows) > PASTE_LIMIT:
            raise ValueError(
                f"Paste lists are limited to {PASTE_LIMIT:,} entries."
            )
    if not rows:
        raise ValueError("No company names were detected.")
    return rows


def _write_pasted_csv(
    path: Path,
    input_type: str,
    values: list[str] | list[dict[str, str]],
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        if input_type == "website":
            writer.writerow(["Website"])
            writer.writerows([website] for website in values)
            return
        writer.writerow(["Company", "Location"])
        writer.writerows(
            [
                [row.get("Company", ""), row.get("Location", "")]
                for row in values
            ]
        )


def _resolved_website_counts(path: Path) -> dict[str, int]:
    counts = {"total": 0, "found": 0, "failed": 0}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            counts["total"] += 1
            finder_status = str(row.get("website_finder_status", "")).casefold()
            website_status = str(row.get("website_status", "")).casefold()
            if finder_status == "found" or website_status == "valid":
                counts["found"] += 1
            else:
                counts["failed"] += 1
    return counts


def _write_uploaded_website_report(upload: UploadRecord, output_path: Path) -> dict[str, int]:
    from .utils import normalize_url, site_key

    counts = {"total": 0, "found": 0, "failed": 0}
    with (
        upload.path.open("r", encoding="utf-8-sig", newline="") as source,
        output_path.open("w", encoding="utf-8-sig", newline="") as target,
    ):
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        fieldnames = list(reader.fieldnames)
        for field in ("normalized_website", "domain", "website_status", "notes"):
            if field not in fieldnames:
                fieldnames.append(field)
        writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            counts["total"] += 1
            updated = dict(row)
            try:
                normalized = normalize_url(str(row.get(upload.website_column, "") or ""))
                updated["normalized_website"] = normalized
                updated["domain"] = site_key(normalized)
                updated["website_status"] = "valid"
                updated["notes"] = "Website supplied in input CSV."
                counts["found"] += 1
            except (TypeError, ValueError) as exc:
                updated["website_status"] = "invalid_input"
                updated["notes"] = str(exc)
                counts["failed"] += 1
            writer.writerow(updated)
    return counts


def _clean_report_website(row: dict[str, str]) -> str:
    finder_status = str(row.get("website_finder_status", "") or "").casefold()
    website_status = str(row.get("website_status", "") or "").casefold()
    if finder_status and finder_status != "found":
        return ""
    if website_status and website_status != "valid":
        return ""
    for key in ("Website", "normalized_website", "final_url"):
        value = str(row.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _write_clean_website_report(
    report_path: Path,
    output_path: Path,
    clean_options: CleanExportOptions,
) -> None:
    with (
        report_path.open("r", encoding="utf-8-sig", newline="") as source,
        output_path.open("w", encoding="utf-8-sig", newline="") as target,
    ):
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=clean_options.columns)
        writer.writeheader()
        for row in reader:
            clean_row: dict[str, str] = {}
            notes: list[str] = []
            for field in clean_options.fields or ():
                column = {
                    "website": "Website",
                    "email": "Email",
                    "phone": "Phone",
                    "facebook": "Facebook",
                    "instagram": "Instagram",
                    "linkedin": "LinkedIn",
                    "twitter_x": "X/Twitter",
                    "youtube": "YouTube",
                    "tiktok": "TikTok",
                    "pinterest": "Pinterest",
                    "threads": "Threads",
                    "address": "Address",
                    "description": "Description",
                }[field]
                clean_row[column] = (
                    _clean_report_website(row) if field == "website" else ""
                )
                if not clean_row[column]:
                    notes.append(f"{column} missing")
            for column in clean_options.columns:
                clean_row.setdefault(column, "")
            if clean_options.include_evidence:
                clean_row["Review Note"] = "; ".join(notes)
            writer.writerow(clean_row)


def _load_bright_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Could not read local Bright Data settings")
        return {}
    return data if isinstance(data, dict) else {}


def _save_bright_settings(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _bright_settings_snapshot(
    stored: dict[str, Any], defaults: Settings
) -> dict[str, Any]:
    api_key = str(stored.get("api_key", "") or defaults.brightdata_api_key)
    password = str(
        stored.get("browser_password", "")
        or defaults.brightdata_browser_password
    )
    return {
        "unlockerZone": stored.get(
            "unlocker_zone", defaults.brightdata_unlocker_zone
        ),
        "browserUsername": stored.get(
            "browser_username", defaults.brightdata_browser_username
        ),
        "proxyHost": stored.get(
            "proxy_host", defaults.brightdata_browser_host
        ),
        "proxyPort": int(
            stored.get("proxy_port", defaults.brightdata_browser_port)
        ),
        "browserConcurrency": int(
            stored.get("browser_concurrency", defaults.browser_concurrency)
        ),
        "useWebUnlocker": bool(
            stored.get("use_web_unlocker", defaults.use_web_unlocker)
        ),
        "useBrowser": bool(stored.get("use_browser", defaults.use_browser)),
        "searxngBaseUrl": str(
            stored.get("searxng_base_url", os.getenv("SEARXNG_BASE_URL", ""))
        ).strip(),
        "apiKeyConfigured": bool(api_key),
        "passwordConfigured": bool(password),
    }


def _apply_bright_settings(
    settings: Settings, stored: dict[str, Any]
) -> Settings:
    return replace(
        settings,
        brightdata_unlocker_zone=str(
            stored.get("unlocker_zone", settings.brightdata_unlocker_zone)
        ).strip(),
        brightdata_api_key=str(
            stored.get("api_key", settings.brightdata_api_key)
        ).strip(),
        brightdata_browser_username=str(
            stored.get(
                "browser_username", settings.brightdata_browser_username
            )
        ).strip(),
        brightdata_browser_password=str(
            stored.get(
                "browser_password", settings.brightdata_browser_password
            )
        ).strip(),
        brightdata_browser_host=str(
            stored.get("proxy_host", settings.brightdata_browser_host)
        ).strip(),
        brightdata_browser_port=int(
            stored.get("proxy_port", settings.brightdata_browser_port)
        ),
        browser_concurrency=int(
            stored.get("browser_concurrency", settings.browser_concurrency)
        ),
        use_web_unlocker=bool(
            stored.get("use_web_unlocker", settings.use_web_unlocker)
        ),
        use_browser=bool(stored.get("use_browser", settings.use_browser)),
    )


def _apply_finder_settings(
    settings: WebsiteFinderSettings,
    stored: dict[str, Any],
) -> WebsiteFinderSettings:
    searxng_base_url = str(
        stored.get("searxng_base_url", settings.searxng_base_url)
    ).strip().rstrip("/")
    settings.searxng_base_url = searxng_base_url
    return settings


def _validated_bright_settings(
    payload: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    zone = str(payload.get("unlockerZone", "")).strip()
    username = str(payload.get("browserUsername", "")).strip()
    host = str(payload.get("proxyHost", "brd.superproxy.io")).strip()
    searxng_base_url = str(payload.get("searxngBaseUrl", "")).strip().rstrip("/")
    if zone and not re.fullmatch(r"[A-Za-z0-9_-]+", zone):
        raise ValueError("Zone name contains unsupported characters.")
    if username and len(username) > 300:
        raise ValueError("Username is too long.")
    if (
        not host
        or "://" in host
        or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
    ):
        raise ValueError("Enter a valid proxy host without http:// or https://.")
    if searxng_base_url and not re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?", searxng_base_url):
        raise ValueError("Enter a valid SearXNG base URL such as https://search.example.com.")
    try:
        port = int(payload.get("proxyPort", 9222))
        browser_concurrency = int(payload.get("browserConcurrency", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("Proxy port and browser concurrency must be numbers.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Proxy port must be between 1 and 65535.")
    if not 1 <= browser_concurrency <= 20:
        raise ValueError("Browser concurrency must be between 1 and 20.")

    updated = dict(current)
    updated.update(
        {
            "unlocker_zone": zone,
            "browser_username": username,
            "proxy_host": host,
            "proxy_port": port,
            "browser_concurrency": browser_concurrency,
            "use_web_unlocker": bool(payload.get("useWebUnlocker", True)),
            "use_browser": bool(payload.get("useBrowser", True)),
            "searxng_base_url": searxng_base_url,
        }
    )
    api_key = str(payload.get("apiKey", "")).strip()
    password = str(payload.get("browserPassword", "")).strip()
    if api_key:
        updated["api_key"] = api_key
    elif payload.get("clearApiKey"):
        updated["api_key"] = ""
    if password:
        updated["browser_password"] = password
    elif payload.get("clearPassword"):
        updated["browser_password"] = ""
    return updated


def create_app(
    base_dir: Path | None = None,
    *,
    job_runner: JobRunner = run_workers,
) -> Flask:
    package_dir = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(package_dir / "templates"),
        static_folder=str(package_dir / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    data_dir = (base_dir or Path.cwd() / "ui_data").resolve()
    uploads_dir = data_dir / "uploads"
    runs_dir = data_dir / "runs"
    bright_settings_path = data_dir / BRIGHT_SETTINGS_FILE
    uploads_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(data_dir)
    ui_build = _ui_build_signature(package_dir)

    uploads: dict[str, UploadRecord] = {}
    jobs: dict[str, JobRecord] = {}
    registry_lock = threading.RLock()

    def job_event(job: JobRecord, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        with job.lock:
            counts = payload.get("counts") or {}
            job.total = int(counts.get("total", job.total))
            job.completed = int(counts.get("completed", 0)) + int(
                counts.get("failed", 0)
            )
            job.success = int(counts.get("completed", 0))
            job.failed = int(counts.get("failed", 0))
            if "active" in payload:
                job.active_domains = list(payload["active"])
            domain = str(payload.get("domain", "") or "")
            if event == "started":
                position = min(job.total, job.completed + len(job.active_domains))
                job.message = f"Processing website {position}/{job.total}"
                job.add_log(f"Started {domain}")
            elif event == "completed":
                result = payload.get("result")
                if isinstance(result, SiteResult):
                    emails = sum(
                        item.kind == "email" for item in result.contacts
                    )
                    phones = sum(
                        item.kind == "phone" for item in result.contacts
                    )
                    socials = sum(
                        item.kind == "social" for item in result.contacts
                    )
                    job.emails += emails
                    job.phones += phones
                    job.socials += socials
                    job.extracted += emails + phones + socials
                    if result.address:
                        job.extracted += 1
                    if result.description:
                        job.extracted += 1
                    if job.stage == "retry":
                        job.recovered += 1
                        job.add_log(
                            f"Recovered {domain} using Bright Data: {len(result.contacts)} contacts"
                        )
                    else:
                        job.add_log(
                            f"Finished {domain}: {len(result.contacts)} contacts"
                        )
            elif event == "failed":
                if job.stage == "retry":
                    job.add_log(f"Still failed {domain} under Bright Data: {payload.get('error', 'error')}")
                else:
                    job.add_log(f"Failed {domain}: {payload.get('error', 'error')}")

    def prepare_downloads(
        job: JobRecord,
        paths: tuple[Path, Path, Path, Path],
        extra: list[tuple[str, Path]] | None = None,
    ) -> None:
        extra = extra or []
        labeled_paths = [
            ("Main Results CSV", paths[0]),
            ("Clean Leads CSV", paths[3]),
            ("Contacts CSV", paths[1]),
            ("Review CSV", paths[2]),
            *extra,
        ]
        with job.lock:
            job.downloads = [
                {
                    "name": label,
                    "fileName": path.name,
                    "url": f"/api/job/{job.id}/download/{path.name}",
                }
                for label, path in labeled_paths
                if path.exists()
            ]

    def execute_job(job: JobRecord) -> None:
        storage = Storage(job.database_path)
        try:
            storage.reset_running()
            settings = Settings.from_env(package_dir.parent / ".env")
            settings = _apply_bright_settings(
                settings, _load_bright_settings(bright_settings_path)
            )
            website_report_path: Path | None = None

            if job.stage == "initial":
                if job.upload.input_type == "company":
                    with job.lock:
                        job.status = "running"
                        job.message = "Finding official websites from company names"
                        job.started_at = time.time()
                    resolved_path = (
                        job.output_dir
                        / f"{Path(job.upload.original_name).stem}_resolved_websites.csv"
                    )
                    job.add_log(
                        f"Finding official websites for {job.upload.valid_count} companies."
                    )
                    finder_settings = _apply_finder_settings(
                        WebsiteFinderSettings.from_env(),
                        _load_bright_settings(bright_settings_path),
                    )
                    finder_settings.concurrency = min(20, max(1, job.workers))
                    asyncio.run(
                        resolve_company_csv(
                            job.upload.path,
                            resolved_path,
                            company_column=job.upload.company_column,
                            location_column=job.upload.location_column or None,
                            settings=finder_settings,
                        )
                    )
                    job.scrape_input_path = resolved_path
                    website_report_path = resolved_path
                    website_counts = _resolved_website_counts(resolved_path)
                    with job.lock:
                        job.websites = website_counts["found"]
                        job.lookup_failed = website_counts["failed"]
                        if WEBSITE_KIND in job.selected_kinds:
                            job.extracted += website_counts["found"]
                    if website_counts["failed"]:
                        job.add_log(
                            f"{website_counts['failed']} companies could not be matched to a verified website."
                        )
                    job.add_log("Website finder finished; starting contact scraping.")
                else:
                    job.scrape_input_path = job.upload.path
                    if WEBSITE_KIND in job.selected_kinds:
                        website_report_path = (
                            job.output_dir
                            / f"{Path(job.upload.original_name).stem}_websites.csv"
                        )
                        website_counts = _write_uploaded_website_report(
                            job.upload, website_report_path
                        )
                        with job.lock:
                            job.websites = website_counts["found"]
                            job.extracted += website_counts["found"]
                        job.add_log(
                            f"Website report created: {website_counts['found']} valid websites."
                        )

                contact_kinds = job.selected_kinds - {WEBSITE_KIND}
                if not contact_kinds:
                    counts = (
                        _resolved_website_counts(website_report_path)
                        if website_report_path
                        else {"total": job.upload.row_count, "found": 0, "failed": job.upload.row_count}
                    )
                    clean_path: Path | None = None
                    if website_report_path:
                        clean_path = (
                            job.output_dir
                            / f"{Path(job.upload.original_name).stem}_clean.csv"
                        )
                        _write_clean_website_report(
                            website_report_path, clean_path, job.clean_options
                        )
                    with job.lock:
                        job.total = counts["total"]
                        job.completed = counts["total"]
                        job.success = counts["found"]
                        job.failed = counts["failed"]
                        job.status = "completed"
                        job.message = "Website finding completed."
                        job.started_at = job.started_at or time.time()
                        job.ended_at = time.time()
                        job.websites = counts["found"]
                        job.extracted = counts["found"]
                        if website_report_path:
                            downloads = [
                                {
                                    "name": (
                                        "Resolved Websites CSV"
                                        if job.upload.input_type == "company"
                                        else "Website Validation CSV"
                                    ),
                                    "fileName": website_report_path.name,
                                    "url": f"/api/job/{job.id}/download/{website_report_path.name}",
                                }
                            ]
                            if clean_path and clean_path.exists():
                                downloads.append(
                                    {
                                        "name": "Clean Leads CSV",
                                        "fileName": clean_path.name,
                                        "url": f"/api/job/{job.id}/download/{clean_path.name}",
                                    }
                                )
                            job.downloads = downloads
                    job.add_log("Website-only run completed.")
                    return

                storage.import_csv(job.scrape_input_path)
                storage.reset_all_tasks()
            elif job.scrape_input_path is None:
                job.scrape_input_path = job.upload.path
            website_report_download = (
                [
                    (
                        "Resolved Websites CSV"
                        if job.upload.input_type == "company"
                        else "Website Validation CSV",
                        website_report_path,
                    )
                ]
                if job.stage == "initial" and website_report_path
                else []
            )
            
            counts = storage.counts()
            with job.lock:
                job.total = counts.get("total", 0)
                job.status = "running"
                job.message = "Starting scraper" if job.stage == "initial" else "Starting Bright Data retry scraper"
                if job.stage == "initial":
                    job.started_at = time.time()
            
            if job.stage == "initial":
                job.add_log(
                    f"Run started: {job.total} unique websites, {job.workers} "
                    f"workers, {job.crawl_mode} mode, {job.max_pages} pages max"
                )
            else:
                job.add_log(
                    f"Bright Data retry started: retrying {counts.get('pending', 0)} failed websites"
                )

            settings.concurrency = job.workers
            settings.max_pages = job.max_pages
            if job.clean_options.phone_region != "AUTO":
                settings.default_phone_region = job.clean_options.phone_region

            # Configure Bright Data based on stage
            if job.stage == "initial":
                settings.use_web_unlocker = False
                settings.use_browser = False
                job.add_log("Bright Data fallbacks are disabled for the initial run (direct connections only).")
            else:
                enabled = []
                if settings.use_web_unlocker and settings.web_unlocker_ready:
                    enabled.append("Web Unlocker")
                if settings.use_browser and settings.browser_ready:
                    enabled.append("Browser API")
                if not enabled:
                    raise RuntimeError(
                        "Bright Data retry was requested, but no enabled fallback "
                        "has complete credentials."
                    )
                job.add_log(
                    f"Bright Data retry enabled: {', '.join(enabled)}."
                )

            asyncio.run(
                job_runner(
                    storage,
                    settings,
                    control=job.control,
                    on_event=lambda payload: job_event(job, payload),
                    selected_kinds=job.selected_kinds - {WEBSITE_KIND},
                    crawl_mode=job.crawl_mode,
                    clean_options=job.clean_options,
                )
            )
            paths = export_csvs(
                storage,
                str((job.scrape_input_path or job.upload.path).resolve()),
                job.output_dir,
                job.upload.original_name,
                selected_kinds=job.selected_kinds - {WEBSITE_KIND},
                settings=settings,
                clean_options=job.clean_options,
            )
            prepare_downloads(job, paths, website_report_download)
            with job.lock:
                job.active_domains = []
                if job.control.stop_requested.is_set():
                    job.status = "stopped"
                    job.message = "Stopped safely. Partial results are ready."
                    job.ended_at = time.time()
                    job.add_log("Run stopped safely; partial output created.")
                else:
                    if job.stage == "initial" and job.failed > 0:
                        retry_settings = _apply_bright_settings(
                            Settings.from_env(package_dir.parent / ".env"),
                            _load_bright_settings(bright_settings_path),
                        )
                        if _bright_fallback_ready(retry_settings):
                            job.status = "completed_initial"
                            job.message = f"Initial scraping done. {job.failed} failed. Retry with Bright Data?"
                            job.paused_at = time.time()
                            job.add_log(f"Initial run completed with {job.failed} failed websites. Waiting for user's decision on Bright Data retry.")
                        else:
                            job.status = "completed"
                            job.message = f"Scraping completed. {job.failed} failed."
                            job.ended_at = time.time()
                            job.add_log(f"Initial run completed with {job.failed} failed websites. Bright Data is not configured.")
                    else:
                        job.status = "completed"
                        job.message = (
                            "Completed with some companies needing website review."
                            if job.lookup_failed
                            else "Scraping completed successfully."
                        )
                        job.ended_at = time.time()
                        job.add_log("Run completed.")
        except Exception as exc:
            LOGGER.exception("UI job failed")
            with job.lock:
                job.status = "failed"
                job.message = "Run failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.ended_at = time.time()
                job.active_domains = []
            job.add_log(job.error)
        finally:
            storage.close()

    @app.get("/")
    def index():
        return render_template("scraper.html")

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "app": "lightning-contact-scraper",
                "build": ui_build,
            }
        )

    @app.get("/api/settings/brightdata")
    def get_brightdata_settings():
        defaults = Settings.from_env(package_dir.parent / ".env")
        stored = _load_bright_settings(bright_settings_path)
        return jsonify(_bright_settings_snapshot(stored, defaults))

    @app.post("/api/settings/brightdata")
    def save_brightdata_settings():
        payload = request.get_json(silent=True) or {}
        try:
            stored = _validated_bright_settings(
                payload, _load_bright_settings(bright_settings_path)
            )
            _save_bright_settings(bright_settings_path, stored)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        defaults = Settings.from_env(package_dir.parent / ".env")
        return jsonify(
            {
                **_bright_settings_snapshot(stored, defaults),
                "message": "Bright Data settings saved locally.",
            }
        )

    @app.post("/api/upload")
    def upload_csv():
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "Choose a CSV file first."}), 400
        original_name = secure_filename(file.filename) or "websites.csv"
        if Path(original_name).suffix.lower() not in ALLOWED_EXTENSIONS:
            return jsonify({"error": "Only CSV files are supported."}), 400
        upload_id = uuid.uuid4().hex[:12]
        target = uploads_dir / f"{upload_id}_{original_name}"
        file.save(target)
        try:
            details = _inspect_csv(target)
        except ValueError as exc:
            target.unlink(missing_ok=True)
            return jsonify({"error": str(exc)}), 400
        record = UploadRecord(
            id=upload_id,
            path=target,
            original_name=original_name,
            **details,
        )
        with registry_lock:
            uploads[upload_id] = record
        return jsonify(record.snapshot())

    @app.post("/api/paste")
    def paste_websites():
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode", "")).strip().casefold()
        try:
            if mode == "website":
                input_type = "website"
                values = _parse_pasted_websites(payload.get("websites", ""))
            elif mode == "company":
                input_type = "company"
                values = _parse_pasted_companies(payload.get("websites", ""))
            else:
                return jsonify({"error": "Choose website paste or company paste."}), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        upload_id = uuid.uuid4().hex[:12]
        original_name = (
            "pasted_websites.csv" if input_type == "website" else "pasted_companies.csv"
        )
        target = uploads_dir / f"{upload_id}_{original_name}"
        _write_pasted_csv(target, input_type, values)
        details = _inspect_csv(target)
        if details["valid_count"] == 0:
            target.unlink(missing_ok=True)
            return jsonify(
                {"error": "No valid website URLs, domains, or company names were detected."}
            ), 400
        record = UploadRecord(
            id=upload_id,
            path=target,
            original_name=original_name,
            **details,
        )
        with registry_lock:
            uploads[upload_id] = record
        return jsonify(record.snapshot())

    @app.get("/api/upload/<upload_id>")
    def get_upload(upload_id: str):
        with registry_lock:
            record = uploads.get(upload_id)
        if record is None or not record.path.exists():
            return jsonify({"error": "Upload not found."}), 404
        return jsonify(record.snapshot())

    @app.post("/api/jobs")
    def start_job():
        payload = request.get_json(silent=True) or {}
        upload_id = str(payload.get("uploadId", ""))
        mode = str(payload.get("mode", ""))
        selected_kinds_list = payload.get("selectedKinds")
        crawl_mode = str(payload.get("crawlMode", "fast"))
        clean_fields_provided = "cleanFields" in payload
        try:
            clean_options = CleanExportOptions(
                fields=payload.get("cleanFields") if clean_fields_provided else None,
                phone_region=str(payload.get("cleanPhoneRegion", "AUTO")),
                phone_format=str(payload.get("cleanPhoneFormat", "national")),
                include_evidence=bool(payload.get("includeEvidenceColumns", False)),
                phone_country_confidence=str(
                    payload.get("phoneCountryConfidence", "strict")
                ),
                email_preference=str(payload.get("emailPreference", "business")),
                fast_quality=str(payload.get("fastQuality", "balanced")),
                enable_mx_check=bool(payload.get("enableMxCheck", False)),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        with registry_lock:
            upload = uploads.get(upload_id)
        if upload is None:
            return jsonify({"error": "Add websites before starting."}), 400

        selected_kinds = set()
        if selected_kinds_list is not None:
            if not isinstance(selected_kinds_list, list) or not selected_kinds_list:
                return jsonify({"error": "Select at least one data type to extract."}), 400
            for item in selected_kinds_list:
                if item == "details":
                    selected_kinds.update(MODE_KINDS["details"])
                elif item in ALL_KINDS:
                    selected_kinds.add(item)
                else:
                    return jsonify({"error": f"Unknown data type: {item}"}), 400
        else:
            if not mode:
                mode = "all"
            if mode not in MODE_KINDS:
                return jsonify({"error": "Unknown data selection."}), 400
            selected_kinds = set(MODE_KINDS[mode])

        if clean_fields_provided:
            for field in clean_options.fields or ():
                required_kind = clean_field_to_kind(field)
                if required_kind:
                    selected_kinds.add(required_kind)
        mode_display = _mode_display(selected_kinds)

        if crawl_mode not in CRAWL_MODES:
            return jsonify({"error": "Unknown scraping mode."}), 400
        try:
            workers = max(1, min(100, int(payload.get("workers", 40))))
            max_pages = max(1, min(20, int(payload.get("maxPages", 6))))
        except (TypeError, ValueError):
            return jsonify(
                {"error": "Workers and maximum pages must be numbers."}
            ), 400

        job_id = uuid.uuid4().hex[:12]
        output_dir = runs_dir / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        job = JobRecord(
            id=job_id,
            upload=upload,
            output_dir=output_dir,
            database_path=output_dir / "checkpoint.sqlite3",
            mode=mode_display,
            selected_kinds=selected_kinds,
            clean_options=clean_options,
            crawl_mode=crawl_mode,
            max_pages=max_pages,
            workers=workers,
        )
        with registry_lock:
            jobs[job_id] = job
        threading.Thread(
            target=execute_job,
            args=(job,),
            name=f"scraper-job-{job_id}",
            daemon=True,
        ).start()
        return jsonify(job.snapshot()), 202

    @app.get("/api/job/<job_id>")
    def get_job(job_id: str):
        with registry_lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        return jsonify(job.snapshot())

    @app.post("/api/job/<job_id>/pause")
    def pause_job(job_id: str):
        with registry_lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        with job.lock:
            if job.status != "running":
                return jsonify({"error": "Only a running job can be paused."}), 409
            job.control.pause_requested.set()
            job.status = "paused"
            job.message = "Paused. Active websites are finishing."
            job.paused_at = time.time()
        job.add_log("Pause requested.")
        return jsonify(job.snapshot())

    @app.post("/api/job/<job_id>/resume")
    def resume_job(job_id: str):
        with registry_lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        with job.lock:
            if job.status != "paused":
                return jsonify({"error": "Job is not paused."}), 409
            if job.paused_at is not None:
                job.paused_seconds += time.time() - job.paused_at
            job.paused_at = None
            job.control.pause_requested.clear()
            job.status = "running"
            job.message = "Resuming scraper"
        job.add_log("Run resumed.")
        return jsonify(job.snapshot())

    @app.post("/api/job/<job_id>/stop")
    def stop_job(job_id: str):
        with registry_lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        with job.lock:
            if job.status not in {"running", "paused"}:
                return jsonify({"error": "Job is not running."}), 409
            if job.paused_at is not None:
                job.paused_seconds += time.time() - job.paused_at
                job.paused_at = None
            job.control.stop_requested.set()
            job.control.pause_requested.clear()
            job.status = "stopping"
            job.message = "Stopping safely after active websites finish."
        job.add_log("Stop requested.")
        return jsonify(job.snapshot())

    @app.post("/api/job/<job_id>/retry_brightdata")
    def retry_brightdata(job_id: str):
        with registry_lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404

        retry_settings = _apply_bright_settings(
            Settings.from_env(package_dir.parent / ".env"),
            _load_bright_settings(bright_settings_path),
        )
        if not _bright_fallback_ready(retry_settings):
            return jsonify(
                {
                    "error": (
                        "Enable and configure Web Unlocker or Browser API "
                        "before retrying."
                    )
                }
            ), 409
        with job.lock:
            if job.status != "completed_initial":
                return jsonify({"error": "Job is not in completed_initial state."}), 409
            
            storage = Storage(job.database_path)
            try:
                retried_count = storage.reset_failed()
            finally:
                storage.close()
            if retried_count <= 0:
                return jsonify({"error": "No failed websites remain to retry."}), 409

            if job.paused_at is not None:
                job.paused_seconds += time.time() - job.paused_at
                job.paused_at = None
            
            job.stage = "retry"
            job.status = "queued"
            job.message = "Queued Bright Data retry"
            job.control.stop_requested.clear()
            job.control.pause_requested.clear()
            job.add_log(f"Bright Data retry requested for {retried_count} failed websites.")
            
        threading.Thread(
            target=execute_job,
            args=(job,),
            name=f"scraper-job-{job_id}",
            daemon=True,
        ).start()
        
        return jsonify(job.snapshot())

    @app.post("/api/job/<job_id>/no_retry")
    def no_retry_job(job_id: str):
        with registry_lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        with job.lock:
            if job.status != "completed_initial":
                return jsonify({"error": "Job is not in completed_initial state."}), 409

            if job.paused_at is not None:
                job.paused_seconds += time.time() - job.paused_at
                job.paused_at = None
            job.status = "completed"
            job.message = "Scraping completed."
            job.ended_at = time.time()
            job.add_log("User declined Bright Data retry; scraping completed.")
        return jsonify(job.snapshot())

    @app.get("/api/job/<job_id>/download/<filename>")
    def download(job_id: str, filename: str):
        with registry_lock:
            job = jobs.get(job_id)
        if job is None:
            abort(404)
        safe_name = secure_filename(filename)
        target = (job.output_dir / safe_name).resolve()
        if (
            not str(target).startswith(str(job.output_dir.resolve()))
            or not target.is_file()
        ):
            abort(404)
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return send_file(target, as_attachment=True, mimetype=mime)

    app.extensions["scraper_uploads"] = uploads
    app.extensions["scraper_jobs"] = jobs
    app.extensions["scraper_data_dir"] = data_dir
    app.extensions["scraper_bright_settings_path"] = bright_settings_path
    return app


def main() -> None:
    app = create_app()
    try:
        port = int(os.getenv("SCRAPER_UI_PORT", "8765"))
    except ValueError:
        port = 8765
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
