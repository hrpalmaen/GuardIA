import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from components.function import LAYER_REQUIREMENTS, GuardiaFunction, shared_layer
from config import SSM_PREFIX

# Skip building the layer: synth tests must not run pip or Docker.
NO_BUNDLING = {"aws:cdk:bundling-stacks": []}


@pytest.fixture
def stack() -> cdk.Stack:
    return cdk.Stack(cdk.App(context=NO_BUNDLING), "FunctionTest")


def test_function_uses_the_shared_runtime_layer_and_json_logs(stack: cdk.Stack) -> None:
    layer = shared_layer(stack)
    GuardiaFunction(stack, "Classify", name="classify_incident", layer=layer,
                    environment={"INCIDENTS_TABLE": "t"})  # fmt: skip
    template = Template.from_stack(stack)

    template.has_resource_properties("AWS::Lambda::Function", {
        "Runtime": "python3.12",
        "Architectures": ["arm64"],
        "Handler": "handler.handler",
        "Layers": [Match.any_value()],
        "LoggingConfig": Match.object_like({"LogFormat": "JSON"}),
        "DeadLetterConfig": {"TargetArn": Match.any_value()},
        "Environment": {"Variables": {"GUARDIA_SSM_PREFIX": SSM_PREFIX, "INCIDENTS_TABLE": "t"}},
    })  # fmt: skip
    template.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})
    template.has_resource_properties("AWS::SQS::Queue", {"SqsManagedSseEnabled": True})
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 2})


def test_layer_targets_python_312_on_arm64(stack: cdk.Stack) -> None:
    shared_layer(stack)
    Template.from_stack(stack).has_resource_properties("AWS::Lambda::LayerVersion", {
        "CompatibleRuntimes": ["python3.12"],
        "CompatibleArchitectures": ["arm64"],
    })  # fmt: skip


def test_layer_pins_the_runtime_dependencies() -> None:
    names = {req.split("==")[0] for req in LAYER_REQUIREMENTS}
    assert names == {"pydantic", "croniter", "python-dateutil"}
    dateutil = next(r for r in LAYER_REQUIREMENTS if r.startswith("python-dateutil"))
    assert int(dateutil.split("==")[1].split(".")[0]) < 3  # botocore needs <3
    assert all("==" in req for req in LAYER_REQUIREMENTS)


def test_unknown_lambda_folder_fails_at_synth(stack: cdk.Stack) -> None:
    with pytest.raises(ValueError, match="no Lambda at src/lambdas/nope"):
        GuardiaFunction(stack, "Nope", name="nope", layer=shared_layer(stack))
