# Cómo funciona PaquitoBot — Guía narrativa completa

> **Para qué sirve este documento**: entender el proyecto de punta a punta, sin tener que leer el código. Cada pieza se explica primero **para qué existe** (qué problema del usuario o del sistema resuelve) y después **cómo se conecta con el resto** (conviven con qué otras piezas, qué le entrega, qué recibe).
>
> Si solo vas a tocar una parte, este documento te dice qué tocar y qué no.

---

## 0. La foto grande en un párrafo

PaquitoBot es un **asistente conversacional para estudiantes de una universidad que usa Canvas LMS**. El estudiante le entrega su token personal de Canvas una sola vez, PaquitoBot sincroniza los datos de su cuenta (cursos, tareas, entregas, calificaciones) en una base de datos propia, y desde ese momento el estudiante puede preguntarle en español cosas como *"¿qué tareas tengo para esta semana?"*, *"¿cuál es mi promedio en el curso de Cálculo?"* o *"explicame las submissions que me faltan"*. Las respuestas se construyen con tres fuentes de información: SQL directo sobre Postgres, búsqueda semántica con embeddings en PGVector, y un modelo de lenguaje (**MiniMax-M3**, expuesto en endpoint Anthropic-compatible) que genera la respuesta natural. **Nunca se escriben datos en Canvas** (es read-only) y los datos de un estudiante nunca se mezclan con los de otro (multiusuario estricto).

Tres actores con los que el sistema conversa:

1. **El estudiante**: hace `POST /query` desde su app cliente y al inicio le entrega su token Canvas en `POST /auth/canvas/connect`.
2. **Canvas (la universidad)**: PaquitoBot consulta su API REST para bajar datos.
3. **MiniMax + Ollama**: MiniMax redacta la respuesta en lenguaje natural (vía endpoint Anthropic-compatible); Ollama genera los vectores de embeddings localmente.

---

## 1. El esqueleto: cómo arranca el monolito

### 1.1 `main.py` (raíz) — el portero del modo

Cuando alguien corre `python main.py`, lo primero que pasa es que este archivo decide **qué versión del programa se va a ejecutar**. ¿Por qué dos versiones? Porque el proyecto pasó por una primera fase experimental (llamada *legacy*) que era un script de consola ejecutando todo en un solo proceso, y después se migró a un monolito FastAPI.

- Si la variable de entorno `LEGACY_MODE=1`, ejecuta el script original: lee JSONs locales, los chunckea, los embebe con Ollama, los guarda en PGVector local, y finalmente corre una query de prueba con MiniMax. Sirve para validar la cadena de embeddings sin levantar el servidor.
- Si `LEGACY_MODE=0` (default), lanza `uvicorn app.main:app` y queda escuchando en `HOST:PORT`. **Este es el camino de producción.**

Sirve para que el equipo de desarrollo tenga un "ground truth" simple cuando algo falla: si la versión legacy anda y la nueva no, el problema es del monolito, no del modelo.

### 1.2 `app/main.py` — la fábrica de la app y su ciclo de vida

Acá se crea la única instancia de `FastAPI` y se montan los routers. El `lifespan` es un contexto async que se ejecuta una vez al arrancar y otra al apagar el servidor. Sirve para arrancar y apagar recursos caros (engine de base, scheduler, tracer de OpenTelemetry) sin que queden colgados.

**En el startup**:
1. Carga `Settings` desde variables de entorno. Si falta algo crítico, falla **antes** de aceptar requests (fail-closed).
2. Configura logs JSON estructurados con `correlation_id` por request.
3. Si `SCHEDULER_ENABLED` o el RAG está habilitado, crea el `engine` SQLAlchemy y la `session_factory`.
4. Construye el `TenantService` (la pieza que descifra tokens Canvas).
5. Construye el `RAGService` vía `build_rag_service()` — **de forma lazy**: no conecta con Ollama ni con MiniMax en el arranque, así la app puede bootear aunque esos servicios estén caídos.
6. Si `SCHEDULER_ENABLED=True`, instala el `SyncScheduler` (APScheduler) que ejecuta el sync cada 6 horas.

**En el shutdown**: apaga scheduler, hace `dispose()` del engine, y cierra el tracer.

El orden importa: ningún router acepta requests hasta que el lifespan termine, así que si la base no está, la app arranca igual pero las rutas devuelven 503.

**Conexión con el resto**: `app/main.py` es la raíz de casi todo. No importa lógica de negocio — solo conexiona.

---

## 2. Configuración: `app/core/config.py`

### 2.1 Para qué sirve

`Settings` (Pydantic Settings) carga **todas** las variables de entorno en un único objeto inmutable. El resto del código hace `settings.minimax_api_key`, `settings.ollama_host`, etc. Esto sirve para tener una sola fuente de verdad sobre qué variables existen, validar tipos al iniciar, y fallar ruidosamente si falta algo crítico.

### 2.2 Las variables de entorno

Las agrupo por **para qué sirven** en vez de por orden alfabético:

| Grupo | Variable | Para qué se usa |
|---|---|---|
| **Conexión a datos** | `SUPABASE_DATABASE_URL` | URL de Postgres/Supabase (con prefijo `postgresql+psycopg://` o `+asyncpg://`). Sirve para todo el SQL relacional y para PGVector. |
| **Seguridad** | `TENANT_TOKEN_KEY` | Llave Fernet (urlsafe-base64 de 32 bytes). Sirve para **cifrar los tokens Canvas** antes de persistirlos. Si esto se pierde, nadie puede entrar a Canvas. |
| | `BACKEND_SECRET` | Secret HS256 para firmar y verificar los JWT que emite PaquitoBot. Sirve para que solo PaquitoBot confíe en los JWT emitidos por sí mismo. |
| **Proveedores externos** | `MINIMAX_API_KEY` | API key de MiniMax (endpoint Anthropic-compatible). Sirve para llamar al modelo que redacta las respuestas. |
| | `OLLAMA_HOST` | URL base del servidor Ollama local. Sirve para generar embeddings. |
| | `CANVAS_API_BASE_URL` | URL base de la API REST de Canvas. Ej: `https://tecsup.instructure.com/api/v1`. |
| | `GOOGLE_CLIENT_ID` | Client ID de Google (audience para `POST /auth/login`). Debe matchear el configurado en el SDK móvil de GoogleSignIn. |
| **Embeddings** | `OLLAMA_EMBEDDING_MODEL` | Modelo de Ollama (default `qwen3-embedding:8b`). Sirve para decidir qué embedding usar. |
| | `OLLAMA_EMBED_DIM` | Dimensión del vector (default 1024). Sirve para hacer match con la columna PGVector. |
| **Sync** | `SYNC_INTERVAL_SECONDS` | Cada cuánto corre el sync automático. Default 21600 (6h). |
| | `SYNC_JITTER_SECONDS` | Jitter por tenant encima del intervalo. Sirve para que muchos tenants no se peguen al mismo segundo. |
| | `MANUAL_SYNC_MIN_INTERVAL_SECONDS` | Mínimo entre sync manuales por tenant. Sirve para que nadie martillee Canvas. |
| **SQL** | `SQL_STATEMENT_TIMEOUT_MS` | Timeout server-side para queries text-to-SQL. Default 2000 ms. |
| | `SQL_ROW_LIMIT` | `LIMIT` máximo server-side. Default 200. |
| **Runtime** | `LOG_LEVEL` | Nivel de log (INFO, DEBUG, etc.). |
| | `SCHEDULER_ENABLED` | Activa APScheduler. |
| | `DISABLE_RAG_ROUTES` | Cuando es true, `/query` y rutas RAG devuelven 503. Útil para debugging o para apagado de emergencia. |
| | `OTEL_ENABLED` | Activa OpenTelemetry tracing. |
| **Auth (login temporal)** | `LOGIN_TOKEN_TTL_SECONDS` | Vida útil del JWT emitido por `/auth/login` (default 3600s = 1h). |
| | `CORS_ALLOWED_ORIGINS` | Lista comma-separated de orígenes permitidos. Vacío desactiva CORS. Default cubre `localhost:3000` para dev. |
| | `HOST`, `PORT`, `RELOAD`, `LEGACY_MODE` | Leídos directamente en `main.py` (no son Settings). |

**Conexión con el resto**: este módulo es el "interruptor general" del sistema. Cualquier pieza que quiera una variable de entorno pasa por acá.

---

## 3. La cadena de una request HTTP

### 3.1 El middleware de correlación — `app/middleware/correlation_id.py`

**Para qué existe**: cuando llega un request, le pega un `correlation_id` (UUID v4) y lo va pasando a todos los logs. Si falla algo, podés copiar ese ID y buscar todos los eventos asociados en la salida JSON. Si el cliente te manda su propio `X-Correlation-ID`, lo respeta (siempre que sea un UUID v4 válido).

**Conexión**: el `correlation_id` fluye a `app/core/logging.py` (que lo añade a cada log) y a `app/core/errors.py` (que lo mete en el body de cada error).

### Login con Google (temporal, removible)

**Para qué existe**: hoy PaquitoBot no emite los JWTs que valida. Como dependencia temporal — pensada para removerse cuando se integre con el auth provider definitivo — hay un endpoint `POST /auth/login` que acepta un `id_token` de Google Sign-In y devuelve un JWT firmado por PaquitoBot (HS256, `BACKEND_SECRET`).

**Cómo se usa**:
1. El frontend móvil hace el redirect a Google con el SDK de GoogleSignIn (iOS/Android). Google devuelve al SDK un `id_token` firmado por Google.
2. El frontend manda `POST /auth/login` con `{id_token: "eyJ..."}`.
3. PaquitoBot valida la firma del `id_token` contra la public key de Google (cacheada), extrae `sub` y `email`, y firma un JWT propio con `iss="paquitobot"`, `iat`, `exp` (`LOGIN_TOKEN_TTL_SECONDS`, default 1h).
4. El frontend recibe `{access_token, token_type, expires_in, sub, email}` y lo usa como `Authorization: Bearer` para los demás endpoints.

**Por qué NO hay endpoint que redirige**: PaquitoBot es un backend API puro (no renderiza HTML). El redirect OAuth lo hace el SDK móvil del cliente; el backend solo verifica firmas y emite su propio JWT.

**Por qué `google_client_id` vive en `Settings`**: es el `aud` que PaquitoBot exige en cada `id_token`. Tiene que matchear el `client_id` configurado en el SDK móvil (iOS: `GoogleService-Info.plist` → `CLIENT_ID`; Android: `google-services.json` → `client_info.mobilesdk_app_id`). Un solo `client_id` para empezar; se puede extender a múltiples audiences después.

**Conexión**: `app/controllers/auth.py::login_with_google` (endpoint) → `google.oauth2.id_token.verify_oauth2_token` (validación) → `issue_backend_jwt()` (firma) → `verify_backend_jwt_dependency` (en `app/core/deps.py`, lo consume el resto de endpoints).

### 3.2 La cadena de auth — `app/core/deps.py`

**Para qué existe**: cualquier endpoint que depende de `tenant_id` o del token Canvas descifrado usa esta cadena. Por construcción, es imposible que un cliente invente un `tenant_id`: solo se deriva del `sub` del JWT.

La cadena es:

```
verify_backend_jwt_dependency → require_tenant → require_tenant_token
```

1. **`verify_backend_jwt_dependency`**: extrae el `Bearer <jwt>` del header. Decodifica con `BACKEND_SECRET` (HS256), exige claims `sub`, y retorna el `user_id` (el `sub`).
2. **`require_tenant`**: con `user_id`, llama `service.get_or_create_tenant(user_id)`. Internamente, si no existe el tenant en la tabla `tenants`, lo crea. Retorna el `UUID` del tenant.
3. **`require_tenant_token`**: con el `tenant_id`, llama `service.get_decrypted_canvas_token(tenant_id)`. Lee el ciphertext cifrado de `canvas_credentials`, lo descifra con Fernet, y retorna el plaintext **dentro del scope de la request** (después se descarta).

