---
name: req-tests
description: Guidelines for writing deterministic, requirement-traceable automated tests for GuardIA, including monitoring, remediation policies, escalation, AWS integrations, and operational deadlines.
---

# Skill: GuardIA Requirement-Based Testing

## Objective

Define a consistent approach for writing automated tests that verify GuardIA's functional, technical, security, and operational requirements.

Tests must demonstrate the expected behavior, preserve traceability to documented requirements, and prevent regressions in monitoring, automated recovery, deterministic decision-making, and human escalation.

## Before Writing Tests

1. Review the applicable requirements and implementation tasks under `.kiro/specs/guardia/`.
2. Read `.kiro/steering/tech.md` and `.kiro/steering/structure.md` when available and relevant.
3. Inspect the implementation and existing tests before adding new test cases.
4. Identify the exact requirement ID for each requirement-based test.
5. Reuse existing fixtures, factories, mocks, and test utilities.
6. Confirm the project's configured test commands and marker conventions rather than assuming they are available.

## Requirement Traceability

1. Associate each applicable requirement-based test with a valid requirement ID using `@pytest.mark.req("x.y")`.
2. Replace `x.y` with the actual identifier from the project's requirements.
3. Never invent, guess, or reuse an unrelated requirement ID.
4. Verify that the referenced requirement exists and that the test validates its intended behavior.
5. Keep tests focused on observable behavior and expected outcomes.
6. Update affected tests when requirements or approved designs change.

## Core GuardIA Behavior to Validate

### Monitoring

When the relevant functionality is implemented or modified, test that GuardIA:

1. Discovers eligible Step Functions state machines using the documented `guardia:*` tags.
2. Correctly interprets `guardia:monitor`, `guardia:process-type`, `guardia:organization`, and `guardia:remediation`.
3. Handles failed, missed, and stuck executions according to the documented requirements.
4. Respects the target of detecting these conditions within two minutes.
5. Handles missing tags, invalid values, incomplete metadata, and unexpected AWS responses safely.

### Deterministic remediation policy

1. Verify that recovery actions are selected by deterministic policy rather than directly by an unconstrained AI recommendation.
2. Verify that known errors follow the applicable `ErrorCatalog` rules.
3. Verify that errors outside the catalog can be classified and summarized by Amazon Bedrock when the requirements call for it.
4. Confirm that an AI classification cannot bypass policy restrictions or independently authorize a recovery action.
5. Test that `auto` permits only recovery actions authorized by the policy.
6. Test that `escalate-only` does not trigger automatic redrive, relaunch, or other prohibited recovery actions.
7. Test that invalid or ambiguous inputs fail safely according to the documented behavior.

### Recovery limits and idempotency

1. Verify the maximum of two recovery attempts within a 20-minute window.
2. Test the relevant boundaries: before the limit, at the limit, and after the limit.
3. Confirm that a further recovery attempt is rejected or prevented when the limit has been reached.
4. Verify that concurrent or repeated events do not unintentionally exceed the recovery limit or trigger duplicate actions.
5. Test redrive and relaunch behavior only where supported by the implementation and requirements.
6. Verify that unsuccessful recovery follows the defined escalation path.
7. Avoid relying on real elapsed time when a deterministic clock or time-mocking utility can be used.

### Escalation and notifications

1. Verify that failures requiring human judgment produce the expected escalation behavior.
2. Check that Microsoft Teams notifications include the diagnostic information required by the documented design.
3. Verify the calculation of the remaining time before the operational deadline when applicable.
4. Test reminder behavior at the configured interval of ten minutes when reminders are part of the changed functionality.
5. Verify that reminders and escalation are resolved according to the documented rule when a later execution succeeds.
6. Ensure notification failures are handled according to the established error-handling policy.
7. Verify that messages and logs do not expose credentials, tokens, or unnecessary sensitive information.

### Operational deadlines

1. Test deadline calculations against the requirement that extraction data must be ready before 06:00 Colombia time.
2. Verify that the relevant time-zone handling is consistent with the implementation.
3. Cover boundary cases around the deadline, date changes, and elapsed-time calculations where applicable.
4. Use deterministic time fixtures or mocking utilities for time-dependent tests.
5. Do not assume that the machine's local time is Colombia time.
6. Do not introduce new deadlines or thresholds that are not documented in the requirements or approved design.

## AWS Integration Testing

1. Mock AWS service interactions in unit tests using the project's established approach, including `moto` when appropriate.
2. Avoid real AWS calls, real credentials, and dependencies on deployed resources in unit tests.
3. Verify expected API parameters, returned data, and error handling where relevant.
4. Test AWS failures such as access denied, missing resources, throttling, and transient errors when they affect the behavior under test.
5. Keep unit tests isolated from account-specific state and environment configuration.
6. Use integration tests for real service interactions only when explicitly required and configured by the project.

## Test Quality Guidelines

1. Test behavior and outcomes rather than merely checking that code executes.
2. Cover positive scenarios, negative scenarios, boundary conditions, and relevant failure paths.
3. Keep tests deterministic, independent, repeatable, and easy to understand.
4. Avoid arbitrary sleeps, uncontrolled network calls, and dependence on real execution timing.
5. Use explicit assertions for recovery actions, escalation decisions, and prohibited operations.
6. Verify both what should happen and what must not happen.
7. Avoid overly broad mocks that allow incorrect behavior to pass.
8. Follow the repository's existing naming, fixture, and organization conventions.

## Validation Workflow

1. Run the tests directly related to the change.
2. Run the broader test suite when appropriate and feasible.
3. Run the configured linting, formatting, typing, or other quality checks relevant to the change.
4. Investigate failures instead of suppressing or bypassing them.
5. Update tests when implementation changes are supported by the requirements.
6. Report any unexecuted checks or known limitations accurately.
7. Do not claim tests passed unless they were actually executed and passed.

## Restrictions

- Never invent requirement IDs or expected behaviors.
- Never use real credentials or expose secrets in test data, output, or logs.
- Never allow tests to legitimize behavior that violates deterministic remediation policy.
- Never let an AI classification bypass remediation mode or recovery limits.
- Never depend on real AWS services in unit tests.
- Never weaken an assertion simply to make a test pass.
- Never mark a requirement as verified solely because a test was written; execute the test and inspect its result.

## Definition of Done

Testing is ready for review when:

1. Each requirement-based test references a valid requirement ID.
2. Tests verify the actual documented behavior.
3. Relevant success, failure, security, policy, and boundary scenarios are covered.
4. AWS interactions and time-dependent behavior are controlled and deterministic where appropriate.
5. The applicable tests and configured quality checks have been executed.
6. Results and any remaining limitations are reported accurately.
