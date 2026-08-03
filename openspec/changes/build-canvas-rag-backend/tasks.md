# Tasks: Build Canvas RAG Backend

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 2400 – 3200 (additions + deletions) |
| 400-line budget risk | High |
| 800-line review budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 scaffold → PR 2 security → PR 3 schema → PR 4 sync → PR 5 router → PR 6 http |
| Delivery strategy | chained |
| Chain strategy | stacked-to-main |
| Decision needed before apply | No (resolved by user preflight; Phase 0 is a prerequisite work unit) |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

### Suggested Work Units

| U  | Goal                                                  | PR   | Focused test command                                  | Runtime harness                                            | Rollback boundary                                                  |
|----|-------------------------------------------------------|------|-------------------------------------------------------|-------------------------------------------------------------|---------------------------------------------------------------------|
| 1  | FastAPI boot, settings, logging, errors, alembic init | PR 1 | `uvicorn app.main:app --help`                          | `GET /healthz` returns 200                                  | drop `app/`, `alembic/`; revert `requirements.txt`                  |
| 2  | Fernet custody, backend JWT, redaction, tenant chain  | PR 2 | tenant/auth unit tests (redaction assertions)         | `POST /auth/canvas/connect` redacted-log assertion          | drop `canvas_credentials`; remove `app/security/`, `app/controllers/auth.py` |
| 3  | SQLAlchemy models, tenant mixin, first Alembic migration | PR 3 | `alembic upgrade head` against `SUPABASE_DATABASE_URL` | integration smoke vs dev DB                                 | `alembic downgrade -1`; PGVector `documents` untouched               |
| 4  | Canvas GET-only client, sync pipeline, lock, scheduler | PR 4 | replay-invariance fixture test                        | scheduled tick + manual `POST /sync`; lock contention probe | disable scheduler; drop `sync_state` rows                           |
| 5  | Deterministic router, Gemini ambiguity, T2SQL allow-list, PGVector wrap | PR 5 | allow-list deny + tenant-injection tests              | relational / semantic / hybrid sample questions            | remove `app/rag/`, `app/text_to_sql/`; `/query` returns 503          |
| 6  | Controllers wiring, ingestion sanitization, observability, e2e | PR 6 | controller unit tests + e2e smoke                      | `POST /query` against synced fixtures                      | `DISABLE_RAG_ROUTES=1`                                              |

## Phase 1: Scaffold (PR 1) — no dependencies

- [x] 1.1 Add `fastapi`, `uvicorn`, `pydantic-settings>=2.10.1,<3`, `alembic`, `structlog`, `prometheus-client`, `bleach`, `sqlglot`, `cryptography`, `apscheduler`, `tenacity` to `requirements.txt` (exact pins, `pydantic-settings` upper-bounded to be compatible with `langchain-community`).
- [x] 1.2 Create `app/main.py` FastAPI app factory + lifespan (boot logging, config) + `/healthz`. Verify: `uvicorn app.main:app --help` succeeds; `GET /healthz` returns 200 with `{status: "ok"}`.
- [x] 1.3 Create `app/core/config.py` with `Settings(BaseSettings)`: `supabase_database_url`, `tenant_token_key`, `backend_secret`, `gemini_api_key`, `ollama_host`, `ollama_embedding_model`, `ollama_embed_dim=1024`, `canvas_api_base_url`, `sync_interval_seconds`, `sync_jitter_seconds`, `manual_sync_min_interval_seconds`, `sql_statement_timeout_ms`, `sql_row_limit`, `log_level`. Risk: missing env must fail closed at startup.
- [x] 1.4 Create `app/core/logging.py` structlog JSON renderer + stub `RedactionFilter` (full impl PR 2). Side effect: log format change for downstream parsers.
- [x] 1.5 Create `app/core/errors.py` + `app/schemas/errors.py` with `ErrorBody(code, message, correlation_id, details)` and FastAPI exception handlers using `safe_message`. Verify: 500 body never contains `Authorization`, `token`, `ciphertext`, `password`, `database_url`.
- [x] 1.6 Initialize `alembic.ini` + `alembic/env.py` against `SUPABASE_DATABASE_URL`; first migration lives in PR 3. Verify: `alembic current` runs without error.
- [x] 1.7 Create `.env.example` with placeholders for every key (no real values); replace `main.py` with thin `uvicorn app.main:app` bootstrap guarded by `LEGACY_MODE`. Side effect: original script opt-in only. Risk: R11 WSL/Windows path encoding — document in `.env.example` comment.
- [x] 1.8 Ensure `./.env` is never committed. Verify: `git status` clean of secrets.


## Phase 2: Security (PR 2) — depends on PR 1

