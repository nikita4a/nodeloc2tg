"""SQLite-хранилище: дедупликация topic_id + состояние.

Таблица posted_topics:
    topic_id   INTEGER PRIMARY KEY
    posted_at  INTEGER (unix ts)
    status     TEXT    ('posted' | 'error' | 'skipped')
    title_orig TEXT
    title_ru   TEXT

Это гарантирует что одна тема не уйдёт в канал дважды
даже после перезапуска бота.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DB = Path(__file__).resolve().parent / "state.db"


class Storage:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = str(db_path)
        # check_same_thread=False — мы однопоточные, но requests иногда
        # дёргает из ниток; подстраховка дешёвая.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def _init(self) -> None:
        # Миграция v1 → v2: PK int topic_id не вмещал несколько источников.
        # v2: PK (source, topic_id). Старые записи = source 'nodeloc'.
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(posted_topics)")]
        if cols and "source" not in cols:
            self.conn.executescript("""
                ALTER TABLE posted_topics RENAME TO posted_topics_v1;
                CREATE TABLE posted_topics (
                    source     TEXT NOT NULL DEFAULT 'nodeloc',
                    topic_id   INTEGER NOT NULL,
                    posted_at  INTEGER NOT NULL,
                    status     TEXT NOT NULL,
                    title_orig TEXT,
                    title_ru   TEXT,
                    PRIMARY KEY (source, topic_id)
                );
                INSERT INTO posted_topics
                    SELECT 'nodeloc', topic_id, posted_at, status, title_orig, title_ru
                    FROM posted_topics_v1;
                DROP TABLE posted_topics_v1;
            """)
            log.info("state.db мигрирована на v2 (мульти-источники)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_topics (
                source     TEXT NOT NULL DEFAULT 'nodeloc',
                topic_id   INTEGER NOT NULL,
                posted_at  INTEGER NOT NULL,
                status     TEXT NOT NULL,
                title_orig TEXT,
                title_ru   TEXT,
                PRIMARY KEY (source, topic_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self.conn.commit()

    # ---------- meta (kv) ----------
    def get_meta(self, key: str, default: str = "") -> str:
        cur = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
        )
        self.conn.commit()

    def is_known(self, topic_id: int, source: str = "nodeloc") -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM posted_topics WHERE source = ? AND topic_id = ?",
            (source, topic_id),
        )
        return cur.fetchone() is not None

    def mark(self, topic_id: int, status: str,
             title_orig: str = "", title_ru: str = "",
             source: str = "nodeloc") -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO posted_topics
                (source, topic_id, posted_at, status, title_orig, title_ru)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source, topic_id, int(time.time()), status, title_orig, title_ru),
        )
        self.conn.commit()

    def count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM posted_topics")
        return cur.fetchone()[0]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
