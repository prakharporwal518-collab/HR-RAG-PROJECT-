# Qorvexa HR Assist: HR policy RAG chatbot

Nobody reads the HR handbook. **HR Assist** lets Qorvexa Technology employees ask questions in plain English and get answers taken **only from the HR policy PDFs they are allowed to see**, with the page number cited for every fact. HR gets a dashboard showing which questions the policies don't answer.

![stack](https://img.shields.io/badge/FastAPI-Python-4f46e5) ![llm](https://img.shields.io/badge/LLM-Claude%20%7C%20Gemini-06b6d4) ![vectors](https://img.shields.io/badge/vectors-ChromaDB-7c3aed)

## How it works

```
 HR uploads PDF + chooses who can read it
        │
        ▼
 page-tagged chunks ──► BM25 keyword index
                   └──► ChromaDB vector index (MiniLM embeddings)

 Employee signs in (SSO) ──► question
        │
        ▼
 search ONLY the documents their groups can read
   BM25 ranking ─┐
   vector ranking┴─► Reciprocal Rank Fusion ──► top passages
        │
        ▼
 Claude or Gemini answers using ONLY those passages ──► answer + [p. N] citations
        │
        ▼
 question logged (anonymously) ──► "not in handbook" / 👎 ──► HR Insights
```

| Feature | Where | How |
|---|---|---|
| **Hybrid search** | `backend/rag.py` | BM25 (written from scratch) catches exact terms like "POSH". Dense embeddings in ChromaDB catch meaning ("quit my job" ≈ "resignation"). The two rankings are merged with **Reciprocal Rank Fusion**. Files are only re-embedded when their contents change. |
| **SSO + access control** | `backend/auth.py`, `config/access.json` | OpenID Connect sign-in (Google Workspace, Microsoft Entra ID, Okta…). Users get groups from `access.json` (wildcards like `*@hr.qorvexa.com`) and from the provider's `groups` claim. Each document lists the groups that may read it, and search filters on that **inside the vector query**, so restricted text never reaches the LLM. |
| **Unanswered-question log** | `backend/store.py` | Every question is stored in SQLite with a pseudonymous id (never the email). Questions the handbook can't answer, and answers marked 👎, are grouped on the **HR Insights** dashboard. |
| **Retrieval evaluation** | `eval/` | 40 realistic employee questions labelled with the page that answers them. `run_eval.py` reports Hit@1, Hit@3 and MRR for each search mode, and CI fails if accuracy drops. |

### Retrieval accuracy (40 questions, top-5)

| Mode | Hit@1 | Hit@3 | MRR |
|---|---|---|---|
| BM25 only | 67.5% | 80.0% | 0.729 |
| Dense only | 70.0% | 85.0% | 0.773 |
| **Hybrid (used)** | **77.5%** | **85.0%** | **0.817** |

Run `python eval/run_eval.py --show-misses` to see which questions each mode gets wrong. That's the place to start when tuning chunk size, the embedding model or the fusion.

## Project structure

```
backend/
  app.py      FastAPI routes: sign-in, /api/ask (streaming), uploads, insights
  auth.py     OIDC single sign-on, groups, document permission checks
  rag.py      PDF chunking, BM25, ChromaDB vectors, hybrid fusion
  llm.py      Grounded prompt + Claude or Gemini streaming (passage-mode fallback)
  store.py    SQLite: document permissions + question log
config/
  access.json Email domains, group membership, default document access, demo users
frontend/     Landing page, chat, sign-in, HR Insights (HTML/CSS/JS)
data/         Policy PDFs (indexed on startup)
eval/         questions.jsonl + run_eval.py
tests/        pytest suite
storage/      Created at runtime: chroma/ vectors + hr_assist.db (git-ignored)
```

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # add ANTHROPIC_API_KEY or GEMINI_API_KEY, and a SESSION_SECRET
cd backend
uvicorn app:app --reload
```

Open http://localhost:8000. The first start downloads the ~80 MB embedding model.

### Choosing the LLM

| Provider | Variables | Notes |
|---|---|---|
| Claude | `ANTHROPIC_API_KEY`, optional `CLAUDE_MODEL` | Default when its key is set |
| Gemini | `GEMINI_API_KEY`, optional `GEMINI_MODEL` (default `gemini-2.5-flash`) | Free tier available. Rate-limited, and Google may use free-tier prompts to improve its products. |

If both keys are set, `LLM_PROVIDER=gemini` or `LLM_PROVIDER=claude` picks one. If Gemini returns a 404, the model name has changed: set `GEMINI_MODEL` to a current Flash model listed in Google AI Studio.

Without SSO settings, the app runs in **demo mode**: the sign-in card lets you pick an employee, a manager or an HR admin (Neha Kapoor), so you can try every role. Set `OIDC_*` in `.env` to switch to real single sign-on. Demo mode is then disabled automatically.

## Access control

- `everyone`: any signed-in employee.
- `hr-admin`: reads every document, uploads PDFs, changes access and sees HR Insights.
- Any other group (e.g. `managers`) is defined in `config/access.json` or comes from your identity provider.

HR admins choose the groups when uploading a document. Access can be changed later with `PUT /api/documents/{name}/access`. A document with no explicit permission falls back to `default_document_groups` (HR only), so a new file is never accidentally public.

## Tests and evaluation

```bash
pytest -q tests                                  # auth, access control, logging, retrieval quality
python eval/run_eval.py --show-misses            # compare bm25 / dense / hybrid
python eval/run_eval.py --mode hybrid --min-hit3 0.8   # exit 1 if accuracy drops
```

GitHub Actions runs both on every push.

## API

| Method | Path | Who | Description |
|---|---|---|---|
| `GET` | `/api/me` | anyone | Current user and sign-in mode |
| `GET` | `/auth/login`, `/auth/callback` | anyone | OIDC single sign-on |
| `POST` | `/auth/dev-login` | demo mode only | `{"email": "..."}` |
| `POST` | `/auth/logout` | user | Sign out |
| `GET` | `/api/status` | user | Documents this user can read, search mode |
| `POST` | `/api/ask` | user | `{"question", "history"}` → SSE: `sources`, `token`…, `done {id, answered}` |
| `POST` | `/api/feedback` | user | `{"id", "helpful"}` for their own question |
| `POST` | `/api/upload` | HR admin | multipart `file` + `groups` (comma-separated) |
| `PUT` | `/api/documents/{name}/access` | HR admin | `{"groups": [...]}` |
| `GET` | `/api/insights` | HR admin | Answer rate and policy gaps |

## Ideas to extend

- Add a cross-encoder reranker on the top 20 hybrid results and measure it with the eval set.
- Let HR mark a gap as "resolved" after updating a policy.
- Grow the eval set from real questions in the log (with HR's review).
- Evaluate answers too, not just retrieval: check that cited pages actually support each claim.
