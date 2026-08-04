# Por qué existe cada pieza del backend Canvas RAG

> Documento narrativo del monolito FastAPI en `Primer RAG`. No documenta qué hace cada función; documenta **por qué** está ahí, **qué problema resuelve** y **con qué se conecta**. Cada endpoint se sigue módulo por módulo, función por función, pero con intención primero y mecánica después.
>
> Versión del backend: build consolidado (`771d43b` + `b331b6e` + arranque). Tests: 213 verde, ruff limpio, pip check limpio.

---

## 0. ¿Qué problema está resolviendo este backend?

Un estudiante de Tecsup abre la app, hace una pregunta ("¿Cuándo es la entrega de mates?"), y la app debe:

1. Saber **quién** pregunta (autenticación).
2. Saber **qué** datos tiene disponibles para ese usuario (sincronización con Canvas).
3. Saber **cómo** responder: con SQL sobre la base relacional, con embeddings sobre la documentación, o con un híbrido.
4. **No filtrar** datos de otros estudiantes.
5. **No filtrar** tokens, llaves ni URLs en logs.

Todo el código que leerás a continuación existe para resolver alguna de esas cinco preguntas.

---

## 1. El arranque — por qué existe `main.py` y `app/main.py`

`main.py` (en la raíz) es el **punto de entrada del proceso**. Existe porque necesitamos un único `python main.py` que decida si arranca el servidor moderno (FastAPI) o el script original (`LEGACY_MODE=1`). Es un wrapper, no lógica.

`app/main.py::create_app()` construye la **única instancia de FastAPI** del proceso. Existe porque FastAPI necesita una `app` global para registrar middleware, routers y handlers. Sin esto, uvicorn no sabría qué servir.

`app/main.py::lifespan()` es el **gancho de boot/shutdown**. Existe porque hay trabajo que sólo puede correr con la app ya construida y antes de que llegue la primera request: arrancar el scheduler, inicializar el tracer, crear el engine. Es la única vía soportada para "arrancar todo y luego esperar".

**Por qué se monta `CorrelationIdMiddleware` antes que cualquier router**: cada request entrante necesita un identificador único que la respuesta y todos los logs de la request compartan. Si el cliente manda `X-Correlation-ID`, se respeta; si no, se genera. Sin esto, debuggear un error 500 con dos usuarios concurrentes sería imposible.

**Por qué se monta cada router en `create_app()`** y no en el lifespan: los routers son inmutables; montarlos una sola vez en `create_app()` evita re-montarlos en cada boot del lifespan (lo que duplicaría handlers).

---

## 2. `app/core/deps.py` — la cadena de auth

Esta es la pieza más importante del backend. Existe para responder **"¿quién pregunta?"** sin permitir que el cliente mienta.

Hay tres pasos, en orden, ejecutados por FastAPI automáticamente:

```
verify_backend_jwt_dependency  →  require_tenant  →  require_tenant_token
```

### 2.1 `verify_backend_jwt_dependency`

**Por qué existe**: el backend no maneja el login (no hay tabla `users`, no hay bcrypt, no hay sesiones). La autenticación la hace otro servicio (futuro IdP) que devuelve un JWT firmado con `BACKEND_SECRET`. Acá sólo verificamos que ese JWT sea válido.

**Cómo se conecta**: extrae `Authorization: Bearer <jwt>` del header, lo decodifica con `BACKEND_SECRET`, exige `algorithms=["HS256"]` (rechaza cualquier otro algoritmo, así un atacante no puede usar `alg: none` o `alg: HS256` con su propio secret), exige `sub` (es el identificador del usuario), y devuelve esa `sub` como `user_id` (str).

**Por qué `sub` y no otro campo**: `sub` es el claim estándar de JWT para "subject", que es el usuario. Usar otro campo sería no-estándar.

### 2.2 `require_tenant`

**Por qué existe**: el token JWT identifica al usuario, pero no autoriza el tenant. Hace falta resolver un `tenant_id` (UUID) interno a partir del `user_id` (str) para usarlo en queries posteriores. Si el usuario no existe en la DB, se crea.

