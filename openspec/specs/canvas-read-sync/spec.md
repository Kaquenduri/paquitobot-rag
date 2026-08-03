# Canvas Read Sync Specification

## Purpose

Define safe synchronization of each authenticated student's own Canvas data.

## Requirements

### Requirement: GET-Only Canvas Access

The Canvas integration MUST issue only HTTP GET requests and MUST NOT perform Canvas mutations. It SHALL follow Canvas pagination while preserving tenant scope.

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

The system SHALL schedule each connected tenant for synchronization every six hours and MAY accept an authenticated manual synchronization request. Manual requests MUST be rate-controlled and MUST NOT bypass coordination safeguards.

#### Scenario: Scheduled synchronization becomes due

- GIVEN six hours have elapsed since a tenant's last successful scheduled sync
- WHEN the scheduler evaluates that tenant
- THEN it MUST make the tenant eligible for synchronization

#### Scenario: Manual synchronization is throttled

- GIVEN a tenant exceeds the allowed manual synchronization rate
- WHEN another manual request is submitted
- THEN the system MUST reject or defer the request without starting duplicate work

### Requirement: Idempotent Incremental Synchronization

Synchronization MUST upsert by tenant-scoped Canvas identifiers and SHALL advance a persisted watermark only after successful processing. Replaying a completed input MUST NOT create duplicates.

#### Scenario: Sync input is replayed

- GIVEN the same Canvas records and watermark are processed twice
- WHEN both runs complete
- THEN the resulting relational and document state MUST be equivalent to one run

#### Scenario: Canvas fails mid-sync

- GIVEN Canvas returns an error before a sync completes
- WHEN the run terminates
- THEN the prior successful watermark MUST remain unchanged
- AND previously committed student data MUST remain available
