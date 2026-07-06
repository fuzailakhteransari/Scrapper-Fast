import pytest

from contact_scraper.utils import (
    canonical_link,
    domain_key,
    normalize_url,
    same_registered_domain,
    site_key,
)


def test_normalize_url_and_domain():
    assert normalize_url(" Example.COM/path/?utm_source=x&a=1#top") == (
        "https://example.com/path/?a=1"
    )
    assert domain_key("https://www.shop.example.co.uk/a") == "example.co.uk"
    assert site_key("https://www.shop.example.co.uk/a") == "shop.example.co.uk"


def test_same_domain_and_relative_link():
    base = "https://www.example.com/"
    assert canonical_link(base, "/contact") == "https://www.example.com/contact"
    assert same_registered_domain(base, "https://support.example.com/help")
    assert not same_registered_domain(base, "https://example.net/")


@pytest.mark.parametrize("value", ["", "mailto:a@example.com", "file:///etc/passwd"])
def test_rejects_invalid_input(value):
    with pytest.raises(ValueError):
        normalize_url(value)