**Cómo se conecta**: recibe `user_id` de la dependencia anterior, abre una sesión SQLAlchemy, instancia el `TenantService` eligiendo el modo (SQL si el lifespan lo marcó, memoria si no) y llama `get_or_create_tenant(user_id)`. Éste mira `SELECT * FROM tenants WHERE backend_user_id = ?`; si no existe, hace `INSERT INTO tenants(backend_user_id) VALUES (?)` y devuelve el `id` UUID.

**Por qué `get_or_create_tenant` en lugar de fail if not exists**: esta es la primera vez que el usuario usa el backend. El sistema no puede fallar porque su tenant aún no fue provisionado.

### 2.3 `require_tenant_token`

**Por qué existe**: el último paso es traer el **token Canvas en claro** del tenant, porque las llamadas downstream a la API de Canvas necesitan ese token. Esta dependencia es la única que descifra el ciphertext y se queda con el plaintext en memoria.

**Cómo se conecta**: recibe `tenant_id` de la dependencia anterior, obtiene el ciphertext desde `canvas_credentials` (SQL), lo descifra con `TokenCipher` (Fernet) usando `key_version` de la fila, y devuelve la tupla `(tenant_id, plaintext)`.

**Por qué no se descifra en el controller**: el descifrado es una operación costosa (Fernet hace AES + HMAC) y además requiere la `TENANT_TOKEN_KEY`. Si lo hiciera el controller, no podría testearse sin configurar Fernet. Encapsularlo en la dependencia de FastAPI permite inyectar un TenantService con tokens pre-cargados en los tests.

**Por qué el plaintext se devuelve al controller**: porque el client Canvas (`CanvasClient`) necesita construir un header `Authorization: Bearer <token>` en cada request. Mantener el token en memoria dentro del scope de la request es aceptable porque la request es efímera; el plaintext nunca toca un log porque pasa por `RedactionFilter` antes de ser emitido.

---

## 3. `app/controllers/auth.py` — `POST /auth/canvas/connect`

**Por qué existe**: el usuario necesita poder dar su token de Canvas al backend. Sin este endpoint, el backend no podría leer los cursos del usuario. El token se guarda **cifrado** en la DB, no plaintext.

**Por qué se hace el probe a Canvas ANTES de persistir**: si el token es inválido, no se quiere escribir basura en la DB. El probe a `GET /users/self` confirma que Canvas lo acepta. La respuesta 401 de Canvas indica token revocado o mal copiado; una 5xx indica Canvas caído.

**Por qué se cifra con Fernet ANTES de ir a DB**: si alguien compromete la DB, no debería poder impersonar al usuario. Fernet agrega AES + HMAC + timestamp; sin la `TENANT_TOKEN_KEY`, el ciphertext es inútil.

**Por qué se usa `tenant_id` del JWT y no del body**: si el cliente pudiera mandar `tenant_id`, podría leer el token de otro usuario. Por eso el schema `QueryRequest` rechaza explícitamente cualquier campo extra (`extra="forbid"`).

**Por qué `setattr(app.state, SESSION_STORE_STATE_FLAG, True)`**: le dice al `TenantService` que use Postgres-backed storage en lugar del in-memory legacy. Sin esta marca, los tests PR 2/3 que crean su propio `TenantService` rompen.

---

## 4. `app/controllers/sync.py` — `POST /sync`

**Por qué existe**: el usuario debe poder forzar una sincronización inmediata. La versión automática (scheduler) corre cada 6 horas, pero si quiere datos frescos ya, llama a este endpoint.

**Por qué la cadena de 3 deps es la misma**: el sync es una operación con efectos, no una consulta. Necesitamos saber **quién** está pidiendo, **a quién** pertenece el tenant, y **con qué token** llamar a Canvas.

**Por qué `enforce_manual_rate_limit` ANTES del fetch a Canvas**: evitar que un usuario bombardee a Canvas. Si pidiéramos sync cada segundo, Canvas nos rate-limitearía y perderíamos acceso.

