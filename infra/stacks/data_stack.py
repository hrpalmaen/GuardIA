from collections.abc import Mapping

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from config import (
    DEFAULT_THRESHOLDS,
    SSM_PREFIX,
    TEAMS_ALERTS_SECRET_NAME,
    TEAMS_SUMMARY_SECRET_NAME,
)
from guardia_common.tables import TABLES, TableSpec

AUDIT_TABLE = "IncidentEvents"
# No principal, GuardIA's or a person's, may change or delete an audit event
# (requirement 10.3). TTL expiry is done by DynamoDB and is not affected.
AUDIT_DENIED_ACTIONS = ["dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"]
_READ_ACTIONS = {"dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable"}


def _attribute(name: str | None) -> dynamodb.Attribute | None:
    return dynamodb.Attribute(name=name, type=dynamodb.AttributeType.STRING) if name else None


class DataStack(Stack):
    """DynamoDB, KMS, Secrets Manager, and SSM.

    The tables come from `guardia_common.tables.TABLES`, the same definition the
    local environments use. `thresholds` are the SSM values of the environment
    (`EnvironmentConfig.thresholds`); the defaults apply when none are given.

    `IncidentEvents` is append-only: its resource policy denies updates and
    deletes to everyone, and Lambdas get write access only through
    `grant_append_events` (PutItem).

    `retain_data` decides what deleting the stack does to the key, tables, and
    secrets: True keeps them (RETAIN, production), False deletes them (DESTROY,
    test account). A KMS key is never deleted at once: AWS waits 7 days (dev)
    or 30 days (prod) and the deletion can be cancelled in that time.

    The webhook secrets are created with a generated placeholder and encrypted
    with the data key (12.3). The real address is set by hand once:
    `aws secretsmanager put-secret-value --secret-id <name> --secret-string ...`.
    CloudFormation does not overwrite it on later deploys.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        thresholds: Mapping[str, str] = DEFAULT_THRESHOLDS,
        retain_data: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.removal_policy = RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY

        self.key = kms.Key(
            self,
            "DataKey",
            alias="alias/guardia-data",
            enable_key_rotation=True,
            removal_policy=self.removal_policy,
            pending_window=Duration.days(30 if retain_data else 7),
            description="GuardIA tables and secrets",
        )

        self.tables: dict[str, dynamodb.Table] = {
            name: self._table(spec) for name, spec in TABLES.items()
        }

        self.parameters: dict[str, ssm.StringParameter] = {
            name: ssm.StringParameter(
                self,
                f"Param-{name}",
                parameter_name=f"{SSM_PREFIX}/{name}",
                string_value=value,
                description=f"GuardIA threshold: {name}",
            )
            for name, value in thresholds.items()
        }

        self.secrets: dict[str, secretsmanager.Secret] = {
            secret_name: secretsmanager.Secret(
                self,
                construct_name,
                secret_name=secret_name,
                description=f"GuardIA {purpose} Teams webhook (set the value by hand)",
                encryption_key=self.key,
                removal_policy=self.removal_policy,
            )
            for construct_name, secret_name, purpose in (
                ("TeamsAlertsWebhook", TEAMS_ALERTS_SECRET_NAME, "alerts"),
                ("TeamsSummaryWebhook", TEAMS_SUMMARY_SECRET_NAME, "summary"),
            )
        }

        # Table names for scripts (for example load_error_catalog.py).
        for name, table in self.tables.items():
            CfnOutput(self, f"{name}TableName", value=table.table_name)

    def table_env(self, *names: str) -> dict[str, str]:
        """The `<TABLE>_TABLE` environment variables a handler reads."""
        return {TABLES[name].env_var: self.tables[name].table_name for name in names}

    def grant_items(self, grantee: iam.IGrantable, name: str, *actions: str) -> iam.Grant:
        """Exactly `actions` on one table (and its indexes), plus the key use
        they need: decrypt for reads, encrypt and decrypt for writes.
        `IncidentEvents` only through `grant_append_events`."""
        if name == AUDIT_TABLE:
            raise ValueError("use grant_append_events for IncidentEvents")
        if set(actions) <= _READ_ACTIONS:
            self.key.grant_decrypt(grantee)
        else:
            self.key.grant_encrypt_decrypt(grantee)
        return self.tables[name].grant(grantee, *actions)

    def grant_read_thresholds(self, grantee: iam.IGrantable) -> iam.Grant:
        """Read the SSM thresholds by path (`guardia_common.config.load_thresholds`)."""
        path = f"arn:{self.partition}:ssm:{self.region}:{self.account}:parameter{SSM_PREFIX}"
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=["ssm:GetParametersByPath"],
            resource_arns=[path, f"{path}/*"],
        )

    def grant_read_secret(self, grantee: iam.IGrantable, secret_name: str) -> iam.Grant:
        """Read one secret's value (`GetSecretValue`) and decrypt it with the data key.

        Both statements go into the grantee's own policy. `Secret.grant_read`
        would write the key policy of this stack with the grantee's role, and
        the flow stack already depends on this one (a dependency cycle).
        """
        self.key.grant_decrypt(grantee)
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=["secretsmanager:GetSecretValue"],
            resource_arns=[self.secrets[secret_name].secret_arn],
        )

    def grant_read_events(self, grantee: iam.IGrantable) -> iam.Grant:
        """Read the audit events of an incident (`Query`). Cannot change them."""
        self.key.grant_decrypt(grantee)
        return self.tables[AUDIT_TABLE].grant(grantee, "dynamodb:Query")

    def grant_append_events(self, grantee: iam.IGrantable) -> iam.Grant:
        """Write access to `IncidentEvents` that can only add events."""
        table = self.tables[AUDIT_TABLE]
        self.key.grant_encrypt_decrypt(grantee)
        return table.grant(grantee, "dynamodb:PutItem", "dynamodb:DescribeTable")

    def _table(self, spec: TableSpec) -> dynamodb.Table:
        policy = _append_only_policy() if spec.name == AUDIT_TABLE else None
        table = dynamodb.Table(
            self,
            spec.name,
            resource_policy=policy,
            partition_key=_attribute(spec.partition_key),
            sort_key=_attribute(spec.sort_key),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.key,
            time_to_live_attribute=spec.ttl_attribute,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=spec.point_in_time_recovery
            ),
            removal_policy=self.removal_policy,
        )
        for index in spec.indexes:
            table.add_global_secondary_index(
                index_name=index.name,
                partition_key=_attribute(index.partition_key),
                sort_key=_attribute(index.sort_key),
                projection_type=dynamodb.ProjectionType.ALL,
            )
        return table


def _append_only_policy() -> iam.PolicyDocument:
    return iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                sid="AppendOnly",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=AUDIT_DENIED_ACTIONS,
                resources=["*"],
            )
        ]
    )
