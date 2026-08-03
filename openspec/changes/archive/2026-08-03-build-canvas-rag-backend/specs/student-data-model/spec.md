# Delta for Student Data Model

## ADDED Requirements

### Requirement: Prohibit Identifiable Peer Records

The system MUST NOT persist, embed, retrieve, or display identifiable classmate records. It MAY retain Canvas-provided aggregate statistics visible to the authenticated student when those aggregates cannot identify an individual peer.

#### Scenario: Canvas payload includes a classmate

- GIVEN a Canvas response includes another student's identifiable record
- WHEN the payload is normalized
- THEN the peer record MUST be discarded before persistence or embedding

#### Scenario: Student requests a classmate's data

- GIVEN an authenticated student asks for a classmate's identifiable data
- WHEN the request is processed
- THEN the system MUST refuse without confirming whether that peer record exists

## MODIFIED Requirements

### Requirement: Tenant-Scoped Academic Records

The system MUST persist the minimum `users`, `courses`, `enrollments`, `assignments`, and `submissions` model, and every record MUST include server-assigned `tenant_id`. Canvas identifiers MUST be unique within a tenant, and user-linked records MUST represent only the authenticated student's own data.

(Previously: Records were tenant-scoped, but the minimum-only model and prohibition on peer-linked rows were not explicit.)

#### Scenario: Persist a student's Canvas data

- GIVEN a student has authenticated and synchronized supported Canvas resources
- WHEN records are persisted
- THEN each record MUST contain the authenticated student's server-assigned `tenant_id`
- AND relationships MUST remain within that tenant

#### Scenario: Reject cross-tenant relationship

- GIVEN records belong to different tenants
- WHEN a relationship between those records is attempted
- THEN the system MUST reject the write

### Requirement: Lifecycle and Materialized Aggregates

The system MUST retain Canvas `workflow_state`, SHALL soft-delete a record when its workflow state becomes inactive, and MUST materialize only Canvas-provided aggregate statistics needed for the authenticated student's questions. Aggregates MUST NOT contain identifiers or peer-level rows.

(Previously: Soft deletion and aggregate limits were required without explicitly tying deletion to inactive `workflow_state` or excluding all peer-level rows.)

#### Scenario: Canvas record becomes inactive

- GIVEN an existing record changes to an inactive `workflow_state`
- WHEN synchronization applies the change
- THEN the record MUST be marked soft-deleted rather than physically removed

#### Scenario: Aggregate statistics are available

- GIVEN Canvas returns class-level score statistics visible to the student
- WHEN the assignment is synchronized
- THEN the allowed aggregate values MUST be materialized
- AND no identifiable classmate data MUST be stored

## REMOVED Requirements

None.
