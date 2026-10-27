import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from config import DEFAULT_THRESHOLDS, SSM_PREFIX
from stacks.data_stack import DataStack


@pytest.fixture
def template() -> Template:
    app = cdk.App()
    return Template.from_stack(DataStack(app, "GuardIA-Data-Test"))


@pytest.mark.req("13.3")
def test_data_stack_synthesizes(template: Template) -> None:
    assert template.to_json()["Resources"]


@pytest.mark.req("13.3")
def test_every_threshold_is_an_ssm_parameter(template: Template) -> None:
    template.resource_count_is("AWS::SSM::Parameter", len(DEFAULT_THRESHOLDS))
    for name, value in DEFAULT_THRESHOLDS.items():
        template.has_resource_properties(
            "AWS::SSM::Parameter",
            {"Name": f"{SSM_PREFIX}/{name}", "Type": "String", "Value": value},
        )


def test_default_deadline_matches_design() -> None:
    assert DEFAULT_THRESHOLDS["deadline-local"] == "06:00"
    assert DEFAULT_THRESHOLDS["timezone"] == "America/Bogota"
