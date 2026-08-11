# Guía de despliegue v1 — Configuración con Supabase y credenciales

> Audiencia: agente IA con acceso al MCP de Supabase del proyecto (herramientas tipo `mcp__supabase__execute_sql`, `mcp__supabase__list_tables`, etc.) y a las credenciales del entorno.
>
> Objetivo: dejar el backend `paquitobot-rag` listo para ejecutarse contra Supabase real (no SQLite), con migración aplicada, rol read-only creado, vector store validado, credenciales rotadas en `.env` y smoke end-to-end verde.
>
> Lo que NO hace esta guía: modificar código del backend, pushear a GitHub, rotar claves de producción, destruir datos existentes.

---

## Convenciones del agente

- Antes de cada paso destructivo, listar lo que se va a tocar y pedir confirmación humana con la evidencia previa.
- Nunca imprimir credenciales en logs. Si necesitás referenciar un valor, mostrar los primeros 8 caracteres seguidos de `…` y nunca el valor completo.
- Antes de modificar la base, correr una consulta `SELECT` de lectura que confirme el estado actual. La diferencia entre `antes` y `después` debe documentarse.
- Si una operación falla, no continuar. Reportar el error textual, la consulta ejecutada y el estado conocido, y detenerse hasta pedir confirmación.
- Idempotencia: cada paso se puede re-ejecutar. Si una migración ya está aplicada, `alembic current` debe decirlo y el agente se saltea el paso sin error.

---

## Pre-requisitos que el agente debe verificar