**Conexión con el resto**: la usan `controllers/query.py` (para `/query`) y `controllers/sync.py` (para `/sync`). También `controllers/auth.py` usa solo el primer eslabón, porque ahí todavía no hay tenant asignado.

### 3.3 Errores unificados — `app/core/errors.py`

**Para qué existe**: garantizar dos cosas: (1) que ninguna credencial (Fernet, JWT, Postgres URL) se filtre en una respuesta HTTP, y (2) que toda respuesta de error tenga la **misma forma** para que el cliente la pueda parsear sin romperse.

**Cómo lo hace**:
- `safe_message()` aplica 4 máscaras regex al mensaje (Fernet `gAAAA…`, `Bearer eyJ…`, `postgresql://…`, blobs de 40+ chars).
- `ErrorBody` es el envelope: `{code, message, correlation_id, details?}`.
- Hay tres handlers (HTTPException, ValidationException, unhandled) que SIEMPRE devuelven este shape más el header `X-Correlation-ID`.

**Conexión con el resto**: `app/main.py` los registra en `create_app()`. Todas las demás piezas sólo tienen que lanzar errores — el formato lo garantiza este módulo.

### 3.4 Logging JSON — `app/core/logging.py`

**Para qué existe**: el log es JSON estructurado con `timestamp`, `level`, `correlation_id`, `event`, y los kwargs del evento. Eso permite ingestar en Loki/Datadog/CloudWatch sin parsear texto. Configura structlog y reemplaza los handlers de uvicorn para que todo (incluso los logs de acceso) salga en JSON.

En Windows hay un parche: `configure_console_encoding()` fuerza UTF-8 en stdout (sino los tracebacks con tildes se rompen en cp1252).

**Conexión con el resto**: el `RedactionFilter` de `app/security/redaction.py` se atacha acá para que ningún secreto (authorization, token, password, etc.) llegue jamás al log stream.

---

## 4. Seguridad: dónde viven los secretos

### 4.1 `app/security/token_crypto.py` — Fernet

**Para qué existe**: cifrar y descifrar los tokens Canvas. Es la **única** pieza permitida para tocar `cryptography.fernet.Fernet`. El resto del código nunca llama Fernet directo.

- `EncryptedToken` (frozen dataclass): ciphertext + `key_version`.
- `TokenCipher`: envuelve un Fernet con un `key_version`. `encrypt()` cifra, `decrypt()` descifra, `is_ciphertext()` detecta envelopes `gAAAA…`.
- Sirve para que si rotás la llave Fernet en el futuro, cada ciphertext lleva su versión y podés descifrar tokens viejos con la llave vieja.

**Conexión con el resto**: lo usa `TenantService` para cifrar al guardar y descifrar al leer. Lo usa `controllers/auth.py` para cifrar el token recién entregado.

### 4.2 `app/security/backend_auth.py` — JWT

**Para qué existe**: verificar los JWT que emite PaquitoBot cuando se loguea un estudiante. **No** confundir con el token Canvas: este JWT es interno. Si el cliente manda un JWT que no está firmado con `BACKEND_SECRET`, se rechaza.

Sirve para que la API sea stateless: no hay sesión de servidor, el JWT contiene todo lo que hace falta (`sub` = user_id).

**Conexión con el resto**: lo invoca `verify_backend_jwt_dependency` en `app/core/deps.py`.

### 4.3 `app/security/redaction.py` — Redacción universal

**Para qué existe**: aplicar `***REDACTED***` a credenciales antes de que lleguen al log stream. Funciona en dos niveles:
- Nombres de keys sensibles (`authorization`, `token`, `ciphertext`, `password`, `database_url`, `minimax_api_key`, etc.) → redacta el valor.
- Mensajes crudos (regex Fernet, Bearer, PG URL) → redacta dentro del string.

Está hecho para **nunca crashear logging**: si el filter falla, mejor logear algo recortado que perder la línea entera.

**Conexión con el resto**: lo instalan `app/core/logging.py`.

---

## 5. Persistencia: el modelo de datos

### 5.1 `app/models/__init__.py` — el lenguaje SQLAlchemy

**Para qué existe**: definir el modelo relacional. Acá viven las 8 tablas que describen el dominio. La estructura es:

- `TenantMixin`: columnas comunes (`tenant_id`, `created_at`, `updated_at`, `deleted_at`).
- `GUID`: cross-dialect UUID (PG nativo, SQLite CHAR(36)).
- `JSONType`: JSONB en Postgres, JSON en SQLite. Sirve para sync flexible con campos semi-estructurados de Canvas.

**Las 8 tablas** (organizadas por para qué):

| Tabla | Para qué guarda | Cómo se identifica |
|---|---|---|
| `tenants` | La identidad: cada usuario backend = un tenant | `backend_user_id` (unique) |
| `canvas_credentials` | El token Canvas cifrado con Fernet | `tenant_id` (1:1) |
| `users` | Solo el perfil del propio estudiante | `(tenant_id, canvas_id)` |
| `courses` | Cursos del estudiante | `(tenant_id, canvas_id)` |
| `enrollments` | Inscripciones a cursos | `(tenant_id, canvas_id)` |
| `assignments` | Tareas y entregables | `(tenant_id, canvas_id)` |
| `submissions` | Entregas propias (no las de compañeros) | `(tenant_id, canvas_id)` |
| `sync_state` | Watermark + estado del último sync | `(tenant_id, table_name)` |

**Reglas comunes**:
- **Aislamiento por tenant**: cada query filtra por `tenant_id`. El `execute_readonly` además hace `SET LOCAL app.tenant_id` en Postgres como salvaguarda.
- **Soft-delete**: cuando el `workflow_state` es `deleted`, `inactive`, `completed`, etc., se setea `deleted_at`. Las queries siempre filtran `deleted_at IS NULL`.
- **Datos de pares descartar**: la tabla `submissions` solo guarda las entregas del estudiante autenticado (el `strip_peer_data` filtra).

