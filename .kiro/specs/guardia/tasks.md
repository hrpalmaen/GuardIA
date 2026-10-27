# Implementation Plan — GuardIA

All tasks are coding and automated testing tasks. Each sub-task takes 1 to 4 hours and references the criteria from `requirements.md` that it implements. Dependencies are indicated in the title of each group.

- [ ] 1. Project foundation and data models
- [x] 1.1 Create the AWS CDK project in Python
  - Monorepo structure: `infra/` (stacks), `src/guardia_common/` (shared library), `src/lambdas/`, and `tests/`.
  - Configuration parameters in SSM (deadline, grouping window, time and wait thresholds) with the default values from the design.
  - Tests: CDK synthesis generates the template without errors.
  - _Requirements: 13.3_

- [ ] 1.2 Define the DynamoDB tables
  - Create `ProcessRegistry`, `Incidents` (unique index by `execution_arn`, with encrypted `task_token` and `teams_thread_id`), `IncidentEvents`, `ErrorCatalog`, and `OnCallSchedule` (with `teams_user_id`, `display_name`, and `email`), all encrypted with KMS.
  - `IncidentEvents` with a policy that only allows conditional `PutItem`, a 13-month TTL, and point-in-time recovery.
  - Tests: an IAM policy test confirms that `IncidentEvents` rejects updates and deletes.
  - _Requirements: 10.2, 10.3_

- [ ] 1.3 Implement the `guardia_common` library
  - Models with pydantic: `Incident`, `IncidentEvent`, `AgentDecision`, `ProcessRecord`.
  - Audit event repository and structured logger.
  - Unit tests for model validation, including rejection of an `AgentDecision` with a disallowed action.
  - _Requirements: 4.6, 10.1_

- [ ] 1.4 Load the initial `ErrorCatalog` content
  - 429 → `wait_and_redrive` (maximum wait of 600 s); 403 → `escalate`; 404 with a failed execution → `escalate`; `TIMED_OUT` → `redrive`.
  - Idempotent load script and schema validation.
  - Tests: the loaded catalog matches the schema.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 2. Process discovery (depends on 1.2, 1.3)
- [ ] 2.1 Implement the `DiscoverProcesses` Lambda
  - List resources tagged `guardia:monitor=true` with the tagging API.
  - Validate `process-type`, `organization`, and `remediation`; exclude and warn if any is missing; treat an invalid `remediation` value as `escalate-only`.
  - Update `ProcessRegistry`.
  - Tests with moto: complete resource, incomplete resource, invalid value, and new process.
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [ ] 2.2 Schedule discovery every 15 minutes
  - EventBridge Scheduler rule in CDK targeting `DiscoverProcesses`.
  - Tests: assertion on the synthesized template.
  - _Requirements: 1.1_

- [ ] 3. Failure detection (depends on 2.1)
- [ ] 3.1 Implement the `OpenIncident` Lambda
  - Create the incident with a conditional write by `execution_arn` to avoid duplicates.
  - Enrich with `DescribeExecution`: error, cause, input, and deadline according to the registry.
  - Start `GuardIA-Incident-Flow`.
  - Tests: duplicate event, recording of all fields, and rejection of `SUCCEEDED` executions.
  - _Requirements: 2.2, 2.3, 2.4_

- [ ] 3.2 Create the EventBridge status-change rule
  - Filter `FAILED`, `TIMED_OUT`, and `ABORTED` from the state machines present in `ProcessRegistry`.
  - Target: `OpenIncident`, with an SQS DLQ.
  - Tests: valid and invalid event patterns against the rule pattern.
  - _Requirements: 2.1_

- [ ] 3.3 Implement the computation of expected executions and maximum duration
  - Utility with `croniter` and time zone that returns the expected executions in a window.
  - Median of the last 14 executions; with fewer than 5, 50% of the periodicity.
  - Tests with simulated time, including the day change.
  - _Requirements: 3.1, 3.2, 3.3_

- [ ] 3.4 Implement the `MissedRunWatchdog` Lambda
  - Every 5 minutes, detect `MISSED_RUN` (10-minute tolerance) and `STUCK_RUN` (more than twice the median).
  - Publish the incident through `OpenIncident`.
  - Tests: on-time execution, missed, delayed, and already with an open incident.
  - _Requirements: 3.1, 3.2, 3.3_

