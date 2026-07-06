from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    concurrency: int = 40
    per_site_concurrency: int = 3
    timeout_seconds: int = 15
    connect_timeout_seconds: int = 8
    max_pages: int = 6
    max_response_bytes: int = 5_000_000
    retries: int = 2
    default_phone_region: str = "US"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    use_web_unlocker: bool = True
    use_browser: bool = True
    respect_robots_txt: bool = True
    brightdata_unlocker_zone: str = ""
    brightdata_api_key: str = ""
    brightdata_browser_username: str = ""
    brightdata_browser_password: str = ""
    brightdata_browser_host: str = "brd.superproxy.io"
    brightdata_browser_port: int = 9222
    browser_concurrency: int = 3
    google_service_account_json_path: str = ""
    google_spreadsheet_id: str = ""
    google_sheet_tab: str = "Scraper Results"

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "Settings":
        load_dotenv(dotenv_path=env_path, override=False)
        return cls(
            concurrency=max(1, _int("SCRAPER_CONCURRENCY", 40)),
            per_site_concurrency=max(1, _int("SCRAPER_PER_SITE_CONCURRENCY", 3)),
            timeout_seconds=max(3, _int("SCRAPER_TIMEOUT_SECONDS", 15)),
            connect_timeout_seconds=max(2, _int("SCRAPER_CONNECT_TIMEOUT_SECONDS", 8)),
            max_pages=max(1, _int("SCRAPER_MAX_PAGES", 6)),
            max_response_bytes=max(
                100_000, _int("SCRAPER_MAX_RESPONSE_BYTES", 5_000_000)
            ),
            retries=max(0, _int("SCRAPER_RETRIES", 2)),
            default_phone_region=os.getenv(
                "SCRAPER_DEFAULT_PHONE_REGION", "US"
            ).upper(),
            use_web_unlocker=_bool("SCRAPER_USE_WEB_UNLOCKER", True),
            use_browser=_bool("SCRAPER_USE_BROWSER", True),
            respect_robots_txt=_bool("SCRAPER_RESPECT_ROBOTS_TXT", True),
            brightdata_unlocker_zone=os.getenv(
                "BRIGHTDATA_UNLOCKER_ZONE", ""
            ).strip(),
            brightdata_api_key=os.getenv("BRIGHTDATA_API_KEY", "").strip(),
            brightdata_browser_username=os.getenv(
                "BRIGHTDATA_BROWSER_USERNAME", ""
            ).strip(),
            brightdata_browser_password=os.getenv(
                "BRIGHTDATA_BROWSER_PASSWORD", ""
            ).strip(),
            brightdata_browser_host=os.getenv(
                "BRIGHTDATA_BROWSER_HOST", "brd.superproxy.io"
            ).strip(),
            brightdata_browser_port=_int("BRIGHTDATA_BROWSER_PORT", 9222),
            browser_concurrency=max(1, _int("SCRAPER_BROWSER_CONCURRENCY", 3)),
            google_service_account_json_path=os.getenv(
                "GOOGLE_SERVICE_ACCOUNT_JSON_PATH", ""
            ).strip(),
            google_spreadsheet_id=os.getenv(
                "GOOGLE_SPREADSHEET_ID", ""
            ).strip(),
            google_sheet_tab=os.getenv(
                "GOOGLE_SHEET_TAB", "Scraper Results"
            ).strip(),
        )

    @property
    def web_unlocker_ready(self) -> bool:
        return bool(self.brightdata_unlocker_zone and self.brightdata_api_key)

    @property
    def browser_ready(self) -> bool:
        return bool(
            self.brightdata_browser_username and self.brightdata_browser_password
        )

