# Tenant Authentication Specification

## Purpose

Define backend identity, Canvas credential custody, and tenant authorization boundaries.

## Requirements

### Requirement: Separate Backend and Canvas Credentials

The system MUST authenticate each backend user independently from Canvas. Each student MAY connect one personal Canvas token for that student's tenant, and the token MUST NOT function as a backend session credential or authorize another tenant.

#### Scenario: Backend user connects Canvas

- GIVEN an authenticated backend user submits a personal Canvas token
- WHEN the connection is accepted
- THEN the token MUST be associated only with that user's tenant
- AND backend authentication MUST remain separate

#### Scenario: Canvas token is used as backend authentication

- GIVEN a request presents only a Canvas token
- WHEN it accesses a protected backend operation
- THEN access MUST be denied

### Requirement: Encrypted Token Custody

Canvas tokens MUST be encrypted before persistent storage and SHALL be decrypted only transiently for an authorized tenant's GET request. Plaintext tokens MUST NOT be stored or emitted in database fields, files, caches, traces, metrics, logs, exception messages, or API errors.

#### Scenario: Token is persisted

- GIVEN a student submits a valid Canvas token
- WHEN credential storage completes
- THEN persistent storage MUST contain only encrypted token material

#### Scenario: Token appears in diagnostic context

- GIVEN an operation includes a Canvas token in memory
- WHEN logging, tracing, or error serialization occurs
- THEN the token MUST be redacted before emission

### Requirement: Server-Enforced Tenant Authorization

The server MUST derive `tenant_id` exclusively from authenticated backend identity and MUST NOT trust client, Canvas, SQL-model, or vector-metadata tenant identifiers. Protected operations SHALL access only the authenticated tenant's own records.

#### Scenario: Client requests another tenant

- GIVEN an authenticated request supplies a different `tenant_id`
- WHEN authorization is evaluated
- THEN the supplied identifier MUST be ignored or rejected
- AND no cross-tenant data SHALL be exposed

### Requirement: Secret-Safe Errors

Errors and logs MUST NOT reveal Canvas tokens, encrypted token material, database URLs, authorization headers, or credential-bearing request bodies. Redaction MUST apply to success diagnostics, validation failures, Canvas failures, and unexpected exceptions.

#### Scenario: Canvas authentication fails

- GIVEN Canvas rejects a student's token
- WHEN the backend returns and logs the failure
- THEN both outputs MUST use a redacted error
- AND neither output SHALL contain the token or authorization header

#### Scenario: Unexpected exception contains a token

- GIVEN an exception message or context contains credential material
- WHEN the exception is logged or serialized
- THEN the system MUST replace the credential with a redaction marker
