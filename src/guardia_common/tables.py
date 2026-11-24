"""DynamoDB tables of GuardIA (task 1.2 · design.md §4), defined once.

The CDK `DataStack` and the local environments (moto in tests and demos,
DynamoDB Local in Docker) both build the tables from `TABLES`, so the keys and
indexes cannot drift between AWS and local runs.

The logical name (`TableSpec.name`) is also the physical table name locally.
In AWS, CloudFormation assigns the physical name and the Lambdas receive it in
environment variables (`INCIDENTS_TABLE`, ...).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HASH = "HASH"
RANGE = "RANGE"


@dataclass(frozen=True)
class IndexSpec:
    name: str
    partition_key: str
    sort_key: str | None = None


@dataclass(frozen=True)
class TableSpec:
    name: str
    partition_key: str
    sort_key: str | None = None
    indexes: tuple[IndexSpec, ...] = ()
    ttl_attribute: str | None = None
    point_in_time_recovery: bool = False
    env_var: str = ""

    def key_attributes(self) -> list[str]:
        names = [self.partition_key, self.sort_key]
        for index in self.indexes:
            names += [index.partition_key, index.sort_key]
        return list(dict.fromkeys(n for n in names if n))  # ordered, unique


EXECUTION_ARN_INDEX = "by_execution_arn"
# ResolveOnSuccess (6.4): the incidents of one process, newest first.
RESOURCE_ARN_INDEX = "by_resource_arn"

TABLES: dict[str, TableSpec] = {
    spec.name: spec
    for spec in (
        TableSpec(
            name="ProcessRegistry",
            partition_key="resource_arn",
            env_var="PROCESS_REGISTRY_TABLE",
        ),
        TableSpec(
            name="Incidents",
            partition_key="incident_id",
            indexes=(
                IndexSpec(EXECUTION_ARN_INDEX, "execution_arn"),
                IndexSpec(RESOURCE_ARN_INDEX, "resource_arn", "opened_at"),
            ),
            env_var="INCIDENTS_TABLE",
        ),
        TableSpec(
            name="IncidentEvents",
            partition_key="incident_id",
            sort_key="sk",
            ttl_attribute="expires_at",
            point_in_time_recovery=True,
            env_var="INCIDENT_EVENTS_TABLE",
        ),
        TableSpec(
            name="ErrorCatalog",
            partition_key="process_type",
            sort_key="error_pattern",
            env_var="ERROR_CATALOG_TABLE",
        ),
        TableSpec(
            name="OnCallSchedule",
            partition_key="week_start",
            env_var="ONCALL_SCHEDULE_TABLE",
        ),
    )
}


def _key_schema(partition_key: str, sort_key: str | None) -> list[dict]:
    schema = [{"AttributeName": partition_key, "KeyType": HASH}]
    if sort_key:
        schema.append({"AttributeName": sort_key, "KeyType": RANGE})
    return schema


def create_table_request(spec: TableSpec) -> dict[str, Any]:
    """`CreateTable` parameters for a local table (every key is a string)."""
    request: dict[str, Any] = {
        "TableName": spec.name,
        "KeySchema": _key_schema(spec.partition_key, spec.sort_key),
        "AttributeDefinitions": [
            {"AttributeName": name, "AttributeType": "S"} for name in spec.key_attributes()
        ],
        "BillingMode": "PAY_PER_REQUEST",
    }
    if spec.indexes:
        request["GlobalSecondaryIndexes"] = [
            {
                "IndexName": index.name,
                "KeySchema": _key_schema(index.partition_key, index.sort_key),
                "Projection": {"ProjectionType": "ALL"},
            }
            for index in spec.indexes
        ]
    return request


def create_local_tables(dynamodb: Any, names: list[str] | None = None) -> dict[str, Any]:
    """Creates the tables on a boto3 DynamoDB resource (moto or DynamoDB Local)
    and returns them by logical name. Existing tables are reused."""
    tables = {}
    existing = {table.name for table in dynamodb.tables.all()}
    for name in names or list(TABLES):
        spec = TABLES[name]
        if spec.name in existing:
            tables[name] = dynamodb.Table(spec.name)
            continue
        table = dynamodb.create_table(**create_table_request(spec))
        table.wait_until_exists()
        if spec.ttl_attribute:
            dynamodb.meta.client.update_time_to_live(
                TableName=spec.name,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": spec.ttl_attribute},
            )
        tables[name] = table
    return tables
