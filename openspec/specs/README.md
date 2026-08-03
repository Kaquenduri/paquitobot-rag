# openspec/specs/

This directory holds **main specs** — the durable source of truth for Primer RAG.

During `sdd-init` we only create the skeleton. Main specs will be created when
an `sdd-archive` step merges delta specs from a completed change into a domain
folder such as `openspec/specs/<domain>/spec.md`.

Possible domains for this project (provisional, to be confirmed during
`sdd-propose`/`sdd-spec`):

- `ingestion` — Canvas LMS API extraction, document loading, chunking.
- `embedding` — Ollama embeddings (`qwen3-embedding:8b`, 1024 dims).
- `vector-store` — ChromaDB and Supabase PGVector persistence.
- `retrieval` — similarity search, prompt construction, answer generation.
- `evaluation` — quality checks over retrieved context and answers.

Do not write spec files here until a change has been archived.