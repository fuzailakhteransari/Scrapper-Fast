from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import Settings
from .exporters import export_csvs, upload_google_sheet
from .logging_utils import configure_logging
from .runner import run_workers
from .storage import Storage
from .website_finder import WebsiteFinderSettings, resolve_company_csv

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contact-scraper",
        description=(
            "Scrape public emails, phones, social profiles, addresses, and "
            "company descriptions from a CSV of websites."
        ),
    )
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--website-column")
    parser.add_argument(
        "--find-websites",
        action="store_true",
        help=(
            "Resolve official websites from a company-name CSV before scraping. "
            "Also runs automatically when no website column is found and a "
            "company column is present."
        ),
    )
    parser.add_argument("--company-column")
    parser.add_argument("--location-column")
    parser.add_argument("--resolved-websites-csv", type=Path)
    parser.add_argument("--website-finder-concurrency", type=int, default=10)
    parser.add_argument("--website-min-confidence", type=float, default=0.62)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--phone-region")
    parser.add_argument("--no-unlocker", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--ignore-robots", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--google-sheets", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _has_website_column(path: Path, requested: str | None) -> bool:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        try:
            Storage._select_website_column(reader.fieldnames, requested)
            return True
        except ValueError:
            return False


async def _prepare_input_csv(
    input_path: Path, output_dir: Path, args: argparse.Namespace
) -> tuple[Path, str | None]:
    should_find = args.find_websites or not _has_website_column(
        input_path, args.website_column
    )
    if not should_find:
        return input_path, args.website_column

    resolved_path = (
        args.resolved_websites_csv.resolve()
        if args.resolved_websites_csv
        else output_dir / f"{input_path.stem}_resolved_websites.csv"
    )
    finder_settings = WebsiteFinderSettings.from_env()
    finder_settings.concurrency = max(1, args.website_finder_concurrency)
    finder_settings.min_confidence = max(0.0, min(0.99, args.website_min_confidence))
    await resolve_company_csv(
        input_path,
        resolved_path,
        company_column=args.company_column,
        location_column=args.location_column,
        settings=finder_settings,
    )
    LOGGER.info("resolved company websites to %s", resolved_path)
    return resolved_path, "Website"


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> None:
    if args.concurrency is not None:
        settings.concurrency = max(1, args.concurrency)
    if args.max_pages is not None:
        settings.max_pages = max(1, args.max_pages)
    if args.timeout is not None:
        settings.timeout_seconds = max(3, args.timeout)
    if args.phone_region:
        settings.default_phone_region = args.phone_region.upper()
    if args.no_unlocker:
        settings.use_web_unlocker = False
    if args.no_browser:
        settings.use_browser = False
    if args.ignore_robots:
        settings.respect_robots_txt = False


async def async_main(args: argparse.Namespace) -> int:
    input_path = args.input_csv.resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    output_dir = args.output_dir.resolve()
    configure_logging(output_dir, args.verbose)
    scrape_input_path, website_column = await _prepare_input_csv(
        input_path, output_dir, args
    )

    settings = Settings.from_env(args.env_file)
    _apply_overrides(settings, args)
    if settings.use_web_unlocker and not settings.web_unlocker_ready:
        LOGGER.warning("Web Unlocker is enabled but credentials are incomplete")
    if settings.use_browser and not settings.browser_ready:
        LOGGER.warning("Browser fallback is enabled but credentials are incomplete")

    database = (
        args.database.resolve()
        if args.database
        else output_dir / "checkpoint.sqlite3"
    )
    storage = Storage(database)
    try:
        reset_count = storage.reset_running()
        if reset_count:
            LOGGER.info("returned %d interrupted task(s) to the queue", reset_count)
        imported, invalid, selected = storage.import_csv(
            scrape_input_path, website_column
        )
        LOGGER.info(
            "input ready: %d rows, %d invalid, website column '%s'",
            imported,
            invalid,
            selected,
        )
        if args.fresh:
            storage.reset_all_tasks()
        elif args.retry_failed:
            count = storage.reset_failed()
            LOGGER.info("queued %d previously failed domain(s)", count)

        counts_before = storage.counts()
        LOGGER.info(
            "starting %d workers for %d unique domains (%d pending)",
            settings.concurrency,
            counts_before.get("total", 0),
            counts_before.get("pending", 0),
        )
        counts = await run_workers(storage, settings)
        source_file = str(scrape_input_path)
        results_path, contacts_path, review_path, clean_path = export_csvs(
            storage,
            source_file,
            output_dir,
            scrape_input_path.name,
            settings=settings,
        )
        if args.google_sheets:
            await upload_google_sheet(results_path, settings)
            LOGGER.info("uploaded results to Google Sheets")
        LOGGER.info(
            "done: %s | results=%s | contacts=%s | review=%s | clean=%s",
            counts,
            results_path,
            contacts_path,
            review_path,
            clean_path,
        )
        return 0 if counts.get("failed", 0) == 0 else 2
    finally:
        storage.close()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("Interrupted. Progress is saved and will resume next run.", file=sys.stderr)
        code = 130
    except Exception as exc:
        logging.getLogger(__name__).exception("fatal error: %s", exc)
        print(f"Fatal error: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
