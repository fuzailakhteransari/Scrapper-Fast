from __future__ import annotations

import asyncio
import csv
import json
import socket
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

import phonenumbers

from .accuracy import (
    CLEAN_FIELD_LABELS,
    CLEAN_PHONE_FORMATS,
    CLEAN_SOCIAL_FIELDS,
    CleanExportOptions,
)
from .config import Settings
from .models import Evidence, SiteResult
from .storage import Storage


RESULT_COLUMNS = [
    "website_finder_status",
    "website_confidence",
    "website_source",
    "website_top_candidates",
    "scrape_status",
    "primary_email",
    "emails",
    "primary_phone",
    "phones",
    "final_url",
    "domain",
    "facebook",
    "instagram",
    "linkedin",
    "twitter_x",
    "youtube",
    "tiktok",
    "pinterest",
    "threads",
    "social_profiles",
    "address",
    "company_description",
    "notes",
]

LONG_COLUMNS = [
    "input_row",
    "input_website",
    "domain",
    "data_type",
    "label",
    "value",
    "confidence",
    "found_via",
    "page_url",
    "original_text",
]

REVIEW_COLUMNS = [
    "website_finder_status",
    "website_confidence",
    "website_source",
    "website_top_candidates",
    "scrape_status",
    "final_url",
    "domain",
    "http_status",
    "fetch_tier",
    "task_attempts",
    "notes",
]

HIDDEN_INPUT_COLUMNS = {
    "website_finder_status",
    "website_confidence",
    "website_source",
    "website_reason",
    "website_top_candidates",
    "website_candidates_json",
    "website_finder_elapsed_ms",
}


def _join(values: list[str]) -> str:
    return "; ".join(dict.fromkeys(value for value in values if value))


def _first(values: list[str]) -> str:
    return next((value for value in values if value), "")


def _clean_note(value: Any) -> str:
    return str(value or "").strip()


def _finder_value(original: dict[str, Any], key: str) -> str:
    return _clean_note(original.get(key, ""))


def _review_needed(result: SiteResult | None, row: Any, original: dict[str, Any]) -> bool:
    if row["import_error"] or row["last_error"]:
        return True
    if _finder_value(original, "website_finder_status") not in {"", "found"}:
        return True
    if result is None:
        return True
    if result.status not in {"ok", "partial"}:
        return True
    return bool(result.errors)


def _notes(result: SiteResult | None, row: Any, original: dict[str, Any]) -> str:
    parts: list[str] = []
    finder_status = _finder_value(original, "website_finder_status")
    finder_reason = _finder_value(original, "website_reason")
    finder_candidates = _finder_value(original, "website_top_candidates")
    if finder_status and finder_status != "found":
        parts.append(finder_reason or f"Website finder status: {finder_status}")
        if finder_candidates:
            parts.append(f"Top candidates: {finder_candidates}")
    if row["import_error"]:
        parts.append(str(row["import_error"]))
    if row["last_error"]:
        parts.append(str(row["last_error"]))
    if result and result.errors:
        parts.extend(result.errors)
    return _join([_clean_note(part) for part in parts])


def _visible_original_fields(original_fields: list[str]) -> list[str]:
    return [field for field in original_fields if field not in HIDDEN_INPUT_COLUMNS]


