# Apply Progress: PR 3 Schema + PR 4 Sync + PR 5 Router + PR 6 HTTP Layer

**Change/status:** `build-canvas-rag-backend` / `apply: completed` (PR 6)
**Mode:** Standard (`strict_tdd: false`)
**Work unit:** Phase 6, PR 6 — HTTP Layer; `stacked-to-main`

## Completed

All previously recorded PR 3, PR 4 and PR 5 tasks remain complete.
PR 6 tasks 6.1–6.7 are complete:

- [x] 6.1 `app/controllers/query.py` — `POST /query` with the full
  dep chain (`verify_backend_jwt_dependency → require_tenant →
  require_tenant_token`), Pydantic `extra="forbid"` so clients
  cannot smuggle `tenant_id`, response shape
  `{answer, lang, route, correlation_id}`.
- [x] 6.2 `app/controllers/health.py` — `GET /healthz` extended to
  report ollama + db + scheduler status; `status` flips to
  `degraded` whenever any dependency is unavailable.
- [x] 6.3 `app/services/ingest_service.py` — `sanitize_html(text,
  *, allowed_tags=None, allowed_attrs=None, strip=False)` with
  presets `strip_all` (no tags) and `readable`
  (`p, br, strong, em, ul, ol, li, a` with `href`). The
  `prepare_assignment_chunks(assignment) -> list[Document]` helper
  sanitizes both `assignment.description` and the related
  `submission.body` and skips empty content.
- [x] 6.4 `app/observability/metrics.py` — six counters/gauges
  declared via `prometheus_client.Counter` /
  `prometheus_client.Gauge`:
  `rag_requests_total`, `sql_validations_total`,
  `canvas_requests_total`, `sync_runs_total`, `sync_lag_seconds`,
  `token_decrypt_failures_total`. Helpers
  (`record_rag_request`, `record_sql_validation`,
  `record_canvas_request`, `record_sync_run`, `set_sync_lag`,
  `record_token_decrypt`) swallow any prometheus error so a
  misconfigured registry never breaks a request.
- [x] 6.5 `app/observability/tracing.py` — optional OTLP exporter
  (`OTEL_ENABLED=1`); off by default. When disabled, `get_tracer`
  returns a `_NoopTracer` whose spans accept `set_attribute`,
  `record_exception`, `end` and `__enter__/__exit__`.
- [x] 6.6 `app/middleware/correlation_id.py` — `CorrelationIdMiddleware`
  generates a UUID v4 per request (or honours an inbound
  `X-Correlation-ID` when it parses as v4), binds it to the
  structlog `contextvars`, echoes it in the response header, and
  clears the binding on the way out. PR 6 also updated
  `app.core.errors` so error bodies and the header agree on the
  same id (reused the bound `correlation_id` instead of minting a
  fresh one in every handler).
- [x] 6.7 `app/main.py` — wires every controller (`auth`, `query`,
  `sync`, `health`), the `CorrelationIdMiddleware`, the optional OTLP
  exporter (lifespan-managed), and the lifespan-managed scheduler.
  `LEGACY_MODE` remains a no-op toggle for the modern API path —
  the FastAPI surface is always live so existing clients keep
  working.

PR 6 also touched:

- `app/services/rag_service.py` — `RAGService.answer(...)` now
  accepts an explicit `language` keyword so the controller can
  thread the language it detected (one detection per request).
- `app/core/db.py` — `make_engine_from_settings(...)` forwards a
  `connect_args` dict so the health endpoint can build an engine
  with a short connect timeout.
- `tests/smoke/test_healthz.py` — `test_healthz_returns_ok_with_status_200`
  updated to match the PR 6 `/healthz` shape (status +
  `ollama`/`db`/`scheduler` blocks + `rag_routes_disabled` flag).

## Work Unit Evidence

| Evidence | Required value | Exact result |
|---|---|---|
| Focused test command | `python -m app.services.ingest_service`, `python -m app.observability.metrics`, `python -m app.observability.tracing`, `python -m app.middleware.correlation_id`, `python -m app.controllers.query`, `python -m app.controllers.health`, `python -m app.services.rag_service` | All embedded `_selftest()` assertions passed; no network, secrets, PII, Ollama, Gemini, or database |
| Runtime harness | FastAPI `TestClient` driven through `CorrelationIdMiddleware`, dep chain, exception handlers, and `httpx.MockTransport`-equivalent TestClient stubs | `/healthz` returns the per-dependency report with degradation visible; `POST /query` returns `{answer, lang, route, correlation_id}` and rejects `tenant_id` injection with 422 |
| Pytest | `python -m pytest -q tests/ --no-header -p no:cacheprovider` | 213 passed, 288 warnings in 30.22s |
| Rollback boundary | `DISABLE_RAG_ROUTES=1` short-circuits `POST /query` to 503; remove `app/observability/`, `app/middleware/correlation_id.py`, and the four controller additions to revert PR 6 without touching PR 5 |

## Deviations / Risks

