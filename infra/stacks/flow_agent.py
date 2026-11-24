"""The AI agent in the flows (T8, tasks 4.2 and 4.3 · requirements 4.5, 4.6, 12.2).

    GuardIA-Incident-Flow:
        ClassifyIncident ─► NeedsAgent? ── unknown ─► InvokeAgentClassify ─► RemediationPolicy
                                        └─ other   ───────────────────────► RemediationPolicy

    GuardIA-Escalation-Flow:
        ... ResolveOnCall ─► InvokeAgentDiagnose ($.agent) ─► first message

The agent only recommends. `InvokeAgent` writes its recommendation to the
incident, and RemediationPolicy reads it from there (rule 2) and applies all
its other rules to it. A failed agent never stops a flow: `InvokeAgent` answers
`ok=false`, and any unexpected error is caught here and the flow goes on
without the AI diagnosis (4.6).

Without `ctx.agent` (an environment with no agent), no state is added and an
`unknown` incident escalates, as before.
"""

from __future__ import annotations

from aws_cdk import aws_iam as iam
from aws_cdk import aws_stepfunctions as sfn

from components.function import GuardiaFunction
from config import AGENT_AZURE_SECRET_NAME, AZURE_OPENAI, BEDROCK

from .flow_context import FlowContext

FUNCTION_KEY = "InvokeAgent"
TIMEOUT_SECONDS = 45  # the agent's own limit is 30 s (agent-timeout-seconds), plus a margin
# Cross-region inference profile ids start with a geography: `us.amazon.nova-pro-v1:0`.
_GEOGRAPHIES = {"us", "eu", "apac", "us-gov", "global"}


def bedrock_model_arns(partition: str, region: str, account: str, model_id: str) -> list[str]:
    """The only resources the agent may invoke: the configured model, nothing else.

    A cross-region inference profile needs the profile (in this account and region) and
    the model it routes to, which can be in any region of the geography."""
    geography, _, rest = model_id.partition(".")
    if geography in _GEOGRAPHIES and "." in rest:
        return [
            f"arn:{partition}:bedrock:{region}:{account}:inference-profile/{model_id}",
            f"arn:{partition}:bedrock:*::foundation-model/{rest}",
        ]
    return [f"arn:{partition}:bedrock:{region}::foundation-model/{model_id}"]


def agent_function(ctx: FlowContext) -> GuardiaFunction | None:
    """The InvokeAgent Lambda, created once for both flows; None without an agent."""
    if ctx.agent is None:
        return None
    if FUNCTION_KEY in ctx.functions:
        return ctx.functions[FUNCTION_KEY]

    data, scope = ctx.data, ctx.scope
    agent = ctx.add_function(
        FUNCTION_KEY,
        GuardiaFunction(
            scope,
            FUNCTION_KEY,
            name="invoke_agent",
            layer=ctx.layer,
            environment={
                **data.table_env("Incidents", "IncidentEvents", "ErrorCatalog"),
                **ctx.agent.lambda_environment(),
            },
            timeout_seconds=TIMEOUT_SECONDS,
            description="GuardIA InvokeAgent (4.2, 4.3): investigates and recommends, never acts",
        ),
    )
    function = agent.function
    # Incidents: read it, write its own recommendation, and look for similar ones (index).
    data.grant_items(
        function, "Incidents", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"
    )
    data.grant_items(function, "ErrorCatalog", "dynamodb:Scan")
    data.grant_append_events(function)
    data.grant_read_thresholds(function)

    # Read-only on Step Functions: the history of the failed run and the recent runs.
    arn = f"arn:{scope.partition}:states:{scope.region}:{scope.account}"
    function.add_to_role_policy(
        iam.PolicyStatement(
            sid="ReadExecutionsOnly",
            actions=["states:GetExecutionHistory", "states:DescribeExecution"],
            resources=[f"{arn}:execution:*"],
        )
    )
    function.add_to_role_policy(
        iam.PolicyStatement(
            sid="ListRunsOnly",
            actions=["states:ListExecutions"],
            resources=[f"{arn}:stateMachine:*"],
        )
    )
    if ctx.agent.provider == AZURE_OPENAI:
        data.grant_read_secret(function, AGENT_AZURE_SECRET_NAME)
    elif ctx.agent.provider == BEDROCK:
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeConfiguredModelOnly",
                actions=["bedrock:InvokeModel"],
                resources=bedrock_model_arns(
                    scope.partition, scope.region, scope.account, ctx.agent.model or ""
                ),
            )
        )
    return agent


def invoke_agent_state(
    ctx: FlowContext, state_name: str, mode: str, next_state: sfn.IChainable
) -> sfn.IChainable | None:
    """A state that asks the agent about this incident and goes on to `next_state`,
    whatever the answer (even an unexpected error). None without an agent."""
    agent = agent_function(ctx)
    if agent is None:
        return None
    task = ctx.invoke(
        state_name,
        agent,
        {"incident_id": sfn.JsonPath.string_at("$.incident_id"), "mode": mode},
        "$.agent",
    )
    task.add_catch(next_state, errors=[sfn.Errors.ALL], result_path="$.agent_error")
    task.next(next_state)
    return task


def route_unknown_to_agent(ctx: FlowContext, recovery: sfn.IChainable) -> sfn.IChainable:
    """After ClassifyIncident: an `unknown` incident goes through the agent first (4.5)."""
    invoke = invoke_agent_state(ctx, "InvokeAgentClassify", "classify", recovery)
    if invoke is None:
        return recovery
    return (
        sfn.Choice(ctx.scope, "NeedsAgent?")
        .when(sfn.Condition.string_equals("$.classification.classification", "unknown"), invoke)
        .otherwise(recovery)
    )
