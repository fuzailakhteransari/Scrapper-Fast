import asyncio

from contact_scraper.config import Settings
from contact_scraper.models import SiteResult
from contact_scraper.runner import run_workers
from contact_scraper.storage import Storage


class DummyFetcher:
    def __init__(self, settings):
        self.settings = settings

    async def start(self):
        pass

    async def close(self):
        pass


class FailedCrawler:
    def __init__(
        self,
        settings,
        fetcher,
        selected_kinds=None,
        crawl_mode="full",
        clean_options=None,
    ):
        pass

    async def crawl(self, input_url, normalized_url):
        return SiteResult(
            input_url=input_url,
            normalized_url=normalized_url,
            domain="failed.example.com",
            status="failed",
            errors=["blocked"],
        )


def test_runner_emits_failed_event_for_failed_crawl(tmp_path, monkeypatch):
    import contact_scraper.runner as runner_module

    monkeypatch.setattr(runner_module, "FetchManager", DummyFetcher)
    monkeypatch.setattr(runner_module, "SiteCrawler", FailedCrawler)
    source = tmp_path / "input.csv"
    source.write_text("Website\nfailed.example.com\n", encoding="utf-8")
    storage = Storage(tmp_path / "checkpoint.sqlite3")
    events = []
    try:
        storage.import_csv(source)
        asyncio.run(
            run_workers(
                storage,
                Settings(concurrency=1),
                on_event=events.append,
            )
        )
        assert storage.counts()["failed"] == 1
        assert any(event["event"] == "failed" for event in events)
        assert not any(event["event"] == "completed" for event in events)
    finally:
        storage.close()
