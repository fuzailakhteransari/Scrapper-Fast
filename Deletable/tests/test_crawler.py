import asyncio

from contact_scraper.config import Settings
from contact_scraper.crawler import SiteCrawler
from contact_scraper.accuracy import CleanExportOptions
from contact_scraper.models import FetchResult


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url, *, homepage, allow_browser):
        self.calls.append(url)
        html = self.pages.get(url, "")
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200 if html else 404,
            html=html,
            tier="test",
            error="" if html else "not found",
        )


HOME_WITH_ALL = """
<html lang="en-US">
  <head>
    <title>Example Company</title>
    <meta name="description" content="Example Company builds reliable software for businesses worldwide.">
  </head>
  <body>
    <a href="mailto:hello@acme.com">Email</a>
    <a href="tel:+14155552671">Call</a>
    <a href="https://linkedin.com/company/example-company">LinkedIn</a>
    <address>123 Market Street, San Francisco, CA 94105</address>
    <a href="/contact">Contact</a>
    <a href="/about">About</a>
    <a href="/team">Team</a>
  </body>
</html>
"""


def test_fast_mode_stops_after_homepage_fulfills_requested_data():
    fetcher = FakeFetcher({"https://example.com": HOME_WITH_ALL})
    crawler = SiteCrawler(
        Settings(max_pages=6),
        fetcher,
        selected_kinds={"email", "phone", "social", "address", "description"},
        crawl_mode="fast",
    )

    result = asyncio.run(
        crawler.crawl("example.com", "https://example.com")
    )

    assert fetcher.calls == ["https://example.com"]
    assert len(result.pages_scraped) == 1
    assert {contact.kind for contact in result.contacts} == {
        "email",
        "phone",
        "social",
    }
    assert result.address
    assert result.description


def test_fast_mode_only_visits_pages_needed_for_missing_categories():
    home = """
    <html><body>
      <a href="mailto:hello@acme.com">Email</a>
      <a href="/contact">Contact</a>
      <a href="/about">About</a>
      <a href="/team">Team</a>
    </body></html>
    """
    contact = """
    <html lang="en-US"><head>
      <meta name="description" content="Example Company serves customers globally with reliable software and support.">
    </head><body>
      <a href="tel:+14155552671">Call</a>
      <a href="https://instagram.com/examplecompany">Instagram</a>
      <address>123 Market Street, San Francisco, CA 94105</address>
    </body></html>
    """
    fetcher = FakeFetcher(
        {
            "https://example.com": home,
            "https://example.com/contact": contact,
            "https://example.com/about": "<p>Should not be needed</p>",
        }
    )
    crawler = SiteCrawler(
        Settings(max_pages=6),
        fetcher,
        selected_kinds={"email", "phone", "social", "address", "description"},
        crawl_mode="fast",
    )

    result = asyncio.run(
        crawler.crawl("example.com", "https://example.com")
    )

    assert fetcher.calls == [
        "https://example.com",
        "https://example.com/contact",
    ]
    assert len(result.pages_scraped) == 2


def test_full_scan_uses_the_request_limit_even_when_data_is_already_found():
    pages = {
        "https://example.com": HOME_WITH_ALL,
        "https://example.com/contact": "<p>Contact page</p>",
        "https://example.com/contact-us": "<p>Contact us page</p>",
        "https://example.com/about": "<p>About page</p>",
        "https://example.com/about-us": "<p>About us page</p>",
        "https://example.com/team": "<p>Team page</p>",
    }
    fetcher = FakeFetcher(pages)
    crawler = SiteCrawler(
        Settings(max_pages=4, per_site_concurrency=2),
        fetcher,
        selected_kinds={"social"},
        crawl_mode="full",
    )

    result = asyncio.run(
        crawler.crawl("example.com", "https://example.com")
    )

    assert len(fetcher.calls) == 4
    assert len(result.pages_scraped) == 4
    assert any(contact.kind == "social" for contact in result.contacts)


def test_fast_mode_requires_exact_selected_social_platform():
    home = """
    <html><body>
      <a href="https://linkedin.com/company/example-company">LinkedIn</a>
      <a href="/contact">Contact</a>
    </body></html>
    """
    contact = """
    <html><body>
      <a href="https://facebook.com/examplecompany">Facebook</a>
    </body></html>
    """
    fetcher = FakeFetcher(
        {
            "https://example.com": home,
            "https://example.com/contact": contact,
        }
    )
    crawler = SiteCrawler(
        Settings(max_pages=4),
        fetcher,
        selected_kinds={"social"},
        crawl_mode="fast",
        clean_options=CleanExportOptions(fields=("facebook",)),
    )

    result = asyncio.run(crawler.crawl("example.com", "https://example.com"))

    assert fetcher.calls == ["https://example.com", "https://example.com/contact"]
    assert any(
        contact.kind == "social" and contact.category == "facebook"
        for contact in result.contacts
    )


def test_fast_mode_uses_sitemap_when_homepage_has_no_candidate_links():
    home = "<html><body><p>No contact links here.</p></body></html>"
    robots = "Sitemap: https://example.com/sitemap.xml\n"
    sitemap = """
    <urlset>
      <url><loc>https://example.com/contact</loc></url>
      <url><loc>https://example.com/blog</loc></url>
    </urlset>
    """
    contact = """
    <html lang="en-US"><body>
      <a href="mailto:info@acme.com">Email</a>
    </body></html>
    """
    fetcher = FakeFetcher(
        {
            "https://example.com": home,
            "https://example.com/robots.txt": robots,
            "https://example.com/sitemap.xml": sitemap,
            "https://example.com/contact": contact,
        }
    )
    crawler = SiteCrawler(
        Settings(max_pages=5),
        fetcher,
        selected_kinds={"email"},
        crawl_mode="fast",
        clean_options=CleanExportOptions(fields=("email",)),
    )

    result = asyncio.run(crawler.crawl("example.com", "https://example.com"))

    assert "https://example.com/contact" in fetcher.calls
    assert any(contact.kind == "email" for contact in result.contacts)
