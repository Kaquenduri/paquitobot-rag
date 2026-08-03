# Embedding Specification

## Purpose

Define creation of tenant-scoped semantic vectors from sanitized academic text.

## Requirements

### Requirement: Approved Embedding Provider

The system MUST use Ollama `qwen3-embedding:8b` with 1024-dimensional vectors for semantic indexing and query embeddings. Stored vectors and query vectors MUST use the same model contract.

#### Scenario: Create an academic embedding

- GIVEN a retrieval-ready academic chunk
- WHEN an embedding is requested
- THEN Ollama `qwen3-embedding:8b` MUST produce a 1024-dimensional vector

#### Scenario: Vector contract mismatches

- GIVEN a vector has an unexpected model or dimension
- WHEN persistence is attempted
- THEN the system MUST reject the vector without altering existing vectors

### Requirement: Sanitized Tenant-Scoped Input

Embedding input MUST be sanitized before model invocation and MUST carry server-assigned tenant provenance. Assignment `description` and own-submission `body` MUST NOT be embedded as unsanitized HTML.

#### Scenario: Unsanitized HTML reaches embedding

- GIVEN a chunk contains unapproved HTML or executable content
- WHEN embedding validation runs
- THEN the system MUST reject or sanitize the chunk before invoking Ollama

#### Scenario: Tenant provenance is absent

- GIVEN a chunk lacks server-assigned `tenant_id`
- WHEN embedding is requested
- THEN the system MUST reject the request

### Requirement: Embedding Failure Isolation

An Ollama failure MUST NOT delete, replace, or corrupt existing vectors. The system SHALL report vector capability as unavailable so question routing can use relational-only behavior.

#### Scenario: Ollama fails during indexing

- GIVEN existing PGVector records are available
- WHEN Ollama fails while embedding changed content
- THEN existing vector records MUST remain unchanged
- AND the failed content MUST remain eligible for a later retry
