"""Escalation branch of GuardIA-Incident-Flow (Level 2) · owned by T5.

T1 leaves a placeholder. T5 replaces the body of `build_escalation` with
ESCALATED, ResolveOnCall, SendEscalation, the reminder loop with the task
token, SLA_BREACHED, and the resolution. See
docs/analisis/tareas-fase-b-equipo.md, T5.
"""

from __future__ import annotations

from aws_cdk import aws_stepfunctions as sfn

from .flow_context import FlowContext


def build_escalation(ctx: FlowContext) -> sfn.IChainable:
    return sfn.Pass(
        ctx.scope,
        "EscalationPlaceholder",
        comment="T5: the escalation branch replaces this state",
    ).next(sfn.Succeed(ctx.scope, "EscalationPending"))