**Por qué `try_acquire_sync_lock` con `pg_try_advisory_xact_lock`**: dos syncs concurrentes del mismo tenant podrían pisarse. El lock advisory de Postgres es **transaction-scoped**: se libera automáticamente al commit o rollback, sin riesgo de leaks.

**Por qué `release_sync_lock` se llama en `finally`**: por si el dialecto es SQLite (dev), donde no hay advisory locks. En Postgres es un no-op.

**Por qué `_finish_failure` en una transacción SEPARADA**: si el sync falló a mitad de camino, no queremos que la fila de `sync_state` con `last_status='failed'` también se pierda en el rollback. La helper abre una transacción nueva, hace rollback del trabajo y persiste sólo el estado de fallo.

**Por qué `advance_watermark` se ejecuta al FINAL del sync exitoso**: garantiza que el cursor sólo avance si TODOS los upserts se commitearon. Si falla el sync, el watermark no se mueve y el próximo retry parte del último punto consistente.

**Por qué se persiste `manual_sync` en `sync_state` ANTES del fetch**: para que dos llamadas simultáneas de `/sync` (la una durante la otra) sepan que la segunda está re-ejecutando un sync ya en curso y devuelvan 429 inmediatamente.

---

## 5. `app/controllers/query.py` — `POST /query`

**Por qué existe**: el usuario hace preguntas en lenguaje natural. El backend debe elegir el camino correcto (SQL o vectorial), generar una respuesta y devolverla.

**Por qué `extra="forbid"` en `QueryRequest`**: el cliente no puede inyectar `tenant_id` ni otros campos para saltarse validaciones. El Pydantic valida ANTES de invocar el handler.

**Por qué `disable_rag_routes` feature flag**: en operaciones de mantenimiento (ej. reindexar embeddings), el operador quiere desactivar `/query` sin redesplegar. El endpoint devuelve 503 en lugar de colgar.

**Por qué `detect_language` heurística y no LLM**: la detección es rápida y determinística. Una palabra como "qué" o "promedio" marca español; sin esas marcas, inglés. No justifica gastar tokens de Gemini.

**Por qué `RAGRouter` determinístico primero, Gemini como fallback**: la mayoría de las preguntas son determinísticas ("¿cuántas tareas?" → SQL). Sólo las preguntas ambiguas ("explícame el tema") merecen un clasificador LLM. Si la regla determinística acierta, evitamos una llamada a Gemini.

**Por qué si la ruta es semantic/hybrid y Ollama no está → unsupported/relational**: si la ruta "semantic" requiere embeddings y Ollama no responde, devolvemos refusal (no podemos responder). Si la ruta es "hybrid" (combinada), caemos a "relational" automáticamente.

**Por qué `record_rag_request` para Prometheus**: queremos medir cuántas requests RAG, por ruta, por idioma, y con qué resultado. Esto permite detectar degradaciones (subida de errores, cambios en la distribución de rutas).

---

## 6. `app/controllers/health.py` — `GET /healthz`

**Por qué existe**: Kubernetes, balanceadores y operadores humanos necesitan saber si el pod está sano. Sin `/healthz`, el balanceador marcaría el pod como vivo aunque la DB esté caída.

**Por qué tres probes separados (Ollama, DB, scheduler)**: la degradación es granular. Si Ollama está caído pero la DB está OK, las queries SQL siguen funcionando y el sistema está parcialmente vivo. Un health agregado que sólo diga "ok" sería demasiado crudo.

**Por qué `_probe_ollama` no hace una llamada real cada vez**: sería lento y ruidoso en logs. Lee el cache de `RAGService.provider_health()`.

**Por qué `connect_timeout=2` en el probe de DB**: si Postgres está inaccesible, queremos fallar rápido sin colgar el endpoint. `2` segundos es un equilibrio entre esperar al DNS y no saturar una red lenta.

**Por qué `_probe_scheduler` lee `app.state.scheduler`**: el scheduler es in-process. Si arrancó, está en `app.state`; si no arrancó (`SCHEDULER_ENABLED=false`), no está.

---

## 7. `app/services/canvas_service.py` — la capa de orquestación

