# Canvas Read Sync Specification

## Purpose

Define safe synchronization of each authenticated student's own Canvas data.

## Requirements

### Requirement: GET-Only Canvas Access

The Canvas integration MUST issue only HTTP GET requests and MUST reject every mutation method before transmission. It SHALL follow Canvas pagination while preserving the authenticated tenant scope across every page.

#### Scenario: Synchronize supported resources

- GIVEN a student has a valid stored Canvas credential
- WHEN synchronization requests supported Canvas resources
- THEN every Canvas request MUST use GET
- AND paginated results MUST remain scoped to that student

#### Scenario: Mutation request is attempted

- GIVEN any component requests a non-GET Canvas operation
- WHEN the Canvas client evaluates the request
- THEN it MUST reject the operation before network transmission

### Requirement: Periodic and Manual Synchronization

The system SHALL schedule each connected tenant every six hours with bounded jitter and MUST acquire a per-tenant distributed lock before work. Authenticated manual synchronization MAY be requested, but it MUST be rate-controlled and MUST share the same lock so runs cannot overlap.

#### Scenario: Scheduled synchronization becomes due

- GIVEN six hours plus the tenant's bounded jitter have elapsed
- WHEN the scheduler evaluates that tenant
- THEN it MUST acquire the tenant lock before starting synchronization

#### Scenario: Manual synchronization is throttled

- GIVEN a tenant exceeds the manual rate or already holds a sync lock
- WHEN another manual request is submitted
- THEN the system MUST reject or defer it without starting duplicate work

### Requirement: Idempotent Incremental Synchronization

Synchronization MUST upsert by tenant-scoped Canvas identifiers and MUST advance a persisted watermark only after successful processing. Replaying a completed input or retrying a failed run MUST NOT create duplicates or skip uncommitted changes.

#### Scenario: Sync input is replayed

- GIVEN the same Canvas records and watermark are processed twice
- WHEN both runs complete
- THEN the resulting relational and document state MUST be equivalent to one run

#### Scenario: Canvas fails mid-sync

- GIVEN Canvas returns an error before a sync completes
- WHEN the run terminates
- THEN the prior successful watermark MUST remain unchanged
- AND previously committed student data MUST remain available

### Requirement: Sync Freshness and Failure Status

The system MUST track the last successful sync and current lag per tenant without recording credentials or raw Canvas payloads. A Canvas failure SHALL preserve previously committed data and MUST expose a safe retryable status.

#### Scenario: Sync is late

- GIVEN a tenant has not completed a successful sync within six hours plus allowed jitter
- WHEN freshness is evaluated
- THEN the tenant MUST be marked as lagging
- AND existing data MUST remain queryable with its freshness status

#### Scenario: Canvas is unavailable

- GIVEN Canvas fails or times out during synchronization
- WHEN the run ends
- THEN the system MUST retain the prior successful state
- AND diagnostics MUST NOT expose authorization data
