# Flujo de extremo a extremo del backend Canvas RAG

> Documento narrativo del comportamiento real del monolito FastAPI en `Primer RAG`. Módulo por módulo, función por función, qué recibe cada endpoint, qué funciones se invocan, qué devuelve y por qué.
>
> Versión del backend: build consolidado (`771d43b` + `b331b6e` + arranque). Tests: 213 verde, ruff limpio, pip check limpio.
>
> Última verificación manual contra el código: 2026-08-03.

---

## 1. Top-level: cómo arranca el servidor

`PROYECTOS/Primer RAG/main.py` es el bootstrap. Una sola función `main()` decide:

- `LEGACY_MODE=0` (default): llama `_run_uvicorn()`. Ejecuta `uvicorn.run("app.main:app", host=..., port=...)`. uvicorn importa `app.main` y FastAPI instancia `app = create_app()`.
- `LEGACY_MODE=1`: cae al modo script original (no aplica al flujo moderno).

`app.main.create_app()`:

1. Lee `Settings` una vez con `get_settings()` (cache de `lru_cache`).
2. Marca `app.state[SESSION_STORE_STATE_FLAG] = True` → le indica a `TenantService.should_use_session_store(...)` que use Postgres-backed.
3. `register_exception_handlers(app)` → cablea `app/core/errors.py` (validación, HTTPException, catch-all).
4. `app.add_middleware(CorrelationIdMiddleware)` → `app/middleware/correlation_id.py` corre antes que cualquier handler.
5. `app.include_router(...)` para `auth`, `query`, `sync`, `health`.

`app.main.lifespan()` se ejecuta al arrancar y apagar:

- Si `SCHEDULER_ENABLED=true`: crea engine, `CanvasService`, `SyncScheduler`, lo arranca y lo guarda en `app.state`.
- En `finally`: llama `scheduler.shutdown()` y `engine.dispose()`.

---

## 2. El middleware global: `CorrelationIdMiddleware`

`app/middleware/correlation_id.py::CorrelationIdMiddleware.dispatch` corre en cada request, **antes** del endpoint:

```python
1. incoming_id = request.headers.get("X-Correlation-ID")
2. correlation_id = incoming_id if _is_valid_uuid4(incoming_id) else new_correlation_id()
3. bind_correlation_id(correlation_id)                # structlog contextvar
4. response = await call_next(request)                  # endpoint real
5. response.headers["X-Correlation-ID"] = correlation_id
6. clear_correlation_id()                                # structlog cleanup
```

Reglas clave:

- Si el cliente manda un `X-Correlation-ID` válido (UUID v4), se respeta.
- Si manda un valor inválido, se descarta y se genera uno nuevo.
- Si no manda nada, se genera uno nuevo.
- El id se vincula al `ContextVar` de structlog → todas las líneas de log de la request lo llevan.
- El id se copia al header `X-Correlation-ID` de la respuesta.

---

## 3. Manejo de errores global

`app/core/errors.py` + `register_exception_handlers(app)` en `app/main.py` cubre 3 tipos:

| Excepción | Handler | Status | Comportamiento |
|---|---|---|---|
| `StarletteHTTPException` | `http_exception_handler` | el del exception | Log con `correlation_id`, `code`, `message` redactado. Cuerpo `ErrorBody` con `X-Correlation-ID`. |
| `RequestValidationError` | `validation_exception_handler` | 422 | Devuelve `errors()` redactado por `_safe_dict`. |
| `Exception` (catch-all) | `unhandled_exception_handler` | 500 | `error_class` en el cuerpo, mensaje genérico, `safe_message` redacta el mensaje original. |

`safe_message` aplica 4 regex: `gAAAAA…` (Fernet), `Bearer …`, `postgresql[+\w]+://`, blobs largos. `_safe_dict` aplica la redacción a dicts y listas anidadas.

Cada error lleva `X-Correlation-ID` en la respuesta y queda trazado en logs.

---

## 4. Endpoints en detalle

El backend expone 4 endpoints:

