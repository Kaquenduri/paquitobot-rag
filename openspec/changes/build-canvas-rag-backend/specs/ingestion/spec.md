# Delta for Ingestion

## ADDED Requirements

### Requirement: FastAPI Dependency Declaration

The backend dependency manifest MUST declare FastAPI so the ingestion, sync, and query HTTP service can be installed reproducibly from `requirements.txt`.

#### Scenario: Install declared backend dependencies

- GIVEN a clean supported Python environment
- WHEN dependencies from `requirements.txt` are installed
- THEN FastAPI MUST be available to the backend runtime

## MODIFIED Requirements

### Requirement: Academic Content Preparation

The system MUST convert only the authenticated student's supported Canvas course, assignment, and own-submission content into normalized documents. Assignment `description` and own-submission `body` MUST be sanitized before chunking and before embedding. Unsafe markup, executable content, credential fragments, and identifiable peer data MUST NOT enter retrieval documents; unsupported or empty content MAY be omitted.

(Previously: Canvas HTML required general sanitization, without naming both protected fields or the pre-chunking and pre-embedding barriers.)

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

## REMOVED Requirements

None.