- [x] 2.1 Create `app/security/token_crypto.py` `TokenCipher` with Fernet encrypt/decrypt using `TENANT_TOKEN_KEY`; supports `key_version`. Verify: round-trip OK; tampering raises `InvalidToken`.
- [x] 2.2 Create `app/security/backend_auth.py` `verify_backend_jwt(token) -> user_id` with HS256 + `BACKEND_SECRET`; constant-time compare. Verify: missing/invalid token returns 401; Canvas token alone is rejected.
- [x] 2.3 Create `app/security/redaction.py` `RedactionFilter` masking `Bearer …`, `gAAAAA…` (Fernet), `postgresql://` / `postgresql+psycopg://`, dict keys `Authorization`/`token`/`ciphertext`/`password`/`database_url`. Verify: log assertion shows no plaintext.
- [x] 2.4 Create `app/core/deps.py` chain `verify_backend_jwt() → require_tenant() → require_tenant_token()`; raises 403 if any step skipped. Verify: `tenant_id` derived from JWT issuer only; client body/query `tenant_id` ignored.
- [x] 2.5 Add SQLAlchemy `canvas_credentials` (ciphertext BYTEA, key_version, created_at, rotated_at) and `tenants` models (stub; full schema in PR 3). Verify: insert stores Fernet ciphertext only.
- [x] 2.6 Create `app/controllers/auth.py` `POST /auth/canvas/connect`; probes `GET /users/self`; rejects on 401. Side effect: probes Canvas per request. Verify: 204 on success; 401 `code: "canvas_token_invalid"` with redacted body.
- [x] 2.7 Create `app/services/tenant_service.py` `get_or_create_tenant(backend_user_id)`. Verify: same backend user resolves to same tenant across calls.

## Phase 3: Schema (PR 3) — depends on PR 2

- [x] 3.1 Create `app/models/__init__.py` with `TenantMixin` (`tenant_id UUID NOT NULL`, `created_at`, `updated_at`, `deleted_at`) + declarative base. Verify: every model inherits the mixin.
- [x] 3.2 Create SQLAlchemy models `users`, `courses`, `enrollments`, `assignments`, `submissions`, `sync_state` with `UNIQUE(tenant_id, canvas_id)` indexes and `score_statistics` JSONB on `assignments`. Side effect: PG schema additions.
- [x] 3.3 Create first Alembic migration `0001_init.py` (additive); does NOT touch `documents` PGVector table. Verify: `alembic upgrade head` succeeds; PGVector row count unchanged.
- [x] 3.4 Create `app/repositories/` upsert helpers keyed on `(tenant_id, canvas_id)`; cross-tenant FK comparator. Verify: cross-tenant insert rejected with explicit error.
- [x] 3.5 Soft-delete helper `mark_inactive(workflow_state)` per spec; inactive state sets `deleted_at`. Verify: inactive rows absent from `/query` results.
- [x] 3.6 Update `src/chroma_db.py`: remove destructive `rmtree`; gate behind `--reset` flag; default upsert. Risk: R6 (destructive reset).
- [x] 3.7 Document read-only Postgres role `pg_role_canvas_readonly` in `alembic/README.md` (DDL snippet; role granted SELECT on the seven tables only). Risk: role misconfiguration breaks text-to-SQL executor.

## Phase 4: Sync (PR 4) — depends on PR 3

- [x] 4.1 Create `app/canvas/dto.py` with Pydantic DTOs (`UserDTO`, `CourseDTO`, `EnrollmentDTO`, `AssignmentDTO`, `SubmissionDTO`, `ScoreStatisticsDTO`); unknown keys dropped at parse; peer-identifying fields stripped unless `user_id == tenant.user_id`. Side effect: DTO whitelist contract.
- [x] 4.2 Create `app/canvas/client.py` `CanvasClient` GET-only assertion (`ALLOWED_METHODS = {"GET"}`), `tenacity` retry 3 attempts 1s/2s/4s on 5xx, 8s timeout. Verify: POST/PUT/PATCH/DELETE raise `CanvasMethodRejected` without network.
- [x] 4.3 Create `app/canvas/pagination.py` cursor follower via `Link: <url>; rel="next"`, `per_page=100`. Verify: multi-page fixture consumed exactly once per item.
- [x] 4.4 Create `app/sync/pipeline.py` idempotent upsert keyed on `(tenant_id, canvas_id)`; watermark advances in same transaction. Verify: replay-equivalence test produces same DB state.
- [x] 4.5 Create `app/sync/lock.py` using `pg_try_advisory_xact_lock(hashtext(tenant_id::text))`; released at tx end. Verify: concurrent manual + scheduled → second returns 429 `Retry-After`.
- [x] 4.6 Create `app/sync/scheduler.py` `AsyncIOScheduler` + `CronTrigger(hour="*/6")` + per-tenant jitter; worker pool cap 4/process. Side effect: scheduled writes every 6h + jitter. Verify: jitter between tenants; tick acquires lock.
- [x] 4.7 Create `app/services/canvas_service.py` orchestrating DTO→repo mapping; soft-delete on inactive `workflow_state`. Side effect: PG writes per sync run.
- [x] 4.8 Create `app/controllers/sync.py` `POST /sync` rate-limited 1/60s/tenant + lock. Verify: throttled → 429; Canvas failure → `sync_state.last_status='failed'`, watermark unchanged, prior data still queryable.

