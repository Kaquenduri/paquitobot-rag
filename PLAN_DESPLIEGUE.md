# Plan de implementación — Despliegue de Canvas RAG Backend v1

> Plan ejecutivo para un agente IA. El agente tiene acceso a:
> - Repositorio GitHub: `https://github.com/Kaquenduri/paquitobot-rag` (rama `main`).
> - Proyecto Supabase con credenciales de servicio (`SUPABASE_DATABASE_URL`).
> - API Key de Google AI Studio (Gemini).
> - API URL + token de Canvas LMS (tecsup.instructure.com).
> - Máquina/host Linux con Python 3.14 y `curl`/`psql`/`pg_dump` disponibles.
>
> El código ya está implementado y verificado (213 tests passing, ruff limpio). Este plan ejecuta el despliegue de extremo a extremo y valida la primera versión funcional.

---

## Convenciones del plan

- **Modo**: autónomo con checkpoints. El agente ejecuta tareas en orden; cuando una tarea valida o falla, registra evidencia antes de continuar.
- **Estado de tareas**: cada paso tiene `STATUS: TODO/IN_PROGRESS/DONE/BLOCKED` y un comando de validación que el agente corre para confirmar.
- **Idempotencia**: cada paso es re-ejecutable. Si falla, el agente diagnostica con la evidencia registrada antes de continuar.
- **Sin secretos en logs**: el agente nunca debe imprimir `SUPABASE_DATABASE_URL`, `TENANT_TOKEN_KEY`, `BACKEND_SECRET`, `GEMINI_API_KEY`, `CANVAS_API_TOKEN`, ni URLs completas con credenciales. Para verificar conexión usar códigos de salida y mensajes de error sanitizados.
- **Reversibilidad**: cada paso deProvisioning tiene una contraparte de rollback. Si un paso destructivo falla, el agente debe poder restaurar el estado previo.
- **Confirmación humana**: las acciones que tocan infraestructura compartida (crear roles en Postgres, drop de tablas, push a `main`) requieren que el agente pida confirmación explícita antes de proceder, salvo donde el plan diga `AUTONOMOUS`.
- **Entorno Windows**: el intérprete configurado en `requirements.txt` y `pyproject.toml` es Windows-native. Si el agente corre en Linux/macOS, debe usar `python3.14` y ajustar `pip` accordingly. No usar `/mnt/c/...` desde Linux sin mapear a la ruta correcta del repo.

---

## FASE 0 — Pre-flight (AUTONOMOUS)

### 0.1 Clonar y verificar el repositorio

```bash
git clone https://github.com/Kaquenduri/paquitobot-rag.git
cd paquetobot-rag
git log --oneline | head -n 5
git status
```

STATUS esperado: limpio, rama `main`, al menos los commits `771d43b` y `b331b6e`.

### 0.2 Confirmar Python y dependencias del sistema

```bash
python --version           # debe ser 3.14.x
pip --version
which psql || echo "psql NOT FOUND"
which curl
which openssl
```

Si Python no es 3.14, documentar la versión actual y continuar (los tests existentes usan 3.14, pero el código es compatible con 3.11+).

### 0.3 Instalar el entorno virtual y dependencias

```bash
python -m venv .venv
. .venv/bin/activate       # en Linux/macOS; en Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
pip check                  # debe decir "No broken requirements found."
```

Si `pip check` reporta conflictos, ejecutar `pip install --upgrade pydantic-settings` (debe quedar en `>=2.10.1,<3`).

### 0.4 Verificar tests existentes

```bash
pytest -q --no-cov tests/unit 2>&1 | tail -n 3   # debe decir "177 passed"
pytest -q --no-cov tests/smoke 2>&1 | tail -n 3  # debe decir "36 passed"
ruff check app/ main.py                          # debe decir "All checks passed!"
```

Si los tests fallan, **STOP**: no proceder. Documentar el output y pedir revisión humana.

### 0.5 Crear `.env` con placeholders

```bash
cp .env.example .env
chmod 600 .env
```

**STOP**: pedir al usuario los valores reales antes de continuar con la Fase 1.

Solicitar:

