"""Recovery branch of GuardIA-Incident-Flow (Level 1) · owned by T4.

T1 leaves a placeholder. T4 replaces the body of `build_recovery` with
RemediationPolicy, the Retry-After wait, RedriveOrRestart, the poll loop, and
the RESOLVED transition, and routes to `escalation` whenever the policy
decides to escalate. See docs/analisis/tareas-fase-b-equipo.md, T4.
"""

from __future__ import annotations

from aws_cdk import aws_stepfunctions as sfn

from .flow_context import FlowContext


def build_recovery(ctx: FlowContext, escalation: sfn.IChainable) -> sfn.IChainable:
    del escalation  # used by T4: the policy's ESCALATE and the recovery failures go there
    return sfn.Pass(
        ctx.scope,
        "RecoveryPlaceholder",
        comment="T4: RemediationPolicy and the recovery branch replace this state",
    ).next(sfn.Succeed(ctx.scope, "RecoveryPending"))
