
---
name: req-tests
description: Guidelines for writing automated tests that are traceable to the Guardia project's functional and technical requirements.
---

# Skill: Requirement-Based Testing

## Objective

Define a consistent approach to writing automated tests that verify the Guardia project's functional and technical requirements and maintain traceability between requirements and test cases.

## Procedure

1. Review `requirements.md` to identify the requirement being validated.
2. Review `tasks.md` and `tech.md` to understand the implementation scope and project conventions.
3. Create tests under `tests/`, following the repository's existing structure.
4. Mark each applicable test with `@pytest.mark.req("x.y")`, replacing `x.y` with an existing requirement ID.
5. Verify successful scenarios, invalid inputs, error conditions, and relevant boundary cases.
6. Use `moto` to mock AWS services when applicable.
7. For time-dependent behavior, use the project's established time-mocking approach to test dates, schedules, expiration periods, and time thresholds deterministically.
8. Avoid real AWS service calls in unit tests.
9. Run the tests and fix failures before submitting changes for review.

## Test Quality Guidelines

- Never invent requirement IDs.
- Never use real credentials or expose secrets in test data or logs.
- Verify expected behavior rather than merely checking that code executes.
- Keep tests isolated, deterministic, and repeatable.
- Cover relevant negative scenarios and boundary conditions.
- Reuse existing fixtures and test utilities when appropriate.
- Keep tests focused and easy to maintain.

## Requirement Traceability

1. Every requirement-based test must reference a valid requirement ID.
2. Confirm that each referenced ID exists in `requirements.md`.
3. Ensure the test actually validates the behavior described by the referenced requirement.
4. Update or add tests when a requirement changes.

## Definition of Done

Tests are complete when they are associated with valid requirement IDs, verify the expected behavior, avoid unintended external dependencies, and pass the project's configured quality checks.