1. `SUPABASE_DATABASE_URL` (formato `postgresql+psycopg://postgres:PASSWORD@HOST:PORT/DBNAME`).
2. `TENANT_TOKEN_KEY`: ejecutar `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` y entregar el resultado.
3. `BACKEND_SECRET`: el agente genera `python -c "import secrets; print(secrets.token_urlsafe(64))"`.
4. `GEMINI_API_KEY`: pedir al usuario.
5. `CANVAS_API_BASE_URL`: confirmar `https://tecsup.instructure.com/api/v1`.
6. `CANVAS_API_TOKEN` (token personal de prueba del usuario en Canvas).
7. Confirmar si `SCHEDULER_ENABLED=true` para esta primera versión.

Una vez recibidos, llenar `.env` con los valores. Verificar que el archivo NO se commitea (`git check-ignore .env` debe decir que está ignorado).

---

## FASE 1 — Provisioning de base de datos (AUTONOMOUS con confirmación)

### 1.1 Validar conectividad a Supabase

```bash
# desde la máquina del agente, no desde el repo
psql "$SUPABASE_DATABASE_URL" -c "SELECT version();"
```

STATUS esperado: PostgreSQL 14 o superior (Supabase usa 15+).

Si falla, **STOP**: pedir al usuario verificar IP allowlist de Supabase o usar connection pooling (Supabase provee dos URLs: directa y pooled).

### 1.2 Listar schema actual de Supabase

```bash
psql "$SUPABASE_DATABASE_URL" -c "\dt"
```

Anotar las tablas existentes. Esperado: `documents` (vector store legacy de PGVector, 1024 dimensiones) y posiblemente ninguna otra.

### 1.3 Aplicar la migración `0001_init`

```bash
cd paquetobot-rag
alembic upgrade head
```

STATUS esperado: 7 tablas creadas (`tenants`, `canvas_credentials`, `users`, `courses`, `enrollments`, `assignments`, `submissions`, `sync_state`).

Si falla por tablas pre-existentes, **STOP**: el agente NO debe hacer drop. Pedir confirmación humana para diagnosticar.

Verificación post-migración:

```bash
psql "$SUPABASE_DATABASE_URL" -c "\dt"
psql "$SUPABASE_DATABASE_URL" -c "\d+ tenants"
psql "$SUPABASE_DATABASE_URL" -c "SELECT count(*) FROM documents;"
```

El conteo de `documents` debe ser el mismo que antes (la migración es aditiva, no toca PGVector).

### 1.4 Provisionar el rol `pg_role_canvas_readonly` (CONFIRMACIÓN HUMANA REQUERIDA)

Ejecutar el snippet de `alembic/README.md` con el nombre de la DB real:

```sql
CREATE ROLE pg_role_canvas_readonly NOLOGIN;
GRANT CONNECT ON DATABASE <DBNAME> TO pg_role_canvas_readonly;
GRANT USAGE ON SCHEMA public TO pg_role_canvas_readonly;
GRANT SELECT ON TABLE
    tenants, canvas_credentials, users, courses,
    enrollments, assignments, submissions, sync_state
TO pg_role_canvas_readonly;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
```

Aplicar con `psql` conectado como superusuario.

Verificar:

```sql
SELECT grantee, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'pg_role_canvas_readonly';
```

STATUS esperado: 8 filas con `privilege_type = 'SELECT'`.

### 1.5 Confirmar dimensión del vector store

```sql
SELECT column_name, udt_name, character_maximum_length
FROM information_schema.columns
WHERE table_name = 'documents';
```

STATUS esperado: `embedding` con tipo `USER-DEFINED` o `vector(1024)`. Si la dimensión NO es 1024, **STOP**: el modelo Ollama es `qwen3-embedding:8b` que produce vectores de 1024. Si la tabla `documents` tiene otra dimensión, el código fallará en runtime. Pedir al usuario si debe recrear la tabla (acción destructiva) o ajustar el modelo Ollama.

### 1.6 (Opcional) Crear índice HNSW en `documents.embedding`

```sql
CREATE INDEX IF NOT EXISTS documents_embedding_hnsw_idx
ON documents USING hnsw (embedding vector_cosine_ops);
```

Este índice acelera la búsqueda semántica. No es bloqueante para la primera versión.

---

## FASE 2 — Provisioning de servicios externos (AUTONOMOUS)

### 2.1 Levantar Ollama localmente

