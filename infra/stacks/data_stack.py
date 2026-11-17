from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from config import DEFAULT_THRESHOLDS, SSM_PREFIX
from guardia_common.tables import TABLES, TableSpec


def _attribute(name: str | None) -> dynamodb.Attribute | None:
    return dynamodb.Attribute(name=name, type=dynamodb.AttributeType.STRING) if name else None


class DataStack(Stack):
    """DynamoDB, KMS, Secrets Manager, and SSM.

    The tables come from `guardia_common.tables.TABLES`, the same definition the
    local environments use. Secrets are added with the escalation tasks (6.x).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.key = kms.Key(
            self,
            "DataKey",
            alias="alias/guardia-data",
            enable_key_rotation=True,
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
            for name, value in DEFAULT_THRESHOLDS.items()
        }

    def _table(self, spec: TableSpec) -> dynamodb.Table:
        table = dynamodb.Table(
            self,
            spec.name,
            partition_key=_attribute(spec.partition_key),
            sort_key=_attribute(spec.sort_key),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.key,
            time_to_live_attribute=spec.ttl_attribute,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=spec.point_in_time_recovery
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        for index in spec.indexes:
            table.add_global_secondary_index(
                index_name=index.name,
                partition_key=_attribute(index.partition_key),
                sort_key=_attribute(index.sort_key),
                projection_type=dynamodb.ProjectionType.ALL,
            )
        return table
