import asyncio
import io
import time
from pathlib import Path

from contact_scraper.models import Evidence, SiteResult
from contact_scraper.web_app import create_app


async def fake_runner(
    storage,
    settings,
    *,
    control,
    on_event,
    selected_kinds,
    crawl_mode,
    clean_options=None,
):
    on_event({"event": "running", "counts": storage.counts()})
    while True:
        if not await control.wait_until_runnable():
            break
        task = storage.claim_task()
        if task is None:
            break
        key = task["domain_key"]
        on_event(
            {
                "event": "started",
                "domain": key,
                "active": [key],
                "counts": storage.counts(),
            }
        )
        contacts = []
        if "email" in selected_kinds:
            contacts.append(
                Evidence(
                    value=f"info@{key}",
                    kind="email",
                    source_url=task["normalized_url"],
                    source_type="mailto",
                    confidence=0.98,
                    category="generic",
                )
            )
        await asyncio.sleep(0.03)

        if "fail" in key:
            if settings.use_web_unlocker or settings.use_browser:
                status = "ok"
                errors = []
            else:
                status = "failed"
                errors = ["simulated direct-scraping failure"]
        else:
            status = "ok"
            errors = []

        result = SiteResult(
            input_url=task["input_url"],
            normalized_url=task["normalized_url"],
            final_url=task["normalized_url"],
            domain=key,
            status=status,
            fetch_tier="test",
            contacts=contacts if status == "ok" else [],
            pages_scraped=[task["normalized_url"]],
            errors=errors,
        )
        storage.complete_task(key, result)
        if status == "ok":
            on_event(
                {
                    "event": "completed",
                    "domain": key,
                    "result": result,
                    "counts": storage.counts(),
                }
            )
        else:
            on_event(
                {
                    "event": "failed",
                    "domain": key,
                    "error": "simulated failure",
                    "counts": storage.counts(),
                }
            )
        on_event(
            {"event": "active", "active": [], "counts": storage.counts()}
        )
    return storage.counts()