```bash
# verificar si Ollama está instalado
which ollama || curl -fsSL https://ollama.com/install.sh | sh

# bajar el modelo
ollama pull qwen3-embedding:8b

# validar que el embedding funciona
curl -s http://localhost:11434/api/embeddings \
  -d '{"model":"qwen3-embedding:8b","prompt":"health check"}' \
  | python -c "import json,sys; d=json.load(sys.stdin); print('dim:', len(d['embedding']))"
```

STATUS esperado: `dim: 1024`. Si Ollama corre en otro host, ajustar `OLLAMA_HOST` en `.env`.

### 2.2 Verificar Gemini API

```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"responde con la palabra OK"}]}]}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['candidates'][0]['content']['parts'][0]['text'])"
```

STATUS esperado: `OK`. Si Gemini no responde, verificar la API key en `https://aistudio.google.com/apikey`.

### 2.3 Verificar acceso a Canvas

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_BASE_URL/users/self" \
  | python -m json.tool
```

STATUS esperado: JSON con `id`, `name`, `email`. Si 401, el token es inválido o expiró.

---

## FASE 3 — Configuración local del backend (AUTONOMOUS)

### 3.1 Cargar `.env` y validar `Settings`

```bash
cd paquetobot-rag
set -a; . ./.env; set +a
python -c "
from app.core.config import Settings, get_settings
get_settings.cache_clear()
s = get_settings()
print('OK', s.supabase_database_url[:25]+'...', s.canvas_api_base_url)
"
```

STATUS esperado: imprime `OK` y los valores masked. Si `ValidationError`, faltan variables críticas.

### 3.2 Probar conexión SQLAlchemy al backend

```python
python -c "
import os
from app.core.db import engine_for_url
from app.models import Base
from sqlalchemy import text
eng = engine_for_url(os.environ['SUPABASE_DATABASE_URL'])
with eng.connect() as conn:
    rows = conn.execute(text('SELECT count(*) FROM tenants')).scalar()
    print('tenants count:', rows)
"
```

STATUS esperado: `tenants count: 0`.

### 3.3 Probar redacción de logs

```bash
python -c "
import logging
from app.security.redaction import RedactionFilter
log = logging.getLogger('redaction.smoke')
log.setLevel(logging.INFO); log.propagate=False
h = logging.StreamHandler(); h.setFormatter(logging.Formatter('%(message)s')); log.addHandler(h); log.addFilter(RedactionFilter())
log.info('test Bearer gAAAAAabcdef postgresql://u:p@h/d safe')
"
```

STATUS esperado: el log impreso contiene `***REDACTED***` y NO contiene `gAAAAAabcdef` ni `postgresql://u:p`.

---

## FASE 4 — Smoke end-to-end (AUTONOMOUS)

### 4.1 Levantar el servidor

```bash
cd paquetobot-rag
uvicorn app.main:app --host 127.0.0.1 --port 8000 > /tmp/uvicorn.log 2>&1 &
SERVER_PID=$!
sleep 3
echo "server pid: $SERVER_PID"
```

### 4.2 `GET /healthz`

```bash
curl -s http://127.0.0.1:8000/healthz | python -m json.tool
```

STATUS esperado:

```json
{
  "status": "ok" | "degraded",
  "ollama": {"available": true, ...},
  "db": {"available": true, ...},
  "scheduler": {"running": false, "enabled": false},
  "rag_routes_disabled": false
}
```

Si `db.available = false`, revisar la URL de Supabase y la IP allowlist.
Si `ollama.available = false`, revisar `OLLAMA_HOST` y que `qwen3-embedding:8b` esté bajado.

### 4.3 Generar un JWT de prueba

```bash
python -c "
import jwt, time, os
secret = os.environ['BACKEND_SECRET']
token = jwt.encode({'sub': 'smoke-user', 'iat': int(time.time()), 'exp': int(time.time())+3600}, secret, algorithm='HS256')
print(token)
" > /tmp/jwt.txt
```

### 4.4 `POST /auth/canvas/connect`

```bash
JWT=$(cat /tmp/jwt.txt)
curl -s -X POST http://127.0.0.1:8000/auth/canvas/connect \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d "{\"canvas_token\":\"$CANVAS_API_TOKEN\"}"
```

STATUS esperado: `204 No Content`. Si 401, verificar el JWT o el token de Canvas. Si 500, revisar `/tmp/uvicorn.log`.

