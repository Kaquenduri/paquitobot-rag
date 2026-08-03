# Ingestion Specification

## Purpose

Define how Canvas academic content becomes tenant-scoped, retrieval-ready documents.

## Requirements

### Requirement: Academic Content Preparation

The system MUST convert supported Canvas course, assignment, and own-submission content into normalized documents. Canvas-originated HTML MUST be sanitized before downstream processing, and unsupported or empty content MAY be omitted.

#### Scenario: Sanitize Canvas HTML

- GIVEN an assignment contains HTML markup and executable content
- WHEN the assignment is prepared for retrieval
- THEN the system MUST retain safe readable text
- AND it MUST remove executable or unsafe markup

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
