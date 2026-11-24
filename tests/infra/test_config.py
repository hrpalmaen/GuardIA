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


# --------------------------------------------------------------------------- T6
from config import MissingAlarmEmailError, alarm_email_from_environment  # noqa: E402


@pytest.mark.req("13.2")
@pytest.mark.parametrize("value", [None, "", "   ", "not-an-email", "a b@c.co"])
def test_synth_fails_without_a_valid_alarm_email(value) -> None:
    environ = {} if value is None else {"GUARDIA_ALARM_EMAIL": value}
    with pytest.raises(MissingAlarmEmailError, match="GUARDIA_ALARM_EMAIL"):
        alarm_email_from_environment(environ)


def test_alarm_email_is_trimmed() -> None:
    assert alarm_email_from_environment({"GUARDIA_ALARM_EMAIL": " team@example.com "}) == (
        "team@example.com"
    )
