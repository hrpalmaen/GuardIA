---
name: guardia-lambda
description: Guidelines for implementing and modifying GuardIA AWS Lambda functions while preserving its architecture, deterministic remediation policies, security requirements, and operational deadlines.
---

# Skill: GuardIA Lambda Development

## Objective

Provide a consistent procedure for implementing and modifying AWS Lambda functions in GuardIA, an AI agent that monitors AWS Step Functions data-extraction processes, performs permitted recovery actions, and escalates failures requiring human judgment.

All implementations must follow the repository's requirements, technical design, security standards, and existing code conventions.

## Before You Start

1. Review `.kiro/specs/guardia/` to understand the applicable requirements, design decisions, and implementation tasks.
2. Read `.kiro/steering/product.md`, `.kiro/steering/tech.md`, and `.kiro/steering/structure.md` when available and relevant.
3. Inspect the existing implementation under `src/`, `infra/`, and `tests/` before creating or modifying components.
4. Review `docs/` for architecture diagrams, operational procedures, and runbooks relevant to the task.
5. Identify the requirement IDs associated with the implementation. Do not invent requirements or identifiers.
6. Reuse existing modules, interfaces, models, utilities, and error-handling patterns whenever possible.

## GuardIA Architecture Principles

### Monitoring and orchestration

1. Preserve the separation of responsibilities between AWS Lambda, AWS Step Functions, EventBridge, DynamoDB, and other existing components.
2. Keep Lambda handlers focused on event handling, validation, and orchestration. Place reusable business logic in the shared library when consistent with the established repository structure.
3. Follow the existing execution-discovery strategy based on Step Functions state machines tagged with `guardia:*`.
4. Respect the process metadata defined by these tags:
   - `guardia:monitor`: whether GuardIA monitors the process.
   - `guardia:process-type`: the process type, such as `master` or `conversations`.
   - `guardia:organization`: the organization associated with the process.
   - `guardia:remediation`: the permitted remediation mode, `auto` or `escalate-only`.
5. Do not assume every monitored process is eligible for automatic recovery.

### AI recommendations and deterministic decisions

1. Preserve the fundamental GuardIA principle: **the AI agent recommends; deterministic policy decides**.
2. Amazon Bedrock may classify and summarize errors outside the established error catalog.
3. Do not delegate authorization to retry, redrive, relaunch, or escalate to an unconstrained AI-generated recommendation.
4. Keep remediation eligibility, retry limits, and action selection governed by the project's deterministic policy and orchestration rules.
5. Never allow an AI-generated classification to override a known error-catalog rule or bypass a safety constraint.
6. Do not invent error-catalog entries, decision rules, or recovery actions.

### Recovery and escalation

1. Follow the configured remediation mode:
   - `auto`: automatic recovery may be performed only when the deterministic policy permits it.
   - `escalate-only`: do not relaunch or otherwise automatically recover the monitored process; follow the applicable escalation path.
2. Respect the operational recovery limit of **at most two attempts within 20 minutes**.
3. Use redrive or relaunch only when explicitly permitted by the applicable policy and supported by the existing implementation.
4. Avoid duplicate or concurrent recovery actions that could violate the retry limit or the intended process behavior.
5. Preserve the escalation workflow for failures requiring human judgment, including Microsoft Teams notifications to the on-call engineer.
6. Include relevant diagnostic information and the remaining time before the operational deadline when required by the existing notification design.
7. Preserve reminder behavior and stop or resolve escalation only according to the defined success and orchestration rules.

## Operational Requirements

1. Preserve the requirement that monitored extraction processes must be ready before **06:00 Colombia time**.
2. Monitoring must detect failed, missed, and stuck executions within the documented target of two minutes.
3. Handle dates, time zones, deadlines, and elapsed-time calculations consistently with the project's established implementation.
4. Do not hardcode environment-specific thresholds or configuration values when the project specifies that they must be loaded from AWS Systems Manager Parameter Store.
5. Do not introduce new operational thresholds or alter existing retry and escalation behavior without a corresponding requirement or approved design change.

## Security and Reliability

1. Apply least-privilege IAM permissions to each Lambda and its execution role.
2. Grant only the AWS permissions required for the function's documented responsibilities.
3. Never hardcode credentials, tokens, secrets, or sensitive configuration in source code or tests.
4. Use the project's established Secrets Manager and Parameter Store conventions.
5. Do not expose credentials, tokens, sensitive payloads, or unnecessary personal data in logs, exceptions, or Microsoft Teams notifications.
6. Handle AWS service errors, invalid events, missing configuration, and unexpected responses consistently with the existing error-handling conventions.
7. Use the project's `ErrorCatalog` and shared utilities when applicable; do not duplicate existing business logic.
8. Design retries and recovery operations to avoid unintended duplicate side effects.

## Implementation and Testing

1. Use Python 3.12 and follow the repository's established coding conventions.
2. Reuse existing Pydantic models for validation and structured data wherever applicable.
3. Keep implementation changes within the established `src/` structure.
4. Add or update unit tests under `tests/` following existing naming, fixture, and mocking conventions.
5. Associate requirement-based tests with valid requirement IDs using `@pytest.mark.req("x.y")`.
6. Test successful behavior, invalid inputs, AWS service failures, policy restrictions, and relevant boundary conditions.
7. Test both `auto` and `escalate-only` behavior when applicable to the changed functionality.
8. Verify retry limits and deadline-related behavior when affected by the implementation.
9. Mock AWS services in unit tests using the project's established tools, such as `moto`, where appropriate. Do not make unintended real AWS calls.
10. Run the relevant tests and configured quality checks before requesting review.

## Restrictions

- Do not invent requirements, error-catalog entries, AWS resources, configuration parameters, or module names.
- Do not allow Amazon Bedrock to make final remediation decisions outside the deterministic policy.
- Do not bypass the `guardia:remediation` setting.
- Do not exceed the documented recovery limit.
- Do not introduce unnecessary IAM permissions or expose sensitive information.
- Do not duplicate logic that belongs in shared modules.
- Do not change operational behavior without a supporting requirement or approved design.
- Do not mark a task complete until its applicable requirements and tests have been verified.

## Definition of Done

A Lambda change is ready for review when it:

1. Satisfies the applicable documented requirements.
2. Follows GuardIA's architecture and deterministic decision-making principles.
3. Respects remediation modes, retry limits, security constraints, and operational deadlines.
4. Includes appropriate tests with valid requirement traceability.
5. Passes the relevant automated tests and configured quality checks.
6. Includes documentation updates when the change affects architecture, configuration, or operational procedures.