| Método | Path | Auth | Descripción |
|---|---|---|---|
| `GET` | `/healthz` | no | Health check de Ollama, DB, scheduler. |
| `POST` | `/auth/canvas/connect` | JWT | Valida y cifra el token Canvas del usuario. |
| `POST` | `/sync` | JWT + tenant + token | Sincroniza cursos/tareas del usuario desde Canvas. |
| `POST` | `/query` | JWT + tenant + token | Pregunta NL → RAG (SQL o vectorial). |

Los tres últimos comparten una cadena de **3 dependencias obligatorias**:

```
verify_backend_jwt_dependency  →  require_tenant  →  require_tenant_token
       (user_id str)              (tenant_id UUID)        (tenant_id, plaintext)
```

Si cualquier paso falla, el endpoint no se ejecuta y se devuelve 401/403.

---

### 4.1 `GET /healthz`

**Ruta**: `app/controllers/health.py::healthz`.

```
GET /healthz
↓
CorrelationIdMiddleware (middleware_id, bind)
↓
healthz(request)                                            # app/controllers/health.py
   ├── _probe_ollama()                                      # línea 41
   │   └── RAGService().provider_health()                    # app/services/rag_service.py
   │       └── RAGRouter.embedding_available                  # app/rag/router.py
   ├── _probe_db(settings)                                  # línea 56
   │   └── make_engine_from_settings(settings, connect_args={"connect_timeout": 2})
   │       └── engine.connect() → SELECT 1
   └── _probe_scheduler(request)                             # línea 96
       └── request.app.state.scheduler
↓
JSONResponse (status 200 siempre)
{"status": "ok"|"degraded", "ollama": {...}, "db": {...}, "scheduler": {...}, "rag_routes_disabled": bool}
```

Notas:

- NO requiere auth.
- `_probe_ollama` no hace una llamada a Ollama real; lee el estado cacheado de `RAGService`. La línea 49 crea un `RAGService` nuevo cada vez, lo cual es ineficiente en producción pero suficiente para un endpoint de health.
- `_probe_db` crea un engine nuevo con `connect_timeout=2` para no colgar el endpoint si Postgres está inaccesible, y lo dispone al final.
- El `status` global es `ok` sólo si las 3 dependencias reportan `available=true` y el scheduler está running. En cualquier otro caso, `degraded`.

---

### 4.2 `POST /auth/canvas/connect`

**Ruta**: `app/controllers/auth.py::connect_canvas`. No montado en `app.main` actual, es opt-in.

```
POST /auth/canvas/connect     Headers: Authorization: Bearer <jwt>, X-Canvas-Token: <plain>
|                              Body: { "canvas_token": "<plain>" }  (no se usa, ver nota)
↓
CorrelationIdMiddleware
↓
connect_canvas(
    request,                                          # app/controllers/auth.py línea 110
    canvas_token: <X-Canvas-Token header>,
    user_id: Depends(verify_backend_jwt_dependency),     # app/core/deps.py línea 54
    settings: Depends(get_settings),                    # app/core/config.py
    session: Depends(get_db_session),                  # app/core/db.py línea 126
    probe_transport: Depends(get_canvas_probe_transport),  # None en prod
)
   ├── _probe_canvas(canvas_token, settings)            # línea 53
   │   └── httpx.AsyncClient().get(f"{settings.canvas_api_base_url}/users/self",
   │                             headers={"Authorization": f"Bearer {canvas_token}"}, timeout=8.0)
   ├── if response.status == 401          → _canvas_invalid_response()  # 401
   ├── if response.status >= 500         → HTTPException(502, "canvas_unavailable")
   ├── if response.status >= 400         → _canvas_invalid_response()
   ├── cipher = TokenCipher(settings.tenant_token_key)   # app/security/token_crypto.py
   │   └── envelope = cipher.encrypt(canvas_token)
   ├── service = _tenant_service_for_request(request, session, cipher)  # línea 89
   │   └── TenantService(ciphers={cipher.key_version:cipher}, session=session)
   ├── tenant = service.get_or_create_tenant(user_id)   # app/services/tenant_service.py
   │   └── TenantRepository.get_or_create_tenant()      # SELECT ... INSERT ...
   ├── persisted = service.store_canvas_token(
   │       tenant.id,
   │       encrypted_ciphertext=envelope.ciphertext,
   │       key_version=envelope.key_version)
   │   └── TenantRepository.upsert_canvas_credential()  # INSERT/UPDATE canvas_credentials
   ├── if not persisted          → HTTPException(500, "tenant_persistence_failed")
   └── session.commit()
   → Response(204)
```

