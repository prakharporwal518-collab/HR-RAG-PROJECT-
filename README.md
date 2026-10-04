# Qorvexa HR Assist: HR policy RAG chatbot

Nobody reads the HR handbook. **HR Assist** lets Qorvexa Technology employees ask questions in plain English and get answers taken **only from the uploaded HR policy PDF**, with the page number cited for every fact.

![stack](https://img.shields.io/badge/FastAPI-Python-4f46e5) ![llm](https://img.shields.io/badge/LLM-Claude-06b6d4)

## How it works (RAG)

```
 HR uploads PDF ──► split into page-tagged chunks ──► BM25 index
                                                          │
 Employee question ──► retrieve top passages ─────────────┘
                              │
                              ▼
            Claude answers using ONLY those passages ──► answer + [p. N] citations
```

1. **Ingest** (`backend/rag.py`): `pypdf` reads each page, strips headers/footers and the table of contents, and splits the text into ~160-word overlapping chunks. Each chunk keeps its **page number and section title**.
2. **Retrieve** (`backend/rag.py`): a from-scratch **BM25** ranker (the same algorithm behind Elasticsearch) finds the most relevant chunks. It needs no vector database or embedding model.
3. **Generate** (`backend/llm.py`): the passages go to Claude with a strict system prompt: answer only from the passages, cite pages, and say "not in the handbook" instead of guessing. The answer streams to the browser word by word.

If no `ANTHROPIC_API_KEY` is set, the app still works in **passage mode** and shows the best-matching handbook text.

## Project structure

```
backend/
  app.py      FastAPI server: /api/ask (streaming), /api/upload, /api/status, serves the UI
  rag.py      PDF chunking + BM25 retrieval
  llm.py      Prompt + Claude streaming call (with passage-mode fallback)
frontend/
  index.html  Landing page + chat UI
  styles.css  Design system, scroll animations, responsive layout
  app.js      Chat streaming, citations, topic cards, admin upload, scroll effects
data/
  Qorvexa_HR_Handbook.pdf   Indexed automatically on startup
```

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then add your ANTHROPIC_API_KEY and an ADMIN_TOKEN
cd backend
uvicorn app:app --reload
```

Open http://localhost:8000.

## Uploading new policies

Click **Upload policy** in the top bar, enter the `ADMIN_TOKEN` from `.env`, and choose a PDF. It's saved to `data/` and indexed right away, and every PDF in `data/` is searched. You can also drop PDFs into `data/` and restart the server.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/status` | Indexed documents and whether AI answers are enabled |
| `POST` | `/api/ask` | `{"question": "...", "history": [...]}` → Server-Sent Events: `sources`, `token`…, `done` |
| `POST` | `/api/upload` | multipart `file` + header `X-Admin-Token` → re-indexes |

## Ideas to extend

- Swap BM25 for embeddings (or combine both: "hybrid search") and store vectors in FAISS/Chroma.
- Add employee login (SSO) and per-document access control.
- Log unanswered questions so HR can see which policies need to be clearer.
- Add an evaluation set of question → expected page pairs to measure retrieval accuracy.