- [ ] 4. Classification (depends on 1.4, 3.1)
- [ ] 4.1 Implement the `ClassifyIncident` Lambda
  - Resolve by (process type, error pattern) against the catalog; return `unknown` if there is no match.
  - Treat `MISSED_RUN` as `requires_escalation`.
  - Table-driven tests with 429, 403, failed 404, `TIMED_OUT`, `MISSED_RUN`, and unknown cause.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 4.2 Define the Bedrock agent
  - Agent with a configurable Amazon Nova Pro model, classification and summarization instructions, and no action groups.
  - JSON output schema (`AgentDecision`) and a prompt with examples.
  - Tests: the CDK template grants no infrastructure permissions to the agent role.
  - _Requirements: 4.5, 12.2_

- [ ] 4.3 Implement the `InvokeAgent` Lambda
  - Invoke the agent with a 30 s timeout, validate the output with pydantic, and escalate on any failure.
  - Tests with simulated responses: valid, invalid JSON, disallowed action, and timeout.
  - _Requirements: 4.5, 4.6_

- [ ] 5. Policy and remediation (depends on 3.1, 4.3)
- [ ] 5.1 Implement `RemediationPolicy`
  - Pure function that returns `REMEDIATE`, `REMEDIATE_AND_ESCALATE`, or `ESCALATE` according to classification, `remediation`, attempts (maximum 2), elapsed minutes (maximum 20), indicated wait (maximum 10 minutes), and remaining minutes (30 or less).
  - Table-driven tests covering each criterion of requirements 5 and 6.
  - _Requirements: 5.1, 5.3, 5.5, 5.6, 6.1_

- [ ] 5.2 Implement the `RedriveOrRestart` Lambda
  - Redrive if the execution is Standard and eligible; otherwise a new execution with the original input.
  - IAM policy bounded by the `guardia:remediation=auto` tag condition.
  - Tests: redrive, relaunch, and rejection of a resource without the tag.
  - _Requirements: 5.1, 12.1, 12.4_

- [ ] 5.3 Implement the `PollExecution` Lambda
  - Query the status of the recovered execution and return the final result.
  - Tests: `SUCCEEDED`, `FAILED`, and `RUNNING`.
  - _Requirements: 5.4_

- [ ] 5.4 Implement the `Retry-After` wait state
  - Extract the wait time from the error cause, cap it at 600 s, and escalate if it is exceeded.
  - Tests: valid value, missing, above the maximum.
  - _Requirements: 5.2, 5.3_

- [ ] 5.5 Define the `GuardIA-Incident-Flow` state machine
  - States: `Classify` → `InvokeAgent` (if unknown) → `Policy` → `Remediate` (wait, redrive, poll, verify) or `Escalate` → `CloseIncident`.
  - Parallel escalation branch when the policy returns `REMEDIATE_AND_ESCALATE`.
  - Tests: definition tests with the Step Functions TestState API for each branch.
  - _Requirements: 5.4, 5.5, 6.1, 13.1_

- [ ] 6. Escalation (depends on 5.5)
- [ ] 6.1 Implement the `ResolveOnCall` Lambda
  - Read `OnCallSchedule` for the current date in Colombia time; if missing, return only the backup and record the warning.
  - Tests: normal week, week change on Monday, and missing on-call engineer.
  - _Requirements: 7.1, 7.4_

- [ ] 6.2 Implement the Teams webhook client
  - Post an adaptive card with mentions (`teams_user_id`) to the Workflows flow webhook; the address is read from Secrets Manager (encrypted with KMS) and is never written to logs.
  - Content: organization, process, error, actions attempted with their result, and minutes remaining until the deadline.
  - Tests: card construction, on-call mention, truncation of long fields, and HTTP error handling.
  - _Requirements: 7.2, 12.3_

- [ ] 6.3 Implement the `SendEscalation` Lambda with email backup
  - Retry the post up to 3 times with backoff; if it keeps failing, send the same content through Amazon SES to the on-call engineer, the backup, and the team list.
  - If there is no on-call engineer, mention the backup and record the configuration warning.
  - Store `teams_thread_id` in the incident.
  - Tests: successful post without email, failure with email backup, missing on-call engineer.
  - _Requirements: 7.2, 7.3, 7.4_

- [ ] 6.4 Implement the `ResolveOnSuccess` Lambda
  - EventBridge rule for `SUCCEEDED` events of processes present in `ProcessRegistry`; find the escalated incident for the same process and organization and call `SendTaskSuccess` with the stored token.
  - Ignore events with no open incident and record the closure with `resolved_by=later_execution`.
  - Tests: open incident, no incident, different organization, and duplicate event.
  - _Requirements: 8.3, 2.2_

