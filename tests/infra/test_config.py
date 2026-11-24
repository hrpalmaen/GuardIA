import pytest

from config import (
    DEFAULT_THRESHOLDS,
    ENVIRONMENTS,
    EnvironmentConfig,
    UnknownEnvironmentError,
    environment,
)


def test_environment_is_required() -> None:
    with pytest.raises(UnknownEnvironmentError, match="-c env=dev"):
        environment(None)


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(UnknownEnvironmentError, match="unknown environment 'qa'"):
        environment("qa")


def test_production_starts_observe_only_and_without_the_simulator() -> None:
    prod = environment("prod")
    assert prod.thresholds["remediation-enabled"] == "false"
    assert prod.deploy_sim is False
    assert prod.retain_data is True
    assert prod.thresholds == DEFAULT_THRESHOLDS


def test_dev_runs_the_simulator_with_redrive_enabled() -> None:
    dev = environment("dev")
    assert dev.deploy_sim is True
    assert dev.retain_data is False
    assert dev.thresholds["remediation-enabled"] == "true"
    assert set(dev.thresholds) == set(DEFAULT_THRESHOLDS)


def test_overrides_must_name_existing_thresholds() -> None:
    with pytest.raises(ValueError, match="reminder-interval"):
        EnvironmentConfig(
            name="x", deploy_sim=False, threshold_overrides={"reminder-interval": "1"}
        )


def test_every_environment_only_overrides_known_thresholds() -> None:
    for config in ENVIRONMENTS.values():
        assert set(config.threshold_overrides) <= set(DEFAULT_THRESHOLDS)


# ------------------------------------------------------------------------- T8: the agent
def test_dev_uses_azure_openai_and_workshop_uses_bedrock() -> None:
    dev, workshop = environment("dev").agent, environment("workshop").agent

    assert dev is not None and dev.provider == "azure_openai"
    assert workshop is not None and workshop.provider == "bedrock" and workshop.model
    assert dev.lambda_environment()["AGENT_SECRET_ID"] == "guardia/agent-azure-openai"  # noqa: S105
    assert workshop.lambda_environment() == {
        "AGENT_PROVIDER": "bedrock",
        "AGENT_MODEL": workshop.model,
    }


def test_production_has_no_agent_until_the_team_decides_so() -> None:
    assert environment("prod").agent is None


def test_workshop_is_a_disposable_environment_with_the_simulator() -> None:
    workshop = environment("workshop")

    assert workshop.deploy_sim is True and workshop.retain_data is False
    assert workshop.thresholds["remediation-enabled"] == "true"


def test_bedrock_needs_a_model_and_the_provider_must_be_known() -> None:
    with pytest.raises(ValueError, match="agent_model"):
        EnvironmentConfig(name="x", deploy_sim=False, agent_provider="bedrock")
    with pytest.raises(ValueError, match="agent_provider"):
        EnvironmentConfig(name="x", deploy_sim=False, agent_provider="other")


def test_the_agent_limits_are_thresholds_like_the_rest() -> None:
    assert {
        "agent-timeout-seconds",
        "agent-max-turns",
        "agent-max-tool-output-chars",
        "agent-min-confidence",
    } <= set(DEFAULT_THRESHOLDS)


# --------------------------------------------------------------- fast demo (D33)
def test_environments_with_the_simulator_run_in_seconds_and_production_does_not() -> None:
    for name in ("dev", "workshop"):
        thresholds = environment(name).thresholds
        assert thresholds["redrive-poll-interval-seconds"] == "5"
        assert thresholds["default-retry-after-seconds"] == "5"
        assert float(thresholds["reminder-interval-minutes"]) < 1

    prod = environment("prod").thresholds
    assert prod["redrive-poll-interval-seconds"] == "180"
    assert prod["default-retry-after-seconds"] == "60"
    assert prod["reminder-interval-minutes"] == "10"
