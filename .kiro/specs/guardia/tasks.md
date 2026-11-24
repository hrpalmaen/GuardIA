# Implementation Plan — GuardIA

All tasks are coding and automated testing tasks. Each sub-task takes 1 to 4 hours and references the criteria from `requirements.md` that it implements. Dependencies are indicated in the title of each group.

- [ ] 1. Project foundation and data models
- [x] 1.1 Create the AWS CDK project in Python
  - Monorepo structure: `infra/` (stacks), `src/guardia_common/` (shared library), `src/lambdas/`, and `tests/`.
  - Configuration parameters in SSM (deadline, grouping window, time and wait thresholds) with the default values from the design.
  - Tests: CDK synthesis generates the template without errors.
  - _Requirements: 13.3_

- [x] 1.2 Define the DynamoDB tables
  - Create `ProcessRegistry`, `Incidents` (unique index by `execution_arn`, with encrypted `task_token` and `teams_thread_id`), `IncidentEvents`, `ErrorCatalog`, and `OnCallSchedule` (with `teams_user_id`, `display_name`, and `email`), all encrypted with KMS.
  - `IncidentEvents` with a policy that only allows conditional `PutItem`, a 13-month TTL, and point-in-time recovery.
  - Tests: an IAM policy test confirms that `IncidentEvents` rejects updates and deletes.
  - Progress: tables defined once in `guardia_common/tables.py` and built from it by `DataStack` (KMS, TTL, PITR, `execution_arn` index) and by the local environments. Append-only: the `IncidentEvents` resource policy denies `UpdateItem`, `DeleteItem`, and `BatchWriteItem` to every principal, and `DataStack.grant_append_events` gives Lambdas only `PutItem`; both are covered by synth tests (Fase B PR 1).
  - _Requirements: 10.2, 10.3_

- [ ] 1.3 Implement the `guardia_common` library
  - Models with pydantic: `Incident`, `IncidentEvent`, `AgentDecision`, `ProcessRecord`.
  - Audit event repository and structured logger.
  - Unit tests for model validation, including rejection of an `AgentDecision` with a disallowed action.
  - _Requirements: 4.6, 10.1_

- [x] 1.4 Load the initial `ErrorCatalog` content
  - 429 → `wait_and_redrive` (maximum wait of 600 s); 403 → `escalate`; 404 with a failed execution → `escalate`; `TIMED_OUT` → `redrive`.
  - Idempotent load script and schema validation.
  - Tests: the loaded catalog matches the schema.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 2. Process discovery (depends on 1.2, 1.3)
- [x] 2.1 Implement the `DiscoverProcesses` Lambda
  - List resources tagged `guardia:monitor=true` with the tagging API.
  - Validate `process-type`, `organization`, and `remediation`; exclude and warn if any is missing; treat an invalid `remediation` value as `escalate-only`.
  - Read the schedules that launch each process (EventBridge rules that target it and Scheduler schedules) and the median duration of the last successful executions (design D14).
  - Update `ProcessRegistry`.
  - Tests with moto: complete resource, incomplete resource, invalid value, and new process.
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2.2 Schedule discovery every 15 minutes
  - EventBridge Scheduler rule in CDK targeting `DiscoverProcesses`.
  - Tests: assertion on the synthesized template.
  - _Requirements: 1.1_

- [ ] 3. Failure detection (depends on 2.1)
- [x] 3.1 Implement the `OpenIncident` Lambda
  - Create the incident with a conditional write by `execution_arn` to avoid duplicates.
  - Enrich with `DescribeExecution`: error, cause, input, and deadline according to the registry.
  - Start `GuardIA-Incident-Flow`.
  - Tests: duplicate event, recording of all fields, and rejection of `SUCCEEDED` executions.
  - _Requirements: 2.2, 2.3, 2.4_

