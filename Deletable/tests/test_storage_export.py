import csv
from pathlib import Path

from contact_scraper.config import Settings
from contact_scraper.exporters import CleanExportOptions, export_csvs
from contact_scraper.models import Evidence, SiteResult
from contact_scraper.storage import Storage


def test_storage_deduplicates_domains_and_exports_original_order(tmp_path: Path):
    source = tmp_path / "input.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Company", "Website"])
        writer.writeheader()
        writer.writerow({"Company": "One", "Website": "example.com"})
        writer.writerow({"Company": "Duplicate", "Website": "https://www.example.com"})
        writer.writerow({"Company": "Bad", "Website": ""})

    storage = Storage(tmp_path / "checkpoint.sqlite3")
    try:
        imported, invalid, column = storage.import_csv(source)
        assert (imported, invalid, column) == (3, 1, "Website")
        assert storage.counts()["total"] == 1
        assert storage.import_csv(source) == (3, 1, "Website")

        task = storage.claim_task()
        assert task is not None
        result = SiteResult(
            input_url=task["input_url"],
            normalized_url=task["normalized_url"],
            final_url="https://example.com/",
            domain="example.com",
            status="ok",
            fetch_tier="direct",
            contacts=[
                Evidence(
                    value="info@example.com",
                    kind="email",
                    source_url="https://example.com/",
                    source_type="mailto",
                    confidence=0.98,
                    category="generic",
                ),
                Evidence(
                    value="+61477533988",
                    kind="phone",
                    source_url="https://example.com/contact",
                    source_type="visible_text",
                    confidence=0.96,
                ),
                Evidence(
                    value="https://facebook.com/examplecompany",
                    kind="social",
                    source_url="https://example.com/",
                    source_type="anchor",
                    confidence=0.94,
                    category="facebook",
                ),
            ],
            pages_scraped=["https://example.com/"],
        )
        storage.complete_task(task["domain_key"], result)
        results, contacts, review, clean = export_csvs(
            storage,
            str(source.resolve()),
            tmp_path,
            source.name,
            settings=Settings(default_phone_region="AU"),
            clean_options=CleanExportOptions(
                fields=("website", "phone", "facebook"),
                phone_region="AUS",
                phone_format="national",
            ),
        )
    finally:
        storage.close()

    with results.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["Company"] for row in rows] == ["One", "Duplicate", "Bad"]
    assert rows[0]["emails"] == "info@example.com"
    assert rows[1]["emails"] == "info@example.com"
    assert rows[2]["scrape_status"] == "invalid_input"
    assert "notes" in rows[0]
    assert "final_url" in rows[0]

    with contacts.open("r", encoding="utf-8-sig", newline="") as handle:
        contact_rows = list(csv.DictReader(handle))
    assert len(contact_rows) == 6
    assert "data_type" in contact_rows[0]
    assert review.exists()

    with clean.open("r", encoding="utf-8-sig", newline="") as handle:
        clean_rows = list(csv.DictReader(handle))
    assert clean_rows[0] == {
        "Website": "https://example.com/",
        "Phone": "0477 533 988",
        "Facebook": "https://facebook.com/examplecompany",
    }
    assert list(clean_rows[0]) == ["Website", "Phone", "Facebook"]
    assert clean_rows[2]["Website"] == ""


def test_clean_export_can_include_evidence_columns(tmp_path: Path):
    source = tmp_path / "input.csv"
    source.write_text("Website\nexample.com\n", encoding="utf-8")
    storage = Storage(tmp_path / "checkpoint.sqlite3")
    try:
        storage.import_csv(source)
        task = storage.claim_task()
        assert task is not None
        result = SiteResult(
            input_url=task["input_url"],
            normalized_url=task["normalized_url"],
            final_url="https://example.com/",
            domain="example.com",
            status="ok",
            contacts=[
                Evidence(
                    value="person@example.com",
                    kind="email",
                    source_url="https://example.com/contact",
                    source_type="visible_text",
                    confidence=0.88,
                    category="named",
                ),
                Evidence(
                    value="info@example.com",
                    kind="email",
                    source_url="https://example.com/contact",
                    source_type="mailto",
                    confidence=0.98,
                    category="generic",
                ),
                Evidence(
                    value="+61477533988",
                    kind="phone",
                    source_url="https://example.com/contact",
                    source_type="tel",
                    confidence=0.99,
                    meta={"region": "AU", "type": "MOBILE"},
                ),
            ],
        )
        storage.complete_task(task["domain_key"], result)
        _, _, _, clean = export_csvs(
            storage,
            str(source.resolve()),
            tmp_path,
            source.name,
            settings=Settings(default_phone_region="AU"),
            clean_options=CleanExportOptions(
                fields=("email", "phone"),
                phone_region="AU",
                include_evidence=True,
            ),
        )
    finally:
        storage.close()

    with clean.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["Email"] == "info@example.com"
    assert rows[0]["Email Source"] == "mailto"
    assert rows[0]["Phone"] == "0477 533 988"
    assert rows[0]["Phone Region"] == "AU"
    assert rows[0]["Phone Type"] == "Mobile"
    assert rows[0]["Review Note"] == ""


def test_changed_input_is_reimported_but_cached_tasks_remain(tmp_path: Path):
    source = tmp_path / "input.csv"
    source.write_text("Website\nexample.com\n", encoding="utf-8")
    storage = Storage(tmp_path / "checkpoint.sqlite3")
    try:
        assert storage.import_csv(source)[:2] == (1, 0)
        source.write_text(
            "Website\nexample.com\nsupport.example.com\n", encoding="utf-8"
        )
        assert storage.import_csv(source)[:2] == (2, 0)
        assert storage.counts()["total"] == 2
    finally:
        storage.close()


def test_reset_failed_resets_exhausted_attempts(tmp_path: Path):
    source = tmp_path / "input.csv"
    source.write_text("Website\nfailed.example.com\n", encoding="utf-8")
    storage = Storage(tmp_path / "checkpoint.sqlite3")
    try:
        storage.import_csv(source)
        for retry in (True, True, False):
            task = storage.claim_task()
            assert task is not None
            storage.fail_task(task["domain_key"], "failed", retry=retry)
        assert storage.claim_task() is None

        assert storage.reset_failed() == 1
        retried = storage.claim_task()
        assert retried is not None
        assert retried["attempts"] == 0
    finally:
        storage.close()