Notas:

- El `X-Canvas-Token` viene por header, no por body. El cuerpo JSON es opcional y se ignora.
- La cadena de auth del backend (`verify_backend_jwt_dependency`) sólo verifica el JWT; no toca la DB.
- El probe a Canvas se hace **antes** de cualquier escritura. Si Canvas rechaza el token (401) o no responde (5xx), nada se persiste.
- El token en claro se cifra con Fernet (`TokenCipher`) y sólo se guarda el ciphertext en `canvas_credentials.ciphertext`.
- `session.commit()` confirma la transacción. El advisory lock de Postgres no entra en juego acá (eso es para `/sync`).

---

### 4.3 `POST /sync`

**Ruta**: `app/controllers/sync.py::post_sync`. No montado en `app.main` actual.

```
POST /sync
↓
CorrelationIdMiddleware
↓
Dependencies (resuelven en cascada):
  1. verify_backend_jwt_dependency(authorization)         # app/core/deps.py línea 54
       └── _bearer_authorization(authorization)             # extrae "Bearer <jwt>"
       └── verify_backend_jwt(token)                       # app/security/backend_auth.py
       └── returns user_id (str) from JWT "sub" claim
  2. require_tenant(user_id, settings, session)         # app/core/deps.py línea 83
       └── _tenant_service_for_request(request, session, settings)
       └── service.get_or_create_tenant(user_id)
       └── returns tenant_id (UUID)
  3. require_tenant_token(tenant_id, settings, session) # app/core/deps.py línea 101
       └── service.get_decrypted_canvas_token(tenant_id)
       └── returns (tenant_id, plaintext_canvas_token)
↓
post_sync(tenant_context, settings, service)            # app/controllers/sync.py línea 138
   ├── tenant_uuid = _coerce_uuid(raw_tenant_id)
   ├── session_generator = get_db_session(settings)     # app/core/db.py:126
   ├── session = next(session_generator)
   ├── try:
   │   service.enforce_manual_rate_limit(session, tenant_uuid)
   │       └── if not SyncThrottled → stamp "manual_sync" row in sync_state
   │   │   session.commit()                             # commit BEFORE long Canvas fetch
   │   ├── if SyncThrottled → 429 with Retry-After header
   │   ├── result = await service.run_sync_for_tenant(tenant_uuid, session=session)
   │       # app/services/canvas_service.py línea 63
   │       ├── self._resolve_canvas_token(session, tenant_id)
   │       │   └── if session: load ciphertext from SQL, decrypt with TokenCipher
   │       │   └── fallback: in-memory service
   │       ├── client = CanvasClient(base_url, token_provider=lambda: token)
   │       │   └── app/canvas/client.py - GET only, 3 retries, 8s timeout
   │       └── self._run_locked(session, tenant_id, client)
   │           ├── scope = session.begin_nested() if session.in_transaction() else session.begin()
   │           ├── with scope:
   │           │   ├── acquired = try_acquire_sync_lock(session, tenant_id)
   │           │   │   └── app/sync/lock.py:161
   │           │   │       ├── if dialect == "postgresql":
   │           │   │       │   └── SELECT pg_try_advisory_xact_lock(hashtext(:tenant_id)::bigint)
   │           │   │       └── else: _memory_lock_for(tenant_id).acquire(blocking=False)
   │           │   ├── if not acquired → return SyncResult(status="locked")
   │           │   └── await sync_tenant(session, tenant_id, client, lock_id=str(tenant_id))
   │           │       # app/sync/pipeline.py línea 164
   │           │       ├── read_watermark(session, tenant_uuid, "users")
   │           │       ├── _sync_self_user → GET /users/self → UserDTO → upsert
   │           │       ├── _sync_favorite_courses → GET /users/self/favorites/courses
   │           │       │   └── for each course:
   │           │       │       ├── upsert_by_canvas_id(Course, ...)
   │           │       │       ├── soft_delete_if_inactive(...)
   │           │       │       └── upsert enrollments (only self)
   │           │       ├── if embedded_enrollments == 0: _sync_fallback_enrollments
   │           │       ├── for each course: _sync_course_assignments
   │           │       │   └── GET /courses/{id}/assignments?include[]=submission,score_statistics
   │           │       │       ├── upsert_by_canvas_id(Assignment, ...)
   │           │       │       └── if own submission: upsert_by_canvas_id(Submission, ...)
   │           │       ├── advance_watermark(session, "users", run_at, status="ok")
   │           │       └── return SyncResult(status="ok")
   │           └── finally: release_sync_lock if acquired
   ├── if TenantCredentialsMissing → 403 tenant_credentials_missing
   ├── if result.locked → 429 sync_locked
   ├── session.commit()
   └── JSONResponse(202, {status, last_successful_at, last_status, last_error_class, correlation_id})
```

