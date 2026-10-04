"""Qorvexa HR Assist - FastAPI server.

Run:  uvicorn app:app --reload   (from the backend/ folder)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, File, Header, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from llm import llm_enabled, stream_answer  # noqa: E402
from rag import KnowledgeBase  # noqa: E402

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(exist_ok=True)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
TOP_K = int(os.getenv("TOP_K", "5"))
MAX_UPLOAD_MB = 20

kb = KnowledgeBase(DATA_DIR)
app = FastAPI(title="Qorvexa HR Assist")


class Ask(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    history: list[dict] = Field(default_factory=list, max_length=20)


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/status")
def status():
    return {"documents": kb.documents(), "llm": llm_enabled(), "uploads_enabled": bool(ADMIN_TOKEN)}


@app.post("/api/ask")
def ask(body: Ask):
    # Include the previous user question so follow-ups like "and for interns?" still retrieve well.
    prev = next((m.get("content", "") for m in reversed(body.history) if m.get("role") == "user"), "")
    chunks = kb.search(f"{body.question} {prev}", k=TOP_K)
    sources = [{"page": c.page, "section": c.section, "source": c.source,
                "snippet": c.text[:220] + ("…" if len(c.text) > 220 else "")} for c in chunks]

    def events():
        yield sse("sources", sources)
        for text in stream_answer(body.question, chunks, body.history):
            yield sse("token", text)
        yield sse("done", {})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN:
        raise HTTPException(403, "Uploads are disabled. Set ADMIN_TOKEN in .env to enable them.")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Invalid admin token.")
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload a PDF file.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF must be under {MAX_UPLOAD_MB} MB.")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file.filename or "policy.pdf").name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    (DATA_DIR / name).write_bytes(data)
    try:
        kb.reload()
    except Exception as e:
        (DATA_DIR / name).unlink(missing_ok=True)
        kb.reload()
        raise HTTPException(400, f"Could not read that PDF: {e}")
    return {"ok": True, "documents": kb.documents()}


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
