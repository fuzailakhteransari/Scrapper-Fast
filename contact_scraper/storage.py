from __future__ import annotations

import csv
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import SiteResult
from .utils import normalize_url, site_key


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS site_tasks (
                    domain_key TEXT PRIMARY KEY,
                    input_url TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_site_tasks_status
                    ON site_tasks(status, updated_at);

                CREATE TABLE IF NOT EXISTS input_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_file TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    website_column TEXT NOT NULL,
                    website TEXT,
                    normalized_url TEXT,
                    domain_key TEXT,
                    row_json TEXT NOT NULL,
                    import_error TEXT,
                    UNIQUE(source_file, row_number)
                );
                CREATE INDEX IF NOT EXISTS idx_input_rows_domain
                    ON input_rows(domain_key);

                CREATE TABLE IF NOT EXISTS input_sources (
                    source_file TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    website_column TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def close(self) -> None:
        self._conn.close()

    def reset_running(self) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE site_tasks
                SET status='pending', updated_at=CURRENT_TIMESTAMP
                WHERE status='running'
                """
            )
            return cursor.rowcount

    def reset_all_tasks(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE site_tasks
                SET status='pending', attempts=0, result_json=NULL,
                    last_error=NULL, updated_at=CURRENT_TIMESTAMP
                """
            )

    def reset_failed(self) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE site_tasks
                SET status='pending', attempts=0, last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE status='failed'
                """
            )
            return cursor.rowcount

    @staticmethod
    def _fingerprint(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def import_csv(
        self,
        csv_path: Path,
        website_column: str | None = None,
        encoding: str = "utf-8-sig",
    ) -> tuple[int, int, str]:
        source = str(csv_path.resolve())
        fingerprint = self._fingerprint(csv_path)
        source_meta = self._conn.execute(
            """
            SELECT fingerprint, website_column
            FROM input_sources WHERE source_file=?
            """,
            (source,),
        ).fetchone()
        if source_meta and source_meta["fingerprint"] == fingerprint:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count,
                       SUM(CASE WHEN import_error IS NOT NULL
                                AND import_error != '' THEN 1 ELSE 0 END) AS invalid
                FROM input_rows WHERE source_file=?
                """,
                (source,),
            ).fetchone()
            return (
                int(row["count"]),
                int(row["invalid"] or 0),
                source_meta["website_column"],
            )

        imported = 0
        invalid = 0
        with csv_path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("CSV has no header row")
            selected = self._select_website_column(reader.fieldnames, website_column)
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM input_rows WHERE source_file=?", (source,)
                )
                for row_number, row in enumerate(reader, start=2):
                    website = str(row.get(selected, "") or "").strip()
                    normalized = ""
                    key = ""
                    error = ""
                    try:
                        normalized = normalize_url(website)
                        key = site_key(normalized)
                        self._conn.execute(
                            """
                            INSERT INTO site_tasks(
                                domain_key, input_url, normalized_url, status
                            ) VALUES (?, ?, ?, 'pending')
                            ON CONFLICT(domain_key) DO NOTHING
                            """,
                            (key, website, normalized),
                        )
                    except (ValueError, TypeError) as exc:
                        error = str(exc)
                        invalid += 1
                    self._conn.execute(
                        """
                        INSERT INTO input_rows(
                            source_file, row_number, website_column, website,
                            normalized_url, domain_key, row_json, import_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source,
                            row_number,
                            selected,
                            website,
                            normalized,
                            key,
                            json.dumps(row, ensure_ascii=False),
                            error,
                        ),
                    )
                    imported += 1
                self._conn.execute(
                    """
                    INSERT INTO input_sources(
                        source_file, fingerprint, website_column, updated_at
                    ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(source_file) DO UPDATE SET
                        fingerprint=excluded.fingerprint,
                        website_column=excluded.website_column,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (source, fingerprint, selected),
                )
        return imported, invalid, selected

    @staticmethod
    def _select_website_column(
        fieldnames: list[str], requested: str | None
    ) -> str:
        if requested:
            for field in fieldnames:
                if field.casefold() == requested.casefold():
                    return field
            raise ValueError(f"website column not found: {requested}")
        preferred = (
            "website",
            "website url",
            "website_url",
            "url",
            "domain",
            "site",
        )
        lookup = {field.casefold().strip(): field for field in fieldnames}
        for candidate in preferred:
            if candidate in lookup:
                return lookup[candidate]
        raise ValueError(
            "Could not auto-detect website column. Use --website-column."
        )

    def claim_task(self, max_attempts: int = 3) -> dict[str, Any] | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT domain_key, input_url, normalized_url, attempts
                FROM site_tasks
                WHERE status='pending' AND attempts < ?
                ORDER BY updated_at, domain_key
                LIMIT 1
                """,
                (max_attempts,),
            ).fetchone()
            if row is None:
                return None
            cursor = self._conn.execute(
                """
                UPDATE site_tasks
                SET status='running', attempts=attempts+1,
                    updated_at=CURRENT_TIMESTAMP
                WHERE domain_key=? AND status='pending'
                """,
                (row["domain_key"],),
            )
            if cursor.rowcount != 1:
                return None
            return dict(row)

    def complete_task(self, key: str, result: SiteResult) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE site_tasks
                SET status=?, result_json=?, last_error=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE domain_key=?
                """,
                (
                    "completed" if result.status in {"ok", "partial"} else "failed",
                    json.dumps(result.to_dict(), ensure_ascii=False),
                    " | ".join(result.errors)[:2000],
                    key,
                ),
            )

    def fail_task(self, key: str, error: str, retry: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE site_tasks
                SET status=?, last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE domain_key=?
                """,
                ("pending" if retry else "failed", error[:2000], key),
            )

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS count FROM site_tasks GROUP BY status"
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts

    def result_rows(self, source_file: str):
        cursor = self._conn.execute(
            """
            SELECT i.*, t.status AS task_status, t.attempts, t.result_json,
                   t.last_error
            FROM input_rows i
            LEFT JOIN site_tasks t ON t.domain_key=i.domain_key
            WHERE i.source_file=?
            ORDER BY i.row_number
            """,
            (source_file,),
        )
        yield from cursor
