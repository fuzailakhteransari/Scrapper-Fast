from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .accuracy import CleanExportOptions
from .config import Settings
from .crawler import SiteCrawler
from .fetchers import FetchManager
from .storage import Storage

LOGGER = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class RunControl:
    pause_requested: threading.Event = field(default_factory=threading.Event)
    stop_requested: threading.Event = field(default_factory=threading.Event)

    async def wait_until_runnable(self) -> bool:
        while self.pause_requested.is_set() and not self.stop_requested.is_set():
            await asyncio.sleep(0.2)
        return not self.stop_requested.is_set()


async def run_workers(
    storage: Storage,
    settings: Settings,
    *,
    control: RunControl | None = None,
    on_event: ProgressCallback | None = None,
    selected_kinds: set[str] | None = None,
    crawl_mode: str = "full",
    clean_options: CleanExportOptions | None = None,
) -> dict[str, int]:
    control = control or RunControl()
    fetcher = FetchManager(settings)
    crawler = SiteCrawler(
        settings,
        fetcher,
        selected_kinds=selected_kinds,
        crawl_mode=crawl_mode,
        clean_options=clean_options,
    )
    await fetcher.start()
    processed = 0
    processed_lock = asyncio.Lock()
    active: set[str] = set()
    active_lock = asyncio.Lock()

    def emit(event: str, **payload: Any) -> None:
        if on_event:
            on_event({"event": event, **payload})

    async def worker(worker_id: int) -> None:
        nonlocal processed
        while True:
            if not await control.wait_until_runnable():
                return
            task = storage.claim_task(max_attempts=3)
            if task is None:
                return
            key = task["domain_key"]
            async with active_lock:
                active.add(key)
                emit(
                    "started",
                    domain=key,
                    active=sorted(active),
                    counts=storage.counts(),
                )
            try:
                result = await crawler.crawl(
                    task["input_url"], task["normalized_url"]
                )
                storage.complete_task(key, result)
                if result.status in {"ok", "partial"}:
                    emit(
                        "completed",
                        domain=key,
                        result=result,
                        counts=storage.counts(),
                    )
                else:
                    emit(
                        "failed",
                        domain=key,
                        error=" | ".join(result.errors) or "scrape failed",
                        result=result,
                        counts=storage.counts(),
                    )
            except asyncio.CancelledError:
                storage.fail_task(key, "cancelled", retry=True)
                raise
            except Exception as exc:
                attempts = int(task["attempts"]) + 1
                retry = attempts < 3
                storage.fail_task(
                    key, f"{type(exc).__name__}: {exc}", retry=retry
                )
                LOGGER.exception(
                    "worker %d failed for %s",
                    worker_id,
                    key,
                    extra={"domain": key, "error": str(exc)},
                )
                emit("failed", domain=key, error=str(exc), counts=storage.counts())
            finally:
                async with active_lock:
                    active.discard(key)
                    emit("active", active=sorted(active), counts=storage.counts())
                async with processed_lock:
                    processed += 1
                    if processed % 25 == 0:
                        counts = storage.counts()
                        LOGGER.info(
                            "progress: %d/%d domains finished or attempted",
                            counts.get("completed", 0) + counts.get("failed", 0),
                            counts.get("total", 0),
                        )

    try:
        emit("running", counts=storage.counts())
        await asyncio.gather(
            *(worker(index + 1) for index in range(settings.concurrency))
        )
    finally:
        await fetcher.close()
    counts = storage.counts()
    emit(
        "stopped" if control.stop_requested.is_set() else "finished",
        counts=counts,
    )
    return counts