@lru_cache(maxsize=20_000)
def _email_domain_has_mail(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    try:
        import dns.resolver  # type: ignore[import-not-found]

        answers = dns.resolver.resolve(host, "MX", lifetime=2.0)
        return bool(answers)
    except ImportError:
        pass
    except Exception:
        return False
    try:
        socket.getaddrinfo(host, 25, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def _email_score(contact: Evidence, options: CleanExportOptions) -> float:
    category_weights = {
        "generic": 0.34,
        "named": 0.28,
        "external_domain": 0.08,
        "free_mail": 0.02,
    }
    if options.email_preference == "named":
        category_weights["named"] = 0.36
        category_weights["generic"] = 0.30
    elif options.email_preference == "highest":
        category_weights = {
            "generic": 0.18,
            "named": 0.18,
            "external_domain": 0.08,
            "free_mail": 0.02,
        }
    source_weights = {
        "mailto": 0.16,
        "json_ld": 0.14,
        "cloudflare": 0.13,
        "obfuscated": 0.10,
        "visible_text": 0.08,
        "html": 0.02,
    }
    score = (
        contact.confidence * 0.52
        + category_weights.get(contact.category, 0.0)
        + source_weights.get(contact.source_type, 0.0)
    )
    if options.enable_mx_check:
        host = contact.value.rsplit("@", 1)[-1]
        score += 0.06 if _email_domain_has_mail(host) else -0.20
    return score


def _primary_email(result: SiteResult, options: CleanExportOptions) -> Evidence | None:
    ranked = sorted(
        (contact for contact in result.contacts if contact.kind == "email"),
        key=lambda contact: (-_email_score(contact, options), contact.value),
    )
    return ranked[0] if ranked else None


def _format_clean_phone(
    value: str,
    settings: Settings,
    options: CleanExportOptions,
) -> tuple[str, str, str]:
    raw = (value or "").strip()
    if not raw:
        return "", "", ""
    target_region = None if options.phone_region == "AUTO" else options.phone_region
    parse_region = target_region or settings.default_phone_region or None
    try:
        number = phonenumbers.parse(raw, parse_region)
    except phonenumbers.NumberParseException:
        return "", "", ""
    if not phonenumbers.is_possible_number(number):
        return "", "", ""
    if not phonenumbers.is_valid_number(number):
        return "", "", ""
    region = phonenumbers.region_code_for_number(number) or ""
    if target_region and region != target_region:
        return "", "", ""
    number_format = CLEAN_PHONE_FORMATS[options.phone_format]
    phone_type = phonenumbers.PhoneNumberType.to_string(
        phonenumbers.number_type(number)
    ).replace("_", " ").title()
    return phonenumbers.format_number(number, number_format), region, phone_type


def _phone_score(contact: Evidence, region: str, options: CleanExportOptions) -> float:
    source_weights = {
        "tel": 0.22,
        "json_ld": 0.18,
        "visible_text": 0.04,
    }
    type_weights = {
        "MOBILE": 0.05,
        "FIXED_LINE": 0.04,
        "FIXED_LINE_OR_MOBILE": 0.04,
        "TOLL_FREE": 0.03,
        "VOIP": 0.01,
    }
    target = "" if options.phone_region == "AUTO" else options.phone_region
    score = contact.confidence * 0.70 + source_weights.get(contact.source_type, 0.0)
    if target and region == target:
        score += 0.14
    elif target and options.phone_country_confidence == "strict":
        score -= 0.30
    score += type_weights.get(str(contact.meta.get("type", "") or ""), 0.0)
    return score


def _primary_phone(
    result: SiteResult,
    settings: Settings,
    options: CleanExportOptions,
) -> tuple[str, Evidence | None, str, str]:
    candidates: list[tuple[float, str, Evidence, str, str]] = []
    for contact in result.contacts:
        if contact.kind != "phone":
            continue
        formatted, region, phone_type = _format_clean_phone(
            contact.value, settings, options
        )
        if not formatted:
            continue
        candidates.append(
            (_phone_score(contact, region, options), formatted, contact, region, phone_type)
        )
    ranked = sorted(candidates, key=lambda item: (-item[0], item[1]))
    if ranked:
        _, formatted, contact, region, phone_type = ranked[0]
        return formatted, contact, region, phone_type
    return "", None, "", ""


def _primary_phone_value(
    result: SiteResult,
    settings: Settings,
    options: CleanExportOptions,
) -> str:
    value, _, _, _ = _primary_phone(result, settings, options)
    return value


def _primary_social(result: SiteResult, category: str) -> Evidence | None:
    ranked = sorted(
        (
            contact
            for contact in result.contacts
            if contact.kind == "social" and contact.category == category
        ),
        key=lambda contact: (-contact.confidence, len(contact.value), contact.value),
    )
    return ranked[0] if ranked else None


def _clean_website(result: SiteResult | None, row: Any, original: dict[str, Any]) -> str:
    if result and result.final_url:
        return result.final_url
    for key in ("final_url", "Website"):
        value = _clean_note(original.get(key, "")) if key in original else ""
        if value:
            return value
    return _clean_note(row["normalized_url"] or row["website"] or "")


def _clean_values(
    result: SiteResult | None,
    row: Any,
    original: dict[str, Any],
    settings: Settings,
    options: CleanExportOptions,
) -> dict[str, str]:
    values: dict[str, str] = {}
    notes: list[str] = []
    email_contact: Evidence | None = None
    phone_contact: Evidence | None = None
    phone_region = ""
    phone_type = ""
    for field in options.fields or ():
        column = CLEAN_FIELD_LABELS[field]
        if field == "website":
            value = _clean_website(result, row, original)
            values[column] = value
            if not value:
                notes.append("Website missing")
        elif result is None:
            values[column] = ""
            notes.append(f"{column} missing")
        elif field == "email":
            email_contact = _primary_email(result, options)
            values[column] = email_contact.value if email_contact else ""
            if email_contact is None:
                notes.append("Email missing")
        elif field == "phone":
            phone_value, phone_contact, phone_region, phone_type = _primary_phone(
                result, settings, options
            )
            values[column] = phone_value
            if not phone_value:
                notes.append("Phone missing or outside selected geography")
        elif field in CLEAN_SOCIAL_FIELDS:
            social = _primary_social(result, CLEAN_SOCIAL_FIELDS[field])
            values[column] = social.value if social else ""
            if social is None:
                notes.append(f"{column} missing")
        elif field == "address":
            values[column] = result.address
            if not result.address:
                notes.append("Address missing")
        elif field == "description":
            values[column] = result.description
            if not result.description:
                notes.append("Description missing")
        else:
            values[column] = ""
    if options.include_evidence:
        if "email" in (options.fields or ()):
            values["Email Source"] = email_contact.source_type if email_contact else ""
            values["Email Confidence"] = (
                f"{email_contact.confidence:.2f}" if email_contact else ""
            )
        if "phone" in (options.fields or ()):
            values["Phone Region"] = phone_region
            values["Phone Type"] = phone_type
            values["Phone Source"] = phone_contact.source_type if phone_contact else ""
            values["Phone Confidence"] = (
                f"{phone_contact.confidence:.2f}" if phone_contact else ""
            )
        values["Review Note"] = _join(notes)
    return values


def _main_result_columns(
    selected_kinds: set[str] | None, has_finder: bool
) -> list[str]:
    columns: list[str] = []
    if has_finder:
        columns.extend(
            [
                "website_finder_status",
                "website_confidence",
                "website_source",
                "website_top_candidates",
            ]
        )
    columns.append("scrape_status")
    if not selected_kinds or "email" in selected_kinds:
        columns.extend(["primary_email", "emails"])
    if not selected_kinds or "phone" in selected_kinds:
        columns.extend(["primary_phone", "phones"])
    if not selected_kinds or "social" in selected_kinds:
        columns.extend(
            [
                "facebook",
                "instagram",
                "linkedin",
                "twitter_x",
                "youtube",
                "tiktok",
                "pinterest",
                "threads",
                "social_profiles",
            ]
        )
    if not selected_kinds or "address" in selected_kinds:
        columns.append("address")
    if not selected_kinds or "description" in selected_kinds:
        columns.append("company_description")
    columns.extend(["final_url", "domain", "notes"])
    return columns


def _wide_values(result: SiteResult | None, row: Any) -> dict[str, Any]:
    original = json.loads(row["row_json"])
    finder_status = _finder_value(original, "website_finder_status")
    finder_confidence = _finder_value(original, "website_confidence")
    finder_source = _finder_value(original, "website_source")
    finder_candidates = _finder_value(original, "website_top_candidates")
    if result is None:
        error = _notes(None, row, original) or "No result available"
        return {
            "website_finder_status": finder_status,
            "website_confidence": finder_confidence,
            "website_source": finder_source,
            "website_top_candidates": finder_candidates,
            "scrape_status": "needs_review"
            if finder_status and finder_status != "found"
            else ("invalid_input" if row["import_error"] else row["task_status"]),
            "primary_email": "",
            "emails": "",
            "primary_phone": "",
            "phones": "",
            "final_url": "",
            "domain": row["domain_key"] or "",
            "notes": error,
        }
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for contact in result.contacts:
        grouped[(contact.kind, contact.category)].append(contact.value)
    emails = [
        contact.value for contact in result.contacts if contact.kind == "email"
    ]
    phones = [
        contact.value for contact in result.contacts if contact.kind == "phone"
    ]
    socials = [
        contact.value for contact in result.contacts if contact.kind == "social"
    ]
    return {
        "website_finder_status": finder_status,
        "website_confidence": finder_confidence,
        "website_source": finder_source,
        "website_top_candidates": finder_candidates,
        "scrape_status": result.status,
        "primary_email": _first(emails),
        "emails": _join(emails),
        "primary_phone": _first(phones),
        "phones": _join(phones),
        "final_url": result.final_url,
        "domain": result.domain,
        "facebook": _join(grouped[("social", "facebook")]),
        "instagram": _join(grouped[("social", "instagram")]),
        "linkedin": _join(grouped[("social", "linkedin")]),
        "twitter_x": _join(grouped[("social", "twitter")]),
        "youtube": _join(grouped[("social", "youtube")]),
        "tiktok": _join(grouped[("social", "tiktok")]),
        "pinterest": _join(grouped[("social", "pinterest")]),
        "threads": _join(grouped[("social", "threads")]),
        "social_profiles": _join(socials),
        "address": result.address,
        "company_description": result.description,
        "notes": _notes(result, row, original),
    }


def _review_values(result: SiteResult | None, row: Any) -> dict[str, Any]:
    original = json.loads(row["row_json"])
    return {
        "website_finder_status": _finder_value(original, "website_finder_status"),
        "website_confidence": _finder_value(original, "website_confidence"),
        "website_source": _finder_value(original, "website_source"),
        "website_top_candidates": _finder_value(original, "website_top_candidates"),
        "scrape_status": (
            result.status
            if result is not None
            else (
                "needs_review"
                if _finder_value(original, "website_finder_status") not in {"", "found"}
                else ("invalid_input" if row["import_error"] else row["task_status"])
            )
        ),
        "final_url": result.final_url if result else "",
        "domain": result.domain if result else (row["domain_key"] or ""),
        "http_status": result.http_status if result and result.http_status else "",
        "fetch_tier": result.fetch_tier if result else "",
        "task_attempts": row["attempts"] or 0,
        "notes": _notes(result, row, original),
    }


def export_csvs(
    storage: Storage,
    source_file: str,
    output_dir: Path,
    input_name: str,
    selected_kinds: set[str] | None = None,
    settings: Settings | None = None,
    clean_options: CleanExportOptions | None = None,
) -> tuple[Path, Path, Path, Path]:
    settings = settings or Settings()
    clean_options = clean_options or CleanExportOptions()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(input_name).stem
    results_path = output_dir / f"{stem}_results.csv"
    contacts_path = output_dir / f"{stem}_contacts.csv"
    review_path = output_dir / f"{stem}_review.csv"
    clean_path = output_dir / f"{stem}_clean.csv"

    rows = storage.result_rows(source_file)
    first_row = next(rows, None)
    if first_row is None:
        raise ValueError("No imported rows found for this input file")
    original_fields = list(json.loads(first_row["row_json"]).keys())
    visible_original_fields = _visible_original_fields(original_fields)
    has_finder = "website_finder_status" in original_fields
    result_columns = _main_result_columns(selected_kinds, has_finder)
    fieldnames = visible_original_fields + [
        column for column in result_columns if column not in visible_original_fields
    ]
    review_fieldnames = visible_original_fields + [
        column for column in REVIEW_COLUMNS if column not in visible_original_fields
    ]

    with (
        results_path.open("w", encoding="utf-8-sig", newline="") as wide_handle,
        contacts_path.open("w", encoding="utf-8-sig", newline="") as long_handle,
        review_path.open("w", encoding="utf-8-sig", newline="") as review_handle,
        clean_path.open("w", encoding="utf-8-sig", newline="") as clean_handle,
    ):
        wide_writer = csv.DictWriter(
            wide_handle, fieldnames=fieldnames, extrasaction="ignore"
        )
        long_writer = csv.DictWriter(long_handle, fieldnames=LONG_COLUMNS)
        review_writer = csv.DictWriter(
            review_handle,
            fieldnames=review_fieldnames,
            extrasaction="ignore",
        )
        clean_writer = csv.DictWriter(clean_handle, fieldnames=clean_options.columns)
        wide_writer.writeheader()
        long_writer.writeheader()
        review_writer.writeheader()
        clean_writer.writeheader()

        def write_row(row) -> None:
            original = json.loads(row["row_json"])
            result = (
                SiteResult.from_dict(json.loads(row["result_json"]))
                if row["result_json"]
                else None
            )
            visible_original = {
                key: value
                for key, value in original.items()
                if key in visible_original_fields
            }
            wide = {**visible_original, **_wide_values(result, row)}
            wide_writer.writerow(wide)
            clean_writer.writerow(
                _clean_values(result, row, original, settings, clean_options)
            )
            if result:
                for contact in result.contacts:
                    long_writer.writerow(
                        {
                            "input_row": row["row_number"],
                            "input_website": row["website"],
                            "domain": result.domain,
                            "data_type": contact.kind,
                            "label": contact.category,
                            "value": contact.value,
                            "confidence": f"{contact.confidence:.2f}",
                            "found_via": contact.source_type,
                            "page_url": contact.source_url,
                            "original_text": contact.raw_value,
                        }
                    )
            if _review_needed(result, row, original):
                review_writer.writerow(
                    {**visible_original, **_review_values(result, row)}
                )

        write_row(first_row)
        for row in rows:
            write_row(row)
    return results_path, contacts_path, review_path, clean_path


def _upload_google_sync(csv_path: Path, settings: Settings) -> None:
    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError(
            "Google export requires: pip install gspread google-auth"
        ) from exc
    if not settings.google_service_account_json_path:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON_PATH is not configured")
    if not settings.google_spreadsheet_id:
        raise ValueError("GOOGLE_SPREADSHEET_ID is not configured")

    client = gspread.service_account(
        filename=settings.google_service_account_json_path
    )
    spreadsheet = client.open_by_key(settings.google_spreadsheet_id)
    try:
        worksheet = spreadsheet.worksheet(settings.google_sheet_tab)
        worksheet.clear()
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=settings.google_sheet_tab, rows=1000, cols=30
        )

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        batch: list[list[str]] = []
        for row in reader:
            batch.append(row)
            if len(batch) >= 500:
                worksheet.append_rows(
                    batch, value_input_option="RAW", table_range="A1"
                )
                batch.clear()
        if batch:
            worksheet.append_rows(batch, value_input_option="RAW", table_range="A1")


async def upload_google_sheet(csv_path: Path, settings: Settings) -> None:
    await asyncio.to_thread(_upload_google_sync, csv_path, settings)
