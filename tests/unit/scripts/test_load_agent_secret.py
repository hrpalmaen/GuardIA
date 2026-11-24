"""`scripts/load_agent_secret.py`: validates the variables and never prints a value."""

from __future__ import annotations

import json

import pytest
from load_agent_secret import SecretInputError, build_secret_string, main

KEY = "az-key-SUPER-SECRET-0123456789"
ENVIRONMENT = {
    "AZURE_OPENAI_ENDPOINT": "https://my-resource.openai.azure.com",
    "OPENAI_API_VERSION": "2025-01-01",
    "AZURE_API_MODEL": "gpt-41-mini",
    "AZURE_OPENAI_API_KEY": KEY,
}


def test_the_secret_has_the_four_fields_the_adapter_reads():
    secret = json.loads(build_secret_string({**ENVIRONMENT, "AZURE_API_MODEL": " gpt-41-mini "}))

    assert secret == {
        "endpoint": "https://my-resource.openai.azure.com",
        "api_version": "2025-01-01",
        "deployment": "gpt-41-mini",
        "api_key": KEY,
    }


@pytest.mark.parametrize("variable", sorted(ENVIRONMENT))
def test_a_missing_variable_is_named_but_no_value_is_shown(variable):
    environment = {k: v for k, v in ENVIRONMENT.items() if k != variable}

    with pytest.raises(SecretInputError, match=variable) as error:
        build_secret_string(environment)

    assert KEY not in str(error.value)


def test_the_endpoint_must_be_https():
    with pytest.raises(SecretInputError, match="https"):
        build_secret_string({**ENVIRONMENT, "AZURE_OPENAI_ENDPOINT": "http://x"})


def test_dry_run_validates_and_writes_nothing(capsys):
    assert main(["--dry-run"], ENVIRONMENT) == 0

    output = capsys.readouterr()
    assert KEY not in output.out + output.err and "my-resource" not in output.out + output.err


def test_a_bad_environment_exits_with_an_error_and_prints_no_value(capsys):
    assert main(["--dry-run"], {**ENVIRONMENT, "AZURE_OPENAI_API_KEY": ""}) == 1

    output = capsys.readouterr()
    assert "AZURE_OPENAI_API_KEY" in output.err and "my-resource" not in output.err
