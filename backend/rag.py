"""Retrieval layer: PDF -> page-aware chunks -> hybrid search.

Two retrievers run side by side and their rankings are fused:
- BM25 (implemented from scratch below) is great at exact words: "POSH", "probation".
- Dense embeddings stored in ChromaDB understand meaning: "quit my job" ~ "resignation".

Every chunk remembers its source file, page and section, which is what lets the
assistant cite exactly where an answer came from, and lets us filter results
down to the documents a user is allowed to see.
"""
from __future__ import annotations

import math
import re
import hashlib
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

log = logging.getLogger("hr_assist.rag")

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
    first_id: int = field(repr=False, default=0)  # id of the first chunk of the same file

    @property
    def key(self) -> str:
        """Stable id used in the vector store, e.g. 'Qorvexa_HR_Handbook.pdf#7'."""
        return f"{self.source}#{self.id - self.first_id}"


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
                      tokenize(section + " " + text), first_id=start_id)
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

    def search(self, query: str, k: int = 5, allowed: set[str] | None = None) -> list[tuple[Chunk, float]]:
        q = tokenize(query)
        scored = []
        for chunk, tf in zip(self.chunks, self.tf):
            if allowed is not None and chunk.source not in allowed:
                continue
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


class VectorStore:
    """Dense retrieval with ChromaDB (persistent, on disk).

    Uses Chroma's built-in all-MiniLM-L6-v2 ONNX embedding model, so there is no
    PyTorch to install. Each file is re-embedded only when its contents change.
    """

    def __init__(self, path: Path):
        import chromadb
        from chromadb.config import Settings

        self.client = chromadb.PersistentClient(str(path), settings=Settings(anonymized_telemetry=False))
        self.col = self.client.get_or_create_collection("hr_policies", metadata={"hnsw:space": "cosine"})

    def sync(self, chunks: list[Chunk], fingerprints: dict[str, str]) -> None:
        stored = {}
        for m in self.col.get(include=["metadatas"])["metadatas"]:
            stored[m["source"]] = m["fingerprint"]
        for source in set(stored) - set(fingerprints):
            self.col.delete(where={"source": source})
        for source, fp in fingerprints.items():
            if stored.get(source) == fp:
                continue
            if source in stored:
                self.col.delete(where={"source": source})
            mine = [c for c in chunks if c.source == source]
            if mine:
                log.info("Embedding %d chunks from %s", len(mine), source)
                self.col.add(
                    ids=[c.key for c in mine],
                    documents=[f"{c.section}\n{c.text}" for c in mine],
                    metadatas=[{"source": c.source, "page": c.page, "fingerprint": fp} for c in mine],
                )

    def search(self, query: str, k: int, allowed: set[str] | None) -> list[tuple[str, float]]:
        """Returns (chunk key, cosine similarity) pairs."""
        n = self.col.count()
        if n == 0 or (allowed is not None and not allowed):
            return []
        where = {"source": {"$in": sorted(allowed)}} if allowed is not None else None
        res = self.col.query(query_texts=[query], n_results=min(k, n), where=where, include=["distances"])
        return [(i, 1 - d) for i, d in zip(res["ids"][0], res["distances"][0])]


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion: score = sum of 1 / (k + rank) across rankers."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1 / (k + rank)
    return scores


def file_fingerprint(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


class KnowledgeBase:
    """All indexed policy PDFs in a folder, searchable with BM25, dense or hybrid retrieval."""

    MIN_SIMILARITY = 0.15  # drop near-random dense matches (MiniLM similarities run low)
    BM25_MIN_RATIO = 0.3   # BM25 matches below 30% of the best score are treated as unrelated
    STRICT_MIN_SIMILARITY = 0.3  # strict mode: meaning-only matches must be at least this similar

    def __init__(self, folder: Path, vector_path: Path | None = None):
        self.folder = folder
        self.index = BM25Index()
        self.vectors: VectorStore | None = None
        if vector_path and os.getenv("EMBEDDINGS", "on").lower() != "off":
            try:
                self.vectors = VectorStore(vector_path)
            except Exception as e:  # e.g. chromadb missing or the model can't be downloaded
                log.warning("Dense retrieval disabled (%s). Falling back to BM25 only.", e)
        self.reload()

    @property
    def dense_enabled(self) -> bool:
        return self.vectors is not None

    def reload(self) -> None:
        chunks: list[Chunk] = []
        fingerprints = {}
        for pdf in sorted(self.folder.glob("*.pdf")):
            chunks += load_pdf(pdf, start_id=len(chunks))
            fingerprints[pdf.name] = file_fingerprint(pdf)
        self.index.build(chunks)
        self.by_key = {c.key: c for c in chunks}
        if self.vectors:
            try:
                self.vectors.sync(chunks, fingerprints)
            except Exception as e:
                log.warning("Could not embed documents (%s). Falling back to BM25 only.", e)
                self.vectors = None

    def documents(self) -> list[dict]:
        docs: dict[str, dict] = {}
        for c in self.index.chunks:
            d = docs.setdefault(c.source, {"name": c.source, "pages": 0, "chunks": 0})
            d["pages"] = max(d["pages"], c.page)
            d["chunks"] += 1
        return list(docs.values())

    def search(self, query: str, k: int = 5, allowed: set[str] | None = None,
               mode: str = "hybrid", strict: bool = False) -> list[Chunk]:
        """Top-k relevant chunks from the documents in `allowed` (None = all documents).

        mode: "bm25", "dense" or "hybrid" (both, fused with Reciprocal Rank Fusion).
        strict: return nothing unless there is a keyword match or a strong semantic match.
            Use it when no LLM is available to judge relevance: retrieval always finds
            *something*, even for "who won the cricket match".
        """
        if mode != "bm25" and not self.vectors:
            mode = "bm25"
        pool = k * 3
        rankings = []
        keyword_match, best_sim = False, 0.0
        if mode in ("bm25", "hybrid"):
            hits = self.index.search(query, pool, allowed)
            keyword_match = bool(hits)
            if hits:
                best = hits[0][1]
                rankings.append([c.key for c, s in hits if s >= best * self.BM25_MIN_RATIO])
        if mode in ("dense", "hybrid"):
            hits = self.vectors.search(query, pool, allowed)
            best_sim = hits[0][1] if hits else 0.0
            rankings.append([key for key, sim in hits if sim >= self.MIN_SIMILARITY and key in self.by_key])
        if strict and not keyword_match and best_sim < self.STRICT_MIN_SIMILARITY:
            return []
        fused = rrf(rankings)
        top = sorted(fused, key=fused.get, reverse=True)[:k]
        return [self.by_key[key] for key in top]
