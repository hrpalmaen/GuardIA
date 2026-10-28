from aws_cdk import Stack
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from config import DEFAULT_THRESHOLDS, SSM_PREFIX


class DataStack(Stack):
    """DynamoDB, KMS, Secrets Manager, and SSM. Tables are added in task 1.2."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

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