Notas:

- La dependencia 2 (`require_tenant`) y la 3 (`require_tenant_token`) están anidadas: la 3 depende de la 2, que depende de la 1. FastAPI las resuelve en orden.
- El rate-limit se chequea **antes** del fetch a Canvas: si violás, obtenés 429 inmediato sin tráfico saliente.
- El `session.commit()` después del rate-limit stamp es importante: el `_stamp_manual_run` agrega (o actualiza) una fila en `sync_state` con `last_run_at=now`. Queda persistente.
- Todo el sync de Canvas ocurre dentro de **una sola transacción** que incluye los upserts y el watermark. Si algo falla, rollback y `last_status='failed'` se escribe en una transacción separada (`_finish_failure`).
- `release_sync_lock` se llama en `finally`, pero Postgres libera el advisory lock automáticamente al commit/rollback.

---

### 4.4 `POST /query`

**Ruta**: `app/controllers/query.py::post_query`. No montado en `app.main` actual.

```
POST /query     Headers: Authorization: Bearer <jwt>
                  Body: { "question": "<texto>", "language": "<ISO-639-1 opcional>" }
↓
CorrelationIdMiddleware
↓
Dependencies (mismas 3 que /sync):
  1. verify_backend_jwt_dependency → user_id
  2. require_tenant → tenant_id
  3. require_tenant_token → (tenant_id, plaintext_canvas_token)
   NOTE: el token plaintext NO se usa directamente acá; RAGService usa el SQL store.
↓
post_query(payload, tenant_context, rag_service, settings)   # app/controllers/query.py línea 115
   ├── if settings.disable_rag_routes → 503 rag_routes_disabled
   ├── correlation_id = get_correlation_id()
   ├── language = payload.language or detect_language(payload.question)
   │       └── detect_language en app/rag/prompts.py línea 8
   ├── rag_service.provider_health()                        # app/services/rag_service.py línea 25
   │   └── RAGRouter.embedding_available
   ├── record_rag_request(route=?, lang=language, outcome="error") if exception
   ├── result = rag_service.answer(question, tenant_id=tenant_id, language=language)
   │       # app/services/rag_service.py línea 30
   │       ├── lang = language or detect_language(question)
   │       ├── decision = self.router.route(question, language=lang)
   │       │       # app/rag/router.py línea 36
   │       │       ├── q = question.lower()
   │       │       ├── deterministic_rule(q):
   │       │       │   ├── match score/grade/count/... → "relational" (o "hybrid" si también explain)
   │       │       │   ├── match explain/summarize/meaning of/... → "semantic"
   │       │       │   ├── match "course #N" o "assignment #N" → "relational"
   │       │       │   └── else → None
   │       │       ├── if decision is None:
   │       │       │   ├── classifier(question) (None por default, devuelve "relational")
   │       │       │   └── si falla → "relational"
   │       │       └── if decision in {"semantic", "hybrid"} and not embedding_available:
   │       │           └── "semantic" → "unsupported", "hybrid" → "relational"
   │       ├── if decision.route == "unsupported":
   │       │       └── result = bounded_refusal(lang)        # app/rag/prompts.py línea 25
   │       ├── if decision.route == "relational":
   │       │       └── rows = self.sql_executor(tenant_id=tenant_id, sql=sql)
   │       │           # app/text_to_sql/executor.py - SELECT-only, SET LOCAL default_transaction_read_only=on,
   │       │           # statement_timeout=2000, idle_in_transaction=10000, tenant_id filtrado
   │       │           └── (sql_executor y llm son None en producción → devuelve string(rows))
   │       └── if decision.route == kSemantic|Hybrid":
   │           if no vector_store: bounded_refusal(lang)
   │           else: self.vector_store.similarity_search(question, k=20|"hybrid" or 8|"semantic")
   │                  + filtro tenant_id (del SQL store)
   └── result = {answer, lang, route}
   └── record_rag_request(route=result["route"], lang=language, outcome="ok")
   └── QueryResponse(answer, lang, route, correlation_id)
```

