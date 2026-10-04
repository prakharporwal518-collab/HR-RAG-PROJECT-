"""Retrieval layer: PDF -> page-aware chunks -> BM25 search.

BM25 is implemented from scratch (no vector DB needed) so it is easy to read
and learn from. Every chunk remembers its source file, page and section, which
is what lets the assistant cite exactly where an answer came from.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

# Repeated header/footer lines in the handbook that add noise to search.
NOISE = re.compile(
    r"^(QORVEXA|Employee HR Handbook \| Internal Use|Qorvexa • Human Resources|Page \d+)$"
)
SECTION = re.compile(r"^(\d{1,2})\.\s+[A-Z][^.]{2,80}$")
TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = set(
    "a an and are as at be by can do does for from has have how i if in is it its "
    "me my of on or our should so that the their there this to was we what when "
    "where which who will with you your".split()
)

CHUNK_WORDS = 160
OVERLAP_WORDS = 40


def tokenize(text: str) -> list[str]:
    out = []
    for t in TOKEN.findall(text.lower()):
        if t in STOPWORDS:
            continue
        # Tiny stemmer: "leaves" -> "leave", "policies" -> "policy".
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


@dataclass
class Chunk:
    id: int
    source: str
    page: int
    section: str
    text: str
    tokens: list[str] = field(repr=False, default_factory=list)


def load_pdf(path: Path, start_id: int = 0) -> list[Chunk]:
    """Split a PDF into overlapping word windows, one page at a time."""
    chunks: list[Chunk] = []
    section = "General"
    for page_no, page in enumerate(PdfReader(path).pages, start=1):
        text = (page.extract_text() or "").replace("\x7f", "•")  # pypdf reads bullets as DEL
        lines = [l.strip() for l in text.splitlines()]
        lines = [l for l in lines if l and not NOISE.match(l)]
        headings = [l for l in lines if SECTION.match(l)]
        if len(headings) > 3:  # table-of-contents page: matches every query, skip it
            continue
        if headings:
            section = headings[0]
        words = " ".join(lines).split()
        step = CHUNK_WORDS - OVERLAP_WORDS
        for i in range(0, max(len(words), 1), step):
            text = " ".join(words[i : i + CHUNK_WORDS])
            if len(text) < 40:
                continue
            chunks.append(
                Chunk(start_id + len(chunks), path.name, page_no, section, text,
                      tokenize(section + " " + text))
            )
            if i + CHUNK_WORDS >= len(words):
                break
    return chunks


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.chunks: list[Chunk] = []

    def build(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.tf = [Counter(c.tokens) for c in chunks]
        self.avgdl = sum(len(c.tokens) for c in chunks) / max(len(chunks), 1)
        df = Counter(t for c in chunks for t in set(c.tokens))
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 5) -> list[tuple[Chunk, float]]:
        q = tokenize(query)
        scored = []
        for chunk, tf in zip(self.chunks, self.tf):
            dl = len(chunk.tokens)
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (
                        f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                    )
            if s > 0:
                scored.append((chunk, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]


class KnowledgeBase:
    """All indexed policy PDFs in a folder."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.index = BM25Index()
        self.reload()

    def reload(self) -> None:
        chunks: list[Chunk] = []
        for pdf in sorted(self.folder.glob("*.pdf")):
            chunks += load_pdf(pdf, start_id=len(chunks))
        self.index.build(chunks)

    def documents(self) -> list[dict]:
        docs: dict[str, dict] = {}
        for c in self.index.chunks:
            d = docs.setdefault(c.source, {"name": c.source, "pages": 0, "chunks": 0})
            d["pages"] = max(d["pages"], c.page)
            d["chunks"] += 1
        return list(docs.values())

    def search(self, query: str, k: int = 5, min_ratio: float = 0.3) -> list[Chunk]:
        """Top-k chunks, dropping weak matches that score far below the best one."""
        hits = self.index.search(query, k)
        if not hits:
            return []
        best = hits[0][1]
        return [c for c, s in hits if s >= best * min_ratio]
