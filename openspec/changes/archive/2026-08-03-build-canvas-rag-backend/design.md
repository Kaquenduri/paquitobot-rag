# Design: Canvas RAG Backend

## 1. Objectives and Trade-offs

**Objectives.** Replace the single-shot RAG script with a multi-user, read-only
HTTP service exposing each authenticated student's own Canvas academic data as
a query surface. Keep today's embedding, vector store, and answer-generation
stack (Ollama `qwen3-embedding:8b` into PGVector, Gemini 2.5 Flash for
classification and generation). Add tenant isolation via a server-derived
`tenant_id`, encrypted Canvas-token custody, idempotent GET-only sync with
per-tenant locks and jitter, a safe relational+vector+hybrid router with
deterministic rules first, and provider degradation (relational-only when
Ollama is down).

**Trade-offs.**

| Decision | Trade-off | Chosen |
|---|---|---|
| Monolith vs. split services | Operational simplicity over per-bounded scaling | Monolith (per spec) |
| FastAPI vs. hand-rolled ASGI | One extra dep, free OpenAPI + Pydantic | FastAPI |
| SQLAlchemy + psycopg2 vs. `supabase-py` | One pool, raw SQL for text-to-SQL, no extra Supabase keys | SQLAlchemy |
| Per-tenant lock via Postgres advisory lock | No new infra (Redis) | Postgres advisory lock |
| Fernet for token custody | Symmetric, simple key rotation | Fernet |
| Deterministic-first routing | Better latency, lower cost, bounded classifier output | Deterministic-first |
| Allow-list SQL templates + read-only role | Brittle for new shapes, but injection-safe | Allow-list |
| In-process APScheduler | No new infra; single-instance until load justifies it | In-process |
| Preserve existing PGVector `documents` table | No destructive refresh | Preserve |

## 2. Directory Layout

FastAPI + MVC-style. Boot entry point: `uvicorn app.main:app`.

```
app/
├── main.py                  # FastAPI app factory + lifespan + scheduler
├── core/{config,logging,security,errors,deps,locks}.py
├── controllers/{auth,sync,query,health}.py
├── services/{tenant,canvas,rag,ingest}_service.py
├── repositories/{tenant,course,assignment,submission,sync_state}_repo.py
├── models/                  # SQLAlchemy declarative; tenant mixin
├── schemas/                 # Pydantic request/response models
├── rag/{router,vector_store,hybrid,prompts}.py
├── text_to_sql/{allow_list,validator,executor}.py + templates/
├── sync/{pipeline,scheduler,watermark,lock}.py
├── canvas/{client,dto,pagination,retry}.py
├── security/{token_crypto,backend_auth,redaction}.py
└── observability/{metrics,tracing}.py
alembic/   alembic.ini   .env.example (placeholders only)
```

`main.py` shrinks to a thin ASGI bootstrap. Existing `main.py` becomes a
wrapper that launches `uvicorn app.main:app` when `LEGACY_MODE=0`.

## 3. Data Model

All tables inherit a tenant mixin (`tenant_id UUID NOT NULL`, `created_at`,
`updated_at`). Soft delete is `deleted_at TIMESTAMPTZ NULL`. Canvas-natural
keys are `canvas_id BIGINT`, unique **per tenant**, not globally.

| Table | Natural key | Notable columns |
|---|---|---|
| `tenants` | server UUID | `name`, `backend_user_id` |
| `users` | `canvas_id` + `tenant_id` | profile fields (self only) |
| `courses` | `canvas_id` + `tenant_id` | `name`, `course_code`, `workflow_state`, `start_at`, `end_at`, `deleted_at` |
| `enrollments` | `canvas_id` + `tenant_id` | `course_id` FK, `role`, `enrollment_state`, `deleted_at` |
| `assignments` | `canvas_id` + `tenant_id` | `course_id` FK, `name`, `due_at`, `points_possible`, `workflow_state`, `score_statistics` JSONB, `deleted_at` |
| `submissions` | `(canvas_id, assignment_id)` + `tenant_id` | `workflow_state`, `submitted_at`, `score`, `grade`, `late`, `missing`, `deleted_at` |
| `sync_state` | `(tenant_id, table_name)` | `last_watermark`, `last_run_at`, `last_status`, `last_error_class` |
| `canvas_credentials` | `tenant_id` | `ciphertext BYTEA`, `key_version INT`, `created_at`, `rotated_at` |