Notas:

- El cuerpo tiene `extra="forbid"`, así que `tenant_id` enviado por el cliente devuelve 422 sin llegar al handler.
- `lang` se autodetecta por palabras (es/en). El cliente puede forzarlo con `language: "es"`.
- `rag_service.answer` depende de `vector_store` y `sql_executor` que se inyectarían en PR 5 completo. En el estado actual ambos son `None`, así que siempre cae al path "relational" sin llegar a ejecutar SQL real (devuelve `str([])`).
- Si `embedding_available` es False y la ruta es "semantic" → cae a "unsupported" automáticamente.
- Prometheus `rag_requests_total` se incrementa por cada request con `outcome=ok|error|unknown`.

---

## 5. Diagrama de componentes

```
                  ┌───────────────────────────────────────────────────────┐
                  │ app/main.py · create_app() + lifespan()              │
                  │ · app.state.engine (lifespan)                       │
                  │ · app.state.scheduler (lifespan)                     │
                  │ · app.state.settings                                │
                  │ · app.state[SESSION_STORE_FLAG] = True              │
                  └─────────────────┬─────────────────────────────────┘
                                    │
            ┌───────────────────────┴───────────────────────────┐
            │ middleware (en orden)                             │
            │ 1. CorrelationIdMiddleware (X-Correlation-ID)     │
            │ 2. Exception handlers (registered by FastAPI)    │
            └───────────────────────┬───────────────────────────┘
                                    │
   ┌──────────────┬─────────────────┼─────────────────┬──────────────┐
   │              │                 │                 │              │
GET /healthz  POST /auth/...   POST /sync       POST /query
   │              │                 │                 │
   │              │                 │                 │
   ▼              ▼                 ▼                 ▼
healthz.py  auth.py            sync.py             query.py
   │              │                 │                 │
   │              │                 │                 │
   │              └─► deps.py  ◄────┘                 │
   │                  │                              │
   │                  │ verify_backend_jwt →        │
   │                  │ require_tenant →          │
   │                  │ require_tenant_token        │
   │                  │                              │
   │                  ▼                              │
   │           tenant_service.py                   │
   │           (SQL o memoria)                       │
   │                                                 │
   │           canvas_service.py ◄──────┐          │
   │           (run_sync_for_tenant)        │          │
   │                                       │          │
   │           canvas_client.py ◄──────────┘          │
   │           (GET-only, retries, 8s)               │
   │                                                 │
   │           sync/pipeline.py                     │
   │           (DTOs, upserts, watermark)            │
   │                                                 │
   │           sync/lock.py                           │
   │           (pg_try_advisory_xact_lock)            │
   │                                                 │
   │                                          rag_service.py
   │                                          RAGRouter
   │                                          detect_language
   │                                          bounded_refusal
   │           sync_pipeline.py ◄────────────────────┘
   │           (re-uses canvas_client + canvas_service)
   ▼
core/handlers.py (JSONResponse con ErrorBody)
```