**Conexión con el resto**: los modelos los consume `app/repositories/upsert.py` (para insertar/actualizar), `app/services/tenant_service.py` (para resolver tenant y credencial), y todos los controllers. Alembic también los lee via `Base.metadata`.

### 5.2 `app/repositories/upsert.py` — el upsert por tenant

**Para qué existe**: hacer `insert-or-update` por `(tenant_id, canvas_id)` sin escribir SQL repetido. Sirve para sincronizar Canvas: si el curso ya existe, lo actualiza; si no, lo crea. Las dos claves siempre van juntas porque Canvas solo garantiza que `(tenant_id, canvas_id)` es único dentro del tenant.

- `upsert_by_canvas_id(session, model, tenant_id, canvas_id, payload)`: filtra `payload` a columnas conocidas (descarta keys desconocidas de Canvas).
- `assert_tenant_fk_target(...)`: si un FK (ej. `course_id`) apunta a otro tenant, falla ruidosamente. Sirve para detectar bugs de sync.
- `soft_delete_if_inactive(...)`: si el `workflow_state` está en el set inactivo, flippea `deleted_at`.

**Conexión con el resto**: lo usa el pipeline de sync (`app/sync/pipeline.py`, no listado pero sigue el mismo patrón).

---

## 6. La integración con Canvas

### 6.1 `app/canvas/client.py` — cliente HTTP GET-only

**Para qué existe**: hablar con Canvas REST. **Solo hace GET**. Cualquier otro método (`POST`, `PUT`, `DELETE`) lanza `CanvasMethodRejected` antes de transmitir. Esto existe como guardrail de seguridad: si alguien por error agregara un `POST /assignments`, fallaría ruidosamente.

**Cómo funciona**:
- Constructor con `base_url`, `token_provider` (sync o async callable), `timeout_seconds=8`, `max_attempts=3`.
- `_request()`: valida método, resuelve URL (Canvas a veces devuelve URL absoluta en `Link: next`), reintenta con `tenacity` (exponential backoff 1-4s) en errores 5xx.
- 4xx no se reintenta: si Canvas dice 401, es algo del token, no un blip.

**Conexión con el resto**: lo construye `CanvasService` con el token del tenant. La paginación la maneja `app/canvas/pagination.py`.

### 6.2 `app/canvas/pagination.py` — seguir el cursor `Link: next`

**Para qué existe**: Canvas pagina con `Link: <url>; rel="next"` (RFC 5988). Esta pieza es un async generator que yield-ea cada item a través de todas las páginas.

Tiene tres redes de seguridad:
- **Dedup**: si la URL que volvió como `next` ya la visitamos, paramos.
- **Circuit breaker**: máximo 1000 iteraciones.
- **Manejo de 4xx**: si Canvas devuelve 4xx mientras paginás, lanza `CanvasRequestError`.

**Conexión con el resto**: lo consume el pipeline de sync.

### 6.3 `app/canvas/dto.py` — DTOs whitelist

**Para qué existe**: validar y filtrar las respuestas de Canvas. Pydantic models con `extra="ignore"` (lo que Canvas no conoce, se descarta) y `frozen=True` (inmutables). Sirve para que el código que viene después no se rompa cuando Canvas agrega un campo nuevo.

- `UserDTO`, `EnrollmentDTO`, `CourseDTO`, `SubmissionDTO`, `AssignmentDTO`, etc.
- `strip_peer_data(dto, tenant_user_id)`: descarta registros de compañeros y campos sensibles (`body`, `preview_url`, `url`, `attachments`) de submissions de pares. Sirve para cumplir la promesa de "no exponemos datos de otros estudiantes".
- `parse_many()`: variante que skip silencioso ante ValidationError (para listas donde un item roto no debería tumbar todo el sync).

**Conexión con el resto**: lo consume el pipeline de sync, y los datos limpios van a `app/repositories/upsert.py`.

### 6.4 `app/services/canvas_service.py` — orquestador de sync

**Para qué existe**: es el boundary entre HTTP (controllers) y la lógica de sync. Resuelve credenciales, construye cliente, dispara pipeline.

**Cómo funciona**:
- `run_sync_for_tenant(tenant_id, *, session=None)`: abre una sesión SQL si no le pasan una, resuelve el token Canvas descifrado, adquiere un lock por tenant, llama `await sync_tenant(...)`, y libera el lock.
- `enforce_manual_rate_limit(...)`: limita los syncs manuales (por tenant, no global). Si alguien triggerea sync manuales en loop, le devuelve 429 con `Retry-After`.
- `try_acquire_sync_lock(...)`: lock lógico en la tabla `sync_state` para evitar que un sync automático y uno manual corran en paralelo.

**Conexión con el resto**: lo usa `controllers/sync.py::POST /sync`. Habla con `TenantService` (credenciales), `CanvasClient` (HTTP), y el pipeline de sync (escritura).

---

## 7. El sync: trayendo datos de Canvas

El pipeline de sync (no listado como archivo separado, vive alrededor de `app/sync/pipeline.py`) es el corazón de "datos frescos":

1. Recibe un `tenant_id`.
2. Adquiere `try_acquire_sync_lock` (si hay otro sync en curso, aborta).
3. Para cada recurso (users, courses, enrollments, assignments, submissions):
   - Llama a `paginate(client, path, ...)` con su endpoint Canvas.
   - Cada item lo pasa por su DTO correspondiente (ej. `AssignmentDTO`).
   - `strip_peer_data()` para filtrar otros estudiantes.
   - `upsert_by_canvas_id(session, model, tenant_id, canvas_id, payload)`.
   - Si el `workflow_state` está inactivo, `soft_delete_if_inactive()`.
   - Si todo OK, actualiza `sync_state.last_watermark`.
4. Si algo falla, loguea `sync_schema_drift_detail` con el campo exacto y la fila. **No avanza** el watermark.
5. Libera el lock.

**Conexión con el resto**: es el puente entre Canvas y la base local. Lo dispara el scheduler (cada 6h), los controllers (manual vía `POST /sync`), y los tests.

---

## 8. El RAG: cuando el estudiante pregunta

### 8.1 Visón de conjunto