Unique indexes on `(tenant_id, canvas_id)` for `users`, `courses`,
`assignments`, `enrollments`, `submissions`. Cross-tenant FKs rejected by a
before-insert comparator. Soft-delete when upstream `workflow_state` becomes
inactive. `score_statistics` is a JSONB column on `assignments` (no peer rows).
Per `(tenant_id, table_name)` watermark in `sync_state`; advances only in the
same transaction as the upsert. Vector refresh uses a content-hash key so
unchanged chunks are not re-embedded.

**Migrations (Alembic).** One Alembic env, async SQLAlchemy against
`SUPABASE_DATABASE_URL`. First migration creates the seven tables and does
**not** touch the existing `documents` PGVector table. Additive-only;
destructive ops require a separate change.

## 4. Security

**Auth.** Backend identity is independent of Canvas: JWT (HS256 with
`BACKEND_SECRET`) or session cookie. The Canvas token is **not** a backend
session credential.

**Token encryption.** Fernet (AES-128-CBC + HMAC-SHA256 + timestamp). Key from
`TENANT_TOKEN_KEY` env var. Storage: `canvas_credentials.ciphertext BYTEA`,
NOT NULL; no plaintext column anywhere. Decryption is transient, in-memory
only, scoped to the active request. Rotation via `key_version`; old data
re-encrypted by a separate change.

**Headers.** Backend auth uses constant-time comparison. `X-Internal-Scheduler`
is an HMAC header used only by the in-process scheduler; not exposed to
external clients.

**Log redaction.** `security.redaction.RedactionFilter` redacts on every
emission: any `Bearer …` Authorization value, Fernet tokens (matched by the
`gAAAAA` prefix), `SUPABASE_DATABASE_URL` (`postgresql://` /
`postgresql+psycopg://`), and `Authorization`-style keys in dict payloads.
Applied to root logger and uvicorn loggers. Exception handlers pass a
structured `safe_message` so the traceback's `args` never contains the
credential.

**Error schema.** Pydantic-typed errors (`schemas/errors.py`). Body includes
only `code`, `message`, `correlation_id`, optional `details`. Never
`Authorization`, `token`, `ciphertext`, `password`, `database_url`. Same
`safe_message` for HTTP response and log emission.

**Tenant dependency chain.** Fixed for `/query`; every step mandatory; skipping
raises `HTTPException(403)`.

```
verify_backend_jwt() → require_tenant() → require_tenant_token()
   → provider_health() → RAG router.dispatch(query)
```

`tenant_id` is never read from the request body, query string, or any JWT
claim other than the issuer. Pydantic schemas use `extra="forbid"` so client
attempts to override `tenant_id` raise validation errors.

## 5. Canvas Client

**Typing.** `canvas/dto.py` declares Pydantic DTOs (`UserDTO`, `CourseDTO`,
`EnrollmentDTO`, `AssignmentDTO`, `SubmissionDTO`, `ScoreStatisticsDTO`)
matching the field whitelist documented in `exploration.md`. Unknown keys
dropped at parse time.

**GET-only.** `CanvasClient` exposes only `get_*`. ALLOWED_METHODS = {"GET"};
`_request()` asserts `method.upper() == "GET"` **before** any network call.
POST/PUT/PATCH/DELETE raise `CanvasMethodRejected` without touching the
network.

**Pagination.** Cursor via Canvas's `Link: <url>; rel="next"`. `per_page=100`.
`get_paginated()` yields a stream of DTOs.

