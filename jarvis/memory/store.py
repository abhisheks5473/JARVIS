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

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts
USING fts5(summary, content=episodes, content_rowid=id);

CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary) VALUES (new.id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary)
    VALUES ('delete', old.id, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary)
    VALUES ('delete', old.id, old.summary);
    INSERT INTO episodes_fts(rowid, summary) VALUES (new.id, new.summary);
END;
"""

# FTS5 treats these as syntax. Someone asking to remember "C++ (the language)"
# should not produce a query parse error.
_FTS_SPECIAL = re.compile(r'["*():^\-]')

# Words that appear in nearly every conversation. Left in, they match
# everything and the ranking becomes meaningless.
_STOPWORDS = {
    "the", "and", "for", "was", "were", "you", "your", "yours", "our", "ours",
    "that", "this", "these", "those", "with", "what", "when", "where", "which",
    "who", "whom", "how", "why", "did", "does", "done", "have", "has", "had",
    "are", "not", "but", "can", "could", "would", "should", "about", "into",
    "from", "they", "them", "their", "there", "here", "just", "then", "than",
    "some", "any", "all", "again", "back", "also", "said", "say", "says",
    "tell", "told", "ask", "asked", "please", "thanks", "okay", "yes", "sir",
    "jarvis", "conversation", "talked", "talking", "discussed", "remember",
}


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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """One-off work that a CREATE TABLE IF NOT EXISTS cannot express.

        Episodes recorded before the search index existed are invisible to it
        until the index is rebuilt -- and those are the oldest conversations,
        the ones most worth being able to recall.

        The obvious check for "is the index empty" does not work here. On an
        FTS5 table with `content=`, COUNT(*) reads through to the content
        table, so it reports the number of episodes and never zero. That
        silently skipped the backfill, and search returned nothing for words
        plainly present in the text. A marker row is used instead, and the
        index is rebuilt through FTS5's own 'rebuild' command.
        """
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        done = self._conn.execute(
            "SELECT value FROM meta WHERE key='episodes_fts_built'"
        ).fetchone()
        if done:
            return
        self._conn.execute("INSERT INTO episodes_fts(episodes_fts) VALUES('rebuild')")
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('episodes_fts_built','1')"
        )

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
    def save_episode(
        self,
        summary: str,
        started_at: float,
        turns: int,
        episode_id: int | None = None,
    ) -> int:
        """Write a conversation summary, replacing an earlier draft of it.

        Passing back the id returned last time updates that row instead of
        adding another. Sessions are checkpointed as they go, so a crash
        leaves a rough record rather than nothing at all, and the tidy summary
        written at the end replaces it rather than duplicating it.
        """
        if episode_id:
            with self._lock:
                self._conn.execute(
                    "UPDATE episodes SET summary=?, ended_at=?, turns=?"
                    " WHERE id=?",
                    (summary, time.time(), turns, episode_id),
                )
                self._conn.commit()
            return episode_id

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

    def search_episodes(self, query: str, limit: int = 4) -> list[dict]:
        """Past conversations matching a query, newest first among matches.

        Returns the summary with how long ago it happened, because "we talked
        about this on Tuesday" is the useful form and a raw timestamp is not.
        """
        # FTS5 ANDs bare terms, so passing a whole question in means every
        # word of it must appear -- "what is my cat called" matched nothing at
        # all, while "cat" matched immediately. People ask questions, so the
        # words are ORed and the ranking decides, and the noise words that
        # would match every conversation ever held are dropped first.
        cleaned = _FTS_SPECIAL.sub(" ", query or "").lower()
        words = [
            w for w in re.findall(r"[a-z0-9']{3,}", cleaned)
            if w not in _STOPWORDS
        ]
        if not words:
            return []
        cleaned = " OR ".join(words[:12])
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT e.id, e.summary, e.created_on, e.turns, e.ended_at"
                    " FROM episodes_fts JOIN episodes e"
                    " ON e.id = episodes_fts.rowid"
                    " WHERE episodes_fts MATCH ?"
                    " ORDER BY bm25(episodes_fts) LIMIT ?",
                    (cleaned, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []          # a query FTS cannot parse is not an error here

        out = []
        for row in rows:
            days = max(0, int((time.time() - row["ended_at"]) // 86400))
            out.append({
                "id": row["id"],
                "summary": row["summary"],
                "on": row["created_on"],
                "turns": row["turns"],
                "days_ago": days,
                "when": "today" if days == 0
                        else "yesterday" if days == 1
                        else f"{days} days ago",
            })
        return out

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