Cuando llega `POST /query`, pasan DOS cosas en paralelo: el sistema decide **de dónde sacar la respuesta** (route) y **cómo formularla** (summarize). Hay tres rutas posibles:

| Ruta | Cuándo se usa | Fuente de datos |
|---|---|---|
| `relational` | Preguntas con datos estructurados (calificaciones, fechas, cuentas) | SQL allow-list |
| `semantic` | Preguntas narrativas, resúmenes, "explicame…" | PGVector (embeddings) |
| `hybrid` | Preguntas que necesitan ambos | SQL + PGVector |

### 8.2 `app/rag/router.py` — el clasificador de rutas

**Para qué existe**: decidir de dónde sacar la respuesta. Es determinístico primero, LLM como fallback.

- `deterministic_rule(question)`: un regex matchea keywords (`score`, `grade`, `count`, `how many`, `explain`, `summarize`, `course #NN`, `assignment #NN`). Retorna `relational`/`hybrid`/`semantic`/`None`.
- `route(question)`: si la regla determinística no llega, llama `self.classifier(question)` (LLM). Si la ruta requiere embeddings pero Ollama está caído, degrada a `relational`.

**Conexión con el resto**: lo llama `RAGService`.

### 8.3 `app/text_to_sql/` — el camino relacional

**Para qué existe**: cuando la respuesta está en datos estructurados, no podés inventar SQL en cada request. Hay un **allow-list** cerrado de templates pre-aprobados. El LLM solo elige UN nombre de la lista y llena los slots.

**Las piezas**:

- `app/text_to_sql/allow_list.py`: registro de templates. Cada `SQLTemplate` tiene `name`, `sql`, `slots`. `default_allow_list()` registra ~14 templates (5 single-template + 8 agent + 2 grounding). Cada SQL incluye `deleted_at IS NULL` y NO termina en `;` (el executor lo envuelve en subquery LIMIT).
- `app/text_to_sql/validator.py`: `validate_sql()` exige SELECT único, rechaza `INSERT/UPDATE/DELETE/MERGE/CREATE/ALTER/DROP/...`, valida con sqlglot o sqlparse.
- `app/text_to_sql/executor.py`: `execute_readonly(session, sql, *, tenant_id, params, row_limit)`:
  - Valida con `validate_sql`.
  - En Postgres: `SET LOCAL default_transaction_read_only = on`, `SET LOCAL statement_timeout = 2000`, `SET LOCAL app.tenant_id = '<uuid>'` (tenant_id se inyecta literal porque SET no soporta bind params).
  - Ejecuta con `params = {"tenant_id": str(tenant_id), **params}`.
  - En SQLite skip los `SET LOCAL`.
- `app/text_to_sql/template_selector.py`: el LLM elige UN template del enum cerrado y llena slots. Los IDs se "groundean" contra `courses_list`/`assignments_list` (los reales del tenant). Si el LLM inventa un ID, cae al `FALLBACK_TEMPLATE`.
- `app/text_to_sql/tools.py`: catálogo de 8 herramientas que el agente tool-calling le pasa al LLM. El LLM ve nombre + descripción + schema JSON; **nunca ve SQL**. Cada tool declara `server_slots` (los completa el backend, no el LLM) y `model_slots` (los llena el LLM).
- `app/text_to_sql/templates/`: mínima wrappers que delegan en `ALLOW_LIST.resolve()`.

**Conexión con el resto**: el executor lo llama `RAGService.answer()` en la ruta `relational`. Los templates los consume tanto el executor directo como el agente tool-calling.

### 8.4 `app/rag/vector_store.py` — el camino semántico

**Para qué existe**: búsqueda por similitud sobre los embeddings en PGVector.

- `provider_health()`: probea Ollama con cache TTL 30s. Si Ollama está caído, marca `embedding_available=False`.
- `similarity_search(query, *, tenant_id, k=...)`: si el store está unhealthy, retorna `[]`. Filtra SIEMPRE por `tenant_id` (no podés buscar embeddings de otro tenant).
- `upsert(...)`: dedup por SHA-256 de `page_content`, marca `metadata.tenant_id` y `metadata.content_hash`.

**Conexión con el resto**: lo llama `RAGService.answer()` en la ruta `semantic`, y `app/rag/hybrid.py` para la ruta `hybrid`.

### 8.5 `app/rag/hybrid.py` — cuando se necesitan ambos

**Para qué existe**: a veces la pregunta necesita tanto semántica (buscar contenido relevante) como SQL (datos precisos). Este módulo hace el recall vectorial, extrae IDs de la metadata, y llama a un callback SQL para enriquecer.

**Conexión con el resto**: lo usa `RAGService` en la ruta `hybrid`.

### 8.6 `app/rag/agent.py` — el agente tool-calling

**Para qué existe**: cuando la pregunta es compleja ("dame el detalle de las tareas que no entregué en el curso de Cálculo"), el LLM necesita varias llamadas. Este módulo implementa el loop bounded.

**Cómo funciona**:
- `MAX_TOOL_STEPS = 4` (cota dura para evitar loops infinitos).
- Mensaje inicial: `[SystemMessage("PaquitoBot …"), HumanMessage(question)]`.
- Loop: invoca `llm.bind_tools(specs)`, lee `tool_calls`, valida cada uno (args correctas, IDs conocidos, no `tenant_id` smuggling), ejecuta, appendea `ToolMessage` con el resultado.
- Si agota los steps, una última vuelta con tools withheld (broad scope question).
- `_validate_call()`: valida nombre ∈ catálogo, args es dict, no hay `tenant_id`/`user_id` smuggling, Pydantic schema valida, y los IDs que mandó el LLM son **reales** (grounding).

**Conexión con el resto**: lo usa `RAGService.answer()` cuando la ruta es relacional y hay agente disponible. `_TenantToolRuntime` (en `rag_factory.py`) es el bridge que pasa la sesión SQL y el tenant_id.

### 8.7 `app/rag/prompts.py` — idioma y prompts