**Retries & timeout.** `tenacity`: 3 attempts, exponential backoff (1s/2s/4s),
retry on `5xx` and connection errors, no retry on `4xx`. Per-request timeout
8s. Total page budget enforced by the ingest pipeline.

**DTO mapping.** Peer-identifying fields (`enrollments[*].user.*`) are
stripped unless the embedded `user_id` equals the authenticated tenant's
user. Peer records never reach persistence or embedding.

## 6. Sync Pipeline

**Watermark.** `sync_state.watermark` updates inside the same transaction as
the upsert; failure rolls back both, leaving the previous watermark intact.

**Distributed lock.** `pg_try_advisory_xact_lock(hashtext(tenant_id::text))`.
Released automatically when the transaction ends.

**Scheduler.** `APScheduler` `AsyncIOScheduler` with `CronTrigger(hour="*/6")`
plus per-tenant jitter (`random.uniform(0, jitter_seconds)`). Started in the
FastAPI lifespan; only the `sync_due` job runs; per-tenant work dispatched to
a worker pool.

**Manual sync.** `POST /sync` acquires the same lock. If the lock is busy or
the last manual sync was < 60s ago, the call returns 429 with `Retry-After`.

**Rate limit.** Manual: 1 per tenant per 60s. Concurrent worker cap: 4 per
process. Canvas-side rate-limit via permissive token-bucket (10 r/s, burst 20).

**Failure semantics.** Canvas failure keeps the prior watermark and data;
writes `last_status='failed'`, `last_error_class='canvas_unavailable'` (or
`schema_drift`, `auth_rejected`, etc.). Endpoint reports
`{stale, last_successful_at, last_error_class}` from `sync_state`.

## 7. RAG Router

**Deterministic-first routing.** Rule-based checks in order:

| Pattern match | Route |
|---|---|
| Date / count / grade / status / aggregate keyword | `relational` |
| Course/assignment title or numbered ID | `relational` |
| `what is`, `explain`, `summarize`, `describe`, `meaning of` | `semantic` |
| "score for assignment X", "average across assignments" | `hybrid` |
| No match | Gemini 2.5 Flash with constrained labels |

A rule hit **never** triggers a model call. Disagreement defaults to the
deterministic choice unless the question explicitly demands both phrasing and
aggregation.

**Ambiguity classifier.** Gemini 2.5 Flash with `response_schema` limited to
`{relational, semantic, hybrid, unsupported}`. Sees only the question text
and a short system prompt — never documents or user data.

**Allow-list SQL.** `text_to_sql/allow_list.py` is a registry of named
templates with `{{tenant_id}}` and other named placeholders. Slot values are
extracted server-side; LLM/rule output supplies only values. Templates in
scope: `assignments_due_between`, `assignment_score`, `course_aggregate`,
`submission_status_for_assignment`, `class_score_statistics`. Adding a
template requires a code change; the LLM cannot write SQL.

**Validator.** `sqlglot.parse` (with `sqlparse` fallback) asserts:

- exactly one statement; type is `SELECT`
- text matches the chosen template signature
- no `MERGE` / `INSERT` / `UPDATE` / `DELETE` / `WITH DELETE` / `WITH UPDATE`
- no `pg_*` / `set_config` / `current_setting` calls
- no dynamic table identifier from user input

Failure returns `409` with `code: "sql_not_allowed"` and **no SQL or detail**
in the response body.

**Executor.** SQLAlchemy session with `SET LOCAL` of:

- `default_transaction_read_only = on`
- `statement_timeout = 2000` (ms)
- `idle_in_transaction_session_timeout = 10000` (ms)
- `app.tenant_id = '<tenant-uuid>'` (server-derived)

Query wrapped with server-side `LIMIT 200`. `tenant_id` predicate appended
unconditionally after slug substitution. Connection owned by a dedicated
Postgres role `pg_role_canvas_readonly` with `GRANT SELECT` only on the
application tables.

**Vector store.** `rag/vector_store.py` wraps the existing
`langchain_postgres.PGVectorStore` table `documents`. Adds a `tenant_id`
filter on every similarity search (server-injected, client cannot override)
and a content-hash-keyed upsert. No `rmtree` path.

