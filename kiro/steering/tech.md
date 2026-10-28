---
inclusion: always
---

# Tech Stack — GuardIA

## Language and infrastructure

- **Python 3.12** for all Lambda code, the shared library, scripts, and tests.
- **AWS CDK in Python** for all infrastructure. No resources are created by hand in the console.
- All services are managed AWS services, deployed in the same region as the existing extraction processes.

## AWS services

| Need | Service |
|---|---|
| Orchestration of the incident flow | AWS Step Functions (`GuardIA-Incident-Flow`) |
| Compute | AWS Lambda |
| Detection | EventBridge rules (Step Functions Execution Status Change) |
| Schedules (discovery, watchdog, summary) | EventBridge Scheduler |
| Process discovery | Resource Groups Tagging API |
| State and audit | Amazon DynamoDB (KMS-encrypted) |
| AI classification and summary | Amazon Bedrock agent, Amazon Nova Pro (configurable), no action groups |
| Secrets (Teams webhooks) | AWS Secrets Manager with KMS |
| Configuration thresholds | SSM Parameter Store |
| Backup email | Amazon SES |
| Metrics, alarms, dashboard | CloudWatch (EMF metrics), SNS |
| Failed async invocations | SQS dead-letter queues |

External integration: Microsoft Teams through a Workflows (Power Automate) webhook that posts adaptive cards with mentions. No Microsoft Graph.

## Libraries

- `boto3` for AWS calls.
- `pydantic` for models and validation of the agent output (`AgentDecision`).
- `croniter` for computing expected executions with time zone.
- `pytest` and `moto` for tests.

## Conventions

- Time zone for every business rule: `America/Bogota`. Store timestamps in ISO 8601 with offset.
- One IAM role per Lambda, least privilege. `RedriveOrRestart` is bounded by a condition on the tag `guardia:remediation=auto`.
- Never log webhook URLs, secrets, or task tokens.
- Thresholds (deadline, grouping window, waits, attempts) come from SSM, never hardcoded in handlers.
- Business rules live in `src/guardia_common/` as pure, testable functions; handlers stay thin.
- Every Lambda writes its lifecycle events to `IncidentEvents` through the shared audit decorator.

## Testing

- Every acceptance criterion in `requirements.md` has at least one test that references it with `@pytest.mark.req("<id>")`, e.g. `@pytest.mark.req("4.6")`.
- Unit tests mock AWS with `moto`; CDK tests use `aws_cdk.assertions` on the synthesized template.
- Time-dependent logic is tested with simulated time, including day changes and the Monday on-call rotation.

## Common commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # all tests
pytest -m "req"             # tests linked to acceptance criteria
cdk synth                   # synthesize the CloudFormation templates
cdk deploy --all            # deploy all stacks
```