- `RAGService` is a PR 5 service seam; PR 6 extends it with an
  optional `language` kwarg to avoid double detection (once at the
  HTTP boundary, once inside the service). Backwards compatible.
- The PR 1 health smoke test
  (`test_healthz_returns_ok_with_status_200`) was the only test
  that asserted the legacy `{status: "ok"}` payload; PR 6
  updates that assertion to match the new shape. The other three
  tests in `tests/smoke/test_healthz.py` were left intact and now
  pass thanks to the bound-`correlation_id` reuse in
  `app.core.errors`.
- `pgvector` and `shutil.rmtree` paths remain untouched (PR 5
  invariants preserved).
- `_selftest()` for the controllers uses `TestClient` with a stub
  RAG service so the assertion path never reaches Ollama, Gemini,
  or the database.
- Token decryption failures are counted via
  `record_token_decrypt(result)` so the design §9 counter remains
  visible without needing to wire the call into
  `TenantService` (PR 7 verification territory).

## Next

Proceed to `sdd-verify-pr6`.

## Post-archive fix: auth wiring

**Status:** completed on 2026-08-04 as one autonomous `stacked-to-main`
post-archive work unit (standard mode; no new files under `tests/`).
**Review impact:** 773 authored additions + deletions, within the configured 800-line budget.

### Diagnosis and implementation

- `POST /auth/canvas/connect` resolved the process-wide in-memory
  `TenantService`, so a successful Canvas probe returned 204 without writing
  `tenants` or `canvas_credentials` through SQLAlchemy.
- `TenantRepository` now exposes the durable contract required by the HTTP
  boundary: `get_or_create_tenant(...) -> UUID`, encrypted credential upsert
  via `store_canvas_token(...) -> bool`, and tenant/key-version lookup via
  `get_canvas_token(...) -> bytes | None`. `TenantService` retains its
  `_memory_store` compatibility path and the original plaintext-plus-cipher
  call shape used by PR 2/3 tests.
- `app.controllers.auth` now injects `Depends(get_db_session)`, probes Canvas
  `GET /users/self` before any write, encrypts with `TokenCipher`, commits the
  tenant/credential rows, and returns the existing secret-safe
  `canvas_token_invalid` 401 response when validation fails.
- The canonical app marks tenant custody as SQLAlchemy-backed. The dependency
  chain, the manual sync controller's existing session handoff, Canvas service,
  and scheduler now read the persisted credential by server-derived
  `tenant_id`; standalone PR 2/3 harnesses retain the explicit in-memory
  fallback.
- `app.sync.pipeline` already receives a constructed GET-only `CanvasClient`
  plus server-derived `tenant_id` and contained no in-memory credential-store
  reference, so no pipeline change was required.
- Pytest settings sources now ignore deployment `.env` / secret-file sources
  while a test is running. This keeps the offline suites from consuming real
  deployment credentials; production `.env` loading is unchanged.

### Work Unit Evidence

| Evidence | Command / boundary | Exact result |
|---|---|---|
| Focused tests | `python.exe -m pytest -q --no-cov tests/unit/test_tenant_service.py tests/unit/test_tenant_service_session.py tests/unit/test_canvas_service.py tests/unit/test_deps_chain.py tests/unit/test_sync_scheduler.py tests/smoke/test_auth_controller.py tests/smoke/test_sync_controller.py` | 53 passed, 209 warnings, exit 0 |
| Runtime harness | `python.exe -m app.controllers.auth` with FastAPI `TestClient`, SQLite `StaticPool`, and `httpx.MockTransport` | exit 0; accepted token created one encrypted `canvas_credentials` row; rejected token returned 401 and did not rotate the persisted ciphertext |
| Repository/service selftests | `python.exe -m app.services.tenant_service` and `python.exe -m app.services.canvas_service` | exit 0; repository insert/update/key-version lookup and SQL-first credential resolution passed |
| Unit suite | `python.exe -m pytest -q --no-cov tests/unit` | 177 passed, 116 warnings in 21.81s, exit 0 |
| Smoke suite | `python.exe -m pytest -q --no-cov tests/smoke` | 36 passed, 172 warnings in 4.81s, exit 0 |
| Lint | `python.exe -m ruff check app/ main.py` | `All checks passed!`, exit 0 |
| Runtime isolation | SQLite in memory + synthetic Fernet material + `httpx.MockTransport`; no Supabase or Canvas connection | PASS |
| Rollback boundary | Revert the auth/session wiring in `app/controllers/auth.py`, `app/core/{db,deps}.py`, `app/services/{tenant,canvas}_service.py`, and `app/main.py`; revert pytest source isolation in `app/core/config.py` | Existing schema/migration and unrelated RAG behavior remain untouched |

### Preserved behavior

Redaction, bound `correlation_id`, `DISABLE_RAG_ROUTES`, and `LEGACY_MODE`
remain intact. No commit or push was created. Deployment validation remains an
operator action; resume the Supabase guide from the JWT/token-connect checks
without reusing or exposing credential values.
