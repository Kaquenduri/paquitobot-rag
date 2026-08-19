# Proposal: Build Canvas RAG Backend

## Intent

Replace the one-shot RAG script with a multi-user read-only HTTP service for querying each student’s Canvas courses, assignments, submissions, grades, and aggregates. It must combine relational and semantic retrieval while enforcing tenant isolation and protecting Canvas credentials.

## Scope

### In Scope
- FastAPI monolith with MVC boundaries, separate authentication, and encrypted per-user Canvas-token custody.
- SQLAlchemy/PostgreSQL via existing `SUPABASE_DATABASE_URL`, minimal tenant-scoped relational schema, and PGVector.
- Idempotent Canvas GET-only sync per user every six hours, distributed scheduling, and manual sync.
- Safe SQL/vector/hybrid routing with tenant constraints, read-only transactions, validation, timeout, row limit, and relational-only fallback when Ollama fails.
- Gemini 2.5 Flash for ambiguous classification/generation; Ollama `qwen3-embedding:8b` for embeddings; answers in the detected language.

### Out of Scope
- Canvas writes or mutations; identifiable peer data.
- `supabase-py`: exploration confirms only `SUPABASE_DATABASE_URL`; no extra Supabase credentials are assumed.
- Non-question Canvas entities, broad analytics, or frontend work.

## Capabilities

### New Capabilities
- `tenant-authentication`: Separate backend identity from encrypted Canvas credentials.
- `canvas-read-sync`: Tenant-scoped, idempotent scheduled/manual GET synchronization.
- `question-routing`: Safe relational, vector, and hybrid answering with language/fallback behavior.
- `student-data-model`: Tenant-isolated courses, assignments, submissions, and aggregates.

### Modified Capabilities
- None.

## Approach

Refactor `main.py` into an ASGI entry point and add API, persistence, sync, retrieval, and security components. Use SQLAlchemy over PostgreSQL for relational and PGVector workloads, explicit migrations, encrypted tokens, tenant predicates, and Canvas-ID upserts with distributed scheduling. Preserve embedding/Gemini choices and remove destructive vector-store behavior.

## Affected Areas

| Area | Impact |
|---|---|
| `main.py`, `src/api/` | FastAPI app and protected endpoints |
| `src/extract_canvas_data.py`, `src/sync/` | Typed read-only client and idempotent upserts |
| `src/supabase.py`, `src/text_to_sql/`, `src/rag/` | SQLAlchemy persistence and guarded routing |
| `src/text_processor.py`, `requirements.txt` | Sanitization/dependencies |

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Cross-tenant leakage or unsafe SQL | High | Server-side scope, allow-list, read-only role, timeout, row limit |
| Token exposure | High | Encryption, redaction, no plaintext logs/artifacts, separate auth |
| Canvas/Ollama failure or sync herd | Med | Retries, jitter/leases, idempotency, fallback |

## Rollback Plan

Disable new routes and scheduler, restore the prior script entry point, and roll back additive migrations. Preserve vector data; never perform destructive refreshes. Revoke Canvas tokens if exposure is suspected.

## Dependencies

FastAPI, PostgreSQL/PGVector via `SUPABASE_DATABASE_URL`, Gemini, Ollama, and leases.

## Success Criteria

- [ ] Authenticated students query only their own synchronized data.
- [ ] Sync is GET-only, idempotent, six-hour scheduled, distributed, and manually controlled.
- [ ] Text-to-SQL is tenant-scoped, validated, read-only, time/row bounded, and safely falls back.
- [ ] Tokens are encrypted and absent from plaintext logs and SDD artifacts.
