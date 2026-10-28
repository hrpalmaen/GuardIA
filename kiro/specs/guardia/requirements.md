# Requirements Document — GuardIA

## Introduction

GuardIA is an AI agent that autonomously monitors Emtelco's Genesys data-extraction processes, acts on the failures it already knows how to fix, and escalates to the on-call engineer, with a mention in Microsoft Teams, only when human judgment is required.

Today, the Data Engineering team runs processes that query the Genesys APIs per organization: a **master process** (once a day, before the nightly cross-reference jobs, handling ~14 master tables per run) and a **conversations process** (every 2 hours). Each process is launched by an Amazon EventBridge cron rule. The data must be available before **6:00 a.m. (Colombia time)** to meet the SLA (Service Level Agreement) with the business. In the worst case, a failure happens around 4:00 a.m., leaving about 60 minutes to detect it, fix it, and have the data ready. Today this monitoring depends on one person who rotates weekly and checks manually during the early hours.

GuardIA operates on three levels: **Level 0** (continuous monitoring), **Level 1** (autonomous first aid), and **Level 2** (intelligent escalation).

## Assumptions

1. Each extraction process is an AWS Step Functions state machine launched by EventBridge. If it is a Standard workflow, GuardIA recovers it with redrive (resumes from the point of failure); otherwise it restarts it with its original input.
2. Processes marked for automatic remediation are idempotent: running them again does not duplicate data. The process owner confirms this by tagging it with `guardia:remediation=auto`.
3. 429 errors (rate limit) are already retried inside the process, using the wait time the API indicates. GuardIA acts only when the execution ends in a failed state.
4. A 404 "no data" response that the process handles internally does not make the execution fail. This is why GuardIA is triggered by the final execution status and not by individual HTTP codes.
5. There is one on-call engineer and one backup per week. The default deadline is 06:00 (America/Bogota) and is configurable per process.
6. Escalation is posted to a Teams channel through a Workflows (Power Automate) flow with a webhook, which publishes the message in the team channel. This route does not require Microsoft Graph permissions or administrator consent.
7. Teams does not report whether a message was delivered or read. For that reason GuardIA does not depend on an acknowledgment: it keeps sending reminders until the incident is resolved.
8. Operational prerequisite: the on-call engineer has the Teams mobile app with mention notifications enabled during their on-call week, including outside working hours.
9. To close an escalated incident, the on-call engineer relaunches the process: GuardIA considers it resolved when that execution ends in `SUCCEEDED`.

## Requirements

### Requirement 1 — Tag-based process discovery

**User Story:** As a data engineer, I want GuardIA to find the processes it must monitor on its own from tags, so that I do not have to maintain manual lists when processes or organizations are added.

#### Acceptance Criteria
1. WHEN 15 minutes have elapsed since the last discovery THEN the system SHALL list all resources tagged `guardia:monitor=true` and update the process registry.
2. WHEN a tagged resource lacks the other three mandatory tags (`guardia:process-type`, `guardia:organization`, `guardia:remediation`) THEN the system SHALL exclude it from monitoring and record a configuration warning.
3. WHEN a new process is tagged THEN the system SHALL include it in monitoring in the next cycle, with no code changes.
4. IF `guardia:remediation` has a value other than `auto` or `escalate-only` THEN the system SHALL treat it as `escalate-only`.

### Requirement 2 — Failed execution detection (Level 0)

**User Story:** As the on-call data engineer, I want GuardIA to detect the moment an execution fails, so that I do not depend on manually checking its status during the early hours.

#### Acceptance Criteria
1. WHEN an execution of a monitored process ends in `FAILED`, `TIMED_OUT`, or `ABORTED` THEN the system SHALL open an incident within 2 minutes.
2. WHEN an execution ends in `SUCCEEDED` THEN the system SHALL NOT create an incident or send a notification.
3. WHEN an incident is opened THEN the system SHALL record the organization, process type, execution identifier, error cause, and applicable SLA deadline.
4. IF an incident already exists for the same execution THEN the system SHALL NOT create a duplicate.

### Requirement 3 — Missed or delayed execution detection

**User Story:** As a data engineer, I want GuardIA to also detect processes that did not start or do not finish, so that I do not find out late about failures that produce no explicit error.

