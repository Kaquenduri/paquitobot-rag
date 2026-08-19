# Plan de Continuación — Backend RAG PaquitoBot (para agente Claude)

> Este documento es el punto de reanudación del desarrollo. Un agente IA (Claude u otro) puede leerlo y continuar el trabajo sin perder el estado previo.
>
> **Proyecto**: `C:\Users\Administrador\Desktop\PROYECTOS\Primer RAG`
> **Rama**: `main` (commit local `3d9b0b7`, NO pusheado)
> **Remoto**: `https://github.com/Kaquenduri/paquitobot-rag.git`
> **Entorno Python**: `C:\Users\Administrador\langchain\Scripts\python.exe` (Windows-native)

---

## 1. Estado actual (dónde estamos)

### Qué está implementado y verificado
- Backend FastAPI monolítico con arquitectura MVC (`app/`).
- Sync Canvas → Supabase (users, courses, enrollments, assignments, submissions).
- Auth JWT + tokens Canvas cifrados (Fernet).
- RAGService con rutas relacional / semántico / híbrido (orquestador).
- Logs JSON con `correlation_id` + redacción de secretos.

### Últimos cambios (en el working tree, algunos sin commit)
| Archivo | Cambio |
|---|---|
| `app/canvas/dto.py` | Fix `AssignmentDTO.important_dates: Any \| None` (Canvas devuelve `False` bool). **Comiteado en `3d9b0b7`.** |
| `app/sync/pipeline.py` | Logging `sync_schema_drift_detail` con el campo exacto. **Comiteado.** |
| `app/rag/vector_store.py` | Normaliza tuplas `(Document, score)` → `Document`. |
| `app/text_to_sql/executor.py` | Guard `SET LOCAL` sólo para Postgres. |
| `app/services/rag_service.py` | Rewrite `answer()`: nunca devuelve string vacío, `provider_health()` antes de rutear, `_default_llm_summarizer`, filter con `tenant_id`. |
| `app/services/rag_factory.py` | **NUEVO** — `_LazyInit` + `build_rag_service()` para no colgar el boot. |
| `app/main.py` | Lifespan instancia `rag_service` en `app.state`. |
| `app/controllers/query.py` | `get_rag_service(request, ...)` lee de `app.state.rag_service`. |

### Tests
- `pytest tests/unit`: **177 passed** ✅
- `pytest tests/smoke`: **36 passed** ✅
- **Pendiente**: `ruff` reporta 4 errores (fixables) en el cableado nuevo.

---

## 2. Objetivo inmediato

> Que `POST /query` responda con `answer` no vacío usando datos reales de Supabase + MiniMax (Anthropic-compatible) + Ollama/PGVector.

Actualmente `/query` devuelve `200` pero `answer` vacío porque el orquestador no tenía dependencias inyectadas. El cableado ya está hecho (factory + lifespan + controller), falta pulir y validar.

---

## 3. Pasos para continuar (en orden)

### Paso 1 — Limpiar ruff (5 min)
```bash
cd "/mnt/c/Users/Administrador/Desktop/PROYECTOS/Primer RAG"
/mnt/c/Users/Administrador/langchain/Scripts/python.exe -m ruff check --fix \
  app/main.py app/services/rag_factory.py app/controllers/query.py \
  app/rag/vector_store.py app/text_to_sql/executor.py app/services/rag_service.py
/mnt/c/Users/Administrador/langchain/Scripts/python.exe -m ruff check \
  app/main.py app/services/rag_factory.py app/controllers/query.py \
  app/rag/vector_store.py app/text_to_sql/executor.py app/services/rag_service.py
```
Devolver `All checks passed!`.

### Paso 2 — Re-correr tests (10 min)
```bash
/mnt/c/Users/Administrador/langchain/Scripts/python.exe -m pytest -q --no-cov tests/unit
/mnt/c/Users/Administrador/langchain/Scripts/python.exe -m pytest -q --no-cov tests/smoke
```
Ambos verdes. Los selftests también:
```bash
PYTHONPATH=. /mnt/c/Users/Administrador/langchain/Scripts/python.exe -X utf8 -m app.services.rag_factory
PYTHONPATH=. /mnt/c/Users/Administrador/langchain/Scripts/python.exe -X utf8 -m app.services.rag_service
PYTHONPATH=. /mnt/c/Users/Administrador/langchain/Scripts/python.exe -X utf8 -m app.rag.vector_store
```

### Paso 3 — Validar que el factory no cuelga (10 min)
Probar con `timeout` para asegurar que el lazy init no bloquea:
```bash
timeout 15 /mnt/c/Users/Administrador/langchain/Scripts/python.exe -X utf8 -c "
import os
os.environ.update({
  'SUPABASE_DATABASE_URL': 'postgresql+psycopg://127.0.0.1:1/selftest',
  'TENANT_TOKEN_KEY': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
  'BACKEND_SECRET': 'selftest-backend-secret-with-sufficient-length',
  'MINIMAX_API_KEY': 'selftest',
  'OLLAMA_HOST': 'http://127.0.0.1:1',
  'CANVAS_API_BASE_URL': 'https://canvas.invalid/api/v1',
})
from app.core.config import get_settings
from app.core.db import get_db_session
from app.services.rag_factory import build_rag_service
svc = build_rag_service(get_settings(), get_db_session)
print('OK service built:', svc is not None)
"
```
Debe terminar en < 15s sin colgarse.