**Por qué existe**: el controller es HTTP-aware; la lógica de sync no debería serlo. `CanvasService` resuelve credenciales, decide si usar sesión fresh o la del caller, y dispara el pipeline.

**Por qué `_resolve_canvas_token(session, tenant_id)`**: dual — leé de SQL por defecto, pero podés caer al store in-memory legacy si la app no marcó `SESSION_STORE_STATE_FLAG`. Esto preserva compatibilidad con tests PR 2/3.

**Por qué `_run_locked(session, tenant_id, client)` existe como método separado**: encapsula la transacción atómica (lock + sync + watermark) en un solo lugar. Sin la separación, el controller tendría que conocer el orden.

**Por qué `_run_with_owned_session` vs `_run_with_session`**: si te pasan una sesión, la usás; si no, abrís una. FastAPI te da la sesión por dependency injection.

---

## 8. `app/sync/pipeline.py` — el motor de sync

**Por qué existe**: la lógica de sincronización es compleja (DTOs, upserts, watermark, errores). Aislarla en `pipeline.py` la hace testeable de forma directa sin tocar HTTP.

**Por qué `_sync_self_user`, `_sync_favorite_courses`, `_sync_course_assignments`, `_sync_fallback_enrollments` son funciones separadas**: cada una corresponde a un endpoint de Canvas. Si Canvas cambia un endpoint, sólo tocás esa función.

**Por qué `strip_peer_data` antes del upsert**: las respuestas de Canvas incluyen datos de otros usuarios (calificaciones de compañeros, etc.). Sin strip, persistiríamos PII de terceros. La función es defensiva y descarta campos peer.

**Por qué `soft_delete_if_inactive`**: si Canvas reporta `workflow_state='deleted'` o `'completed'`, no borramos la fila (preservamos histórico) pero sí la marcamos con `deleted_at`. Las queries downstream filtran por `deleted_at IS NULL`.

**Por qué `advance_watermark` SOLO en éxito**: si el sync falla, el watermark no se mueve. El próximo retry parte del último punto consistente. Sin esta garantía, un sync parcialmente fallido podría perder datos.

**Por qué `_finish_failure` en una transacción separada**: para que el estado de fallo se persista aún cuando la transacción principal se rollea.

---

## 9. `app/sync/lock.py` — el lock por tenant

**Por qué existe**: el sync de un tenant no debe pisarse a sí mismo. Si dos requests llaman `/sync` para el mismo tenant casi simultáneamente, sólo uno debe ejecutar; el otro recibe 429.

**Por qué `pg_try_advisory_xact_lock` en lugar de `SELECT FOR UPDATE`**: el advisory lock es global a la DB (no acoplado a una tabla), es no-bloqueante (`try_`), y se libera solo al final de la transacción. `SELECT FOR UPDATE` requiere una fila específica.

**Por qué `hashtext(:tenant_id)::bigint`**: Postgres `pg_try_advisory_xact_lock` toma un `bigint`. `hashtext` convierte el UUID a texto y luego a int64.

**Por qué fallback a `threading.Lock` por tenant en SQLite**: el código debe correr en tests sin Postgres. El dict `_tenant_locks` está protegido por `_pool_guard`.

**Por qué `@event.listens_for(Session, "after_transaction_end")` con `_release_session_fallback_locks`**: en tests con SQLite, el `threading.Lock` debe liberarse cuando la transacción termina, sin importar el resultado. El hook SQLAlchemy avisa sin que el pipeline tenga que recordar.

---

## 10. `app/rag/router.py` — el clasificador de intención

**Por qué existe**: las preguntas requieren distintos motores. SQL para agregados, vectorial para semántica, híbrido para ambos. Un solo clasificador evita una llamada LLM por cada request.

**Por qué reglas heurísticas primero**: la mayoría de las preguntas son determinísticas ("¿cuántas tareas?", "¿cuál es mi nota?"). Una regex es O(1) y exacta. La LLM es lenta y costosa.

**Por qué fallback a `relational` cuando no hay classifier**: el sistema es defensivo. Si no hay Gemini configurado, devolvemos la ruta más probable (SQL) en lugar de fallar.

