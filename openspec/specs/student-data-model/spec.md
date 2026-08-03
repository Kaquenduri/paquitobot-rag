# Student Data Model Specification

## Purpose

Define the minimum relational model for each student's own Canvas academic data.

## Requirements

### Requirement: Tenant-Scoped Academic Records

The system MUST persist `users`, `courses`, `enrollments`, `assignments`, and `submissions`, and every record MUST include `tenant_id`. Canvas identifiers MUST be unique within a tenant rather than globally trusted as authorization boundaries.

#### Scenario: Persist a student's Canvas data

- GIVEN a student has authenticated and synchronized supported Canvas resources
- WHEN records are persisted
- THEN each record MUST contain the authenticated student's `tenant_id`
- AND relationships MUST remain within that tenant

#### Scenario: Reject cross-tenant relationship

- GIVEN records belong to different tenants
- WHEN a relationship between those records is attempted
- THEN the system MUST reject the write

### Requirement: SQLAlchemy PostgreSQL Persistence

Relational persistence SHALL use SQLAlchemy against PostgreSQL configured only through `SUPABASE_DATABASE_URL`. The system MUST NOT require `supabase-py` credentials for these workloads.

#### Scenario: Database configuration is absent

- GIVEN `SUPABASE_DATABASE_URL` is unavailable
- WHEN persistence initializes
- THEN initialization MUST fail without exposing connection details

### Requirement: Lifecycle and Materialized Aggregates

The system MUST retain Canvas `workflow_state`, SHALL soft-delete records whose state makes them inactive, and MUST materialize only Canvas-provided aggregate statistics needed for the authenticated student's questions. Aggregates MUST NOT contain identifiable peer rows.

#### Scenario: Canvas record becomes inactive

- GIVEN an existing record changes to an inactive `workflow_state`
- WHEN synchronization applies the change
- THEN the record MUST be marked soft-deleted rather than physically removed

#### Scenario: Aggregate statistics are available

- GIVEN Canvas returns class-level score statistics visible to the student
- WHEN the assignment is synchronized
- THEN the allowed aggregate values MUST be materialized
- AND no identifiable classmate data MUST be stored