- [ ] 6.5 Implement the reminders and SLA-breached subflow
  - Wait with a task token and a maximum of 10 minutes; on expiry, `SendReminder` posts a reminder mentioning the on-call engineer and the backup, and the cycle repeats until resolved or the deadline is reached.
  - At the deadline without resolution: mark `SLA_BREACHED`, post the alert mentioning the on-call engineer and the backup, stop the reminders, and keep waiting for the signal for up to 6 hours to record the resolution.
  - On resolution, post the resolution message in the incident thread.
  - Tests with simulated time: resolution before the first reminder, after two reminders, and SLA expiry.
  - _Requirements: 6.2, 8.1, 8.2, 8.3, 8.4_

- [ ] 6.6 Implement the `GroupIncidents` Lambda
  - Detect 3 or more incidents with the same cause within 5 minutes, send a single message with the list of organizations, and keep the individual handling of each incident.
  - Tests: two incidents, three incidents, and expired window.
  - _Requirements: 9.1, 9.2_

- [ ] 7. Traceability and morning summary (depends on 1.3, 5.5)
- [ ] 7.1 Instrument all Lambdas with event logging
  - Decorator that writes opening, classification, action, result, notification, reminder, and closure to `IncidentEvents`.
  - Tests: each step of the flow leaves its event with a timestamp.
  - _Requirements: 10.1_

- [ ] 7.2 Implement the `DailySummary` Lambda
  - Query events since 6:00 p.m. of the previous day and aggregate successful processes, incidents by level, actions, resolution times, `SLA_BREACHED`, and warnings.
  - Snapshot tests on a fixed set of events.
  - _Requirements: 11.1, 11.2_

- [ ] 7.3 Implement the summary delivery
  - Post to the Teams channel reusing the client from 6.2, with its own webhook stored in Secrets Manager; 3 retries and then email with SES.
  - Schedule the run at 06:15 (America/Bogota) with EventBridge Scheduler.
  - Tests: successful delivery, Teams failure with email backup.
  - _Requirements: 11.1, 11.3, 11.4_

- [ ] 8. Observability and self-monitoring of GuardIA (depends on 5.5)
- [ ] 8.1 Emit metrics and the dashboard
  - EMF-format metrics: heartbeat every minute, detection time, time to first action, and resolution time.
  - CDK dashboard with the success indicators.
  - Tests: metrics are emitted with the expected dimensions.
  - _Requirements: 13.1_

- [ ] 8.2 Create the heartbeat alarm
  - CloudWatch alarm for missing heartbeat data for 10 minutes, targeting an SNS topic with an email subscription for the team list and a Lambda that posts to Teams, without going through the main flow.
  - Tests: the template includes the alarm with missing data treated as breaching.
  - _Requirements: 13.2_

- [ ] 8.3 Configure DLQs and alarms for GuardIA's own Lambdas
  - Retries with exponential backoff, SQS as DLQ, and an alarm on messages in the queue.
  - Tests: template assertions for each Lambda.
  - _Requirements: 13.3_

- [ ] 9. End-to-end tests (depends on 6.5, 7.3)
- [ ] 9.1 Create synthetic test state machines
  - CDK test stack with tagged processes that fail on demand with 429, 403, and timeout.
  - Tests: the state machines are discovered by tags and generate incidents.
  - _Requirements: 1.3, 13.4_

- [ ] 9.2 Test the 429 → automatic recovery scenario
  - Verify opening in 2 minutes or less, wait, redrive, and closure as resolved at Level 1.
  - _Requirements: 2.1, 5.1, 5.2, 5.4, 13.1_

- [ ] 9.3 Test the 403 → escalation and resolution scenario
  - Verify the Teams message with mention and diagnosis, the reminder at 10 minutes, and the automatic closure when the relaunched process ends in `SUCCEEDED`.
  - _Requirements: 4.3, 7.2, 8.1, 8.3_

- [ ] 9.4 Test the webhook-down, SLA-breached, and low-time scenarios
  - Webhook down: 3 retries and backup email.
  - Unresolved until the deadline: `SLA_BREACHED` with an alert to on-call and backup, with no further reminders.
  - 30 minutes or less remaining: escalation in parallel with remediation.
  - _Requirements: 6.1, 6.2, 7.3, 8.2, 8.4_
