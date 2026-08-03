# Retrieval Specification

## Purpose

Define tenant-safe evidence retrieval and grounded answer generation.

## Requirements

### Requirement: Tenant-Scoped Evidence

Relational, vector, and hybrid retrieval MUST use the authenticated student's server-derived `tenant_id`. Evidence MUST include only that student's records and permitted non-identifying aggregates visible to that student.

#### Scenario: Cross-tenant evidence is requested

- GIVEN a question attempts to reference another tenant or classmate
- WHEN retrieval executes
- THEN no identifiable peer or other-tenant evidence SHALL be returned

#### Scenario: Allowed aggregate is requested

- GIVEN Canvas exposed a non-identifying aggregate to the authenticated student
- WHEN retrieval answers an aggregate question
- THEN the system MAY use the materialized aggregate
- AND it MUST NOT disclose peer-level records

### Requirement: Relational and Semantic Retrieval

The system SHALL use relational retrieval for exact facts and aggregates, PGVector retrieval for semantic evidence, and hybrid retrieval only when both are required. Relational execution MUST use validated allow-listed queries with server-enforced tenant scope and result bounds.

#### Scenario: Semantic explanation is requested

- GIVEN a question requires meaning from sanitized academic text
- WHEN PGVector is available
- THEN retrieval SHOULD return the most relevant tenant-scoped chunks

#### Scenario: Exact grade status is requested

- GIVEN a question asks for the student's exact submission status
- WHEN retrieval executes
- THEN it MUST use a validated tenant-scoped relational query

### Requirement: Relational-Only Degradation

If Ollama or vector search is unavailable, the system MUST skip semantic and hybrid retrieval and SHALL answer only questions supported by relational evidence. It MUST identify unsupported semantic retrieval without fabricating context.

#### Scenario: Ollama is unavailable

- GIVEN vector capability is unavailable
- WHEN an exact relational question is asked
- THEN retrieval MUST continue with relational evidence only

#### Scenario: Semantic-only question during degradation

- GIVEN vector capability is unavailable
- WHEN a semantic-only question is asked
- THEN the system MUST return a bounded unavailability response
- AND it MUST NOT fabricate an answer

### Requirement: Grounded Multilingual Generation

Gemini 2.5 Flash SHALL generate answers only from authorized retrieved evidence. The answer MUST use the language detected from the student's question and MUST NOT expose credentials, raw authorization data, or identifiable peer data.

#### Scenario: Evidence is insufficient

- GIVEN authorized retrieval cannot support the requested answer
- WHEN Gemini generates the response
- THEN it MUST state that the available data is insufficient in the detected question language
