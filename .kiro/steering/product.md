---
inclusion: always
---

# Product — GuardIA

GuardIA is an AI agent that monitors Emtelco's Genesys data-extraction processes, fixes the failures it already knows how to fix, and escalates to the on-call engineer in Microsoft Teams only when human judgment is required.

## Context

- The extraction processes are AWS Step Functions state machines launched by EventBridge cron rules, one per organization:
  - **Master process**: once a day, before the nightly cross-reference jobs (~14 master tables per run).
  - **Conversations process**: every 2 hours.
- The data must be available before **06:00 (America/Bogota)** to meet the business SLA. In the worst case a failure happens around 04:00, leaving about 60 minutes to detect, fix, and deliver.
- Today, one engineer who rotates weekly checks the processes by hand during the early hours. GuardIA replaces that manual check.

## Operating levels

| Level | Name | What GuardIA does |
|---|---|---|
| 0 | Continuous monitoring | Discovers processes by tags, detects failed, missed, and stuck executions |
| 1 | Autonomous first aid | Recovers known failures (redrive or relaunch) within fixed limits |
| 2 | Intelligent escalation | Mentions the on-call engineer in Teams with a full diagnosis and keeps reminding until resolved |

## Core principle

**The AI agent recommends, a deterministic policy decides.** The Bedrock agent only classifies and summarizes errors outside the catalog. Which action runs, how many times, and within what time limits is decided by Step Functions and `RemediationPolicy`. A model error must never become an action on the infrastructure.

## Non-negotiable rules

- GuardIA never modifies the extraction or transformation logic of the existing processes.
- Automatic remediation only on processes tagged `guardia:remediation=auto`; anything else is escalated.
- Errors outside the catalog are never remediated autonomously.
- The incident record is append-only and kept for at least 13 months.
- Escalation goes through Teams only (webhook to a Workflows flow), with SES email as backup. No phone, WhatsApp, or SMS in this version.

## Time budget (from an explicit failure)

- Detection: ≤ 2 minutes.
- Autonomous remediation: ≤ 20 minutes, at most 2 attempts, wait capped at 10 minutes.
- Escalation message: ≤ 1 minute after deciding to escalate.
- 30 minutes or less until the deadline: escalate in parallel with remediation.
- Reminders every 10 minutes until resolved or until the deadline.

## Source of truth

The full specification lives in `.kiro/specs/guardia/` (`requirements.md`, `design.md`, `tasks.md`). If this file and the spec disagree, the spec wins.
