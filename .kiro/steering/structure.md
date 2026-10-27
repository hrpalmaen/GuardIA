---
inclusion: always
---

# Project Structure — GuardIA

Monorepo with infrastructure, shared library, Lambdas, agent definition, and tests. Folders marked *(task 1.1)* are created when that task runs.

```
GuardIA/
├── .kiro/
│   ├── specs/guardia/            # requirements.md, design.md, tasks.md
│   └── steering/                 # product.md, tech.md, structure.md
├── docs/
│   ├── architecture/             # architecture diagrams
│   └── runbooks/                 # tagging guide, on-call guide
├── infra/                        # (task 1.1) AWS CDK app
│   ├── app.py
│   ├── config.py                 # default thresholds published to SSM
│   ├── stacks/
│   │   ├── data_stack.py             # DynamoDB, KMS, Secrets Manager, SSM
│   │   ├── detection_stack.py        # discovery, EventBridge rules, watchdog
│   │   ├── incident_flow_stack.py    # GuardIA-Incident-Flow and remediation Lambdas
│   │   ├── escalation_stack.py       # Teams, SES, ResolveOnSuccess
│   │   ├── observability_stack.py    # heartbeat, alarms, DLQs, dashboard
│   │   └── synthetic_test_stack.py   # state machines that fail on demand
│   └── statemachines/
│       └── incident_flow.asl.json
├── src/
│   ├── guardia_common/           # (task 1.1) shared library
│   │   ├── models.py                 # Incident, IncidentEvent, AgentDecision, ProcessRecord
│   │   ├── policy.py                 # RemediationPolicy (pure function)
│   │   ├── schedule.py               # expected executions, median duration
│   │   ├── audit.py                  # append-only repository and event decorator
│   │   ├── teams_client.py           # adaptive cards with mentions
│   │   └── logger.py
│   ├── lambdas/                  # (task 1.1) one folder per Lambda, each with handler.py
│   │   ├── discover_processes/
│   │   ├── open_incident/
│   │   ├── missed_run_watchdog/
│   │   ├── classify_incident/
│   │   ├── invoke_agent/
│   │   ├── redrive_or_restart/
│   │   ├── poll_execution/
│   │   ├── resolve_on_call/
│   │   ├── send_escalation/
│   │   ├── send_reminder/
│   │   ├── resolve_on_success/
│   │   ├── group_incidents/
│   │   ├── daily_summary/
│   │   └── heartbeat_alert/
│   └── agent/                    # Bedrock agent: instructions.md, output_schema.json, examples.json
├── data/                         # error_catalog.json, oncall_schedule.sample.json
├── scripts/                      # load_error_catalog.py and other idempotent loaders
├── tests/
│   ├── unit/                     # pytest + moto, mirrors src/
│   ├── infra/                    # CDK template and IAM assertions
│   ├── agent/fixtures/           # real and adversarial error causes
│   └── integration/              # end-to-end scenarios (tasks 9.2 to 9.4)
├── cdk.json
├── pyproject.toml
├── .gitignore
└── README.md
```

## Rules

- **One folder per Lambda** in `src/lambdas/`, named in `snake_case` after the component in `design.md` (`OpenIncident` → `open_incident`). The entry point is always `handler.py` with a `handler(event, context)` function.
- **Handlers are thin.** They parse the event, call `guardia_common`, and return. Business rules (policy, schedules, classification) live in `guardia_common`.
- **`RemediationPolicy` is a library function**, not a Lambda. The state machine calls it through the Lambda that needs it.
- **Stacks are split by level** so each part can be deployed and demonstrated on its own. Cross-stack references go through stack outputs or constructor parameters, never hardcoded ARNs.
- **Tests mirror the source**: `src/lambdas/open_incident/handler.py` → `tests/unit/lambdas/test_open_incident.py`; `src/guardia_common/policy.py` → `tests/unit/guardia_common/test_policy.py`.
- **Seed data is JSON in `data/`** and is loaded only through idempotent scripts in `scripts/`. Never put real on-call names, emails, or Teams IDs in the repo; use the `.sample.json` file.
- **Specs stay in `.kiro/specs/guardia/`.** When a design decision changes, update `design.md` in the same change.
