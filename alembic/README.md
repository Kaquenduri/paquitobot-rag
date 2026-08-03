# Alembic migrations — Canvas RAG backend

This directory owns the database schema migrations for the Canvas RAG
backend. The first migration (`0001_init.py`) creates the seven
application tables:

| Table              | Purpose                                            |
|--------------------|----------------------------------------------------|
| `tenants`          | One row per backend user (server-assigned UUID).   |
| `canvas_credentials` | Fernet-encrypted Canvas token custody (PR 2).    |
| `users`            | Authenticated Canvas user (the tenant's own).      |
| `courses`          | Course metadata.                                   |
| `enrollments`      | Enrollment rows with FK to `courses` / `users`.   |
| `assignments`      | Assignment metadata + `score_statistics` JSONB.   |
| `submissions`      | Submission rows tied to one assignment + user.    |
| `sync_state`       | Per-tenant sync watermarks.                        |

The migration **does NOT touch the existing PGVector `documents`
table** (see design §8 "Vector Store Preservation"). A later change
that adds columns to `documents` would still be additive; this
project never deletes from it.

## Running migrations

```bash
# Apply the latest schema:
alembic upgrade head

# Roll back the last migration:
alembic downgrade -1

# Emit SQL without applying it (offline mode):
alembic upgrade head --sql
```

The async URL is loaded from `SUPABASE_DATABASE_URL`; see
`app/core/config.py` for the scheme validation.

## Read-only Postgres role (`pg_role_canvas_readonly`)

The text-to-SQL executor (PR 5) runs every allow-listed query through
a dedicated Postgres role that **only** has `SELECT` privileges on the
seven application tables above. The role is provisioned out-of-band
(via your standard Postgres role-management process) and is **not**
part of an Alembic migration — it would otherwise drift every time
someone rebuilt the dev cluster from scratch.

DDL snippet — apply once per environment, then hand the role out to
the executor connection pool:

```sql
-- 1. Create the role with no login by default; the connection pool
--    supplies a password via the standard credential store.
CREATE ROLE pg_role_canvas_readonly NOLOGIN;

-- 2. Grant connect on the database that holds the application
--    tables. Replace ``primer_rag`` with the real DB name.
GRANT CONNECT ON DATABASE primer_rag TO pg_role_canvas_readonly;

-- 3. Grant usage on the application schema (defaults to ``public``
--    in Supabase projects; adjust if you move the tables).
GRANT USAGE ON SCHEMA public TO pg_role_canvas_readonly;

-- 4. Grant SELECT on each of the seven tables ONLY. No INSERT /
--    UPDATE / DELETE / TRUNCATE; no REFERENCES so the executor
--    cannot piggyback FK creation; no TRIGGER to keep audit
--    chains under the application role's control.
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

-- 5. Explicitly revoke anything the role might have inherited from
--    PUBLIC. ``EXECUTE`` on functions, ``USAGE`` on languages, etc.
--    are deliberately absent so the executor cannot call pg_* /
--    set_config helpers even by accident.
REVOKE ALL ON DATABASE primer_rag FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
```

> **Note on `documents`.** The PGVector `documents` table is **not**
> in the grant list. The executor MUST NOT touch vector data — that
> path runs through the dedicated `app/rag/vector_store.py` module
> with a different role that owns DML on `documents`.

### Verification

After applying the DDL above, confirm the role can read but not
write:

```sql
-- Should succeed:
SET ROLE pg_role_canvas_readonly;
SELECT count(*) FROM users;
RESET ROLE;

-- Should fail with ``ERROR: permission denied``:
SET ROLE pg_role_canvas_readonly;
INSERT INTO users (id, tenant_id, canvas_id) VALUES (gen_random_uuid(),
                                                    gen_random_uuid(),
                                                    1);
RESET ROLE;
```

A failed `INSERT` is the success signal here: the role is correctly
locked down.

## Adding a new migration

```bash
# Generate an empty revision (autogenerate requires a live DB):
alembic revision -m "describe the change"

# Apply locally and run the test suite:
alembic upgrade head && pytest -q tests/
```

PR 3+ migrations MUST stay additive (no DROP COLUMN, no DROP TABLE)
unless the corresponding `state.yaml` records an explicit
`size:exception` from the maintainer. Destructive operations belong
in a follow-up change so the rollout is reviewable and reversible.