---

## 6. Auth chain para endpoints privados en detalle

`app/core/deps.py` define la cadena. Los tres pasos son obligatorios; omitir uno devuelve 401/403:

```
verify_backend_jwt_dependency(authorization: "Bearer <jwt>")
  ├── _bearer_authorization Header
  │   ├── parts = authorization.split(None, 1)
  │   ├── if not bearer or missing → 401 "missing Authorization header"
  │   └── parts[0].lower() != "bearer" → 401 "must be 'Bearer <token>'"
  └── verify_backend_jwt(token) → returns user_id (str)
      ├── jwt.get_unverified_header(token) → if alg != "HS256" → InvalidToken
      ├── jwt.decode(token, BACKEND_SECRET, algorithms=["HS256"], options={"require": ["sub"]})
      └── payload["sub"] is empty → InvalidToken
        → raise HTTPException(401, "invalid backend token")

require_tenant(user_id, settings, session)
  ├── _tenant_service_for_request(request, session, settings)
  │   ├── if should_use_session_store(app.state, session):
  │   │   └── TenantService(ciphers={cipher.key_version: cipher}, session=session)
  │   └── else: get_tenant_service() (singleton in-memory)
  └── service.get_or_create_tenant(user_id)
      ├── if _repository is not None:
      │   └── TenantRepository.get_or_create_tenant(user_id)
      │       └── SELECT * FROM tenants WHERE backend_user_id = ? → INSERT if missing
      └── else: in-memory dict
        → return TenantRecord(id=UUID, backend_user_id=str)
      → raise TenantNotFound('tenant resolution failed') → 403

require_tenant_token(tenant_id, settings, session)
  ├── service = _tenant_service_for_request(...)
  └── service.get_decrypted_canvas_token(tenant_id)
      ├── if _repository is not None:
      │   ├── row = TenantRepository.get_canvas_credential(tenant_id)
      │   ├── if row is None → TenantNotFound
      │   ├── cipher = self._cipher_map()[row.key_version]
      │   ├── if cipher is None → TenantNotFound
      │   └── TenantCipher.decrypt(EncryptedToken(row.ciphertext, row.key_version))
      └── else: in-memory dict
        → return plaintext or raise TenantNotFound
      → raise HTTPException(403, "no Canvas credentials for tenant")
```

**Importante**: el `user_id` que sale del JWT es el `sub` claim, y es la **única** fuente de `tenant_id`. Los clientes no pueden mandar `tenant_id` en el body porque `extra="forbid"` lo rechaza con 422 antes de llegar al handler.

---

## 7. Persistencia y cifrado

### Tablas y columnas clave

| Tabla | Columna | Tipo | Notas |
|---|---|---|---|
| `tenants` | `id` | UUID | PK, server-assigned. |
| `tenants` | `backend_user_id` | str | Unique. Es el `sub` del JWT. |
| `canvas_credentials` | `tenant_id` | UUID | FK a `tenants.id`. |
| `canvas_credentials` | `ciphertext` | bytes | Fernet (AES-128-CBC + HMAC-SHA256). |
| `canvas_credentials` | `key_version` | int | Para rotación. |
| `canvas_credentials` | `rotated_at` | timestamp | NULL al crear. |
| `sync_state` | `(tenant_id, table_name)` | PK | Watermark por tabla. |

### Cómo se cifra

```
TokenCipher(key).encrypt(plaintext) -> EncryptedToken(ciphertext: bytes, key_version: int)
```

- `key` viene de `settings.tenant_token_key` (Fernet urlsafe-base64 de 32 bytes).
- `key_version` arranca en 1.
- En el repo, `ciphertext` se guarda con `tenant_id` y `key_version`.

