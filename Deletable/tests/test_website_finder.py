import csv
from pathlib import Path

import pytest

from contact_scraper.web_app import create_app
from contact_scraper.website_finder import (
    Verification,
    WebsiteCandidate,
    WebsiteMatch,
    WebsiteFinderSettings,
    _search_query,
    resolve_company_csv,
    score_candidate,
)


def test_score_prefers_matching_official_domain():
    candidate = WebsiteCandidate(
        url="https://redpeaktechnical.com/",
        source="clearbit",
        title="Red Peak Technical Services",
        snippet="Engineering and field technical services",
        source_score=0.12,
    )
    verification = Verification(
        url=candidate.url,
        final_url=candidate.url,
        title="Red Peak Technical Services",
        description="Red Peak Technical Services provides technical services.",
        text_sample="Contact Red Peak Technical Services for support.",
        http_status=200,
        ok=True,
    )

    score, reason = score_candidate(
        "RED PEAK TECHNICAL SERVICES, LLC",
        "",
        candidate,
        verification,
    )

    assert score >= 0.62
    assert "domain=" in reason


def test_score_penalizes_parked_domain():
    candidate = WebsiteCandidate(
        url="https://redpeaktechnical.com/",
        source="searxng",
        title="Red Peak Technical Services",
        snippet="Official website",
        source_score=0.18,
    )
    verification = Verification(
        url=candidate.url,
        final_url=candidate.url,
        title="Red Peak Technical Services",
        description="Buy this domain today",
        text_sample="This domain may be for sale.",
        http_status=200,
        ok=True,
        parked=True,
    )

    score, _ = score_candidate(
        "RED PEAK TECHNICAL SERVICES, LLC",
        "",
        candidate,
        verification,
    )

    assert score < 0.62


def test_score_boosts_structured_data_and_source_agreement():
    candidate = WebsiteCandidate(
        url="https://exampleco.com/",
        source="searxng",
        title="Example Co",
        snippet="",
        source_score=0.16,
        source_count=3,
    )
    verification = Verification(
        url=candidate.url,
        final_url=candidate.url,
        title="Example Co",
        description="",
        text_sample="",
        http_status=200,
        ok=True,
        structured_names=["Example Company Pty Ltd"],
        structured_urls=["https://exampleco.com/"],
    )

    score, reason = score_candidate("Example Company", "", candidate, verification)

    assert score >= 0.62
    assert "agreement=3" in reason
    assert "structured=" in reason


def test_search_query_quotes_company_and_excludes_directories():
    query = _search_query("Red Peak Technical Services, LLC", "Dallas TX")

    assert '"Red Peak Technical Services, LLC"' in query
    assert "Dallas TX" in query
    assert '"official website"' in query
    assert "-site:linkedin.com" in query
    assert "-site:zoominfo.com" in query


def test_settings_read_searxng_base_url(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "https://search.example.com/")

    settings = WebsiteFinderSettings.from_env()

    assert settings.searxng_base_url == "https://search.example.com"


class FakeFinder:
    def __init__(self, settings):
        self.settings = settings

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def find(self, company, location=""):
        domain = company.casefold().replace(" ", "")
        return WebsiteMatch(
            company=company,
            location=location,
            website=f"https://{domain}.com/",
            status="found",
            confidence=0.91,
            source="test",
            reason="fake resolver",
        )


@pytest.mark.asyncio
async def test_resolve_company_csv_writes_website_column(tmp_path: Path, monkeypatch):
    import contact_scraper.website_finder as finder_module

    monkeypatch.setattr(finder_module, "WebsiteFinder", FakeFinder)
    source = tmp_path / "companies.csv"
    source.write_text(
        "Company,Location\nRed Peak Technical Services,Dallas TX\n",
        encoding="utf-8",
    )

    output = await resolve_company_csv(source, tmp_path / "resolved.csv")

    with output.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["Website"] == "https://redpeaktechnicalservices.com/"
    assert rows[0]["website_finder_status"] == "found"
    assert rows[0]["website_source"] == "test"


def test_ui_upload_accepts_company_csv(tmp_path: Path):
    app = create_app(tmp_path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/upload",
            data={
                "file": (
                    __import__("io").BytesIO(b"Company,Location\nAcme Corp,Austin\n"),
                    "companies.csv",
                )
            },
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    data = response.get_json()
    assert data["inputType"] == "company"
    assert data["companyColumn"] == "Company"
    assert data["locationColumn"] == "Location"
    assert data["validCount"] == 1