**Para qué existe**: detectar el idioma de la pregunta (`es`/`en`) para el campo `lang` de la respuesta y para el `bounded_refusal` determinístico. Los prompts `sql_prompt`, `vector_prompt`, `hybrid_prompt`, `refusal_prompt` se construyen acá.

**Conexión con el resto**: lo usa `RAGService`.

### 8.8 `app/services/rag_service.py` — el orquestador

**Para qué existe**: es **donde toda la magia se conecta**. Recibe `(question, tenant_id)`, refresca `provider_health()`, decide la ruta, y delega.

**Cómo funciona**:
1. `provider_health()` actualiza `router.embedding_available`.
2. `decision = router.route(question, language=lang)`.
3. `route="unsupported"` → `bounded_refusal`.
4. `route="relational"`:
   - Si hay agente, intenta `_agent_answer` (tool-calling).
   - Si no, `sql_executor(tenant_id, raw_sql?, question)` → `_summarize` (MiniMax).
   - Si no hay LLM, devuelve las filas formateadas.
5. `route="hybrid"`: si no hay vector store o LLM, degrada a `relational`. Si no, hace `vector_store.similarity_search(k=20)`, también invoca el executor, manda prompt híbrido a `_summarize`.
6. `route="semantic"`: `similarity_search(k=8)`, prompt vector, `_summarize`.

**Nunca devuelve string vacío**: en el peor caso retorna `bounded_refusal` ("No puedo responder con evidencia disponible"). Esto fue una decisión importante para que el cliente nunca reciba `answer=""`.

**Conexión con el resto**: lo llaman los controllers (`query.py::POST /query`). Inyecta dependencias vía `rag_factory.py`.

### 8.9 `app/services/rag_factory.py` — composición lazy

**Para qué existe**: el problema de la versión anterior era que `RAGService` se construía en el lifespan y, si Ollama o MiniMax estaban caídos, la app **no podía bootear**. La solución: lazy init.

- `class _LazyInit(builder, name)`: thread-safe. En la primera llamada a `.get()`, ejecuta el builder. Cachea el resultado o captura la excepción y devuelve `None`. Tiene `.reset()` para tests.
- `build_rag_service(settings, db_session_factory)`: arma los 4 componentes lazy:
  - **`vector_store`**: `OllamaEmbeddings(base_url, model=..., validate_model_on_init=False, client_kwargs={"timeout": 5.0})` + `PGVector`. Envuelto en `VectorStore`.
  - **`sql_executor`**: closure que (a) llama `select_template(llm, question, courses, assignments)` con grounding, o (b) usa `FALLBACK_TEMPLATE`, y (c) ejecuta `execute_readonly`.
  - **`sql_agent`**: closure que invoca `run_sql_agent(llm, question, runtime=_TenantToolRuntime(...))`.
  - **`llm`**: `ChatAnthropic(model=settings.minimax_model, api_key=..., base_url=settings.minimax_base_url, temperature=0.0, max_tokens=4096)` apuntando al endpoint Anthropic-compatible de MiniMax.

**Conexión con el resto**: lo llama `app/main.py` lifespan. Es el pegamento entre `RAGService` y todos los submódulos.

---

## 9. Ingesta de documentos para embeddings

### 9.1 `app/services/ingest_service.py`

**Para qué existe**: PaquitoBot no solo responde preguntas sobre datos estructurados; también ingiere contenido de las tareas (descripción HTML, body de submissions) para que la búsqueda semántica tenga material.

**Cómo funciona**:
- `sanitize_html(text)`: usa `bleach.clean()` con tag whitelist. Sirve para no confiar en HTML arbitrario de Canvas.
- `strip_all(text)`: borra todos los tags.
- `readable(text)`: solo deja `p`, `br`, `strong`, `em`, `ul`, `ol`, `li`, `a` (con `href`).
- `prepare_assignment_chunks(assignment)`: arma un `Document` por source no-vacía, con metadata `{source, tenant_id, canvas_id, assignment_id}`. Sirve para que después `vector_store.upsert()` pueda filtrar por tenant.

**Conexión con el resto**: lo usa el pipeline de sync para alimentar el vector store.

---

## 10. Los endpoints públicos

### 10.1 `app/controllers/auth.py` — `POST /auth/canvas/connect`

**Para qué existe**: la primera vez que un estudiante llega, le entrega su token Canvas. Esta pieza lo valida contra Canvas (`GET /users/self`), lo cifra con Fernet, y lo persiste.

**Flujo**:
1. `verify_backend_jwt_dependency` → `user_id`.
2. Lee `X-Canvas-Token` del header.
3. Probe `GET {canvas_api_base_url}/users/self` con el token. Si 401/4xx → 401 al cliente. Si 5xx → 502.
4. `cipher = TokenCipher(settings.tenant_token_key)`.
5. `envelope = cipher.encrypt(canvas_token)`.
6. `service.get_or_create_tenant(user_id)` → `tenant.id`.
7. `service.store_canvas_token(tenant.id, encrypted_ciphertext, key_version)`.
8. `session.commit()`.
9. `Response(204)`.

**Conexión con el resto**: valida con `CanvasClient` (vía httpx directo para el probe), cifra con `TokenCipher`, persiste con `TenantService`.

### 10.2 `app/controllers/query.py` — `POST /query`

**Para qué existe**: el endpoint principal del RAG. Acepta una pregunta y devuelve respuesta natural.

**Flujo**:
1. `require_tenant_token` → `(tenant_id, _canvas_token)`.
2. Si `DISABLE_RAG_ROUTES=True` → 503.
3. `correlation_id = get_correlation_id() or new_correlation_id()`.
4. `language = payload.language or detect_language(payload.question)`.
5. `rag_service.provider_health()`.
6. `result = rag_service.answer(payload.question, tenant_id, language)`.
7. `record_rag_request(route, lang, "ok")`.
8. `QueryResponse(answer, lang, route, correlation_id)`.

**`QueryRequest` y `QueryResponse`**: ambos con `extra="forbid"` (rechaza `tenant_id` smuggling). El cliente no puede pretender ser otro tenant.

**Conexión con el resto**: usa la cadena de auth, `RAGService`, y `app/observability/metrics.py`.