## Phase 5: Router (PR 5) — depends on PR 4

- [x] 5.1 Create `app/rag/router.py` deterministic rules (date/count/grade/status/aggregate → relational; explain/summarize → semantic; mixed → hybrid); Gemini only on ambiguity with constrained labels `{relational, semantic, hybrid, unsupported}`. Verify: rule hit triggers no model call; ambiguity falls back to classifier.
- [x] 5.2 Create `app/text_to_sql/allow_list.py` registry of templates (`assignments_due_between`, `assignment_score`, `course_aggregate`, `submission_status_for_assignment`, `class_score_statistics`) with `{{tenant_id}}` and named slots. Verify: LLM cannot inject SQL.
- [x] 5.3 Create `app/text_to_sql/validator.py` using `sqlglot.parse` (sqlparse fallback) enforcing SELECT-only, no DDL/DML, no `pg_*`/`set_config`. Failure → 409 `sql_not_allowed` with no SQL or detail in body. Verify: rejected queries never reach DB.
- [x] 5.4 Create `app/text_to_sql/executor.py` SQLAlchemy session with `SET LOCAL default_transaction_read_only=on`, `statement_timeout=2000`, `idle_in_transaction_session_timeout=10000`, `app.tenant_id`; server-side `LIMIT 200`; `tenant_id` appended after slot substitution. Verify: tenant-injection attempt fails; 2s timeout fires.
- [x] 5.5 Create `app/text_to_sql/templates/*.py` per template. Verify: each template round-trips with stub DB returning expected rows.
- [x] 5.6 Create `app/rag/vector_store.py` wrapping existing PGVector `documents` table; server-injected `tenant_id` filter on every similarity search; content-hash-keyed upsert; NO `rmtree` path. Risk: R6 (must not introduce `shutil.rmtree`). Verify: cross-tenant similarity search returns 0 rows.
- [x] 5.7 Create `app/rag/prompts.py` language-detected prompts; refuses to fabricate. Verify: unsupported semantic-only request returns bounded response in detected language.
- [x] 5.8 Create `app/services/rag_service.py` `provider_health()` flips `embedding_available=False` on Ollama failure; router skips vector/hybrid when down. Verify: degraded mode routes only relational questions; vector/hybrid return bounded unavailability response.
- [x] 5.9 Create `app/rag/hybrid.py` orchestrating vector recall (k=20) → constrained SQL aggregate using retrieved IDs. Verify: hybrid answer uses retrieved IDs only.

## Phase 6: HTTP Layer (PR 6) — depends on PR 5

- [x] 6.1 Create `app/controllers/query.py` `POST /query` running full dep chain; returns `{answer, lang, route, correlation_id}`. Verify: missing dependency → 403; `tenant_id` from JWT only.
- [x] 6.2 Create `app/controllers/health.py` `GET /healthz` reporting ollama + db + scheduler status. Verify: degradation visible in response.
- [x] 6.3 Create `app/services/ingest_service.py` HTML sanitization via `bleach` for `assignment.description` and `submission.body` before chunking/embedding. Verify: `<script>` and unsafe markup stripped; safe readable text retained.
- [x] 6.4 Create `app/observability/metrics.py` `prometheus_client` counters: `rag_requests_total`, `sql_validations_total`, `canvas_requests_total`, `sync_runs_total`, `sync_lag_seconds`, `token_decrypt_failures_total`. Verify: `/metrics` exposes all counters.
- [x] 6.5 Create `app/observability/tracing.py` optional OTLP exporter (off by default). Side effect: opt-in spans only.
- [x] 6.6 Middleware: `correlation_id` (UUID v4) in response header + every log line. Verify: id matches request↔response.
- [x] 6.7 End-to-end smoke: sync → query → answer in detected language; relational-only when Ollama down. Verify: ADDED/MODIFIED scenarios across all five delta specs pass.

## Phase 7: Verification (post-PR 6) — depends on PR 6

- [x] 7.1 Map every ADDED/MODIFIED scenario in the five delta specs to an automated or manual verification step. Verify: no orphan requirement.
- [x] 7.2 Confirm PGVector `documents` table untouched by all migrations. Verify: pre/post row counts equal.
- [x] 7.3 Confirm no `shutil.rmtree` path remains in normal ingestion. Verify: grep returns 0 in `app/`.
- [x] 7.4 Confirm redacted log assertion passes across Canvas 401, SQL reject, exception with token. Verify: no plaintext credential in any log line.
