# Ingestion Specification

## Purpose

Define how Canvas academic content becomes tenant-scoped, retrieval-ready documents.

## Requirements

### Requirement: Academic Content Preparation

The system MUST convert only the authenticated student's supported Canvas course, assignment, and own-submission content into normalized documents. Assignment `description` and own-submission `body` MUST be sanitized before chunking and before embedding. Unsafe markup, executable content, credential fragments, and identifiable peer data MUST NOT enter retrieval documents; unsupported or empty content MAY be omitted.

#### Scenario: Sanitize Canvas HTML

- GIVEN an assignment `description` or own-submission `body` contains HTML and executable content
- WHEN the source is prepared for retrieval
- THEN the system MUST retain only safe readable text
- AND unsafe markup MUST NOT reach chunking or embedding

#### Scenario: Omit empty content

- GIVEN sanitization leaves a source field without meaningful text
- WHEN ingestion prepares documents
- THEN the system MAY omit that field from document content
- AND it MUST retain the source record for relational use

### Requirement: Tenant-Aware Provenance

Every ingested document MUST carry server-assigned `tenant_id`, source type, and stable source identifier metadata. The system MUST NOT derive tenant scope from client-supplied document metadata.

#### Scenario: Missing tenant provenance

- GIVEN a source record has no authenticated tenant context
- WHEN document ingestion is requested
- THEN the system MUST reject the ingestion operation
- AND it MUST NOT create chunks or embeddings

#### Scenario: Valid tenant provenance

- GIVEN an authenticated student's own source record
- WHEN the record is ingested
- THEN every resulting document and chunk MUST retain that student's server-assigned tenant scope

### Requirement: FastAPI Dependency Declaration

The backend dependency manifest MUST declare FastAPI so the ingestion, sync, and query HTTP service can be installed reproducibly from `requirements.txt`.

#### Scenario: Install declared backend dependencies

- GIVEN a clean supported Python environment
- WHEN dependencies from `requirements.txt` are installed
- THEN FastAPI MUST be available to the backend runtime