### 10.3 `app/controllers/sync.py` — `POST /sync`

**Para qué existe**: dispara un sync manual del tenant autenticado. Útil cuando el estudiante necesita datos frescos antes de una sync programada.

**Flujo**:
1. `require_tenant_token` → `(tenant_id, _canvas_token)`.
2. `enforce_manual_rate_limit(session, tenant_id)` → si no pasó el intervalo, 429 con `Retry-After`.
3. `service.run_sync_for_tenant(tenant_id, session=session)`.
4. Si `status="locked"` → 429 `sync_locked`.
5. Si `TenantCredentialsMissing` → 403 `tenant_credentials_missing`.
6. `session.commit()`.
7. 202 con `{status, last_successful_at, last_status, last_error_class, correlation_id}`.

**Conexión con el resto**: usa `CanvasService` (oroquestro de sync), `TenantService` (rate limit), la cadena de auth.

### 10.4 `app/controllers/health.py` — `GET /healthz`

**Para qué existe**: reporte granular del estado de dependencias. Sirve para que el cliente sepa qué está y qué no.

**Qué reporta**: `status` (ok solo si Ollama + DB OK y scheduler running), `ollama`, `db`, `scheduler`, `rag_routes_disabled`.

**Conexión con el resto**: probea `RAGService().provider_health()` (Ollama), `make_engine_from_settings` + `SELECT 1` (DB), `app.state.scheduler` (scheduler).

---

## 11. Tenant Service: la pieza multi-tenant

### 11.1 `app/services/tenant_service.py`

**Para qué existe**: la pieza núcleo del multi-tenant. Hace dos cosas:
1. Resuelve `tenant_id` a partir de `user_id` (crea fila si no existe).
2. Custodia el token Canvas cifrado.

**Modos**:
- **SQLAlchemy** (default, producción): usa repositorio SQL. Todo va a Postgres.
- **In-memory** (legacy, tests): singleton con dicts. No persiste nada.

**`TenantRepository`**: métodos `get_or_create_tenant`, `get_tenant`, `get_canvas_credential`, `store_canvas_token`, `get_canvas_token`, `upsert_canvas_credential`. Nunca toca plaintext.

**`TenantService`**: `get_or_create_tenant(backend_user_id)`, `store_canvas_token(tenant_id, canvas_token, cipher, ...)`, `get_decrypted_canvas_token(tenant_id)`, `has_credentials(tenant_id)`. La descifrado siempre queda dentro del scope de request.

**`SESSION_STORE_STATE_FLAG`**: cuando está set en `app.state`, fuerza el modo SQL. Es lo que hace `create_app()` para producción.

**Conexión con el resto**: la pieza más transversal. La consumen todos los controllers, el `CanvasService`, y la cadena de auth.

---

## 12. Observabilidad

### 12.1 `app/observability/tracing.py`

**Para qué existe**: OpenTelemetry opcional. Default es no-op (`OTEL_ENABLED=0`). Activa con `OTEL_ENABLED=1` → OTLP gRPC + BatchSpanProcessor. Sirve para correlacionar spans cuando falla algo en producción.

**Conexión con el resto**: lo llama `app/main.py` lifespan.

### 12.2 `app/observability/metrics.py`

**Para qué existe**: declarar 6 métricas Prometheus que se incrementan en los puntos clave. Sirve para que un dashboard Grafana te diga "cuántas requests RAG fallaron por Ollama caído" en vez de tener que adivinar.

**Las 6 métricas**:
- `rag_requests_total{route, lang, outcome}`: counter RAG por ruta/idioma/resultado.
- `sql_validations_total{result}`: counter de validaciones text-to-SQL.
- `canvas_requests_total{endpoint, result}`: counter de requests a Canvas.
- `sync_runs_total{tenant_id_hash, result}`: counter de sync (tenant **hasheado**, no expone PII).
- `sync_lag_seconds{tenant_id_hash}`: gauge de lag (segundos desde el último sync).
- `token_decrypt_failures_total{result}`: counter de fallos Fernet.

Las funciones `record_*` están hechas para **nunca lanzar**: si Prometheus está caído, el sistema sigue.

**Conexión con el resto**: lo llaman los controllers (al terminar una request), `CanvasService` (al completar un sync), el executor (al validar SQL).

---

## 13. Migraciones: `alembic/`

### 13.1 `alembic/versions/0001_init.py`

**Para qué existe**: la primera migración. Crea las 8 tablas con DDL escrito a mano (no autogenerado). Sirve para que el modelo de PaquitoBot quede fijo y reproducible.

**Conexión con el resto**: Alembic las aplica contra `SUPABASE_DATABASE_URL`. `app/models/__init__.py` expone la `Base.metadata` que Alembic usa para detectar drift.

---

## 14. Tests

### 14.1 Estructura `tests/`

**Qué hay**:
- `tests/smoke/`: 4 archivos con tests de imports, healthz, controllers (auth, sync). Son **smoke** porque verifican que el sistema arranca y responde, no la lógica.
- `tests/unit/`: ~22 archivos con tests profundos de cada pieza (DTOs, repositories, validator, executor, sync pipeline, agent, etc.). Cubren casos felizes y bordes.

**213 tests pasando totales** (177 unit + 36 smoke).

### 14.2 `_selftest()` en cada módulo

**Para qué existe**: en vez de tener todos los tests en `tests/`, cada módulo tiene al final un `_selftest()` o bloque `__main__` ejecutable con `python -m app.x.y`. Sirve para que cuando estés debugeando un módulo puntual puedas correr su selftest aislado sin levantar el resto.

**Conexión con el resto**: conviven con los tests de pytest. No compiten, se complementan.

---

## 15. Cómo se corre el sistema