**Orchestrator.**

```
RAGRouter.dispatch(query, ctx)
   ├── route = deterministic_rule(query) ?? classifier(query)
   ├── relational: tpl.match → render(tenant_id, slots) → validate → execute → answer
   ├── semantic:   embed(query) → similarity_search(tenant_id, k=8) → answer
   ├── hybrid:     embed → similarity_search(tenant_id, k=20) → ids
   │               tpl.render(tenant_id, ids) → validate → execute → answer
   └── unsupported or embedding_available=False:
                       bounded_unavailability_response(query.lang)
```

The detected language is computed once and threaded through every step.

## 8. Vector Store Preservation

- No `shutil.rmtree` of any persistent directory. `src/chroma_db.py` is
  replaced by `app/rag/vector_store.py` referencing the existing PGVector
  table `documents`.
- The local `./chroma` directory is preserved but unused by the new service.
- Refresh is upsert by content hash. Initialization failures leave the table
  untouched.

## 9. Observability, Metrics, Configuration

**Logging.** `structlog` JSON output, redacted via `security.redaction`. Per-
request `correlation_id` (UUID v4) in the response header and every log line.
Default `INFO`; `DEBUG` only behind `LOG_LEVEL=debug`.

**Metrics.** `prometheus_client` counters:

| Name | Labels |
|---|---|
| `rag_requests_total` | `route`, `lang`, `outcome` |
| `sql_validations_total` | `result` (allowed/rejected) |
| `canvas_requests_total` | `endpoint`, `result` (ok/4xx/5xx) |
| `sync_runs_total` | `tenant_id_hash`, `result` |
| `sync_lag_seconds` | `tenant_id_hash` (gauge) |
| `token_decrypt_failures_total` | `result` |

**Tracing.** Optional OpenTelemetry OTLP exporter; off by default. Same
redaction filter wraps spans.

**Configuration.** `core/config.py` exposes `Settings(BaseSettings)` with
`supabase_database_url`, `tenant_token_key`, `backend_secret`,
`gemini_api_key`, `ollama_host`, `ollama_embedding_model`, `ollama_embed_dim`,
`canvas_api_base_url`, `sync_interval_seconds`, `sync_jitter_seconds`,
`manual_sync_min_interval_seconds`, `sql_statement_timeout_ms`,
`sql_row_limit`, `log_level`. `.env.example` documents every key with a
placeholder; `./.env` is never committed.

## 10. Migration and Rollback

**Migration.** `LEGACY_MODE=1` keeps the script-mode flow
(`extract_canvas_data.py` + `chroma_db.py`) running. New `app.main:app` boots
side-by-side. After verification, `LEGACY_MODE` drops in a follow-up change.
Migrations are additive; the first migration does not backfill (source of
truth is replayable through the new sync pipeline).

**Rollback.** Disable new routes via `DISABLE_RAG_ROUTES=1`. Stop the
scheduler via `SCHEDULER_ENABLED=0`. Re-enable `LEGACY_MODE=1`. The new
`canvas_credentials` table is dropped by an additive-only downgrade
migration. Vector data is **never** deleted; rollback is purely about
removing the new read paths.

**Token revocation.** If exposure is suspected, the affected tenant's
`canvas_credentials.ciphertext` is re-encrypted and the user re-prompts for a
new token through `POST /auth/canvas/connect`, which re-validates via
`GET /users/self` before accepting.

## 11. Risk Coverage