#### Acceptance Criteria
1. WHEN an expected execution has not started within 10 minutes after its scheduled time THEN the system SHALL open an incident of type `MISSED_RUN` within 5 minutes after that tolerance expires.
2. WHEN an execution stays in `RUNNING` for more than twice the median duration of its last 14 executions THEN the system SHALL open an incident of type `STUCK_RUN`.
3. IF the process has fewer than 5 executions of history THEN the system SHALL use 50% of its periodicity as the maximum duration.

### Requirement 4 — Incident classification

**User Story:** As a data engineer, I want GuardIA to distinguish between failures it can fix on its own and failures that need my judgment, so that I am contacted only when I am truly needed.

#### Acceptance Criteria
1. WHEN an incident is opened THEN the system SHALL first classify it using the deterministic error catalog, by process type and cause.
2. WHEN the cause is a 429 error THEN the system SHALL classify it as `auto_recoverable`.
3. WHEN the cause is a 403 or authentication error THEN the system SHALL classify it as `requires_escalation`.
4. WHEN the cause is a 404 that made the execution fail, or the incident is `MISSED_RUN` THEN the system SHALL classify it as `requires_escalation`.
5. WHEN the cause is not in the catalog THEN the system SHALL consult the AI agent to obtain a suggested classification and a diagnosis summary.
6. IF the agent does not respond within 30 seconds, or returns an action outside the allowed list THEN the system SHALL classify the incident as `requires_escalation`.

### Requirement 5 — Autonomous remediation (Level 1)

**User Story:** As a data engineer, I want GuardIA to resolve known, low-risk failures on its own, so that I am not woken up for incidents with a standard fix.

#### Acceptance Criteria
1. WHEN an incident is `auto_recoverable` AND the process has `guardia:remediation=auto` THEN the system SHALL recover the execution through redrive, or with a new execution using the original input if redrive does not apply.
2. WHEN the cause is a 429 with an indicated wait time THEN the system SHALL wait that time, up to a maximum of 10 minutes, before recovering the execution.
3. IF the indicated wait time exceeds 10 minutes THEN the system SHALL escalate the incident.
4. WHEN a recovery action is executed THEN the system SHALL follow the resulting execution to its final status and mark the incident as resolved only if it ends in `SUCCEEDED`.
5. IF recovery fails 2 times, or 20 minutes pass since opening without resolution THEN the system SHALL escalate the incident.
6. IF the process has `guardia:remediation=escalate-only` THEN the system SHALL NOT execute any action and SHALL escalate directly.

### Requirement 6 — Parallel escalation based on remaining time

**User Story:** As a data engineer, I want GuardIA not to spend the little time left trying on its own, so that we meet the 6:00 a.m. deadline.

#### Acceptance Criteria
1. WHEN 30 minutes or less remain until the deadline at the moment the incident is opened THEN the system SHALL escalate immediately, in parallel with the remediation attempt.
2. WHEN the deadline passes with the incident unresolved THEN the system SHALL mark it as `SLA_BREACHED` and post an alert that mentions the on-call engineer and the backup.

### Requirement 7 — Escalation through Microsoft Teams (Level 2)

**User Story:** As the on-call data engineer, I want to receive a complete diagnosis in Microsoft Teams, with a direct mention, when my judgment is needed, so that I reach the problem knowing what happened and what was tried without having to investigate from scratch.

#### Acceptance Criteria
1. WHEN an incident must be escalated THEN the system SHALL identify the on-call engineer and the backup according to the rotation calendar for the current date (Colombia time).
2. WHEN the message is posted THEN the system SHALL mention the on-call engineer and include the organization, process, error, actions attempted with their result, and minutes remaining until the deadline.
3. IF posting to Teams returns an error THEN the system SHALL retry up to 3 times and, if it keeps failing, SHALL send the same content by email to the on-call engineer, the backup, and the team distribution list.
4. IF there is no on-call engineer registered for the date THEN the system SHALL mention the backup and record a configuration warning.

### Requirement 8 — Reminders and automatic closure

**User Story:** As the team lead, I want GuardIA to keep insisting until the incident is resolved, without depending on someone acknowledging in a chat, so that no alert is forgotten.