**Por qué si la ruta es semantic/hybrid y no hay embeddings → unsupported/relational**: semantic puro sin embeddings es irrealizable. Hybrid degrada a relational porque la parte SQL sigue funcionando.

---

## 11. `app/rag/prompts.py` — idioma y refusal

**Por qué existe**: la respuesta debe respetar el idioma del usuario. Las palabras clave en español se detectan con regex simple (no hace falta LLM).

**Por qué `bounded_refusal`**: cuando no hay evidencia, devolvemos un mensaje corto y fijo en lugar de inventar. Es defensivo contra alucinaciones.

**Por qué prompts separados por ruta**: la LLM recibe instrucciones distintas según SQL vs vectorial. SQL debe enumerar resultados; vectorial debe resumir evidencia.

---

## 12. `app/rag/vector_store.py` y `app/text_to_sql/*` — los backends

**Por qué existen separados**: el router decide la ruta, pero la ejecución depende del backend. Vector store y SQL executor son inyectables; en tests podés inyectar fakes.

**Por qué `tenant_id` se filtra en CADA query**: defensa en profundidad. Aunque la dep chain garantiza `tenant_id`, el backend lo vuelve a aplicar.

**Por qué SQL tiene `statement_timeout` y `LIMIT`**: defensa contra queries lentas o masivas. `2000ms` es suficiente para queries indexadas; `LIMIT 200` evita返回一个 dataset de 10k filas.

**Por qué `SET LOCAL default_transaction_read_only = on`**: el executor corre con un rol que sólo puede SELECT. Aún si el rol está mal configurado, este `SET LOCAL` previene escrituras accidentales.

---

## 13. `app/core/errors.py` — redacción y handlers

**Por qué existe**: sin handlers personalizados, FastAPI devuelve `{"detail": "..."}` que podría incluir el token de Canvas o la URL de la DB. `safe_message` enmascara 4 patrones críticos.

**Por qué `safe_message` es regex y no parsing**: regex es rápido, fácil de extender, y razonablemente robusto. No intenta entender tokens; sólo enmascara substrings que_matchean.

**Por qué `_safe_dict` recurre a valores y listas**: los handlers reciben dicts arbitrarios de FastAPI; necesitamos redactar antes de emitir.

**Por qué `unhandled_exception_handler` filtra el mensaje crudo**: si una excepción escapa, su `str(exc)` podría incluir el token. `safe_message` lo enmascara.

**Por qué siempre se incluye `X-Correlation-ID` en la respuesta**: aunque el cliente no mandó nada, le damos un id para que pueda rastrear.

---

## 14. `app/core/logging.py` — logs estructurados

**Por qué existe**: sin logs estructurados, buscar un error X en un timestamp Y es grep a ojo. Con `structlog + JSON`, los logs son consultables.

**Por qué `ContextVar` para `correlation_id`**: cada request tiene un id, y los handlers de error quieren loguearlo. `ContextVar` setea el valor por request automáticamente.

**Por qué `RedactionFilter` antes de la salida**: aunque `safe_message` redacta los errores, otras llamadas a `log.info(...)` podrían incluir secretos. El filter es universal.

**Por qué `JSONRenderer`**: stdout recibe JSON. Un log aggregator (Loki, Datadog) puede parsearlo y agrupar por `correlation_id`.

---

## 15. `app/middleware/correlation_id.py` — el id por request

**Por qué existe**: los logs estructurados necesitan un campo común para unir entradas. Sin correlation_id, las queries multi-step son imposibles de debuggear.

**Por qué el middleware corre antes que los handlers**: para que un error 500 también lleve el id. Si lo generara el handler, un crash temprano quedaría sin id.

**Por qué UUID v4 específicamente**: simple, suficientemente único, parseable por todos los stacks.

**Por qué `_is_valid_uuid4` descarta formatos malos**: si el cliente manda `garbage`, no confiamos en su valor. Generamos uno nuevo.

---

## 16. `app/core/db.py` — el engine y la sesión

**Por qué existe**: la sesión de SQLAlchemy es el boundary transaccional. Sin abstracción, cada controller tendría que crear su propia sesión y olvidaría de cerrar.

