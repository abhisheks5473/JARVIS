"""Long-term memory.

The guide is right that nearly everyone starts with a vector database and
nearly everyone regrets it. This is Level 2: SQLite with an FTS5 index.
Keyword search is fast, free, deterministic, and -- the part that matters at
3am -- debuggable. You can open the file and read exactly what it knows.

Two access patterns, because they answer different questions:

  * **Injection.** A small set of high-importance facts goes into the system
    instruction every turn. A few hundred facts is only a few thousand
    tokens, and it means JARVIS knows who you are without spending a call.
  * **Search.** Everything else is reachable through a `search_memory` tool,
    so the context stays small while the archive can grow.

Conversation history is deliberately separate from this. Memory is what
should outlive the conversation; history is the conversation.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from .. import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fact       TEXT    NOT NULL,
    category   TEXT    DEFAULT 'general',
    importance INTEGER DEFAULT 1,
    source     TEXT    DEFAULT 'user',
    created_at REAL    NOT NULL,
    created_on TEXT    NOT NULL,
    last_used  REAL    DEFAULT 0,
    use_count  INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
USING fts5(fact, category, content=facts, content_rowid=id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, fact, category)
    VALUES (new.id, new.fact, new.category);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact, category)
    VALUES ('delete', old.id, old.fact, old.category);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact, category)
    VALUES ('delete', old.id, old.fact, old.category);
    INSERT INTO facts_fts(rowid, fact, category)
    VALUES (new.id, new.fact, new.category);
END;

CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    summary    TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL NOT NULL,
    turns      INTEGER DEFAULT 0,
    created_on TEXT NOT NULL
);
"""

# FTS5 treats these as syntax. Someone asking to remember "C++ (the language)"
# should not produce a query parse error.
_FTS_SPECIAL = re.compile(r'["*():^\-]')


@dataclass
class Fact:
    id: int
    fact: str
    category: str
    importance: int
    created_on: str

    def __str__(self) -> str:
        return self.fact


class MemoryStore:
    def __init__(self, db_path=None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path or config.MEMORY_DB), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ write
    def remember(
        self,
        fact: str,
        category: str = "general",
        importance: int = 1,
        source: str = "user",
    ) -> dict:
        """Store one fact, or update it if we already knew something like it.

        Deduplication matters more than it looks: without it, "remember I use
        Windows" said three times becomes three facts, all injected into every
        prompt, all costing tokens forever.
        """
        cleaned = " ".join(fact.split())
        if not cleaned:
            return {"error": "empty fact"}

        with self._lock:
            existing = self._conn.execute(
                "SELECT id, fact FROM facts WHERE LOWER(fact) = LOWER(?)", (cleaned,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE facts SET importance = MAX(importance, ?) WHERE id = ?",
                    (importance, existing["id"]),
                )
                self._conn.commit()
                return {"stored": cleaned, "id": existing["id"], "already_known": True}

            now = time.time()
            cursor = self._conn.execute(
                "INSERT INTO facts (fact, category, importance, source, created_at,"
                " created_on) VALUES (?,?,?,?,?,?)",
                (
                    cleaned,
                    category,
                    max(1, min(int(importance), 5)),
                    source,
                    now,
                    datetime.now().astimezone().strftime("%Y-%m-%d"),
                ),
            )
            self._conn.commit()
            return {"stored": cleaned, "id": cursor.lastrowid, "already_known": False}

    def forget(self, fact_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------ read
    def search(self, query: str, limit: int = 8) -> list[Fact]:
        """Full-text search. Falls back to LIKE if the FTS query is unparseable."""
        cleaned = _FTS_SPECIAL.sub(" ", query).strip()
        if not cleaned:
            return []

        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT f.id, f.fact, f.category, f.importance, f.created_on"
                    " FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid"
                    " WHERE facts_fts MATCH ?"
                    " ORDER BY bm25(facts_fts), f.importance DESC LIMIT ?",
                    (cleaned, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = self._conn.execute(
                    "SELECT id, fact, category, importance, created_on FROM facts"
                    " WHERE fact LIKE ? ORDER BY importance DESC LIMIT ?",
                    (f"%{cleaned}%", limit),
                ).fetchall()

            if rows:
                self._conn.executemany(
                    "UPDATE facts SET use_count = use_count + 1, last_used = ?"
                    " WHERE id = ?",
                    [(time.time(), r["id"]) for r in rows],
                )
                self._conn.commit()

        return [Fact(**dict(r)) for r in rows]

    def top_facts(self, limit: int = 40) -> list[str]:
        """The facts worth injecting into every system instruction.

        Ranked by importance first, then by how often they have actually been
        useful. Facts nobody ever needs sink and stop costing tokens.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT fact FROM facts"
                " ORDER BY importance DESC, use_count DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["fact"] for r in rows]

    def all_facts(self, limit: int = 500) -> list[Fact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, fact, category, importance, created_on FROM facts"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Fact(**dict(r)) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    # ------------------------------------------------------------ episodes
    def save_episode(self, summary: str, started_at: float, turns: int) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO episodes (summary, started_at, ended_at, turns,"
                " created_on) VALUES (?,?,?,?,?)",
                (
                    summary,
                    started_at,
                    time.time(),
                    turns,
                    datetime.now().astimezone().strftime("%Y-%m-%d"),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def recent_episodes(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT summary, created_on, turns FROM episodes"
                " ORDER BY ended_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


memory = MemoryStore()