### Cómo se rota

Aún no implementado. El campo `key_version` existe para habilitarlo:

1. Generar nueva `key_version` (e.g. 2) con un nuevo `key_N`.
2. Re-encriptar todos los `canvas_credentials` con `key_2`, manteniendo `key_version=2`.
3. Cambiar `settings.tenant_token_key` a `key_2`.
4. El `TenantService.get_decrypted_canvas_token` ya consulta el `key_version` por fila y elige el cipher correspondiente.

---

## 8. Errores y redacción

| Patrón | Reemplazo | Aplicado en |
|---|---|---|
| `gAAAAA…` (Fernet envelope) | `***REDACTED***` | `safe_message`, `RedactionFilter` |
| `Bearer <token>` | `***REDACTED***` | `safe_message`, `RedactionFilter` |
| `postgresql[+\w]+://…` | `***REDACTED***` | `safe_message`, `RedactionFilter` |
| Blob alfanumérico de 40+ chars | `***REDACTED***` | `safe_message` |
| Keys `authorization`, `token`, `ciphertext`, `password`, `database_url`, `supabase_database_url`, `tenant_token_key`, `backend_secret`, `gemini_api_key`, `canvas_api_token`, `api_key`, `secret` | `***REDACTED***` | `RedactionFilter` (`redact_dict`) |

Nunca se loguea plaintext: ni en errores, ni en canvas_token_invalid, ni en el lifespan startup, ni en el success de `/auth/canvas/connect`.

---

## 9. Logs y correlation_id

`CorrelationIdMiddleware` setea un `ContextVar` que `app/core/logging.py` inyecta en cada `structlog` event. Estructura típica de una línea de log:

```json
{
  "event": "rag_answer_failed",
  "level": "error",
  "timestamp": "2026-08-03T13:00:00.000Z",
  "correlation_id": "0fa1b2c3-...",
  "tenant_id": "...",
  "lang": "es"
}
```

Para trazar una request de punta a punta:

```bash
grep '"correlation_id":"0fa1b2c3-..."' logs.json
```

Si el `X-Correlation-ID` de la respuesta y el que el cliente pasó se acoplan, esa línea aparece en TODOS los logs generados por la request, incluyendo los `print` de los embeddings, los warnings de Canvas y los errores 5xx.

---

## 10. Resumen del archivo por endpoint

| Endpoint | Módulo entrada | Dependencias | Servicios invocados | DB tables tocadas | Salida |
|---|---|---|---|---|---|
| `GET /healthz` | `app/controllers/health.py` | ninguna | `RAGService.provider_health()` | (probe `SELECT 1`) | `200 {status, ollama, db, scheduler, rag_routes_disabled}` |
| `POST /auth/canvas/connect` | `app/controllers/auth.py` | `verify_backend_jwt_dependency`, `get_db_session`, `get_settings` | `httpx.AsyncClient`, `TokenCipher`, `TenantService`, `TenantRepository` | `tenants`, `canvas_credentials` | `204` o `401 canvas_token_invalid` o `502 canvas_unavailable` |
| `POST /sync` | `app/controllers/sync.py` | 3 deps + `get_db_session` | `CanvasService.run_sync_for_tenant`, `CanvasClient`, `sync_tenant`, `try_acquire_sync_lock`, `upsert_by_canvas_id`, `soft_delete_if_inactive`, `advance_watermark` | `tenants`, `canvas_credentials`, `users`, `courses`, `enrollments`, `assignments`, `submissions`, `sync_state` | `202 {status, last_successful_at, last_status, last_error_class, correlation_id}` o `429` o `403` |
| `POST /query` | `app/controllers/query.py` | 3 deps + `get_rag_service` + `get_settings` | `provider_health`, `RAGRouter.route`, `detect_language`, `RAGService.answer`, `vector_store.similarity_search`, `sql_executor`, `record_rag_request` | (si ruta SQL: las que monte el executor) | `200 {answer, lang, route, correlation_id}` o `422` o `503` o `500` |

---