- [x] 3.2 Create the EventBridge status-change rule
  - Filter `FAILED`, `TIMED_OUT`, and `ABORTED` from the state machines present in `ProcessRegistry`.
  - Target: `OpenIncident`, with an SQS DLQ.
  - Tests: valid and invalid event patterns against the rule pattern.
  - Progress: `GuardIA-Detection` (Fase B PR 2). The rule matches failures of every state machine in the account with `STATUS_CHANGE_PATTERN`; OpenIncident ignores the ones not in `ProcessRegistry` (including GuardIA's own flow). Verified in the test account with a manual MISSED_RUN event: incident opened, flow started, classified, and a redelivered event did not open a second one.
  - _Requirements: 2.1_

- [x] 3.3 Implement the computation of expected executions and maximum duration
  - Utility with `croniter` and time zone that returns the expected executions in a window.
  - Median of the last 14 executions; with fewer than 5, 50% of the periodicity.
  - Tests with simulated time, including the day change.
  - _Requirements: 3.1, 3.2, 3.3_

- [x] 3.4 Implement the `MissedRunWatchdog` Lambda
  - Every 5 minutes, detect `MISSED_RUN` (10-minute tolerance) and `STUCK_RUN` (more than twice the median).
  - Publish the incident through `OpenIncident`.
  - Tests: on-time execution, missed, delayed, and already with an open incident.
  - _Requirements: 3.1, 3.2, 3.3_

- [ ] 4. Classification (depends on 1.4, 3.1)
- [x] 4.1 Implement the `ClassifyIncident` Lambda
  - Resolve by (process type, error pattern) against the catalog; return `unknown` if there is no match.
  - Treat `MISSED_RUN` as `requires_escalation`.
  - Table-driven tests with 429, 403, failed 404, `TIMED_OUT`, `MISSED_RUN`, and unknown cause.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 4.2 Define the agent (`AgentPort`, tools, and `AgentDecision`)
  - Provider-independent tool-use loop with read-only tools resolved from the incident, a `submit_decision` tool validated as `AgentDecision`, Spanish system instructions with examples and prompt-injection rules (D24).
  - Adapters: Azure OpenAI (test account) and Bedrock Converse (workshop account), selected by `EnvironmentConfig.agent_provider`.
  - Tests: the CDK template grants the agent role no write permissions on the infrastructure, and Bedrock access is limited to one model ARN.
  - Detailed brief: `docs/analisis/tareas-fase-b-equipo.md`, T8.
  - _Requirements: 4.5, 12.2_

- [ ] 4.3 Implement the `InvokeAgent` Lambda
  - Run the agent loop with a 30 s budget, validate the output with pydantic, record `AGENT_DECIDED` or `AGENT_FAILED`, and escalate on any failure without breaking the flow.
  - Tests with simulated responses: valid, invalid JSON, disallowed action, and timeout.
  - _Requirements: 4.5, 4.6_

- [ ] 5. Policy and remediation (depends on 3.1, 4.3)
- [x] 5.1 Implement `RemediationPolicy`
  - Pure function that returns `REMEDIATE`, `REMEDIATE_AND_ESCALATE`, or `ESCALATE` according to classification, `remediation`, attempts (maximum 2), elapsed minutes (maximum 20), indicated wait (maximum 10 minutes), and remaining minutes (30 or less).
  - Table-driven tests covering each criterion of requirements 5 and 6.
  - Progress (T4): `guardia_common/policy.py` (10 ordered rules, observe-only, `escalation_running`) and the `remediation_policy` Lambda, which reads everything from the Incidents table. Deployed.
  - _Requirements: 5.1, 5.3, 5.5, 5.6, 6.1_

- [x] 5.2 Implement the `RedriveOrRestart` Lambda
  - Redrive if the execution is Standard and eligible; otherwise a new execution with the original input.
  - IAM policy bounded by the `guardia:remediation=auto` tag condition.
  - Tests: redrive, relaunch, and rejection of a resource without the tag.
  - _Requirements: 5.1, 12.1, 12.4_

- [x] 5.3 Implement the `PollExecution` Lambda
  - Query the status of the recovered execution and return the final result.
  - Tests: `SUCCEEDED`, `FAILED`, and `RUNNING`.
  - _Requirements: 5.4_

- [x] 5.4 Implement the `Retry-After` wait state
  - Extract the wait time from the error cause, cap it at 600 s, and escalate if it is exceeded.
  - Progress (T4): `retry_after_seconds` reads `Retry-After`, `retryAfter`, and `retry after N s/ms`; a 429 without one waits 60 s (D26). The flow waits with `SecondsPath: $.policy.wait_seconds`.
  - Tests: valid value, missing, above the maximum.
  - _Requirements: 5.2, 5.3_

- [x] 5.5 Define the `GuardIA-Incident-Flow` state machine
  - States: `Classify` → `InvokeAgent` (if unknown) → `Policy` → `Remediate` (wait, redrive, poll, verify) or `Escalate` → `CloseIncident`.
  - Parallel escalation branch when the policy returns `REMEDIATE_AND_ESCALATE`.
  - Progress (T1): the flow is composed in `infra/stacks/flow_stack.py` from `flow_recovery.build_recovery` (T4) and `flow_escalation.build_escalation` (T5), with placeholders; `UpdateIncidentStatus` is the only status writer (idempotent, `RESOLVED` is final). Deployed; both routes verified in the test account.
  - Progress (T4): recovery branch done (`flow_recovery.py`): policy choice, Retry-After wait, RedriveOrRestart with catch, 3-minute poll loop bounded to 10 polls, RESOLVED only on SUCCEEDED, and the parallel branch for `REMEDIATE_AND_ESCALATE`. Escalation runs as its own state machine, `GuardIA-Escalation-Flow` (D25). RedriveOrRestart and PollExecution deployed; redrive and restart limited by IAM to `guardia:remediation=auto` (executions inherit the state machine tags). Verified in the test account: watchdog escalation, rejected recovery (fail-closed tag check), and the parallel branch. Pending with the simulator (T3/T7): a real redrive ending in SUCCEEDED, attempts exhausted, and the IAM deny on a process without the tag.
  - Tests: definition tests with the Step Functions TestState API for each branch.
  - _Requirements: 5.4, 5.5, 6.1, 13.1_

- [ ] 6. Escalation (depends on 5.5)
- [ ] 6.1 Implement the `ResolveOnCall` Lambda
  - Read `OnCallSchedule` for the current date in Colombia time; if missing, return only the backup and record the warning.
  - Tests: normal week, week change on Monday, and missing on-call engineer.
  - Progress: implemented and unit tested. T5 adds the environment mode (D19: `GUARDIA_ONCALL_EMAIL`, `GUARDIA_ONCALL_NAME`, `GUARDIA_ONCALL_BACKUP_EMAIL`; the table is the fallback) and deploys it in `GuardIA-Flow` (`stacks/flow_escalation.py`); `cdk synth` fails without the email. Pending: deploy and check in the test account.
  - _Requirements: 7.1, 7.4_

- [ ] 6.2 Implement the Teams webhook client
  - Post an adaptive card with mentions (`teams_user_id`) to the Workflows flow webhook; the address is read from Secrets Manager (encrypted with KMS) and is never written to logs.
  - Content: organization, process, error, actions attempted with their result, and minutes remaining until the deadline.
  - Tests: card construction, on-call mention, truncation of long fields, and HTTP error handling.
  - Progress: implemented and unit tested; T5 adds the follow-up cards (`build_status_card`: reminder, SLA breached, resolved). Pending: check in the real channel.
  - _Requirements: 7.2, 12.3_

- [ ] 6.3 Implement the `SendEscalation` Lambda
  - Retry the post up to 3 times with backoff; if it keeps failing, record `NOTIFICATION_FAILED` and raise so the DLQ alarm fires (no email backup, D21).
  - If there is no on-call engineer, mention the backup and record the configuration warning.
  - Store `teams_thread_id` in the incident.
  - Tests: successful post, failure after 3 retries with `NOTIFICATION_FAILED`, missing on-call engineer.
  - Progress: implemented and unit tested; T5 adds `mode` (`initial`, `reminder`, `sla_breached`, `resolved`) and builds the diagnosis from `IncidentEvents`. Wired in the flow: a failed first message ends the execution in `NotificationFailed`. Pending: deploy and check in the real channel.
  - _Requirements: 7.2, 7.3, 7.4_

- [ ] 6.4 Implement the `ResolveOnSuccess` Lambda
  - EventBridge rule for `SUCCEEDED` events of processes present in `ProcessRegistry`; find the escalated incident for the same process and organization and call `SendTaskSuccess` with the stored token.
  - Ignore events with no open incident and record the closure with `resolved_by=later_execution`.
  - Tests: open incident, no incident, different organization, and duplicate event.
  - Progress: implemented and unit tested (T5): `ResolveOnSuccess`, `StoreTaskToken`, the `SUCCEEDED_PATTERN` rule and the `by_resource_arn` index on `Incidents`. Pending: deploy (one index per deploy) and check with a real success after an escalation.
  - _Requirements: 8.3, 2.2_

- [ ] 6.5 Implement the reminders and SLA-breached subflow
  - Wait with a task token and a maximum of 10 minutes; on expiry, `SendReminder` posts a reminder mentioning the on-call engineer and the backup, and the cycle repeats until resolved or the deadline is reached.
  - At the deadline without resolution: mark `SLA_BREACHED`, post the alert mentioning the on-call engineer and the backup, stop the reminders, and keep waiting for the signal for up to 6 hours to record the resolution.
  - On resolution, post the resolution message in the incident thread.
  - Tests with simulated time: resolution before the first reminder, after two reminders, and SLA expiry.
  - Progress: implemented (T5): `PlanReminder` and the chain in `stacks/flow_escalation.py` (reminder loop with task token, `SLA_BREACHED`, `ClosedUnresolved` after the post-deadline wait, `RESOLVED` with the resolution message). Unit and synth tested; the chain is not yet run in Step Functions. Pending: deploy and the cases of the T5 sheet.
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
  - Post to the Teams channel reusing the client from 6.2, with its own webhook stored in Secrets Manager; 3 retries and then the failure is recorded and the alarm fires (D21).
  - Schedule the run at 06:15 (America/Bogota) with EventBridge Scheduler.
  - Tests: successful delivery, Teams failure recorded.
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
  - Webhook down: 3 retries, `NOTIFICATION_FAILED`, and the alarm email through SNS.
  - Unresolved until the deadline: `SLA_BREACHED` with an alert to on-call and backup, with no further reminders.
  - 30 minutes or less remaining: escalation in parallel with remediation.
  - _Requirements: 6.1, 6.2, 7.3, 8.2, 8.4_