### 4.5 Confirmar cifrado del token

```bash
psql "$SUPABASE_DATABASE_URL" -c "SELECT tenant_id, length(ciphertext), key_version FROM canvas_credentials;"
```

STATUS esperado: una fila con `tenant_id` UUID, `length(ciphertext)` ≈ 200 (Fernet produce ~200 bytes), `key_version = 1`.

Verificar que el ciphertext NO contiene texto plano del token:

```bash
psql "$SUPABASE_DATABASE_URL" -tAc "SELECT encode(ciphertext, 'escape') FROM canvas_credentials;" | grep -c "$CANVAS_API_TOKEN" || echo "no plaintext token in DB"
```

STATUS esperado: `0` (no plaintext).

### 4.6 `POST /sync` (manual)

```bash
JWT=$(cat /tmp/jwt.txt)
curl -s -X POST http://127.0.0.1:8000/sync \
  -H "Authorization: Bearer $JWT"
```

STATUS esperado: `202 Accepted` con cuerpo `{status, last_successful_at, last_status, last_error_class}`.

Si `last_status = 'failed'`, revisar el log de uvicorn y la tabla `sync_state`.

### 4.7 Confirmar ingestión

```bash
psql "$SUPABASE_DATABASE_URL" -c "SELECT count(*) FROM courses;"
psql "$SUPABASE_DATABASE_URL" -c "SELECT count(*) FROM assignments;"
psql "$SUPABASE_DATABASE_URL" -c "SELECT count(*) FROM submissions;"
```

STATUS esperado: > 0 en las tres tablas (depende de cuántos cursos tenga el usuario en Canvas).

```bash
psql "$SUPABASE_DATABASE_URL" -c "SELECT last_status, last_successful_at, last_error_class FROM sync_state;"
```

STATUS esperado: `last_status='success'` o `last_status='failed'` con `last_error_class` específica.

### 4.8 `POST /query` (semántica)

```bash
JWT=$(cat /tmp/jwt.txt)
curl -s -X POST http://127.0.0.1:8000/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"question":"¿Cuándo es la próxima entrega de cualquier curso?","lang":"es"}' \
  | python -m json.tool
```

STATUS esperado: `{answer, lang, route, correlation_id}` con `route` ∈ `{relational, semantic, hybrid, unsupported}` y `answer` no vacío.

### 4.9 `POST /query` (SQL)

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"question":"¿Cuántas tareas tengo en total?","lang":"es"}' \
  | python -m json.tool
```

STATUS esperado: `route: "relational"` con conteo numérico en `answer`.

### 4.10 Verificar aislamiento entre tenants

```bash
# generar un JWT con un sub distinto
python -c "
import jwt, time, os
secret = os.environ['BACKEND_SECRET']
print(jwt.encode({'sub': 'attacker-user', 'iat': int(time.time()), 'exp': int(time.time())+3600}, secret, algorithm='HS256'))
" > /tmp/jwt2.txt
JWT2=$(cat /tmp/jwt2.txt)

# intentar conectar un token Canvas
curl -s -X POST http://127.0.0.1:8000/auth/canvas/connect \
  -H "Authorization: Bearer $JWT2" \
  -H "Content-Type: application/json" \
  -d "{\"canvas_token\":\"$CANVAS_API_TOKEN\"}"
```

STATUS esperado: `204` (segundo tenant creado). Confirmar aislamiento en DB:

```bash
psql "$SUPABASE_DATABASE_URL" -c "SELECT tenant_id FROM canvas_credentials;"
```

STATUS esperado: 2 filas con `tenant_id` distintos.

### 4.11 Verificar rechazo de `tenant_id` por body

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"question":"hola","tenant_id":"attacker-uuid"}' \
  -w "\nHTTP %{http_code}\n"
```

STATUS esperado: `422 Unprocessable Entity` (Pydantic `extra="forbid"` rechaza `tenant_id`).

### 4.12 Apagar el servidor

```bash
kill $SERVER_PID
sleep 2
ps -p $SERVER_PID || echo "server stopped"
```

---

## FASE 5 — Endurecimiento opcional (AUTONOMOUS)

### 5.1 Activar el scheduler

Si `SCHEDULER_ENABLED=true` está en `.env`, reiniciar el servidor y confirmar que `scheduler.running = true` en `/healthz`.