## 11. Variables de entorno necesarias

| Variable | Required | Default | Usada en |
|---|---|---|---|
| `SUPABASE_DATABASE_URL` | sí | — | `app/core/config.py` |
| `TENANT_TOKEN_KEY` | sí | — | `app/security/token_crypto.py` |
| `BACKEND_SECRET` | sí | — | `app/security/backend_auth.py` |
| `GEMINI_API_KEY` | sí | — | `app/services/rag_service.py` (vía LLM) |
| `OLLAMA_HOST` | sí | — | `app/rag/vector_store.py` |
| `CANVAS_API_BASE_URL` | sí | — | `app/services/canvas_service.py` |
| `OLLAMA_EMBEDDING_MODEL` | no | `qwen3-embedding:8b` | `app/core/config.py` |
| `OLLAMA_EMBED_DIM` | no | `1024` | `app/core/config.py` |
| `SYNC_INTERVAL_SECONDS` | no | `21600` | `app/sync/scheduler.py` |
| `SYNC_JITTER_SECONDS` | no | `60` | `app/sync/scheduler.py` |
| `MANUAL_SYNC_MIN_INTERVAL_SECONDS` | no | `60` | `app/services/canvas_service.py` |
| `SQL_STATEMENT_TIMEOUT_MS` | no | `2000` | `app/text_to_sql/executor.py` |
| `SQL_ROW_LIMIT` | no | `200` | `app/text_to_sql/executor.py` |
| `LOG_LEVEL` | no | `INFO` | `app/core/logging.py` |
| `SCHEDULER_ENABLED` | no | `True` | `app/main.py`, `app/sync/scheduler.py` |
| `DISABLE_RAG_ROUTES` | no | `False` | `app/controllers/query.py` |
| `LEGACY_MODE` | no | `0` | `main.py` (top-level) |

Todas las requeridas son **fail-closed**: `Settings(...)` lanza `ValidationError` antes de servir tráfico.

---

## 12. Cosas que valen la pena mirar cuando algo falla

1. `GET /healthz` → `db.available` y `ollama.available`. Si uno está degraded, ese es el sospechoso.
2. `GET /healthz` → `scheduler.running`. Si el scheduler no arrancó, la cola automática no funciona.
3. Logs con `correlation_id` → grep por el ID que devolvió el endpoint.
4. `sync_state.last_status='failed'` + `last_error_class` → buscar el caso correspondiente en `app/sync/pipeline.py`:
   - `auth_rejected` → token de Canvas expirado o revocado.
   - `canvas_unavailable` → Canvas 5xx o timeout.
   - `schema_drift` → Canvas cambió un campo del payload.
   - `unexpected` → mirar el traceback.
5. `pg_try_advisory_xact_lock` falla → pipeline conflict. Otro sync en vuelo o una transacción no cerrada.
6. `ciphertext` en base de datos parece Fernet → correcto. Si aparece `12345~abc` plano, alguien rompió el cifrado.
7. `X-Correlation-ID` en la respuesta no coincide con el de la request → bug en el middleware.

---

## 13. Lo que falta para cerrar la primera versión

- **PR 5 (router) está implementado en código pero el orquestador no ejecuta SQL ni PGVector en producción** porque `RAGService` se inicializa sin `vector_store` ni `sql_executor`. Para `/query` responda con datos reales, falta conectar PGVector (clase ya en `app/rag/vector_store.py`) y el executor SQL (clase en `app/text_to_sql/executor.py`).
- **El scheduler está cableado pero el ciclo de sync por tenant necesita un bucle por `tenants` con session fresca**. La función `_tick` de `app/sync/scheduler.py` lista `CanvasCredential.tenant_id` y dispatcha; el handler llama `asyncio.to_thread(self._tenant_service.get_decrypted_canvas_token, tenant_id)` pero la implementación actual no esa parte.
- **Falta OIDC/OAuth de Canvas para que los usuarios entreguen tokens sin copy-paste manual**. El alcance de v1 es token personal; OAuth queda para v2.
