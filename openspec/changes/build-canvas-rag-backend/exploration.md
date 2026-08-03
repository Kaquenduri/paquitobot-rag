# Exploration: `build-canvas-rag-backend`

> Change target: monolithic Python backend server with modular MVC, hydrating a
> question-answering layer over Canvas LMS data. Persistence in Supabase
> PostgreSQL (relational for Text-to-SQL) + PGVector (semantic search); routing
> orchestrated by LangChain.
>
> Artifact store: `openspec`. Delivery strategy: `ask-on-risk`. Review budget:
> 800 lines. Exploration only — no proposal, specs, design, tasks, or code.

---

## 1. Context Snapshot

### 1.1 Current State (what exists today)

- **Repository**: `/mnt/c/Users/Administrador/Desktop/PROYECTOS/Primer RAG`
- **Runtime**: `C:\Users\Administrador\langchain\Scripts\python.exe` (Python 3.14.2).
  Windows-native binary; paths must be Windows-style when invoked from this
  interpreter (the `/mnt/c/...` form is WSL-only and resolves to nothing).
- **Architecture today** (`openspec/config.yaml` + `main.py`): single-process
  RAG script. `main.py` orchestrates ingestion → chunking → embedding
  (Ollama `qwen3-embedding:8b`, 1024 dims) → persistence (`save_to_pgvector_db`
  via `langchain_postgres.PGVectorStore`) → similarity search → Gemini 2.5
  Flash answer. No HTTP layer, no auth, no router, no sync.
- **Source modules**:
  - `src/extract_canvas_data.py` — paginated Canvas extraction (`requests`,
    `per_page=100`, `Link` header). Writes JSON to `src/data/course_*.json`.
  - `src/text_processor.py` — `DirectoryLoader` for PDFs/MD/TXT/JSON plus
    `RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=500)`.
  - `src/supabase.py` — PGVectorStore wrapper around `langchain_postgres.PGEngine`.
  - `src/chroma_db.py` — destructive `shutil.rmtree(CHROMA_PATH)` then
    `Chroma.from_documents`.
  - `main.py` — top-level orchestrator (script-mode).
- **Local data already cached** (`src/data/course_*.json`):
  12 course files, 318 assignments total, 1 empty course, distribution
  `{0,1,2,14,17,22,23,32,35,48,53,71}`. Each assignment carries the 9 fields
  produced by the existing extractor (`assignment_id, name, description,
  due_at, points_possible, html_url, submission_status, score, submitted_at`).
  Local JSONs were **not** re-emitted by this exploration; only structural
  counts were derived.
- **Vector store state**: ChromaDB persistent at `./chroma` (3.2 MB sqlite +
  one HNSW dir). Supabase PGVector table `documents` (1024-dim) exists.