1. Acceso al repo en `https://github.com/Kaquenduri/paquitobot-rag` (rama `main`).
2. Acceso de lectura al entorno (Linux/macOS/Windows con Python 3.11+).
3. Acceso al MCP de Supabase del proyecto donde está el vector store `documents` (1024 dim, según el diseño).
4. Las credenciales que el usuario debe entregar ANTES de arrancar:
   - `SUPABASE_DATABASE_URL` con esquema `postgresql+psycopg://postgres:PASSWORD@HOST:PORT/DBNAME` (URL pooled de Supabase, no directa, para que el advisory lock funcione con `psycopg`).
   - `MINIMAX_API_KEY` (https://platform.minimax.io/user-center/basic-information/interface-key).
   - Token personal de Canvas (`CANVAS_API_TOKEN`).
   - URL base de Canvas (`CANVAS_API_BASE_URL`, default `https://tecsup.instructure.com/api/v1`).
5. Permisos en Supabase: el rol `postgres` debe poder `CREATE ROLE`, `GRANT`, `CONNECT`, y leer `information_schema`. El agente valida esto con una consulta previa.

---

## PASO 1 — Reconocimiento del proyecto Supabase

Antes de tocar nada, el agente inspecciona lo que ya existe.

### 1.1 Listar tablas

Usar `mcp__supabase__list_tables` o ejecutar:

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY table_schema, table_name;
```

Anotar:
- ¿Existe la tabla `documents`?
- ¿Qué otras tablas del backend (`tenants`, `canvas_credentials`, `users`, `courses`, `enrollments`, `assignments`, `submissions`, `sync_state`) ya existen?

### 1.2 Si existen tablas del backend

Para cada tabla que ya exista, mostrar conteo:

```sql
SELECT 'tenants' AS name, count(*) FROM tenants
UNION ALL SELECT 'canvas_credentials', count(*) FROM canvas_credentials
UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'courses', count(*) FROM courses
UNION ALL SELECT 'enrollments', count(*) FROM enrollments
UNION ALL SELECT 'assignments', count(*) FROM assignments
UNION ALL SELECT 'submissions', count(*) FROM submissions
UNION ALL SELECT 'sync_state', count(*) FROM sync_state;
```

**Si hay tablas con datos** (count > 0 en cualquiera), el agente debe pedir confirmación humana antes de continuar. La migración `0001_init` es aditiva y no dropea tablas, pero si existe un esquema previo con columnas distintas, podría haber conflictos.

### 1.3 Validar vector store

```sql
SELECT column_name, data_type, udt_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'documents'
ORDER BY ordinal_position;
```

Esperado: una columna `embedding` de tipo `USER-DEFINED` o `vector` con dimensión 1024.

Si la columna no existe o la dimensión es distinta a 1024, **DETENERSE y reportar**. El modelo Ollama configurado (`qwen3-embedding:8b`) emite vectores de 1024 dimensiones; cualquier otra dimensión hará fallar las inserciones.

Para confirmar la dimensión exacta (Postgres + extensión vector):

```sql
SELECT format_type(a.atttypid, a.atttypmod) AS type
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relname = 'documents' AND a.attname = 'embedding';
```

La salida debe contener `(1024)` o `vector(1024)`.

### 1.4 Confirmar versión de Postgres

```sql
SHOW server_version;
```

Esperado: `14.x` o superior (Supabase usa 15+ por defecto).

---

## PASO 2 — Aplicar migración Alembic

### 2.1 Clonar el repo

```bash
git clone https://github.com/Kaquenduri/paquitobot-rag.git
cd paquetobot-rag
git log --oneline | head -n 3
```

### 2.2 Instalar dependencias

```bash
python -m venv .venv
. .venv/bin/activate   # en Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
pip check
```

Si `pip check` reporta conflictos de `pydantic-settings`, ejecutar `pip install --upgrade pydantic-settings` y verificar que quede en una versión `>=2.10.1,<3`.

### 2.3 Cargar `.env` con la URL real de Supabase

El usuario debe entregar `SUPABASE_DATABASE_URL`. Mientras no lo entregue, **DETENERSE**.

Cuando lo entregue:

```bash
cp .env.example .env
chmod 600 .env
```

Editar `.env` y poner la URL real en `SUPABASE_DATABASE_URL`. Mantener el resto de los campos con sus defaults o placeholders hasta PASO 5.

### 2.4 Verificar que la migración es nueva vs ya aplicada

```bash
export SUPABASE_DATABASE_URL="<la-url-real>"
alembic current
```

- Si la salida muestra `0001_init (head)`, la migración ya está aplicada. Saltar a PASO 3.
- Si no hay nada, continuar.

### 2.5 Aplicar la migración

```bash
alembic upgrade head
```

El agente debe leer la salida. Si hay errores, capturar el SQL exacto que falló y reportarlo. La migración es ADITIVA (no hace drop, no toca `documents`); cualquier error indica un conflicto con estado previo.

### 2.6 Confirmar

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
AND table_name IN ('tenants','canvas_credentials','users','courses','enrollments','assignments','submissions','sync_state')
ORDER BY table_name;
```

Esperado: 8 filas.

```sql
SELECT count(*) FROM documents;
```

Esperado: el mismo número que en PASO 1.1.

---

## PASO 3 — Provisionar el rol `pg_role_canvas_readonly`

Este rol aplica `SET LOCAL default_transaction_read_only=on` cuando el executor de Text-to-SQL ejecuta queries. Es crítico para defensa en profundidad del Text-to-SQL.

### 3.1 Confirmar que el rol no existe

```sql
SELECT 1 FROM pg_roles WHERE rolname = 'pg_role_canvas_readonly';
```

Si la consulta devuelve una fila, el rol ya existe. Saltar a PASO 3.4 para re-validar los grants.

### 3.2 Crear el rol (CONFIRMACIÓN HUMANA REQUERIDA)

Explicar al usuario que este paso crea un rol nuevo en la base. Pedir confirmación explícita antes de ejecutar.

```sql
CREATE ROLE pg_role_canvas_readonly NOLOGIN;
```

### 3.3 Otorgar permisos mínimos

Sustituir `<DBNAME>` por el nombre real de la DB (en Supabase suele ser `postgres`).

```sql
GRANT CONNECT ON DATABASE <DBNAME> TO pg_role_canvas_readonly;
GRANT USAGE ON SCHEMA public TO pg_role_canvas_readonly;
GRANT SELECT ON TABLE
    tenants,
    canvas_credentials,
    users,
    courses,
    enrollments,
    assignments,
    submissions,
    sync_state
TO pg_role_canvas_readonly;
```

### 3.4 Validar grants

```sql
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'pg_role_canvas_readonly'
ORDER BY table_name;
```

Esperado: 8 filas, todas con `privilege_type = 'SELECT'`.

### 3.5 Validar que NO tiene permisos de escritura

```sql
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'pg_role_canvas_readonly'
AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES');
```

Esperado: 0 filas.

Si esta consulta devuelve filas, **DETENERSE y reportar**: el rol no tiene la configuración esperada y cualquier query Text-to-SQL podría escribir datos.

---

## PASO 4 — Generar credenciales del backend

Este paso NO toca Supabase; genera los secretos que el backend usará.

### 4.1 Generar `TENANT_TOKEN_KEY` (Fernet)

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Esta es la clave que cifra los tokens de Canvas. Guardarla en `.env` como `TENANT_TOKEN_KEY`.

### 4.2 Generar `BACKEND_SECRET`

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Esta es la clave HMAC para los JWT del backend. Guardarla como `BACKEND_SECRET`.

### 4.3 Validar manualmente que la clave Fernet es válida

```bash
python -c "
from cryptography.fernet import Fernet
import os
key = os.environ['TENANT_TOKEN_KEY']
f = Fernet(key.encode())
token = f.encrypt(b'prueba')
assert f.decrypt(token) == b'prueba'
print('Fernet OK')
"
```

Si esto falla, la clave no es Fernet válida; regenerar.

### 4.4 Anotar credenciales entregadas por el usuario

El usuario entrega:
- `MINIMAX_API_KEY` (https://platform.minimax.io/user-center/basic-information/interface-key).
- `CANVAS_API_TOKEN` (token personal del estudiante).
- `CANVAS_API_BASE_URL` (default: `https://tecsup.instructure.com/api/v1`).

Confirmar que cada una fue entregada antes de continuar. Si falta alguna, **DETENERSE**.

---

## PASO 5 — Configurar `.env` completo

Editar `.env` con todos los valores. El archivo final debe verse así (reemplazar `<…>` con los valores reales):

```
SUPABASE_DATABASE_URL=postgresql+psycopg://postgres:<password>@<host>:<port>/<dbname>
TENANT_TOKEN_KEY=<fernnet-key>
BACKEND_SECRET=<backend-secret>
MINIMAX_API_KEY=<minimax-key>
MINIMAX_BASE_URL=https://api.minimax.io/anthropic
MINIMAX_MODEL=MiniMax-M3
OLLAMA_HOST=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=qwen3-embedding:8b
OLLAMA_EMBED_DIM=1024
CANVAS_API_BASE_URL=https://tecsup.instructure.com/api/v1
SYNC_INTERVAL_SECONDS=21600
SYNC_JITTER_SECONDS=600
MANUAL_SYNC_MIN_INTERVAL_SECONDS=60
SQL_STATEMENT_TIMEOUT_MS=2000
SQL_ROW_LIMIT=200
LOG_LEVEL=INFO
SCHEDULER_ENABLED=false
DISABLE_RAG_ROUTES=false
LEGACY_MODE=0
```

### 5.1 Validar `Settings` (fail-closed)

```bash
python -c "
from app.core.config import get_settings
get_settings.cache_clear()
s = get_settings()
print('OK supabase:', s.supabase_database_url[:25] + '...')
print('OK canvas:', s.canvas_api_base_url)
"
```

Si esto lanza `ValidationError`, falta una variable crítica. Reportar cuál.

### 5.2 Probar conexión SQLAlchemy

```bash
python -c "
import os
from app.core.db import engine_for_url
from sqlalchemy import text
eng = engine_for_url(os.environ['SUPABASE_DATABASE_URL'])
with eng.connect() as c:
    print('tenants:', c.execute(text('SELECT count(*) FROM tenants')).scalar())
    print('courses:', c.execute(text('SELECT count(*) FROM courses')).scalar())
"
```

Esperado: imprime los conteos sin error.

### 5.3 Probar redacción de logs

```bash
python -c "
import logging
from app.security.redaction import RedactionFilter
log = logging.getLogger('redaction.smoke')
log.setLevel(logging.INFO); log.propagate = False
h = logging.StreamHandler()
h.setFormatter(logging.Formatter('%(message)s'))
log.addHandler(h); log.addFilter(RedactionFilter())
log.info('test Bearer gAAAAAabcdef0123 postgresql://u:p@h/d safe')
"
```

Esperado: el log NO contiene `gAAAAAabcdef0123` ni `postgresql://u:p`. Sí debe contener `***REDACTED***` y `safe`.

---

## PASO 6 — Validar servicios externos

### 6.1 MiniMax (Anthropic-compatible)

```bash
curl -s -X POST "$MINIMAX_BASE_URL/v1/messages" \
  -H "x-api-key: $MINIMAX_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"'"$MINIMAX_MODEL"'","max_tokens":32,"messages":[{"role":"user","content":"responde con la palabra OK"}]}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['content'][0]['text'])"
```

Esperado: `OK`. Si falla, la API key es inválida o el modelo cambió.

### 6.2 Canvas

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_BASE_URL/users/self" \
  | python -m json.tool | head -n 30
```

Esperado: JSON con campos `id`, `name`, `email`. Si 401, el token es inválido o expiró. Pedir al usuario uno nuevo.

### 6.3 Ollama local

```bash
which ollama || curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3-embedding:8b
curl -s http://localhost:11434/api/embeddings \
  -d '{"model":"qwen3-embedding:8b","prompt":"health check"}' \
  | python -c "import json,sys; d=json.load(sys.stdin); print('dim:', len(d['embedding']))"
```

Esperado: `dim: 1024`. Si Ollama corre en otro host, ajustar `OLLAMA_HOST` en `.env`.

---

## PASO 7 — Smoke end-to-end

### 7.1 Levantar el servidor

```bash
python main.py
```

Esto delega a `uvicorn app.main:app` (cuando `LEGACY_MODE=0`, el default).

Esperado: el proceso queda corriendo y muestra en logs:
- `app boot started` con configuración cargada.
- `provider health ok` o equivalente.

### 7.2 Health check

```bash
curl -s http://127.0.0.1:8000/healthz | python -m json.tool
```

Esperado:

```json
{
  "status": "ok",
  "ollama": {"available": true, "error_class": null},
  "db": {"available": true, "error_class": null},
  "scheduler": {"running": false, "enabled": false},
  "rag_routes_disabled": false
}
```

Si `status: "degraded"` o alguna dependencia falla, diagnosticar antes de continuar.

### 7.3 Generar un JWT de prueba

```bash
python -c "
import jwt, time, os
secret = os.environ['BACKEND_SECRET']
print(jwt.encode({'sub': 'smoke-user', 'iat': int(time.time()), 'exp': int(time.time())+3600}, secret, algorithm='HS256'))
" > /tmp/jwt.txt
```

### 7.4 Conectar token de Canvas

```bash
JWT=$(cat /tmp/jwt.txt)
curl -s -X POST http://127.0.0.1:8000/auth/canvas/connect \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d "{\"canvas_token\":\"$CANVAS_API_TOKEN\"}" \
  -w "\nHTTP %{http_code}\n"
```

Esperado: `HTTP 204`. Si 401, el JWT o el token de Canvas es inválido.

### 7.5 Confirmar cifrado en DB

```sql
SELECT tenant_id, length(ciphertext), key_version FROM canvas_credentials;
```

Esperado: 1 fila con `tenant_id` UUID, `length(ciphertext)` ≈ 200, `key_version = 1`.

Verificar que el ciphertext NO contiene el token en plaintext:

```sql
SELECT encode(ciphertext, 'escape') FROM canvas_credentials;
```

El agente debe inspeccionar el resultado y confirmar visualmente que NO aparece el token de Canvas. Si aparece, **DETENERSE y reportar**: el cifrado Fernet no se aplicó.

### 7.6 Sincronización manual

```bash
curl -s -X POST http://127.0.0.1:8000/sync \
  -H "Authorization: Bearer $JWT" \
  -w "\nHTTP %{http_code}\n"
```

Esperado: `HTTP 202` con cuerpo `{status, last_successful_at, last_status, last_error_class}`.

### 7.7 Confirmar ingestión

```sql
SELECT
    (SELECT count(*) FROM courses) AS courses,
    (SELECT count(*) FROM assignments) AS assignments,
    (SELECT count(*) FROM submissions) AS submissions;
```

Esperado: cursos y assignments con conteo > 0 (depende del estudiante). Submissions puede ser 0 si no ha entregado nada.

```sql
SELECT last_status, last_successful_at, last_error_class FROM sync_state;
```

Esperado: `last_status='success'` o un error específico.

### 7.8 Query semántica

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"question":"¿Cuándo es la próxima entrega de cualquier curso?","lang":"es"}' \
  | python -m json.tool
```

Esperado: `{answer, lang, route, correlation_id}` con `route: "semantic"` o `"hybrid"` y `answer` no vacío.

### 7.9 Query relacional

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"question":"¿Cuántas tareas tengo en total?","lang":"es"}' \
  | python -m json.tool
```

Esperado: `route: "relational"` con un número entero en `answer`.

### 7.10 Verificar aislamiento entre tenants

Generar un segundo JWT con `sub` distinto:

```bash
python -c "
import jwt, time, os
print(jwt.encode({'sub': 'second-user', 'iat': int(time.time()), 'exp': int(time.time())+3600}, os.environ['BACKEND_SECRET'], algorithm='HS256'))
" > /tmp/jwt2.txt
JWT2=$(cat /tmp/jwt2.txt)

curl -s -X POST http://127.0.0.1:8000/auth/canvas/connect \
  -H "Authorization: Bearer $JWT2" \
  -H "Content-Type: application/json" \
  -d "{\"canvas_token\":\"$CANVAS_API_TOKEN\"}" \
  -w "\nHTTP %{http_code}\n"
```

Esperado: `HTTP 204`.

```sql
SELECT tenant_id FROM canvas_credentials ORDER BY created_at;
```

Esperado: 2 filas con `tenant_id` distintos. Si son iguales, **DETENERSE**: hay un bug en el aislamiento por tenant.

### 7.11 Verificar rechazo de `tenant_id` por body

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"question":"hola","tenant_id":"00000000-0000-0000-0000-000000000000"}' \
  -w "\nHTTP %{http_code}\n"
```

Esperado: `HTTP 422` (Pydantic `extra="forbid"`).

### 7.12 Verificar logs sin secretos

```bash
# Verificar que el log de uvicorn no contiene el token en plaintext
grep -c "$CANVAS_API_TOKEN" /tmp/uvicorn.log 2>/dev/null || echo "0 matches (safe)"
```

Esperado: `0 matches`.

---

## PASO 8 — Activar scheduler (opcional)

Si el usuario quiere que el sync corra automáticamente cada 6 horas:

### 8.1 Editar `.env`

```
SCHEDULER_ENABLED=true
```

### 8.2 Reiniciar el servidor

```bash
# matar el proceso anterior
pkill -f 'uvicorn app.main:app'
python main.py
```

### 8.3 Confirmar

```bash
curl -s http://127.0.0.1:8000/healthz | python -c "import json,sys; d=json.load(sys.stdin); print('scheduler:', d['scheduler'])"
```

Esperado: `{'running': True, 'enabled': True}`.

---

## PASO 9 — Reporte final

El agente debe generar `deploy-report.md` con:

1. Resumen ejecutivo de 3-5 líneas.
2. Tabla con resultado de cada paso (PASS / FAIL / SKIPPED) y evidencia.
3. Conteos finales en DB después del smoke.
4. Confirmaciones de seguridad (token cifrado, log sin secretos, multi-tenant).
5. Riesgos residuales y recomendaciones.
6. Lista de variables de entorno activas (sin valores).

Si el usuario lo autoriza, hacer `git add deploy-report.md` y commitear localmente. NO pushear sin autorización explícita.

---

## Comandos de rollback (sólo si algo sale mal)

### Detener el servidor

```bash
pkill -f 'uvicorn app.main:app'
```

### Revertir migración

```bash
alembic downgrade -1
```

NO borra las tablas, sólo revierte a una versión anterior. Como sólo hay una migración, baja a "base".

### Eliminar datos del smoke

```sql
DELETE FROM sync_state;
DELETE FROM submissions;
DELETE FROM assignments;
DELETE FROM enrollments;
DELETE FROM courses;
DELETE FROM users;
DELETE FROM canvas_credentials;
DELETE FROM tenants;
```

### Eliminar el rol read-only

```sql
REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM pg_role_canvas_readonly;
REVOKE USAGE ON SCHEMA public FROM pg_role_canvas_readonly;
REVOKE CONNECT ON DATABASE <DBNAME> FROM pg_role_canvas_readonly;
DROP ROLE IF EXISTS pg_role_canvas_readonly;
```

---

## Riesgos a documentar en el reporte

1. `TENANT_TOKEN_KEY` se guarda en `.env`. Si la máquina se compromete, hay que re-encriptar todos los `canvas_credentials.ciphertext` con una nueva clave. Esta rotación NO está implementada en v1.
2. El token de Canvas personal se guarda en `.env` para el smoke, pero en producción cada usuario lo entrega por `POST /auth/canvas/connect`. El agente NO debe seguir usando el token del `.env` después del smoke.
3. Si la dimensión de `documents.embedding` cambia, los embeddings nuevos fallarán.
4. El scheduler es in-process. Si se reinicia el servidor, los ticks se pierden hasta el siguiente.
5. `pg_role_canvas_readonly` no encripta datos; sólo limita permisos.
6. No hay CI; cualquier cambio se valida localmente.
7. El agente no debe commitear `.env`. Verificar con `git status` antes de cualquier commit.

---

## Definition of Done

- [ ] Las 8 tablas existen en Supabase (7 del backend + `documents` preservada).
- [ ] El rol `pg_role_canvas_readonly` tiene exactamente SELECT sobre las 7 tablas.
- [ ] `pip check` limpio.
- [ ] 213 tests passing (177 unit + 36 smoke).
- [ ] `GET /healthz` reporta `status: "ok"`.
- [ ] `POST /auth/canvas/connect` cifra el token (ciphertext NO contiene plaintext).
- [ ] `POST /sync` ejecuta e ingiere al menos 1 curso y 1 assignment.
- [ ] `POST /query` responde en español.
- [ ] Cross-tenant: JWT distinto crea `tenant_id` distinto.
- [ ] `POST /query` con `tenant_id` en body devuelve 422.
- [ ] Logs no contienen credenciales en plaintext.
- [ ] `.env` está en `.gitignore` y NO está commiteado.