**Por qué `get_db_session` es un dependency**: FastAPI lo inyecta por request. Garantiza que se cierra y se hace rollback en caso de error.

**Por qué `try/except/finally` con `session.rollback()` y `session.close()`**: la sesión puede quedar en transacción si hay un error. El cleanup es defensivo.

**Por qué `make_engine_from_settings` con `connect_timeout=2`**: usado sólo en health probe. Para queries reales, el lifespan crea un engine con timeout estándar.

**Por qué `sqlite:///:memory:` con `StaticPool`**: en tests, cada thread puede ver la misma DB. Sin StaticPool, cada thread tendría su propia conexión y su propia DB vacía.

---

## 17. `app/security/redaction.py` — el filtro universal

**Por qué existe**: una sola línea de `log.info(...)` con un secreto puede comprometer toda la DB. La defensa en profundidad es urgente.

**Por qué dict-keys + text patterns**: los secrets viven tanto en logs estructurados (dict) como en mensajes libres (text). El filter cubre ambos.

**Por qué `setattr(record, 'msg', ...)` en lugar de retornar string**: `logging.LogRecord` tiene un campo `msg` separado del `args`. Mutamos eso y dejamos que los formatters vean la versión redactada.

---

## 18. `app/security/backend_auth.py` y `app/security/token_crypto.py`

**Por qué existen separados**: la primera pieza sabe sobre JWTs (PyJWT), la segunda sobre Fernet (cryptography). Mezclar dependencias sería más difícil de mockear.

**Por qué `algorithms=["HS256"]` explícito**: previene el ataque `alg: none` y deja claro qué algoritmos aceptamos.

**Por qué `cryptography.fernet`**: Fernet da AEAD con HMAC, rotación de claves y timestamp. Es la recomendación oficial para cifrado simétrico en Python.

**Por qué `key_version` en el ciphertext**: para rotación. Si en el futuro querés migrar a una nueva llave, basta con descifrar todo con la llave vieja y re-encriptar con la nueva.

---

## 19. `app/observability/metrics.py` y `app/observability/tracing.py`

`metrics.py` declara 6 contadores Prometheus: `rag_requests_total`, `sql_validations_total`, `canvas_requests_total`, `sync_runs_total`, `sync_lag_seconds`, `token_decrypt_failures_total`. Cada `record_*` los incrementa y nunca lanza. La razón: si Prometheus está caído, los requests HTTP no deben fallar.

`tracing.py` configura OTLP gRPC si `OTEL_ENABLED=1`. Por defecto es no-op. Por qué: no queremos que todos los deploys necesiten un collector OTLP; quienes lo quieran, lo activan explícitamente.

---

## 20. `app/core/config.py` — Settings fail-closed

**Por qué existe**: un config mal cargado debe explotar al arrancar, no en producción cuando algo falla silenciosamente.

**Por qué validators `mode="after"`**: la validación ocurre después de la carga. Los validators custom como `_validate_fernet_key` y `_validate_db_scheme` exigen formatos específicos.

**Por qué `lru_cache` en `get_settings`**: cargamos una sola vez. Reevaluar en cada request浪费时间.

---

## 21. Diagrama de causa y efecto

```
                ┌──────────────────────────┐
                │  Usuario abre la app      │
                └────────────┬─────────────┘
                             │
                             ▼
       ┌──────────────────────────────┐
       │ ¿Cómo sabemos quién es?       │
       │ → JWT firmado con BACKEND_SECRET│
       │ → verify_backend_jwt_dependency│
       └────────────┬─────────────────┘
                    │
                    ▼
       ┌──────────────────────────────┐
       │ ¿A qué tenant pertenece?     │
       │ → require_tenant              │
       │ → SELECT/INSERT en tenants   │
       └────────────┬─────────────────┘
                    │
                    ▼
       ┌──────────────────────────────┐
       │ ¿Tiene token de Canvas?      │
       │ → require_tenant_token       │
       │ → SELECT canvas_credentials  │
       │ → descifra con TokenCipher   │
       └────────────┬─────────────────┘
                    │
   ┌────────────────┴───────────────┐
   │                               │
   ▼                               ▼
/sync                          /query
   │                               │
   │ (necesita plaintext)         │ (sólo tenant_id)
   │                               │
   ▼                               ▼
CanvasService                 RAGService
   │                               │
   │ probe sync                   │ detecta idioma
   │ acquire lock                │ route = RAGRouter.route
   │ sync_tenant                  │ answer = LLM/SQL/vector
   │ upsert + watermark           │
   │                               │
   ▼                               ▼
sincroniza PG                RAGService responde
   │
   ▼
el front-end muestra datos frescos
```

