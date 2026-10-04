"""SQLite storage: document permissions and the question log behind HR Insights."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    name        TEXT PRIMARY KEY,
    groups      TEXT NOT NULL,          -- JSON list of groups allowed to read it
    uploaded_by TEXT,
    uploaded_at TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    asker     TEXT NOT NULL,            -- pseudonymous hash, never the raw email
    question  TEXT NOT NULL,
    answered  INTEGER NOT NULL,         -- 0 = the handbook did not cover it
    pages     TEXT NOT NULL,            -- JSON list of cited source pages
    feedback  INTEGER                   -- 1 helpful, -1 not helpful, NULL no vote
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(q: str) -> str:
    """Group near-identical questions: lowercase, no punctuation, single spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", q.lower())).strip()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    # ---------- document permissions ----------
    def document_groups(self) -> dict[str, list[str]]:
        rows = self.db.execute("SELECT name, groups FROM documents").fetchall()
        return {r["name"]: json.loads(r["groups"]) for r in rows}

    def set_document_groups(self, name: str, groups: list[str], by: str | None = None,
                            overwrite: bool = True) -> None:
        verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        with self.lock, self.db:
            self.db.execute(f"{verb} INTO documents VALUES (?, ?, ?, ?)",
                            (name, json.dumps(sorted(set(groups))), by, now()))

    # ---------- question log ----------
    def log_question(self, asker: str, question: str, answered: bool, pages: list[int]) -> int:
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO questions (ts, asker, question, answered, pages) VALUES (?, ?, ?, ?, ?)",
                (now(), asker, question.strip(), int(answered), json.dumps(pages)),
            )
            return cur.lastrowid

    def set_feedback(self, qid: int, asker: str, helpful: bool) -> bool:
        with self.lock, self.db:
            cur = self.db.execute("UPDATE questions SET feedback = ? WHERE id = ? AND asker = ?",
                                  (1 if helpful else -1, qid, asker))
            return cur.rowcount == 1

    def insights(self, limit: int = 50) -> dict:
        rows = self.db.execute("SELECT * FROM questions ORDER BY id DESC").fetchall()
        total = len(rows)
        unanswered = sum(1 for r in rows if not r["answered"])
        unhelpful = sum(1 for r in rows if r["feedback"] == -1)
        groups: dict[str, dict] = {}
        for r in rows:  # newest first, so the first text we see is the latest wording
            if r["answered"] and r["feedback"] != -1:
                continue
            g = groups.setdefault(normalize(r["question"]), {
                "question": r["question"], "count": 0, "last_asked": r["ts"],
                "reason": "not_in_handbook" if not r["answered"] else "unhelpful",
            })
            g["count"] += 1
        gaps = sorted(groups.values(), key=lambda g: (g["count"], g["last_asked"]), reverse=True)
        return {
            "total": total,
            "unanswered": unanswered,
            "unhelpful": unhelpful,
            "answer_rate": (total - unanswered) / total if total else None,
            "gaps": gaps[:limit],
        }