#### Acceptance Criteria
1. WHEN 10 minutes pass since the escalation was posted with the incident unresolved THEN the system SHALL post a reminder that mentions the on-call engineer and the backup.
2. WHILE the incident remains unresolved and the deadline has not passed THEN the system SHALL repeat the reminder every 10 minutes.
3. WHEN a later execution of the same process and organization ends in `SUCCEEDED` THEN the system SHALL mark the incident as `RESOLVED`, stop the reminders, and post a resolution message in the same thread.
4. WHEN the deadline passes THEN the system SHALL stop the periodic reminders and apply the SLA-breached criterion defined in Requirement 6.

### Requirement 9 — Grouping of mass incidents

**User Story:** As the on-call engineer, I want to receive a single message when one cause takes down several organizations at once, so that I do not receive dozens of identical alerts.

#### Acceptance Criteria
1. WHEN 3 or more incidents with the same cause are opened within a 5-minute window THEN the system SHALL send a single grouped notification listing the affected organizations.
2. WHEN incidents are grouped THEN the system SHALL keep handling each incident individually in its remediation and its record.

### Requirement 10 — Traceability

**User Story:** As the team lead, I want a complete and tamper-proof record of what GuardIA did, so that I can audit every incident.

#### Acceptance Criteria
1. WHEN any event in an incident's lifecycle occurs (opening, classification, action, result, notification, reminder, closure) THEN the system SHALL record it with its timestamp.
2. THE system SHALL retain the records for at least 13 months.
3. THE system SHALL prevent any role from editing or deleting incident records.

### Requirement 11 — Morning summary

**User Story:** As a data engineer, I want to see every morning what happened overnight, so that I have full visibility without having sacrificed my rest.

#### Acceptance Criteria
1. WHEN it is 06:15 a.m. (Colombia time, configurable) THEN the system SHALL generate the summary of what happened since 6:00 p.m. of the previous day.
2. WHEN the summary is generated THEN the system SHALL include processes executed successfully, incidents by level, GuardIA actions with their result, escalations with their resolution time, `SLA_BREACHED` incidents, and configuration warnings.
3. WHEN the summary is ready THEN the system SHALL post it to the team's Microsoft Teams channel.
4. IF posting to Teams fails after 3 attempts THEN the system SHALL send it by email to the team distribution list.

### Requirement 12 — Security and action limits

**User Story:** As the platform owner, I want GuardIA to be able to do only what is strictly defined, so that an agent error cannot cause damage.

#### Acceptance Criteria
1. THE system SHALL grant permission to redrive or relaunch only on resources with `guardia:remediation=auto`.
2. THE system SHALL limit the AI agent to classifying and summarizing: the agent does not execute actions on the infrastructure.
3. THE system SHALL encrypt the Teams webhook addresses and other integration credentials with a managed key.
4. WHEN a process does not have `guardia:remediation=auto` THEN the system SHALL NOT execute any recovery action on it.

### Requirement 13 — Timing, availability, and extensibility

**User Story:** As a data engineer, I want GuardIA to be reliable and extensible to other critical processes, so that the model is useful beyond Genesys.

#### Acceptance Criteria
1. THE system SHALL meet this time budget from an explicit failure: detection in 2 minutes or less, autonomous remediation in 20 minutes or less, and escalation message posted in 1 minute or less after deciding to escalate.
2. WHEN GuardIA does not emit its heartbeat signal for 10 minutes THEN the system SHALL send an email to the team distribution list and an alert to Teams, through a path independent of the main flow.
3. THE system SHALL operate 24/7 on managed AWS services.
4. WHEN a new process, from Genesys or another source, needs to be monitored THEN the system SHALL require only that tags be added to it, with no code changes.

## Pilot Success Indicators

- Zero undetected failures: 100% of failed executions generate an incident within 2 minutes or less.
- Time to first action (remediation or message sent): median of 5 minutes or less.
- On-time resolution: 90% of escalated incidents reach `RESOLVED` before the SLA deadline.
- Zero `SLA_BREACHED` incidents without a posted alert during the pilot.

## Out of Scope for This Version

- Phone call, WhatsApp, and SMS: the initial version escalates only through Microsoft Teams. Other channels can be added later.
- Delivery or read confirmation of the message: Teams does not expose it through a webhook, so it is replaced with reminders.
- Autonomous remediation of errors outside the catalog: they are always escalated.
- Modifying the extraction or transformation logic of the existing processes.