---

## 22. Resumen: qué módulos existen por qué

| Módulo | Por qué existe |
|---|---|
| `app/main.py` | Construir la app FastAPI y su ciclo de vida. |
| `app/core/deps.py` | Cadena de auth reutilizable: `verify_backend_jwt → require_tenant → require_tenant_token`. |
| `app/core/config.py` | Validar fail-closed de la configuración. |
| `app/core/db.py` | Engine y sesión de SQLAlchemy compartidas. |
| `app/core/errors.py` | Redacción y formato de errores. |
| `app/core/logging.py` | Logs estructurados JSON con `correlation_id`. |
| `app/security/backend_auth.py` | Verificar firma JWT. |
| `app/security/token_crypto.py` | Cifrar y descifrar tokens de Canvas. |
| `app/security/redaction.py` | Redacción de secretos en logs. |
| `app/middleware/correlation_id.py` | Id único por request. |
| `app/observability/metrics.py` | Contadores Prometheus. |
| `app/observability/tracing.py` | OTLP opcional. |
| `app/services/canvas_service.py` | Orquestar sync sin HTTP. |
| `app/services/tenant_service.py` | Tenant + credencial cifrada. |
| `app/services/rag_service.py` | Pipeline RAG: router → backend. |
| `app/sync/pipeline.py` | Lógica atómica de sync. |
| `app/sync/lock.py` | Lock por tenant. |
| `app/sync/scheduler.py` | Cron cada 6 horas. |
| `app/sync/watermark.py` | Cursor incremental. |
| `app/canvas/client.py` | GET-only con retries. |
| `app/canvas/dto.py` | Whitelist de campos. |
| `app/canvas/pagination.py` | Cursor `Link: next`. |
| `app/rag/router.py` | Reglas determinísticas + fallback LLM. |
| `app/rag/prompts.py` | Idioma y refusal. |
| `app/rag/hybrid.py` | Recall + SQL restringido. |
| `app/rag/vector_store.py` | Wrapper PGVector. |
| `app/text_to_sql/allow_list.py` | Plantillas SQL seguras. |
| `app/text_to_sql/validator.py` | SELECT-only. |
| `app/text_to_sql/executor.py` | `SET LOCAL` + límite. |
| `app/text_to_sql/templates/` | Plantillas por caso. |
| `app/repositories/upsert.py` | Upsert idempotente. |
| `app/models/` | Modelos SQLAlchemy. |
| `app/schemas/` | Pydantic request/response. |

Cada uno de estos archivos existe porque el problema completo (autenticación multi-tenant + RAG + sync Canvas + redacción) no cabe en uno solo. Cada uno hace una cosa y se conecta con los demás vía dependency injection o llamadas explícitas.

---

## 23. Próximos pasos para producción

1. **PR 5 (router) está implementado pero el orquestador no ejecuta SQL ni PGVector sin inyecciones adicionales**. Para `/query` responda con datos reales, conectar `vector_store` y `sql_executor` desde `app/main.py::lifespan`.
2. **El scheduler lista `CanvasCredential.tenant_id` y dispatcha**, pero la implementación actual de `_tick` itera sin abrir sesión por tenant. Conectar el ciclo con la fábrica de sesiones.
3. **OAuth de Canvas** para que los usuarios no copien tokens a mano. Queda fuera de v1.
4. **CI con GitHub Actions** que corra pytest + ruff + pip check en cada push a `main`.

El doc está vivo en `FLUJO_ENDPOINTS.md`. Si una parte queda oscura, decime y la amplío.
