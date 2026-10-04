"""Qorvexa HR Assist - FastAPI server.

Run:  uvicorn app:app --reload   (from the backend/ folder)
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

import auth  # noqa: E402
from auth import OIDC, AccessConfig, User, can_read, current_user, require_admin, require_user  # noqa: E402
from llm import is_unanswered, llm_enabled, provider, stream_answer  # noqa: E402
from rag import KnowledgeBase  # noqa: E402
from store import Store  # noqa: E402

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", ROOT / "storage"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOP_K = int(os.getenv("TOP_K", "5"))
MAX_UPLOAD_MB = 20
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)

access = AccessConfig(Path(os.getenv("ACCESS_CONFIG", ROOT / "config" / "access.json")))
store = Store(STORAGE_DIR / "hr_assist.db")
kb = KnowledgeBase(DATA_DIR, vector_path=STORAGE_DIR / "chroma")
oidc = OIDC()
AUTH_MODE = auth.auth_mode(oidc)

app = FastAPI(title="Qorvexa HR Assist")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=8 * 60 * 60,  # sign in again after a working day
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)


# ---------- access helpers ----------
def doc_groups(name: str) -> list[str]:
    """Who may read a document: set at upload (database) > config/access.json > default."""
    return store.document_groups().get(name) or access.documents.get(name) or access.default_document_groups


def readable_docs(user: User) -> set[str]:
    return {d["name"] for d in kb.documents() if can_read(user, doc_groups(d["name"]))}


# ---------- auth routes ----------
@app.get("/api/me")
def me(request: Request):
    user = current_user(request)
    return {
        "user": user.public() if user else None,
        "auth": {"mode": AUTH_MODE, "provider": oidc.provider_name},
        "demo_users": access.demo_users if AUTH_MODE == "dev" else [],
    }


@app.get("/auth/login")
async def sso_login(request: Request):
    if AUTH_MODE != "oidc":
        raise HTTPException(404, "SSO is not configured.")
    redirect_uri = os.getenv("OIDC_REDIRECT_URI") or str(request.url_for("sso_callback"))
    return await oidc.client.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback", name="sso_callback")
async def sso_callback(request: Request):
    if AUTH_MODE != "oidc":
        raise HTTPException(404, "SSO is not configured.")
    token = await oidc.client.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = (info.get("email") or info.get("preferred_username") or "").lower()
    if not email or info.get("email_verified") is False:
        raise HTTPException(403, "Your identity provider did not return a verified email.")
    if not access.domain_allowed(email):
        raise HTTPException(403, "This account is not allowed to use HR Assist.")
    groups = access.groups_for(email, info.get("groups"))
    auth.login(request, email, info.get("name") or email.split("@")[0], groups)
    return RedirectResponse("/#ask")


class DevLogin(BaseModel):
    email: str = Field(min_length=3, max_length=200)


@app.post("/auth/dev-login")
def dev_login(body: DevLogin, request: Request):
    if AUTH_MODE != "dev":
        raise HTTPException(404, "Dev sign-in is disabled.")
    email = body.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not access.domain_allowed(email):
        raise HTTPException(403, "Use a company email address.")
    demo = next((u for u in access.demo_users if u["email"].lower() == email), {})
    name = demo.get("name") or email.split("@")[0].replace(".", " ").title()
    auth.login(request, email, name, access.groups_for(email))
    return {"user": current_user(request).public()}


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


# ---------- knowledge base ----------
@app.get("/api/status")
def status(user: User = Depends(require_user)):
    allowed = readable_docs(user)
    docs = [d for d in kb.documents() if d["name"] in allowed]
    if user.is_admin:
        for d in docs:
            d["groups"] = doc_groups(d["name"])
    return {
        "documents": docs,
        "llm": llm_enabled(),
        "llm_provider": provider(),
        "retrieval": "hybrid" if kb.dense_enabled else "bm25",
        "groups": access.all_groups() if user.is_admin else None,
    }


class Ask(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    history: list[dict] = Field(default_factory=list, max_length=20)


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/ask")
def ask(body: Ask, user: User = Depends(require_user)):
    # Include the previous user question so follow-ups like "and for interns?" still retrieve well.
    prev = next((m.get("content", "") for m in reversed(body.history) if m.get("role") == "user"), "")
    query = f"{body.question} {prev}" if prev else body.question
    allowed = readable_docs(user)
    chunks = kb.search(query, k=TOP_K, allowed=allowed)
    # Without an LLM to say "not in the handbook", judge relevance ourselves, on the new
    # question alone (otherwise an off-topic follow-up inherits the previous question's matches).
    if not llm_enabled() and not kb.search(body.question, k=1, allowed=allowed, strict=True):
        chunks = []
    sources = [{"page": c.page, "section": c.section, "source": c.source,
                "snippet": c.text[:220] + ("…" if len(c.text) > 220 else "")} for c in chunks]
    asker = auth.pseudonym(user.email, SESSION_SECRET)

    def events():
        yield sse("sources", sources)
        answer = ""
        for text in stream_answer(body.question, chunks, body.history):
            answer += text
            yield sse("token", text)
        answered = bool(chunks) and not is_unanswered(answer)
        qid = store.log_question(asker, body.question, answered, sorted({c.page for c in chunks}))
        yield sse("done", {"id": qid, "answered": answered})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class Feedback(BaseModel):
    id: int
    helpful: bool


@app.post("/api/feedback")
def feedback(body: Feedback, user: User = Depends(require_user)):
    if not store.set_feedback(body.id, auth.pseudonym(user.email, SESSION_SECRET), body.helpful):
        raise HTTPException(404, "Question not found.")
    return {"ok": True}


# ---------- HR admin ----------
def clean_groups(raw: str | list[str]) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else raw
    groups = [g.strip() for g in items if g.strip()]
    unknown = set(groups) - set(access.all_groups())
    if not groups or unknown:
        raise HTTPException(400, f"Choose at least one valid group. Unknown: {', '.join(sorted(unknown)) or 'none'}")
    return groups


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), groups: str = Form("everyone"),
                 user: User = Depends(require_admin)):
    allowed_groups = clean_groups(groups)
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload a PDF file.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF must be under {MAX_UPLOAD_MB} MB.")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file.filename or "policy.pdf").name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    path = DATA_DIR / name
    previous = path.read_bytes() if path.exists() else None
    path.write_bytes(data)
    try:
        kb.reload()
    except Exception as e:  # restore the old file so a bad upload can't break search
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)
        kb.reload()
        raise HTTPException(400, f"Could not read that PDF: {e}")
    store.set_document_groups(name, allowed_groups, by=user.email)
    return {"ok": True, "name": name, "groups": allowed_groups}


class DocAccess(BaseModel):
    groups: list[str]


@app.put("/api/documents/{name}/access")
def set_access(name: str, body: DocAccess, user: User = Depends(require_admin)):
    if name not in {d["name"] for d in kb.documents()}:
        raise HTTPException(404, "Document not found.")
    groups = clean_groups(body.groups)
    store.set_document_groups(name, groups, by=user.email)
    return {"ok": True, "name": name, "groups": groups}


@app.get("/api/insights")
def insights(user: User = Depends(require_admin)):
    return store.insights()


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