def _upload(client):
    payload = b"Company,Website\nOne,example.com\nTwo,python.org\n"
    response = client.post(
        "/api/upload",
        data={"file": (io.BytesIO(payload), "sites.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    return response.get_json()


def _upload_many(client, count=12):
    lines = ["Company,Website"]
    lines.extend(f"Site {index},site{index}.example.com" for index in range(count))
    response = client.post(
        "/api/upload",
        data={
            "file": (
                io.BytesIO(("\n".join(lines) + "\n").encode()),
                "many-sites.csv",
            )
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    return response.get_json()


def _paste(client, websites, mode="website"):
    response = client.post("/api/paste", json={"websites": websites, "mode": mode})
    assert response.status_code == 200
    return response.get_json()


async def _fake_resolve_company_csv(
    input_csv,
    output_csv,
    *,
    company_column=None,
    location_column=None,
    settings=None,
):
    rows = []
    with input_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        import csv

        reader = csv.DictReader(handle)
        for row in reader:
            company = row[company_column]
            slug = company.casefold().replace(" ", "").replace(",", "")
            rows.append(
                {
                    **row,
                    "Website": f"https://{slug}.com/",
                    "website_finder_status": "found",
                    "website_confidence": "0.91",
                    "website_source": "test",
                    "website_reason": "fake resolver",
                    "website_top_candidates": "",
                }
            )
        fieldnames = list(rows[0].keys()) if rows else ["Company", "Website"]
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output_csv


async def _fake_resolve_company_csv_partial(
    input_csv,
    output_csv,
    *,
    company_column=None,
    location_column=None,
    settings=None,
):
    rows = []
    with input_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        import csv

        reader = csv.DictReader(handle)
        for index, row in enumerate(reader):
            company = row[company_column]
            slug = company.casefold().replace(" ", "").replace(",", "")
            rows.append(
                {
                    **row,
                    "Website": f"https://{slug}.com/" if index == 0 else "",
                    "website_finder_status": "found" if index == 0 else "review",
                    "website_confidence": "0.91" if index == 0 else "0.41",
                    "website_source": "test",
                    "website_reason": "fake resolver",
                    "website_top_candidates": "",
                }
            )
        fieldnames = list(rows[0].keys()) if rows else ["Company", "Website"]
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output_csv


def _wait_for_job(client, job_id, terminal=True):
    deadline = time.time() + 5
    while time.time() < deadline:
        response = client.get(f"/api/job/{job_id}")
        assert response.status_code == 200
        job = response.get_json()
        if not terminal or job["status"] in {"completed", "completed_initial", "stopped", "failed"}:
            return job
        time.sleep(0.03)
    raise AssertionError("job did not reach expected state")


def test_ui_upload_run_and_download(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        page = client.get("/")
        assert page.status_code == 200
        assert b"Lightning Contact Scraper" in page.data
        health = client.get("/api/health")
        health_data = health.get_json()
        assert health_data["ok"] is True
        assert health_data["app"] == "lightning-contact-scraper"
        assert len(health_data["build"]) == 16

        upload = _upload(client)
        assert upload["rowCount"] == 2
        assert upload["uniqueCount"] == 2

        response = client.post(
            "/api/jobs",
            json={
                "uploadId": upload["id"],
                "mode": "email",
                "cleanFields": ["website", "email"],
                "cleanPhoneRegion": "AU",
                "cleanPhoneFormat": "international",
                "crawlMode": "fast",
                "maxPages": 3,
                "workers": 2,
            },
        )
        assert response.status_code == 202
        assert response.get_json()["crawlMode"] == "fast"
        assert response.get_json()["maxPages"] == 3
        job = _wait_for_job(client, response.get_json()["id"])
        assert job["status"] == "completed"
        assert job["completed"] == 2
        assert job["emails"] == 2
        assert job["phones"] == 0
        assert job["socials"] == 0
        assert job["downloads"]
        assert any(item["name"] == "Clean Leads CSV" for item in job["downloads"])

        results = next(
            item for item in job["downloads"] if item["name"] == "Main Results CSV"
        )
        download = client.get(results["url"])
        assert download.status_code == 200
        assert b"emails" in download.data
        assert b"phones" not in download.data.splitlines()[0]
        clean = next(
            item for item in job["downloads"] if item["name"] == "Clean Leads CSV"
        )
        clean_download = client.get(clean["url"])
        assert clean_download.status_code == 200
        assert b"Website,Email" in clean_download.data.splitlines()[0]
        assert b"Phone" not in clean_download.data.splitlines()[0]


def test_rejects_non_csv_and_unknown_mode(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"x"), "sites.txt")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400

        empty = client.post("/api/paste", json={"websites": "  \n  "})
        assert empty.status_code == 400

        invalid_websites = client.post(
            "/api/paste", json={"websites": "not a website\nstill invalid", "mode": "website"}
        )
        assert invalid_websites.status_code == 400

        company_paste = client.post(
            "/api/paste", json={"websites": "not a website\nstill invalid", "mode": "company"}
        )
        assert company_paste.status_code == 200
        assert company_paste.get_json()["inputType"] == "company"


def test_brightdata_settings_are_saved_without_returning_secrets(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/settings/brightdata",
            json={
                "unlockerZone": "unlocker_test",
                "apiKey": "secret-api-key",
                "browserUsername": "browser-user",
                "browserPassword": "secret-password",
                "proxyHost": "brd.superproxy.io",
                "proxyPort": 9222,
                "browserConcurrency": 4,
                "useWebUnlocker": True,
                "useBrowser": False,
            },
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["apiKeyConfigured"] is True
        assert data["passwordConfigured"] is True
        assert data["browserConcurrency"] == 4
        assert data["useBrowser"] is False
        assert "secret-api-key" not in response.get_data(as_text=True)
        assert "secret-password" not in response.get_data(as_text=True)

        loaded = client.get("/api/settings/brightdata")
        assert loaded.status_code == 200
        assert "apiKey" not in loaded.get_json()
        assert "browserPassword" not in loaded.get_json()

        saved_path = app.extensions["scraper_bright_settings_path"]
        saved = saved_path.read_text(encoding="utf-8")
        assert "secret-api-key" in saved
        assert "secret-password" in saved


def test_rejects_invalid_crawl_and_brightdata_settings(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        upload = _upload(client)
        bad_mode = client.post(
            "/api/jobs",
            json={"uploadId": upload["id"], "crawlMode": "endless"},
        )
        assert bad_mode.status_code == 400

        bad_host = client.post(
            "/api/settings/brightdata",
            json={"proxyHost": "https://bad-host", "proxyPort": 9222},
        )
        assert bad_host.status_code == 400


def test_paste_websites_deduplicates_and_runs(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        upload = _paste(
            client,
            "Website\nexample.com\nhttps://www.example.com\npython.org, w3.org\n"
            "bad value\tgithub.com\n",
        )
        assert upload["rowCount"] == 6
        assert upload["validCount"] == 5
        assert upload["uniqueCount"] == 4
        assert upload["invalidCount"] == 1
        assert upload["originalName"] == "pasted_websites.csv"

        response = client.post(
            "/api/jobs",
            json={"uploadId": upload["id"], "mode": "email", "workers": 2},
        )
        assert response.status_code == 202
        job = _wait_for_job(client, response.get_json()["id"])
        assert job["status"] == "completed"
        assert job["completed"] == 4
        assert job["emails"] == 4
        assert job["downloads"]


def test_paste_companies_is_detected(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        upload = _paste(
            client,
            "Company,Location\nRed Peak Technical Services,Dallas\nAcme LLC,Austin\n",
            mode="company",
        )
        assert upload["inputType"] == "company"
        assert upload["companyColumn"] == "Company"
        assert upload["locationColumn"] == "Location"
        assert upload["originalName"] == "pasted_companies.csv"


def test_website_only_job_exports_website_csv(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        upload = _upload(client)
        response = client.post(
            "/api/jobs",
            json={"uploadId": upload["id"], "selectedKinds": ["website"]},
        )
        assert response.status_code == 202
        job = _wait_for_job(client, response.get_json()["id"])
        assert job["status"] == "completed"
        assert job["websites"] == 2
        assert job["downloads"]
        assert [item["name"] for item in job["downloads"]] == [
            "Website Validation CSV",
            "Clean Leads CSV",
        ]


def test_company_csv_can_find_websites_only(tmp_path: Path, monkeypatch):
    import contact_scraper.web_app as web_app_module

    monkeypatch.setattr(
        web_app_module, "resolve_company_csv", _fake_resolve_company_csv
    )
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/upload",
            data={
                "file": (
                    io.BytesIO(b"Company,Location\nRed Peak,Dallas\nAcme,Austin\n"),
                    "companies.csv",
                )
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        upload = response.get_json()
        assert upload["inputType"] == "company"

        started = client.post(
            "/api/jobs",
            json={"uploadId": upload["id"], "selectedKinds": ["website"]},
        )
        assert started.status_code == 202
        job = _wait_for_job(client, started.get_json()["id"])
        assert job["status"] == "completed"
        assert job["websites"] == 2
        assert job["downloads"]
        assert job["downloads"][0]["name"] == "Resolved Websites CSV"
        assert any(item["name"] == "Clean Leads CSV" for item in job["downloads"])


def test_company_contact_run_counts_lookup_failures(tmp_path: Path, monkeypatch):
    import contact_scraper.web_app as web_app_module

    monkeypatch.setattr(
        web_app_module, "resolve_company_csv", _fake_resolve_company_csv_partial
    )
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/upload",
            data={
                "file": (
                    io.BytesIO(b"Company,Location\nRed Peak,Dallas\nAcme,Austin\n"),
                    "companies.csv",
                )
            },
            content_type="multipart/form-data",
        )
        upload = response.get_json()
        started = client.post(
            "/api/jobs",
            json={"uploadId": upload["id"], "selectedKinds": ["email"]},
        )
        job = _wait_for_job(client, started.get_json()["id"])
        assert job["status"] == "completed"
        assert job["websites"] == 1
        assert job["lookupFailed"] == 1
        assert job["failedTotal"] == 1
        assert job["completed"] == 2
        assert job["total"] == 2


def test_pause_resume_and_safe_stop(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        upload = _upload_many(client)
        response = client.post(
            "/api/jobs",
            json={"uploadId": upload["id"], "mode": "email", "workers": 1},
        )
        job_id = response.get_json()["id"]

        deadline = time.time() + 2
        while time.time() < deadline:
            job = client.get(f"/api/job/{job_id}").get_json()
            if job["status"] == "running":
                break
            time.sleep(0.01)
        assert job["status"] == "running"

        paused = client.post(f"/api/job/{job_id}/pause")
        assert paused.status_code == 200
        assert paused.get_json()["status"] == "paused"

        resumed = client.post(f"/api/job/{job_id}/resume")
        assert resumed.status_code == 200
        assert resumed.get_json()["status"] == "running"

        stopped = client.post(f"/api/job/{job_id}/stop")
        assert stopped.status_code == 200
        assert stopped.get_json()["status"] == "stopping"

        final = _wait_for_job(client, job_id)
        assert final["status"] == "stopped"
        assert final["completed"] < final["total"]
        assert final["downloads"]

        upload = _upload(client)
        response = client.post(
            "/api/jobs",
            json={"uploadId": upload["id"], "mode": "unknown"},
        )
        assert response.status_code == 400


def test_selected_kinds_and_retry_flow(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        # Save Bright Data credentials to allow completed_initial state transition
        client.post(
            "/api/settings/brightdata",
            json={
                "unlockerZone": "zone",
                "apiKey": "key",
                "browserUsername": "user",
                "browserPassword": "pass",
            }
        )

        # Upload two websites: one that will fail initially and one that will succeed
        upload = _paste(client, "failingsite.com\nsuccesssite.com")
        
        # Start a job with selectedKinds
        response = client.post(
            "/api/jobs",
            json={
                "uploadId": upload["id"],
                "selectedKinds": ["email", "phone"],
                "crawlMode": "fast",
                "maxPages": 3,
                "workers": 2,
            },
        )
        assert response.status_code == 202
        job_id = response.get_json()["id"]

        # Wait for the initial run to finish
        job = _wait_for_job(client, job_id)
        
        # The job should be in 'completed_initial' status because there is a failure and Bright Data is configured
        assert job["status"] == "completed_initial"
        assert job["stage"] == "initial"
        assert job["completed"] == 2
        assert job["success"] == 1
        assert job["failed"] == 1
        assert job["recovered"] == 0

        # Now, request Bright Data retry
        retry_response = client.post(f"/api/job/{job_id}/retry_brightdata")
        assert retry_response.status_code == 200
        
        # Wait for the retry run to finish
        job = _wait_for_job(client, job_id)
        
        # The job should now be fully 'completed' and failingsite.com should be recovered
        assert job["status"] == "completed"
        assert job["stage"] == "retry"
        assert job["completed"] == 2
        assert job["success"] == 2
        assert job["failed"] == 0
        assert job["recovered"] == 1


def test_selected_kinds_and_declined_retry(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        # Save Bright Data credentials
        client.post(
            "/api/settings/brightdata",
            json={
                "unlockerZone": "zone",
                "apiKey": "key",
                "browserUsername": "user",
                "browserPassword": "pass",
            }
        )

        upload = _paste(client, "failingsite.com\nsuccesssite.com")
        response = client.post(
            "/api/jobs",
            json={
                "uploadId": upload["id"],
                "selectedKinds": ["email"],
                "crawlMode": "fast",
                "maxPages": 3,
                "workers": 2,
            },
        )
        assert response.status_code == 202
        job_id = response.get_json()["id"]

        # Wait for the initial run to finish
        job = _wait_for_job(client, job_id)
        assert job["status"] == "completed_initial"

        # Decline the retry
        no_retry_response = client.post(f"/api/job/{job_id}/no_retry")
        assert no_retry_response.status_code == 200
        data = no_retry_response.get_json()
        assert data["status"] == "completed"
        assert data["stage"] == "initial"
        assert data["failed"] == 1


def test_disabled_brightdata_does_not_offer_retry(tmp_path: Path):
    app = create_app(tmp_path, job_runner=fake_runner)
    app.config["TESTING"] = True
    with app.test_client() as client:
        saved = client.post(
            "/api/settings/brightdata",
            json={
                "unlockerZone": "zone",
                "apiKey": "key",
                "browserUsername": "user",
                "browserPassword": "pass",
                "proxyHost": "brd.superproxy.io",
                "proxyPort": 9222,
                "browserConcurrency": 3,
                "useWebUnlocker": False,
                "useBrowser": False,
            },
        )
        assert saved.status_code == 200
        upload = _paste(client, "failingsite.com")
        response = client.post(
            "/api/jobs",
            json={"uploadId": upload["id"], "selectedKinds": ["email"]},
        )
        job = _wait_for_job(client, response.get_json()["id"])
        assert job["status"] == "completed"
        assert job["failed"] == 1
