# Deployment

## Current setup
| Component | Host | Notes |
|---|---|---|
| Backend (FastAPI) | Render, Docker, Singapore | Free tier; sleeps when idle |
| Frontend (Next.js) | Vercel | `NEXT_PUBLIC_API_URL` points at the Render URL |
| Vectors | Pinecone, index `doc-intelligence` | 2,462 vectors, built by `rag/ingestion.py` |

## Backend
Render builds from the `Dockerfile` on every push to `main`.
Environment variables (set in Render, never committed):
- `OPENAI_API_KEY` — restricted key: chat completions + embeddings only
- `PINECONE_API_KEY`

## Frontend
`NEXT_PUBLIC_API_URL` is compiled in at build time. After changing it,
redeploy on Vercel for it to take effect.

## History
Originally on Google Cloud Run. Moved to Render in September 2026 after
trial billing lapsed and the service stopped silently (see MISTAKES.md, Bug 14).