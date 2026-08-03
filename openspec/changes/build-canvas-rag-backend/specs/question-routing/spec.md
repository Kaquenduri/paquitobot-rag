# Delta for Question Routing

## ADDED Requirements

None.

## MODIFIED Requirements

### Requirement: Deterministic-First Routing

The router MUST apply deterministic rules before any model classifier. It SHALL route explicit dates, counts, grades, statuses, and aggregations to relational retrieval; semantic explanation to vector retrieval; and mixed evidence to hybrid retrieval. Gemini 2.5 Flash MAY classify only unresolved ambiguity and MUST return a supported route label.

(Previously: Deterministic-first routing existed without making the classifier's constrained output mandatory.)

#### Scenario: Rule identifies an aggregate question

- GIVEN a question explicitly asks for a count or aggregate
- WHEN routing begins
- THEN the deterministic router MUST select relational retrieval
- AND no classifier model call SHALL be made

#### Scenario: Rules cannot disambiguate intent

- GIVEN deterministic rules produce no confident route
- WHEN routing continues
- THEN Gemini 2.5 Flash MAY classify the question
- AND its output MUST be limited to supported route labels

### Requirement: Guarded Relational Routing

Every generated relational query MUST be SELECT-only, match an allow-list, and pass structural validation before execution. Execution MUST use a PostgreSQL read-only role, a bounded `statement_timeout`, a server-enforced row limit, parameterized values, and a server-injected `tenant_id` predicate that client or model output cannot override.

(Previously: Allow-listing, validation, read-only execution, timeout, row limit, and tenant injection were required without explicitly requiring SELECT-only structure and non-overridable scope.)

#### Scenario: SQL is outside the allow-list

- GIVEN generated SQL contains an unsupported statement or query shape
- WHEN validation runs
- THEN execution MUST be denied
- AND the response MUST NOT include SQL secrets or database details

#### Scenario: Cross-tenant SQL is attempted

- GIVEN a question or generated query references another tenant
- WHEN server-side scope is applied
- THEN the server MUST force the authenticated `tenant_id`
- AND no other tenant's rows SHALL be returned

### Requirement: Provider Degradation and Response Language

If Ollama embedding or vector retrieval is unavailable, routing MUST disable vector and hybrid execution and SHALL continue only with supported relational answers. Final answers MUST use the detected language of the student's current question; an unsupported semantic-only request MUST receive a bounded, non-fabricated response in that language.

(Previously: Relational-only degradation and detected-language answers were required without explicit semantic-only refusal behavior.)

#### Scenario: Ollama is unavailable

- GIVEN the embedding provider cannot serve a query
- WHEN the question has a supported relational route
- THEN the system MUST answer using relational evidence only
- AND it MUST NOT claim semantic evidence was searched

#### Scenario: Question language is detected

- GIVEN a supported question in a detectable language
- WHEN an answer is generated
- THEN the answer MUST use that detected language

## REMOVED Requirements

None.