| Risk | Likelihood | Mitigation |
|---|---|---|
| PII leakage in logs | High | Redaction filter, schema rejects, no `verify` traces include tokens |
| Cross-tenant leakage via SQL | High | Server-only `tenant_id`, allow-list, read-only role, predicate injected server-side |
| Cross-tenant leakage via vector | High | `tenant_id` filter on every search, content-hash upsert |
| Token exposure in errors/logs | High | Fernet ciphertext only, redaction filter, Pydantic error schema, exception handler rewrites `args` |
| Sync storm / herd | Medium | Per-tenant advisory lock, jitter, manual throttling, rate limit |
| Ollama down | Medium | `provider_health()` flips `embedding_available=False`; router skips vector/hybrid |
| Canvas down | Medium | Watermark stays put; prior data queryable; retries with backoff |
| `rmtree` reintroduced | Low | Lint rule + PR review + defensive path checks; tests assert no destructive call |
| Schema drift from Canvas | Medium | DTO whitelist; unknown keys dropped; `last_error_class='schema_drift'` |
| Gemini down | Medium | Fail closed to deterministic rules; "no classifier, no answer" semantics |
| Debugging without leaking secrets | Medium | Redaction filter, `safe_message` everywhere, no token in `exc_info` |

## 12. Flow Sequences

```
12.1 Register Token
   Client → AuthController (Authorization: Bearer <backend-jwt>)
            → verify JWT → require_tenant() → encrypt(token) → write creds
            → GET /users/self (probe) → 200 OK
            → 204 No Content (token cipher id)
            (no plaintext token anywhere downstream)

12.2 Periodic Sync
   Scheduler.tick(6h+jitter) → try_acquire(tenant_id)
            → watermark = read latest
            → GET /favorites/courses?updated_after → pages → DTOs → upsert (tx)
            → GET /assignments per course → upsert assignments + submissions
            → advance watermark (tx end) → release lock

12.3 Manual Sync
   POST /sync → rate_limit_check → try_acquire(tenant)
            → pipeline.run → write sync_state
            → 200/202; release on tx end
            If lock busy or rate-throttled: 429 Retry-After: <seconds>

12.4 Relational Query
   POST /query → rules → route = relational
            → tpl = allow_list.match → sql = render(tenant_id, slots)
            → validator.assert_safe(sql)
            → SET LOCAL read-only, timeout, app.tenant_id
            → execute with LIMIT 200, tenant_id forced
            → answer = lang_prompt(rows, lang) → 200 {answer, lang, route}

12.5 Semantic Query
   POST /query "explain…" → rules → route = semantic
            → embed(query) → similarity_search(tenant_id, k=8) → chunks
            → answer = lang_prompt(chunks) → 200 {answer, lang, route}

12.6 Hybrid Query
   POST /query "class average on assignment X" → route = hybrid
            → similarity_search(tenant_id, k=20) → ids
            → tpl = course_aggregate_for_ids
            → sql = render(tenant_id, ids) → validate → execute
            → answer = lang_prompt(rows, chunks) → 200 {answer, lang, route}

12.7 Token Error
   POST /auth/canvas/connect (bad token)
            → encrypt OK → GET /users/self → 401 Unauthorized
            → ciphertext discarded
            → 401 {code: "canvas_token_invalid",
                   message: "Canvas rejected the token",
                   correlation_id: <uuid>}
            (no token, no header, no Authorization in body)

12.8 Sync Failure
   Scheduler.tick → try_acquire(tenant) → GET /favorites/courses
            → 503 Service Unavailable → retry x3 (backoff) → abort
            → sync_state.last_status='failed',
                    last_error_class='canvas_unavailable'
            → watermark UNCHANGED; prior data queryable
            → release lock
            /query continues to work: data is stale but visible
```

## 13. Threat Matrix

Routing/shell/subprocess/VCS/PR boundaries: **N/A**. The change is a Python
service over HTTP/SQL/Canvas only. No shell automation, no `git` or `gh`
invocations, no executable-file classification, no PR automation. The matrix
is therefore omitted.

## 14. Open Questions

- Single-process scheduler vs. external worker: in-process is the chosen
  default; revisit if horizontal scaling is required.
- Token rotation tooling is out of scope; documented as a follow-up proposal.
- Aggregate boundary: rely on Canvas's `score_statistics` (visible to the
  student only) until product feedback indicates a richer model.
- LangChain 1.x deprecation churn: pin installed versions in
  `requirements.txt` during `sdd-apply`.