### Paso 4 — Validar /query end-to-end (con entorno real o simulado)
**Opción A (recomendada, sin servicios externos)**: montar un `RAGService` con stubs y verificar que `/query` responde:
```bash
PYTHONPATH=. /mnt/c/Users/Administrador/langchain/Scripts/python.exe -X utf8 -m app.controllers.query
```
(El `_selftest()` del controller ya valida el flujo con stubs.)

**Opción B (real)**: con Supabase + Ollama + MiniMax configurados en `.env`:
```bash
set LEGACY_MODE=0
/mnt/c/Users/Administrador/langchain/Scripts/python.exe main.py
# en otra terminal:
curl -X POST http://127.0.0.1:8000/query \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"question":"cuales son mis cursos"}'
```
Se espera `answer` no vacío (ej: lista de cursos del tenant).

### Paso 5 — Commit (sin push)
```bash
git add app/ services/rag_factory.py main.py 2>/dev/null || git add -A
git commit -m "feat(rag): wire RAG end-to-end (PGVector + MiniMax + SQL executor)"
```
**NO incluir**:
- `.env`
- `users_self.json`, `users_courses.json`, `users_enrollments.json`, `users_assignemnts.json` (PII)

### Paso 6 — Push (requiere PAT del usuario)
Solicitar al usuario un Personal Access Token de GitHub. Usar una sola vez:
```bash
git push -u origin main
```

---

## 4. Trampas y gotchas críticos (leé esto antes de tocar código)

1. **Windows + stdout Unicode**: siempre usar `-X utf8` al correr scripts con caracteres españoles.
2. **`OllamaEmbeddings`**: NO usar `langchain_community.embeddings.OllamaEmbeddings` (deprecated, sin `base_url`). Usar:
   ```python
   from langchain_ollama import OllamaEmbeddings
   OllamaEmbeddings(model="qwen3-embedding:8b", base_url=settings.ollama_host, validate_model_on_init=False, client_kwargs={"timeout": 5.0})
   ```
3. **El lifespan NO debe colgarse**: si Ollama/Postgres no responden, la app debe arrancar igual (por eso `_LazyInit`). No revertir a init eager en el lifespan.
4. **`important_dates`**: Canvas envía `False` (bool) cuando no hay fechas. El DTO ya acepta `Any | None`. Si aparece otro campo con tipo raro, el logging `sync_schema_drift_detail` ahora da el campo exacto.
5. **No crear tests en `tests/`**: usar `_selftest()` al final de cada módulo.
6. **`extra="ignore"` en DTOs**: los campos nuevos de Canvas se ignoran silenciosamente. Si el sync falla con `schema_drift`, el log dirá el campo exacto.
7. **Sesiones SQL**: `get_db_session` es un generator de FastAPI; en el factory se usa `db_session_factory()` (sessionmaker) para el executor.

---

## 5. Arquitectura objetivo (post-fix)

```
POST /query
   │
   ▼
verify_backend_jwt → require_tenant → require_tenant_token
   │
   ▼
get_rag_service(request)  →  app.state.rag_service
   │
   ▼
RAGService.answer(question, tenant_id, language)
   │
   ├── provider_health() refresca router.embedding_available
   │
   ├── RAGRouter.route(question)
   │     ├── relational  → sql_executor (allow-list) → _summarize(MiniMax)
   │     ├── semantic    → vector_store (PGVector + Ollama) → _summarize(MiniMax)
   │     └── hybrid      → vector + sql → _summarize(MiniMax)
   │
   ▼
{answer, lang, route, correlation_id}
```

---

## 6. Contexto de producto (decisiones ya tomadas)

- **Multiusuario**: cada estudiante entrega su token Canvas personal (cifrado Fernet).
- **Canvas read-only**: nunca escribir en Canvas.
- **Sólo datos propios**: no exponer datos de compañeros.
- **Sync cada 6 horas** + manual (rate-limited).
- **Respuestas en el idioma de la pregunta**.
- **MiniMax-M3 (Anthropic-compatible endpoint)** para chat; **Ollama qwen3-embedding:8b** para embeddings.
- **PostgreSQL/Supabase** como base relacional + PGVector para vectores.
- **Tenant_id** como clave de aislamiento (UUID interno, derivado del JWT `sub`).

---

## 7. Riesgos conocidos

| Riesgo | Mitigación |
|---|---|
| `PGVector` cuelga al boot | `_LazyInit` en el factory |
| Ollama caído | `provider_health()` degrada semántico→`unsupported`, híbrido→relational |
| MiniMax falla | `_default_llm_summarizer` devuelve `bounded_refusal` no vacío |
| Cambios de schema de Canvas | Logging `sync_schema_drift_detail` con campo exacto |
| Push requiere PAT | Pedir al usuario un token antes del push |

---

## 8. Criterios de aceptación (Definition of Done)

- [ ] `ruff check` limpio en el cableado.
- [ ] `pytest tests/unit` (177) y `tests/smoke` (36) verdes.
- [ ] `python -m app.services.rag_factory` con timeout no se cuelga.
- [ ] `python -m app.controllers.query` (selftest) pasa.
- [ ] `POST /query` con `"cuales son mis cursos"` devuelve `answer` no vacío (con stubs o con entorno real).
- [ ] Commit del cableado RAG (sin push hasta tener PAT).
