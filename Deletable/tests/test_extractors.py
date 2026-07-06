from contact_scraper.extractors import extract_page, rank_subpages


HTML = """
<!doctype html>
<html lang="en-US">
<head>
  <title>Acme Manufacturing</title>
  <meta name="description" content="Acme Manufacturing builds reliable industrial products for customers around the world.">
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@graph": [{
      "@type": "Organization",
      "email": "sales@acme.example",
      "telephone": "+1 415 555 2671",
      "sameAs": [
        "https://www.linkedin.com/company/acme-manufacturing/",
        "https://x.com/acme"
      ],
      "address": {
        "@type": "PostalAddress",
        "streetAddress": "123 Market Street",
        "addressLocality": "San Francisco",
        "addressRegion": "CA",
        "postalCode": "94105",
        "addressCountry": "US"
      }
    }]
  }
  </script>
</head>
<body>
  <a href="mailto:info@acme.example?subject=Hello">Email us</a>
  <a href="tel:+14155552671">Call</a>
  <span>support [at] acme [dot] example</span>
  <a href="/contact-us">Contact</a>
  <a href="/about">About</a>
  <a href="https://facebook.com/sharer/sharer.php?u=x">Share</a>
  <a href="https://facebook.com/acmeofficial/reviews">Facebook reviews</a>
  <a href="https://instagram.com/acme_official/">Instagram</a>
  <a href="https://instagram.com/p/ABC123/">Instagram post</a>
  <a href="https://x.com/acme/status/12345">Latest tweet</a>
</body>
</html>
"""


def test_extract_page_finds_and_validates_contacts():
    page = extract_page(
        HTML,
        "https://www.acme.example/",
        "acme.example",
        "US",
    )
    emails = {item.value for item in page.contacts if item.kind == "email"}
    phones = {item.value for item in page.contacts if item.kind == "phone"}
    socials = {
        (item.category, item.value)
        for item in page.contacts
        if item.kind == "social"
    }

    assert "info@acme.example" in emails
    assert "sales@acme.example" in emails
    assert "support@acme.example" in emails
    assert phones == {"+14155552671"}
    assert ("linkedin", "https://linkedin.com/company/acme-manufacturing") in socials
    assert ("twitter", "https://x.com/acme") in socials
    assert ("instagram", "https://instagram.com/acme_official") in socials
    assert ("facebook", "https://facebook.com/acmeofficial") in socials
    assert not any("sharer" in value for _, value in socials)
    assert not any("/status/" in value for _, value in socials)
    assert not any("/p/" in value for _, value in socials)
    assert not any("/reviews" in value for _, value in socials)
    assert page.address.startswith("123 Market Street")
    assert page.description.startswith("Acme Manufacturing")


def test_rank_subpages_prefers_contact_and_limits():
    links = [
        ("https://acme.example/blog/post", "News"),
        ("https://acme.example/about-us", "About our company"),
        ("https://acme.example/contact", "Get in touch"),
        ("https://acme.example/privacy", "Privacy"),
    ]
    ranked = rank_subpages(links, "https://acme.example", 3)
    assert ranked[0] == "https://acme.example/contact"
    assert "https://acme.example/about-us" in ranked
    assert len(ranked) == 3
