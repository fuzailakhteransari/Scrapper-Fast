# Lightning Contact Scraper

High-throughput scraper for publicly available website contact data:

- Company-name to official-website finding with confidence scoring
- Email addresses, including common obfuscation and Cloudflare email protection
- Validated international phone numbers
- Facebook, Instagram, LinkedIn, X, YouTube, TikTok, Pinterest, and Threads links
- Physical address and company description when available
- CSV and optional Google Sheets output
- SQLite checkpoints for automatic resume
- Direct HTTP first, Bright Data Web Unlocker second, Browser API last

The scraper does not log in, bypass private pages, or collect restricted content.

## Install

Python 3.11 or newer is required.

```powershell
.\setup.ps1
```

Credentials and runtime defaults are read from `.env`. Keep that file private.
Use `.env.example` as the template on another machine.

## Input

Create a CSV with either:

- a `Website`, `URL`, `Domain`, or `Site` column; or
- a `Company` / `Company Name` column and, optionally, `Location`, `City`,
  `State`, `Address`, or `Country`.

Other columns are preserved in the result.

```csv
Company,Website
Example,example.com
Python,python.org
```

For a differently named column, pass `--website-column "Company Website"`.

Company-name input example:

```csv
Company,Location
RED PEAK TECHNICAL SERVICES, LLC,Dallas TX
Python Software Foundation,Wilmington DE
```

## Run

```powershell
.\run_scraper.ps1 -InputCsv input.csv
```

When no website column is present, the CLI automatically runs the website
finder first and writes `output/<input>_resolved_websites.csv`. You can also
force this step:

```powershell
.\.venv\Scripts\python.exe -m contact_scraper companies.csv --find-websites
.\.venv\Scripts\python.exe -m contact_scraper companies.csv --find-websites --company-column "Business Name" --location-column "City"
```

## Local Web UI

Double-click:

```text
Launch Scraper UI.cmd
```

The launcher starts the server silently and opens the app automatically. If
the default port is busy, it selects another local port. It also reuses an
already-running scraper server when its build matches the current files. After
an update, a stale server is not reused.

Terminal alternative:

