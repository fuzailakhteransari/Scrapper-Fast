from __future__ import annotations

from dataclasses import dataclass

from phonenumbers import PhoneNumberFormat


DEFAULT_CLEAN_FIELD_IDS = ("website", "email", "phone", "facebook")

CLEAN_FIELD_LABELS = {
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
}

CLEAN_SOCIAL_FIELDS = {
    "facebook": "facebook",
    "instagram": "instagram",
    "linkedin": "linkedin",
    "twitter_x": "twitter",
    "youtube": "youtube",
    "tiktok": "tiktok",
    "pinterest": "pinterest",
    "threads": "threads",
}

CLEAN_PHONE_REGION_ALIASES = {
    "": "AUTO",
    "AUTO": "AUTO",
    "AUTODETECT": "AUTO",
    "AUTO_DETECT": "AUTO",
    "AU": "AU",
    "AUS": "AU",
    "AUSTRALIA": "AU",
    "US": "US",
    "USA": "US",
    "UNITEDSTATES": "US",
    "UNITED_STATES": "US",
    "IN": "IN",
    "INDIA": "IN",
    "KR": "KR",
    "KOREA": "KR",
    "SOUTHKOREA": "KR",
    "SOUTH_KOREA": "KR",
}

CLEAN_PHONE_FORMATS = {
    "national": PhoneNumberFormat.NATIONAL,
    "international": PhoneNumberFormat.INTERNATIONAL,
    "e164": PhoneNumberFormat.E164,
}

PHONE_CONFIDENCE_POLICIES = {"strict", "balanced", "loose"}
EMAIL_PREFERENCES = {"business", "named", "highest"}
FAST_QUALITY_LEVELS = {"loose", "balanced", "strict"}
FAST_QUALITY_THRESHOLDS = {
    "loose": 0.75,
    "balanced": 0.86,
    "strict": 0.95,
}

EVIDENCE_COLUMNS = {
    "email": ("Email Source", "Email Confidence"),
    "phone": ("Phone Region", "Phone Type", "Phone Source", "Phone Confidence"),
    "review": ("Review Note",),
}


def normalize_clean_field(value: str) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    if normalized in {"x", "twitter"}:
        normalized = "twitter_x"
    if normalized == "company_description":
        normalized = "description"
    if normalized not in CLEAN_FIELD_LABELS:
        raise ValueError(f"Unknown clean CSV field: {value}")
    return normalized


def normalize_phone_region(value: str) -> str:
    region_key = (
        str(value or "AUTO")
        .strip()
        .upper()
        .replace(" ", "_")
        .replace("-", "_")
    )
    region = CLEAN_PHONE_REGION_ALIASES.get(region_key)
    if region is None:
        raise ValueError(f"Unknown phone geography: {value}")
    return region


def normalize_phone_format(value: str) -> str:
    phone_format = (
        str(value or "national")
        .strip()
        .casefold()
        .replace(".", "")
        .replace("-", "_")
    )
    if phone_format in {"local", "domestic"}:
        phone_format = "national"
    if phone_format in {"intl", "global"}:
        phone_format = "international"
    if phone_format not in CLEAN_PHONE_FORMATS:
        raise ValueError(f"Unknown phone format: {value}")
    return phone_format


def normalize_choice(value: str, allowed: set[str], default: str, label: str) -> str:
    normalized = str(value or default).strip().casefold().replace("-", "_")
    if normalized not in allowed:
        raise ValueError(f"Unknown {label}: {value}")
    return normalized


def clean_field_to_kind(field: str) -> str | None:
    if field == "website":
        return "website"
    if field in {"email", "phone", "address", "description"}:
        return field
    if field in CLEAN_SOCIAL_FIELDS:
        return "social"
    return None


def kinds_for_clean_fields(fields: tuple[str, ...]) -> set[str]:
    kinds: set[str] = set()
    for field in fields:
        kind = clean_field_to_kind(field)
        if kind and kind != "website":
            kinds.add(kind)
    return kinds


@dataclass(frozen=True, slots=True)
class CleanExportOptions:
    fields: tuple[str, ...] | list[str] | None = DEFAULT_CLEAN_FIELD_IDS
    phone_region: str = "AUTO"
    phone_format: str = "national"
    include_evidence: bool = False
    phone_country_confidence: str = "strict"
    email_preference: str = "business"
    fast_quality: str = "balanced"
    enable_mx_check: bool = False

    def __post_init__(self) -> None:
        raw_fields = self.fields if self.fields is not None else DEFAULT_CLEAN_FIELD_IDS
        normalized_fields: list[str] = []
        for field in raw_fields:
            normalized = normalize_clean_field(str(field))
            if normalized not in normalized_fields:
                normalized_fields.append(normalized)
        if not normalized_fields:
            raise ValueError("Select at least one clean CSV field.")
        object.__setattr__(self, "fields", tuple(normalized_fields))
        object.__setattr__(self, "phone_region", normalize_phone_region(self.phone_region))
        object.__setattr__(self, "phone_format", normalize_phone_format(self.phone_format))
        object.__setattr__(
            self,
            "phone_country_confidence",
            normalize_choice(
                self.phone_country_confidence,
                PHONE_CONFIDENCE_POLICIES,
                "strict",
                "phone country confidence",
            ),
        )
        object.__setattr__(
            self,
            "email_preference",
            normalize_choice(
                self.email_preference,
                EMAIL_PREFERENCES,
                "business",
                "email preference",
            ),
        )
        object.__setattr__(
            self,
            "fast_quality",
            normalize_choice(
                self.fast_quality,
                FAST_QUALITY_LEVELS,
                "balanced",
                "Fast Mode quality threshold",
            ),
        )
        object.__setattr__(self, "include_evidence", bool(self.include_evidence))
        object.__setattr__(self, "enable_mx_check", bool(self.enable_mx_check))

    @property
    def columns(self) -> list[str]:
        columns = [CLEAN_FIELD_LABELS[field] for field in self.fields or ()]
        if not self.include_evidence:
            return columns
        if "email" in (self.fields or ()):
            columns.extend(EVIDENCE_COLUMNS["email"])
        if "phone" in (self.fields or ()):
            columns.extend(EVIDENCE_COLUMNS["phone"])
        columns.extend(EVIDENCE_COLUMNS["review"])
        return columns

    @property
    def fast_quality_threshold(self) -> float:
        return FAST_QUALITY_THRESHOLDS[self.fast_quality]
