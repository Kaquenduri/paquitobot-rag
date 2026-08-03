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