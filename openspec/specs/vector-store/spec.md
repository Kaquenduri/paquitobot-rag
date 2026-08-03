# Vector Store Specification

## Purpose

Define durable, tenant-safe vector persistence without destructive refreshes.

## Requirements

### Requirement: PostgreSQL PGVector Persistence

The system MUST persist semantic vectors in the existing PostgreSQL PGVector store and SHALL access PostgreSQL through SQLAlchemy using `SUPABASE_DATABASE_URL`. Vector records MUST include tenant and source provenance.

#### Scenario: Persist a changed chunk

- GIVEN a tenant-scoped chunk has a valid embedding
- WHEN vector persistence runs
- THEN the vector MUST be upserted in PGVector by stable tenant-scoped source identity

#### Scenario: Database configuration fails

- GIVEN PostgreSQL cannot be initialized
- WHEN vector persistence starts
- THEN the operation MUST fail without exposing `SUPABASE_DATABASE_URL`

### Requirement: Preserve Existing Vector Data

Normal ingestion MUST preserve the existing PGVector table and unchanged vector records. The system MUST NOT truncate or recreate vector storage as part of refresh.

#### Scenario: Incremental refresh runs

- GIVEN existing PGVector documents and one changed source document
- WHEN refresh completes
- THEN only vectors affected by the changed source MAY be replaced
- AND unrelated existing vectors MUST remain intact

### Requirement: Prohibit Destructive Local Reset

The application MUST NOT automatically delete a persistent vector directory. The destructive `shutil.rmtree` reset behavior SHALL be removed from normal storage paths, and an indexing failure MUST NOT trigger directory deletion.

#### Scenario: Local vector directory already exists

- GIVEN a persistent local vector directory contains data
- WHEN vector initialization runs
- THEN the directory MUST NOT be recursively deleted

#### Scenario: Vector initialization fails

- GIVEN initialization raises an error
- WHEN failure handling executes
- THEN existing local and PGVector data MUST remain preserved

### Requirement: Tenant-Filtered Vector Access

Every vector write and search MUST apply server-derived `tenant_id`. Client metadata MUST NOT widen vector scope.

#### Scenario: Cross-tenant vector query is attempted

- GIVEN an authenticated student submits another tenant identifier
- WHEN similarity search executes
- THEN the search MUST use only the authenticated tenant's server-derived scope