### 5.2 Crear índices HNSW en `documents`

(referenciar Fase 1.6)

### 5.3 Configurar CI en GitHub

Crear `.github/workflows/ci.yml` que corra `pytest`, `ruff check`, `pip check` en cada push a `main`.

### 5.4 Reverse proxy + HTTPS

Apuntar un dominio a la máquina del backend; configurar Nginx/Caddy con certificado de Let's Encrypt.

### 5.5 Backups automáticos

Configurar `pg_dump` programado de la DB de Supabase.

### 5.6 Rotación de `TENANT_TOKEN_KEY`

Documentar procedimiento: generar `TENANT_TOKEN_KEY_NEXT`, re-encriptar ciphertexts existentes, promover a `key_version = 2`.

---

## FASE 6 — Reporte final (AUTONOMOUS)

Generar `deploy-report.md` con:

- Resumen ejecutivo.
- Resultado de cada fase con timestamps.
- Confirmaciones de aislamiento multi-tenant.
- Conteos finales en DB (cursos, tareas, entregas, vector embeddings).
- Errores encontrados y cómo se resolvieron.
- Lista de riesgos residuales.
- Recomendaciones para la siguiente iteración (CI, monitoring, OAuth Canvas, etc.).

---

## Apéndice A — Comandos de rollback

```bash
# detener servidor
pkill -f 'uvicorn app.main:app'

# rollback de migración (NO recomendado si ya hay datos)
cd paquetobot-rag
alembic downgrade -1

# revocar el rol read-only
psql "$SUPABASE_DATABASE_URL" -c "
REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM pg_role_canvas_readonly;
DROP ROLE IF EXISTS pg_role_canvas_readonly;
"
```

## Apéndice B — Endpoints expuestos

| Método | Path | Auth | Propósito |
|---|---|---|---|
| GET | `/healthz` | no | Health check con estado de dependencias |
| POST | `/auth/canvas/connect` | JWT | Cifra y guarda token Canvas del usuario |
| POST | `/sync` | JWT + tenant + token | Sincroniza cursos/tareas del usuario desde Canvas |
| POST | `/query` | JWT + tenant + token | Pregunta NL → RAG (SQL o vectorial o híbrido) |

## Apéndice C — Variables de entorno requeridas

```
SUPABASE_DATABASE_URL   # postgresql+psycopg://...
TENANT_TOKEN_KEY        # Fernet key
BACKEND_SECRET          # JWT HMAC secret
GEMINI_API_KEY          # Google AI Studio
OLLAMA_HOST             # default http://localhost:11434
CANVAS_API_BASE_URL     # https://tecsup.instructure.com/api/v1
```

## Apéndice D — Criterios de éxito (Definition of Done)

- [ ] Las 7 tablas existen en Supabase.
- [ ] El rol `pg_role_canvas_readonly` tiene SELECT sobre las 7 tablas.
- [ ] `pip check` limpio.
- [ ] 213 tests passing.
- [ ] `GET /healthz` reporta `status: ok` con Ollama y DB disponibles.
- [ ] `POST /auth/canvas/connect` cifra el token (no aparece en plaintext en DB).
- [ ] `POST /sync` ejecuta e ingiere al menos 1 curso y 1 tarea.
- [ ] `POST /query` responde en el idioma de la pregunta.
- [ ] Cross-tenant isolation: JWT distinto crea tenant distinto.
- [ ] `POST /query` con `tenant_id` en body devuelve 422.
- [ ] Los logs no contienen credenciales en plaintext.

## Apéndice E — Riesgos conocidos

1. **`TENANT_TOKEN_KEY` rotativo no implementado**: si la key se filtra, hay que re-encriptar manualmente los ciphertexts.
2. **Sin CI**: cualquier cambio se valida solo en local.
3. **PGVector `documents` puede tener dimensión distinta**: si Supabase la creó con otra dim, los embeddings nuevos fallarán.
4. **Token Canvas en `.env`**: el agente debe asegurarse de NO commitearlo.
5. **`pg_role_canvas_readonly` no lo crea Alembic**: provisionado manualmente.
6. **Sin rate-limit por IP**: solo hay rate-limit por tenant en `POST /sync`.
7. **Sin OAuth de Canvas**: cada usuario entrega su token manualmente.