```powershell
.\run_ui.ps1
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). The UI supports CSV
upload or directly pasted website/company lists, all-data or focused extraction modes,
configurable clean CSV columns, phone geography/format controls, Fast Mode or
Full Website Scan, live progress and ETA, Pause/Resume/Stop controls, summary
counts, and downloadable result files.

CSV upload also accepts company-name files. If a website column is missing but
a company column is found, the UI first finds likely official websites, adds
`Website`, `website_finder_status`, `website_confidence`, `website_source`,
`website_reason`, and `website_top_candidates`, then scrapes only rows with a
confident website.

## Website Finder Accuracy

The old single-source approach, for example calling Clearbit autocomplete from
Google Sheets, is fast but weak because autocomplete can return similarly named
companies, directories, old brands, or unrelated domains. The new resolver uses
a free-first evidence pipeline:

1. Collect candidates from free public sources: Clearbit autocomplete, Wikidata
   official website claims, DuckDuckGo Instant Answer data, cautious domain
   guesses, and an optional SearXNG metasearch instance.
2. Shape search queries toward official sites by quoting the company name,
   adding the optional location, asking for the official website, and excluding
   common directory/social hosts from the first-pass search results.
3. Optionally collect better SERP candidates when free/trial keys are present:
   `SEARXNG_BASE_URL`, `GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_ID`,
   `BRAVE_SEARCH_API_KEY`, `SERPAPI_API_KEY`, or `SEARCHAPI_API_KEY`.
4. Reject common directory/social/search-result hosts as final answers.
5. Fetch each candidate homepage directly and score real page evidence:
   company-name similarity, domain-name similarity, optional location match,
   source quality, redirect target, title, meta description, visible text, and
   JSON-LD organization names.
6. Penalize parked or for-sale domains even if they rank in search.
7. Return `found` only above the confidence threshold. Weak matches are marked
   `review` with blank `Website`, which prevents the contact scraper from
   confidently scraping the wrong company.

This is deliberately conservative. It may leave some rows for review, but that
is safer than poisoning the output with a wrong website.

Optional search-provider environment variables:

```dotenv
SEARXNG_BASE_URL=
GOOGLE_CSE_API_KEY=
GOOGLE_CSE_ID=
BRAVE_SEARCH_API_KEY=
SERPAPI_API_KEY=
SEARCHAPI_API_KEY=
```

If your goal is maximum accuracy while staying free, `SEARXNG_BASE_URL` is the
best upgrade because it lets the resolver use real search-result candidates
without making the whole workflow depend on paid search APIs.

Many public SearXNG instances disable JSON output. If an instance returns 403
to `/search?format=json`, use a different trusted instance or your own
deployment.

UI jobs run direct HTTP first with Bright Data disabled. Failed websites and
their reasons are exported immediately. When an enabled Bright Data fallback
is configured, the completion screen offers an optional retry of only the
failed websites; declining the retry does not use Bright Data. The command-line
scraper retains its direct-first automatic fallback behavior unless Bright Data
is disabled with its CLI flags.

Fast Mode stops each requested data category once it has been found. Full
Website Scan keeps checking relevant internal pages up to the selected request
limit. The UI also includes a Bright Data settings drawer. Its overrides are
stored locally in `ui_data/brightdata_settings.json`, which is ignored by Git;
secret values are never returned to the browser after saving.

Useful controls:

```powershell
.\run_scraper.ps1 -InputCsv input.csv -Concurrency 60 -MaxPages 5
.\run_scraper.ps1 -InputCsv input.csv -RetryFailed
.\.venv\Scripts\python.exe -m contact_scraper input.csv --fresh
.\.venv\Scripts\python.exe -m contact_scraper input.csv --no-browser
.\.venv\Scripts\python.exe -m contact_scraper input.csv --no-unlocker
.\run_scraper.ps1 -InputCsv input.csv -GoogleSheets
```

Interrupting the program is safe. Run the same command again and completed
domains are skipped using `output/checkpoint.sqlite3`.

## Outputs

- `*_results.csv`: original rows plus one set of structured result columns
- `*_clean.csv`: delivery-friendly columns selected in the UI, with one best
  value per field and optional phone geography/format filtering
- `*_contacts.csv`: one contact per row with source URL and confidence
- `*_review.csv`: invalid inputs, unresolved website matches, and scrape issues
- `checkpoint.sqlite3`: queue state and cached domain results
- `scraper.log`: rotating JSON diagnostic log

Duplicate domains are scraped once but exported for every matching input row.

## Google Sheets

Create a Google service account, enable the Google Sheets API, share the target
spreadsheet with the service account email, and set:

```dotenv
GOOGLE_SERVICE_ACCOUNT_JSON_PATH=C:\secure\service-account.json
GOOGLE_SPREADSHEET_ID=spreadsheet_id_here
GOOGLE_SHEET_TAB=Scraper Results
```

Then add `--google-sheets`. Local CSV and checkpoint writes happen before the
upload, so a Sheets failure does not lose scraped data.

## Operational Tuning

- Start with concurrency 30-50. Increase gradually while watching success rate,
  machine memory, and Bright Data spend.
- Web Unlocker is only called after direct requests fail, are blocked, or return
  a suspicious JavaScript shell.
- Browser API is limited separately and is not used for ordinary HTML pages.
- `robots.txt` is respected by default. Use `--ignore-robots` only when you have
  confirmed that doing so is appropriate for your use case.
- Phone parsing defaults to `US` for numbers without a country code. Set
  `SCRAPER_DEFAULT_PHONE_REGION` or use `--phone-region IN`, for example.

## Security

- Never commit `.env`, service-account JSON, checkpoint databases, or logs.
- Use product-scoped and expiring Bright Data credentials.
- Rotate credentials immediately if they are exposed in chat, screenshots,
  tickets, or source files.
- Input URLs resolving to local, private, reserved, or link-local networks are
  rejected.

## Tests

```powershell
.\.venv\Scripts\pytest.exe -q
```