```bash
# 1. Sincronizar modelo con la DB
alembic upgrade head

# 2. Levantar el monolito FastAPI
python main.py
# Equivale a uvicorn app.main:app --host $HOST --port $PORT --log-level $LOG_LEVEL [--reload]

# 3. Sincronizar manualmente un tenant (con JWT en mano)
curl -X POST http://localhost:8000/sync \
  -H "Authorization: Bearer $JWT" \
  -H "X-Correlation-ID: $(uuidgen)"

# 4. Preguntar al RAG
curl -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"question": "¿cuáles son mis cursos?"}'

# 5. Tests
pytest -q --no-cov tests/unit
pytest -q --no-cov tests/smoke
pytest -q --no-cov tests/unit tests/smoke  # toda la suite
```

**Las piezas que participan en cada comando**:
- Al arrancar: `main.py` → `app/main.py:create_app()` → lifespan → `Settings` → `RAGService (lazy)` → scheduler.
- Al pedir `/query`: middleware (correlation_id) → `core/deps.py:require_tenant_token` → `TenantService.get_decrypted_canvas_token` → `RAGService.answer` → router → (relational | semantic | hybrid) → MiniMax + Ollama → `QueryResponse`.
- Al pedir `/sync`: middleware → `require_tenant_token` → `CanvasService.run_sync_for_tenant` → lock → `sync_tenant` → `paginate` + DTOs + `upsert_by_canvas_id` + `sync_state`.

---

## 16. Mapa mental en una sola imagen

```
                      ┌──────────────────────────┐
                      │    Estudiante / Cliente  │
                      └────────────┬─────────────┘
                                   │ HTTP + JWT
                                   ▼
        ┌──────────────────────────────────────────────┐
        │  CorrelationIdMiddleware  →  /auth/canvas    │
        │                              /sync             │
        │                              /query            │
        │                              /healthz          │
        └────────┬────────────────────┬─────────────────┘
                 │                    │
                 ▼                    ▼
        ┌────────────────┐    ┌──────────────────────────┐
        │ controllers/   │    │ controllers/query.py     │
        │ auth.py /sync  │    │  → RAGService.answer     │
        └────────┬───────┘    └────────────┬─────────────┘
                 │                         │
                 ▼                         ▼
        ┌────────────────────┐    ┌──────────────────────────┐
        │ TenantService      │    │ rag_factory (lazy)      │
        │ (cipher + tenant)  │    │  → RAGService           │
        └────────┬───────────┘    └────────┬─────────────────┘
                 │                         │
                 ▼                         ▼
        ┌──────────────────────────────────────────────┐
        │              Postgres / Supabase             │
        │   tenants, canvas_credentials, users,        │
        │   courses, enrollments, assignments,         │
        │   submissions, sync_state,                   │
        │   documents (PGVector)                       │
        └──────────────────────────────────────────────┘
                 ▲                         ▲
                 │                         │
        ┌────────┴────────┐        ┌───────┴──────────┐
        │ CanvasService   │        │  Ollama (qwen3)  │
        │ + CanvasClient  │        │  embeddings      │
        │ + paginate      │        │                  │
        │ + DTOs          │        └──────────────────┘
        └────────┬────────┘
                 │ HTTPS GET
                 ▼
        ┌────────────────────┐
        │ Canvas REST API    │
        └────────────────────┘



MiniMax-M3 ←──────────  RAGService.answer
(Anthropic-compatible)  usa MiniMax para redactar
```

---

## 17. Decisiones de producto que están detrás del código

Si vas a tocar código, estas decisiones ya están tomadas y hay que respetarlas:

1. **Read-only con Canvas**: jamás escribir en Canvas. Solo GET.
2. **Multiusuario estricto**: cada estudiante = un tenant. El `tenant_id` se deriva del JWT `sub`, nunca del body.
3. **Datos solos propios**: nunca se exponen datos de otros estudiantes. `strip_peer_data` filtra las submissions de pares.
4. **Sync cada 6h + manual rate-limited**: no se martillea Canvas.
5. **Respuestas en el idioma de la pregunta**. El LLM detecta, no se fuerza.
6. **MiniMax-M3 (endpoint Anthropic-compatible) para chat**, Ollama para embeddings. Costo y latencia.
7. **Postgres para todo**: SQLAlchemy + PGVector. No hay otra base.
8. **TenantId como UUID interno**: derivado del `sub` (string) del JWT. Eso evita adivinar.
9. **Watermark solo avanza si el sync fue exitoso**: si algo falla, no se pierde progreso.
10. **Fail-closed en config**: si falta una variable de entorno crítica, boom al startup, no en producción.
11. **RedactionFilter everywhere**: un secreto nunca llega al log.
12. **Lazy init de dependencias externas**: la app arranca aunque Ollama o MiniMax estén caídos.

---

## 18. Glosario rápido

- **Tenant**: un usuario del sistema (un estudiante). Tiene un `tenant_id` (UUID) que se autogenera la primera vez.
- **Backend JWT**: JWT firmado con `BACKEND_SECRET`. Lo emite PaquitoBot. El cliente lo manda en `Authorization: Bearer`.
- **Canvas token**: el token personal del estudiante en Canvas. Se cifra con Fernet y se guarda en `canvas_credentials`. **Nunca** se guarda en plaintext.
- **RAG**: Retrieval-Augmented Generation. Acá significa "una pregunta → buscar evidencia → generar respuesta".
- **Embedding**: un vector numérico (1024 dimensiones) que representa el significado de un texto. PaquitoBot lo usa para búsqueda semántica con PGVector.
- **PGVector**: extensión de Postgres para guardar y buscar vectores.
- **Allow-list**: lista cerrada de SQL templates que PaquitoBot puede ejecutar. El LLM no puede inventar SQL.
- **Correlación**: un UUID v4 por request que aparece en todos los logs y respuestas para facilitar el debugging.
- **Soft-delete**: marcar un registro como `deleted_at = NOW()` en vez de borrarlo. Permite recuperar y mantener trazabilidad.
- **Watermark**: marca de progreso en `sync_state.last_watermark`. Representa hasta dónde llegó el último sync exitoso.
- **Fail-closed**: si falta una pieza crítica, el sistema se niega a arrancar en vez de degradar silenciosamente.
- **Lazy init**: diferir la construcción de un objeto hasta que se use por primera vez. Sirve para que el lifespan no se cuelgue si una dependencia externa está caída.