- **Env keys present in `.env`** (presence only — values never read):
  `CANVAS_API_URL`, `CANVAS_API_TOKEN`, `SUPABASE_DATABASE_URL`. Absent:
  `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
  `GEMINI_API_KEY`, `OLLAMA_HOST`. Note: `main.py` reads `GEMINI_API_KEY` via
  `os.getenv`, so Gemini auth is sourced from a non-`.env` location (shell /
  runtime env).
- **Installed vs. listed libs** (verified at runtime):
  - **Installed**: `uvicorn 0.51.0`, `pydantic 2.13.4`, `pydantic_settings 2.14.2`,
    `sqlalchemy 2.0.51`, `psycopg2 2.9.12`, `requests 2.34.2`, `httpx 0.28.1`,
    `aiohttp 3.14.2`, `langchain 1.3.14`, `langchain_core 1.5.0`,
    `langchain_community 0.4.2`, `langchain_postgres 0.0.17`,
    `langchain_ollama 1.1.0`, `langchain_google_genai 4.3.1`,
    `chromadb 1.5.9`.
  - **Listed but NOT installed**: `fastapi==0.115.5` (in `requirements.txt`).
  - **Missing**: `starlette`, `flask`, `sanic`, `tornado`, `openai`, `pytest`,
    `ruff`, `mypy`, `black`, `flake8`, `isort`, `tiktoken`.

### 1.2 Why a server now (driver)

The script today answers one ad-hoc query and dies. The change target is a
**running HTTP service** that:

1. Syncs Canvas → Supabase on demand and on a schedule.
2. Exposes endpoints to ask natural-language questions about a student's
   courses/assignments/grades.
3. Routes each question to either relational (Text-to-SQL) or semantic
   (PGVector) retrieval, or a hybrid mix, using LangChain.
4. Persists `users`, `courses`, `enrollments`, `assignments` minimum, plus
   only the additional entities that product questions actually need.

---

## 2. Canvas API Evidence (live, redacted)

Probe summary: ephemeral Python script (`/tmp/opencode/canvas-probe/probe.py`,
outside the repo) loaded `.env` via `python-dotenv`, called Canvas with
`requests.Session` + `Authorization: Bearer <token>` only in headers, no
`verbose`, and emitted sanitized schema JSON to
`/tmp/opencode/canvas-probe/result.json`. No raw responses persisted inside
the repo, no curl commands run. Bounded: 5–9 items per endpoint, 8 s timeout,
`per_page≤100`. Counts/presence/types only — IDs redacted, names masked,
values never copied verbatim.

### 2.1 `/users/self` (n=1)

| Key | Type | Nullable | Example (redacted) |
|---|---|---|---|
| `id` | int | no | int |
| `name` | str | no | `<str len=26>` |
| `sortable_name` | str | no | `<str len=26>` |
| `short_name` | str | no | `<str len=26>` |
| `first_name` | str | no | `<str len=11>` |
| `last_name` | str | no | `<str len=13>` |
| `avatar_url` | str | no | `<str len=98>` |
| `created_at` | str (ISO-8601) | no | `<str len=20>` |
| `locale` | str | yes | `<redacted>` |
| `effective_locale` | str | no | `<s>` |
| `permissions` | dict | no | `{can_update_name: bool, can_update_avatar: bool, limit_parent_app_web_access: bool}` |

### 2.2 `/users/self/favorites/courses` (n=9)

Total keys observed: 33. Notable fields and shapes (presence = 9/9 unless noted):

| Key | Type | Nullable | Notes |
|---|---|---|---|
| `id` | int | no | primary key |
| `name` | str | no | display name |
| `course_code` | str | no | short code |
| `account_id`, `root_account_id`, `enrollment_term_id` | int | no | scope FKs |
| `uuid` | str | no | `<str len=40>` |
| `workflow_state` | str | no | e.g. `<str len=9>` ("available" / "completed" / …) |
| `start_at`, `end_at` | str (ISO) | yes | nullable |
| `default_view` | str | no | e.g. `<str len=4>` |
| `is_public` | bool | yes | 6 bool / 3 null |
| `license` | str | yes | 7 str / 2 null |
| `enrollments` | list[dict] | no | per-user role objects (see 2.3) |
| `calendar` | dict | no | `{ics: <str len=98>}` |
| `time_zone` | str | no | IANA name |
| `storage_quota_mb`, `enrollments_count` … | varies | varies | coarse fields |

`enrollments` is **already embedded** in the favorites payload. No need for a
second hop to answer "what courses am I in?".

### 2.3 Nested `enrollments` per course (sampled, n=9)

| Key | Type | Nullable |
|---|---|---|
| `type` | str | no |
| `role` | str | no |
| `role_id` | int | no |
| `user_id` | int | no |
| `enrollment_state` | str | no |
| `limit_privileges_to_course_section` | bool | no |

### 2.4 `/courses/{course_id}/assignments?include[]=submission&include[]=score_statistics` (n=5)

Top-level shape: 75 keys. The minimum question-oriented subset is below (full
key list available in the probe JSON if needed; recorded here only the fields
that map to user-asked questions).

| Key | Type | Nullable | Role |
|---|---|---|---|
| `id` | int | no | PK |
| `course_id` | int | no | FK to course |
| `name` | str | no | display |
| `description` | str | no | often HTML, **MUST be sanitized** |
| `points_possible` | float | no | grading context |
| `due_at` | str (ISO) | no | "when is this due?" |
| `lock_at`, `unlock_at` | str (ISO) | no | window |
| `workflow_state` | str | no | published/unpublished |
| `grading_type` | str | no | "points" / "pass_fail" / etc. |
| `submission_types` | list[str] | no | "online_text_entry", "online_upload", "external_tool" … |
| `html_url` | str | no | link |
| `important_dates`, `muted` | bool | no | product flags |
| `availability_status` | dict | no | `{status: str, date: iso|null}` |
| `lock_info` | dict | no | `{lock_at, can_view, asset_string}` |
| `integration_data` | dict | no | can be empty `{}` |
| `submission` | dict | no | nested — see 2.5 |
| `score_statistics` | dict | no | nested — see 2.6 |

### 2.5 Nested `submission` (n=5)

31 keys. High-value subset for "what's my grade / did I submit":

| Key | Type | Nullable |
|---|---|---|
| `id`, `assignment_id`, `user_id`, `grader_id` | int | no |
| `workflow_state` | str | no | "submitted" / "graded" / "unsubmitted" … |
| `submission_type` | str | yes | 4 str / 1 null in sample |
| `submitted_at` | str (ISO) | yes | 4 str / 1 null |
| `score`, `entered_score` | float | no | numeric grade |
| `grade`, `entered_grade` | str | no | letter or numeric |
| `grade_matches_current_submission` | bool | no | |
| `graded_at`, `posted_at`, `cached_due_date` | str | no | |
| `late`, `missing`, `excused`, `redo_request` | bool | no | |
| `attempt` | int | yes | 4 int / 1 null |
| `seconds_late`, `late_policy_status` | int / str | both nullable variants | |
| `body`, `url`, `preview_url`, `attachments` | str / list | mixed | |
| `points_deducted`, `extra_attempts`, `sticker`, `custom_grade_status_id`, `grading_period_id` | various | yes | |

### 2.6 Nested `score_statistics` (n=5)

6 float fields, all non-null:

| Key | Meaning (per Canvas doc convention) |
|---|---|
| `min`, `max`, `mean`, `median` | descriptive stats |
| `lower_q`, `upper_q` | quartile bounds (lower quartile = 25th percentile; upper = 75th) |

> Empirical note: in the 5-item sample, `score_statistics` is **always present**
> when the assignment is published and has submissions. For empty/unpublished
> assignments the field is typically `null` at the parent level — code MUST
> tolerate that.

### 2.7 `/courses/{course_id}/enrollments` (extra hop, sampled, n=5)

Confirmed accessible with the same token. 24 keys per record. Notable extras
beyond the favorites-embedded `enrollments`: `grades` (dict), `user` (nested
full user dict), `sis_*` ids, `html_url`, `course_section_id`. **Only worth
calling** if the user query needs another student's grade in the same course
("how is X doing in this class?").

### 2.8 Pagination and rate-limit evidence

- Canvas returns `Link: <url>; rel="next"` on paginated GETs. `per_page=100`
  is accepted; we observed ≤9 favorites and ≤5 assignments in the bounded
  probe.
- `Content-Type: application/json` for all probed endpoints.
- No 429 observed in bounded retries; no need to sleep for this volume.

---

## 3. Affected Areas (proposed change footprint)

| Path | Why it changes |
|---|---|
| `main.py` | Becomes a thin entry point that boots the FastAPI/ASGI app and the sync scheduler, not a script. |
| `src/extract_canvas_data.py` | Refactor into a typed `CanvasClient` (sync+async wrappers, retry/backoff, cursor pagination). |
| `src/supabase.py` | Add a relational persistence layer (SQLAlchemy `Engine` over `psycopg2`) alongside the existing PGVector wrapper. |
| `src/chroma_db.py` | Drop destructive `rmtree`; gate behind an explicit flag. |
| `src/text_processor.py` | Augment with HTML sanitization for `assignment.description` and `submission.body`. |
| `src/text_to_sql/` *(new)* | Question→SQL router, schema-aware SQL generation, read-only DB session, query allow-list. |
| `src/rag/` *(new)* | LangChain `RouterChain`/`MultiRetrievalQACreator` with vector + relational branches. |
| `src/api/` *(new)* | HTTP routes (`POST /sync`, `POST /query`, `GET /healthz`). Pydantic request/response models. |
| `src/sync/` *(new)* | Idempotent upsert pipeline keyed on Canvas IDs; drift detection. |
| `requirements.txt` | Add the chosen HTTP framework (likely `fastapi`), `sqlalchemy[asyncio]`, `alembic` (migrations), `bleach` (HTML sanitize), `tenacity` (retry). |
| `.env.example` *(new)* | Document required keys; never commit `.env`. |
| `openspec/specs/ingestion`, `…/embedding`, `…/vector-store`, `…/retrieval` *(new)* | Main-spec domains that will be introduced after this change. |

---

## 4. Comparison of Concrete Options

### 4.1 HTTP framework

| Option | Pros | Cons | Effort |
|---|---|---|---|
| **A. Install `fastapi` + `uvicorn`** (already have `uvicorn 0.51.0`, `pydantic 2.13.4`, `pydantic_settings 2.14.2`) | FastAPI is already in `requirements.txt` (signals intent); mature ecosystem; OpenAPI for free; pydantic v2 already compatible. | One `pip install fastapi` required (currently missing). Adds 2 deps (fastapi + any sub-deps pulled by pip). | Low |
| **B. Hand-rolled ASGI 3 app on `uvicorn`** | Zero new deps; full control; small surface. | Re-implements routing, validation, OpenAPI; high maintenance cost. | Medium |
| **C. Switch to `aiohttp` server (`aiohttp.web`)** | `aiohttp 3.14.2` is installed; async native. | Different ecosystem, no OpenAPI, custom validation; rewriting `main.py` style. | Medium |

**Recommendation**: **A**. The repo signals intent (`fastapi==0.115.5` is pinned
in `requirements.txt`), the supporting stack is already present
(`uvicorn`, `pydantic`, `pydantic_settings`), and FastAPI's Pydantic-based
validation matches the schemas we'll define anyway. The install is one command
and falls inside the 800-line review budget.

### 4.2 Persistence: official Supabase Python lib vs. direct Postgres

| Option | Pros | Cons | Effort |
|---|---|---|---|
| **A. Official `supabase-py` (REST/PostgREST)** | Auto-generated types; built-in auth helpers; row-level-security aware. | Requires `SUPABASE_URL` + `SUPABASE_ANON_KEY`/`SUPABASE_SERVICE_ROLE_KEY`, **none of which are in `.env` today**; bypasses the rich SQL we need for Text-to-SQL. | Medium |
| **B. Direct PostgreSQL via `psycopg2` / SQLAlchemy** | Already installed (`psycopg2 2.9.12`, `sqlalchemy 2.0.51`); `SUPABASE_DATABASE_URL` is the only key needed; one connection pool for vector + relational; mature migrations via `alembic`. | Lose auto-generated TS-style types; need explicit schema migrations; must enforce RLS in SQL if desired. | Low |
| **C. Hybrid — `supabase-py` for storage, `psycopg2` for SQL/vector** | Best of both. | Two auth surfaces, two SDKs, double config. | High |

**Recommendation**: **B**. `SUPABASE_DATABASE_URL` is the only Supabase key
present; the relational and vector workloads share one pool; Text-to-SQL
needs raw SQL anyway. Add `alembic` for migrations. If the team later wants
REST auth, layer `supabase-py` on top without ripping out the pool.

### 4.3 Minimum question-oriented schema

The user requested persist `users, courses, enrollments, assignments` "if
necessary" and "only justified entities". Evidence from §2 maps each entity
to a concrete question:

| Entity | Justified by question | Cardinality (from local cache) |
|---|---|---|
| `users` | "Who am I?", "show my profile" | 1 (self) |
| `courses` | "what courses am I in?", "course X details" | ≤9 favorites |
| `enrollments` | "what's my role in course X?", embedded in favorites so no extra hop | 1 per (user, course) |
| `assignments` | "what's due this week?", "show assignment Y", "what's my grade on Z?" | 0–71 per course (318 total) |
| `submissions` *(per assignment)* | "did I submit?", "what's my score on X?", "is my grade posted?" | 0–1 per (assignment, user) |
| `score_statistics` *(per assignment)* | "what's the class average on X?", "max score?" | 0–1 per assignment |
| `enrollments` full list | "what's another student's grade?" — needs `/courses/{id}/enrollments` | optional |

Entities **not** added (no question requires them yet):
discussion topics, modules, pages, announcements, rubrics, calendar events,
groups, outcomes. Add only when a product question forces it.

### 4.4 Idempotent sync / upsert strategy

| Strategy | Pros | Cons | Effort |
|---|---|---|---|
| **A. Full-refresh truncate-and-reload** | Simple, matches current Chroma behavior. | Destroys auditability, cannot run concurrently, painful if a single record fails. | Low |
| **B. `INSERT … ON CONFLICT (canvas_id) DO UPDATE` (Postgres upsert)** | Idempotent, survives concurrent runs, preserves history of soft-deleted rows via `deleted_at`. | Needs explicit unique constraints per table; `updated_after` watermark preferred. | Low |
| **C. Event log + projector** | Auditable, time-travel. | Overkill for this scale. | High |

**Recommendation**: **B** with a per-table `updated_at` watermark persisted
in a small `sync_state` table:

- Each table has `canvas_id BIGINT UNIQUE NOT NULL` as the natural key.
- Each sync run fetches `?updated_after=<last_watermark>` (Canvas supports it).
- For favorites/assignments, also store `enrollment_state` and `workflow_state`
  to soft-delete rows that disappear or get unpublished.
- Wrap each row upsert in a single transaction per batch; `tenacity` retry on
  transient errors.
- Sync endpoints: `POST /sync/courses`, `POST /sync/assignments?course_id=…`,
  and a scheduled job driver (out of scope of this exploration — likely
  APScheduler in-process, or external cron hitting the endpoint).

### 4.5 Text-to-SQL security

| Option | Pros | Cons | Effort |
|---|---|---|---|
| **A. Raw NL→SQL on a read-only role** | Minimal effort. | SQL injection surface even with NL guardrails; students could enumerate other students. | Low (but unsafe) |
| **B. Allow-list of SQL templates + parameterised slot filling** | Injection-safe by construction; reproducible; auditable. | Requires writing templates for every supported question shape. | Medium |
| **C. Read-only Postgres role + `SET LOCAL statement_timeout` + row-count cap + statement-level read-only transaction** | Defense in depth; cheap. | Doesn't replace template allow-list if questions are open-ended. | Low |
| **D. B + C combined** | Belt + suspenders. | Slightly more code; recommended. | Medium |

**Recommendation**: **D**. The text-to-SQL generator MUST emit only SQL
matched against an allow-list of templates (e.g. "list assignments due
between X and Y", "average score for assignment A", "my latest submission
status for course C"). At the DB layer, run on a **read-only role**
(`pg_role_canvas_readonly`) with `default_transaction_read_only=on`,
`statement_timeout=2s`, `idle_in_transaction_session_timeout=10s`, and a
cursor limit (`LIMIT 200`). The LLM never sees raw rows it doesn't need:
filter by `user_id = current_user_id()` enforced in every template.

### 4.6 Routing: SQL vs. vector vs. hybrid

| Option | Pros | Cons | Effort |
|---|---|---|---|
| **A. Always vector** | Trivial. Current behavior. | Misses exact aggregations ("how many assignments due Friday?"). | Low |
| **B. Always relational** | Exact, fast, cheap. | Fails on fuzzy phrasing ("what's that reading about photosynthesis?"). | Low |
| **C. LLM-as-router choosing branch per question** (LangChain `RouterChain` / `MultiRouteChain`) | Matches question shape to right tool; explainable. | Adds one LLM call per question; risk of bad routing. | Medium |
| **D. Vector recall → SQL precision** (always retrieve top-k, then run a constrained SQL aggregation constrained by the retrieved IDs) | Best recall + exact stats. | More moving parts; need careful ID handling. | Medium-High |

**Recommendation**: **C with D as fallback**. Concretely:

- **Vector branch**: PGVector on a `documents` table fed from
  `assignments.name + description (sanitized) + submission.body (sanitized) +
  course.name`. Same embedding pipeline as today.
- **Relational branch**: SQLDatabaseChain from LangChain, restricted to the
  allow-list (4.5-B) on a read-only connection.
- **Router**: lightweight classifier (rule-based first, LLM only on
  ambiguity) — questions with explicit aggregations, dates, IDs, or
  numbers go relational; open-ended "explain / summarize" go vector.
- **Hybrid path**: when the vector branch returns candidates but the
  question demands a numeric answer, run a constrained SQL query restricted
  to those `canvas_id`s.

---

## 5. Recommendation (single-paragraph)

Ship a **FastAPI** server (one `pip install` to honor the already-pinned
`fastapi==0.115.5`) using **direct PostgreSQL via SQLAlchemy/`psycopg2`** (only
`SUPABASE_DATABASE_URL` is configured, and Text-to-SQL needs raw SQL).
Persist the **minimum** five tables — `users`, `courses`, `enrollments`,
`assignments`, `submissions` — plus `score_statistics` materialized as a
column on `assignments` (no separate table). Sync is **idempotent upsert**
keyed on Canvas `canvas_id` with a per-table `updated_at` watermark.
Text-to-SQL runs on a **read-only Postgres role** with an
**allow-listed template** layer. Routing is a **classifier first, LLM-router
on ambiguity**, with vector recall + SQL precision as the hybrid fallback.
Embeddings keep using Ollama `qwen3-embedding:8b` (1024-dim) into the
existing PGVector `documents` table; HTML from `description`/`submission.body`
is sanitized before chunking.

---

## 6. Open Product Questions (must be resolved before `sdd-propose`)

1. **Single-user vs. multi-user** — is the service personal to one student
   (the one whose token is in `.env`), or must it fan out to many Canvas
   accounts? Affects auth model and per-user data partitioning.
2. **Sync frequency** — on-demand only, hourly, daily, or "after every
   Canvas-side change"? Affects whether we need a scheduler in-process.
3. **Read-only vs. read+write to Canvas** — current scope is read-only.
   Confirm we will not post grades or submit assignments.
4. **PII handling** — student names, emails, grades. The user has explicitly
   forbidden persisting or printing PII in this exploration. The
   implementation must apply the same rule: store the minimum needed,
   never log row contents, never echo IDs/names in `/query` responses
   unless explicitly requested, and never write to `.env`.
5. **Language of answers** — `main.py`'s prompt is Spanish; queries in
   English vs. Spanish both possible. Confirm the desired answer language
   and whether to embed Spanish-aware tokens.
6. **"Other students" questions** — does the system answer questions about
   classmates' grades (requires `/courses/{id}/enrollments` + RLS scoping)
   or only the authenticated user's own data? Strongly affects schema and
   security design.
7. **Embeddings refresh policy** — re-embed on every sync, or only when
   `assignment.description` hash changes?
8. **Quota / rate limits** — Canvas rate-limit per token; how many courses
   and assignments must a single sync pull? Affects pagination strategy.
9. **LLM choice for routing + answer generation** — Gemini 2.5 Flash today
   (but `GEMINI_API_KEY` is NOT in `.env`). Confirm model and billing.

---

## 7. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **`fastapi` not installed** despite being in `requirements.txt` — env drift between declared and installed deps. | Medium | Verify in `sdd-apply`; add `pip install fastapi` (or upgrade requirements) as the first task. Pin via `pip freeze` afterwards. |
| R2 | **`GEMINI_API_KEY` not in `.env`** — `main.py` reads it at runtime; the new server may break silently if sourced only from `.env`. | Medium | Decide the canonical source (env var, secret manager, `.env`) and document it; never log the value. |
| R3 | **Supabase connection string is the only key** — losing it blocks both relational and vector retrieval. | High | Document rotation; never log the URL; treat as a deploy-time secret. |
| R4 | **PII leakage** — student names, emails, grades can leak through prompts, logs, or vector content. | High | Redact in loggers; sanitize HTML; never include `description`/`body` verbatim in errors; enforce the same redaction in `/query` responses. |
| R5 | **Text-to-SQL injection** — LLM-generated SQL on a writable role is catastrophic. | High | Allow-list templates + read-only role + statement_timeout + row-count cap. |
| R6 | **Chroma destructive `rmtree`** — current `save_to_chroma_db` deletes the chroma directory on every call. | High | Gate behind an explicit `--reset` flag; default to upsert. |
| R7 | **Schema drift** — Canvas may add/remove keys (we saw 33 in favorites, 75 in assignments). | Medium | Persist the **whitelist** only; ignore unknown keys; record schema version per sync. |
| R8 | **Ollama availability** — `qwen3-embedding:8b` runs locally; the server must degrade gracefully if Ollama is down. | Medium | Health-check `/healthz` reports embedding provider status; fall back to relational-only when vector branch unavailable. |
| R9 | **Review budget** — the user set `review_budget_lines=800`. FastAPI scaffold + 5 SQLAlchemy models + sync + router + tests may exceed this. | Medium | Apply `work-unit-commits` skill: split into chained PRs (scaffold → schema → sync → routing → HTTP layer). |
| R10 | **langchain 1.3.x deprecation churn** — many APIs changed between 0.3.x (in `requirements.txt`) and 1.3.x (installed). | Medium | In `sdd-propose`/`sdd-design`, reconcile installed vs. declared versions; pin one. |
| R11 | **Path encoding gotcha** — the configured Python is a Windows-native binary invoked from WSL bash. `/mnt/c/...` paths silently fail; only Windows paths work. | Low | Document in the runbook; CI must run on Windows or WSL with explicit `cmd.exe` paths. |

---

## 8. Evidence Trail

- **Probe script**: `/tmp/opencode/canvas-probe/probe.py` (outside repo, ephemeral).
- **Probe output**: `/tmp/opencode/canvas-probe/result.json` (outside repo,
  sanitized JSON of keys/types/presence; no values, no IDs, no URLs).
- **Library inventory**: live `importlib.import_module` check against the
  configured Python.
- **Local schema spot-check**: structural counts only (`collections.Counter`)
  over `src/data/course_*.json` — no field values copied.

---

## 9. Ready for Proposal

**Yes** — the technical direction is clear enough to write a proposal. The
orchestrator should hand off to `sdd-propose` next, but FIRST collect
answers to the open product questions in §6 (especially Q1 multi-user,
Q3 read-only confirmation, Q6 classmate data, Q9 LLM choice). Without
those, the proposal cannot fix the auth model, sync frequency, or
data-scoping rules.