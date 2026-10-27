
---
name: guardia-lambda
description: Guidelines for implementing AWS Lambda functions that follow the Guardia project's architecture, security requirements, and engineering standards.
---

# Skill: Guardia Lambda

## Objective

Define a consistent procedure for creating and modifying AWS Lambda functions in accordance with the Guardia project's architecture, requirements, and security standards.

## Before You Start

1. Review `tasks.md` to identify the task to be implemented.
2. Review `requirements.md` to identify the applicable functional and technical requirements.
3. Read `tech.md` to understand the project's technical conventions and security constraints.
4. Inspect the existing codebase to reuse established patterns and components.

## Architecture and Implementation

1. Keep Lambda handlers thin and focused on request handling and orchestration.
2. Place reusable business logic in `src/guardia_common/`.
3. Place Lambda-specific code under `src/lambdas/`, following the repository's existing structure.
4. Reuse existing Pydantic models to validate inputs and outputs where applicable.
5. Avoid duplicating business logic that belongs in shared modules.

## Auditing and Error Handling

1. Apply the auditing decorator established by the project.
2. Handle exceptions consistently and avoid exposing sensitive information.
3. Follow the project's error-handling conventions and use the `ErrorCatalog` when applicable.
4. Ensure logs do not contain credentials, access tokens, or other sensitive data.

## Security Requirements

1. Apply the principle of least privilege to each Lambda's IAM policy.
2. Grant only the permissions required by the function.
3. Retrieve operational thresholds and configuration parameters from AWS Systems Manager Parameter Store (SSM), following the project's conventions.
4. Never hardcode credentials, tokens, or secrets in source code.
5. Follow the security requirements documented in `tech.md`.

## Testing and Validation

1. Write unit tests for the implemented business logic.
2. Associate tests with existing requirement IDs using `@pytest.mark.req("x.y")`.
3. Cover successful scenarios, invalid inputs, error conditions, and relevant boundary cases.
4. Run the applicable tests and configured quality checks.
5. Fix identified failures before requesting code review.

## Restrictions

- Do not invent requirements, resource names, or configuration parameters.
- Do not duplicate logic that belongs in `guardia_common`.
- Do not introduce unnecessary IAM permissions.
- Do not consider a task complete until its applicable requirements and tests have been verified.

## Definition of Done

An implementation is complete when it satisfies the documented requirements, follows the project's architecture and security standards, includes appropriate tests, and passes the applicable quality checks.
