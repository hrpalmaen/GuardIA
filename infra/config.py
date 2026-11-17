"""Default configuration thresholds published to SSM Parameter Store.

Values come from design.md (decision D8 and the component table). Handlers read
them from SSM at runtime and never hardcode them.
"""

SSM_PREFIX = "/guardia/config"

DEFAULT_THRESHOLDS: dict[str, str] = {
    # SLA
    "deadline-local": "06:00",
    "timezone": "America/Bogota",
    # Level 1 · remediation budget
    "remediation-max-attempts": "2",
    "remediation-window-minutes": "20",
    "max-wait-seconds": "600",
    # Level 2 · escalation
    "reminder-interval-minutes": "10",
    "parallel-escalation-minutes-left": "30",
    "post-deadline-wait-hours": "6",
    # Grouping and watchdog
    "grouping-window-minutes": "5",
    "grouping-min-incidents": "3",
    "watchdog-interval-minutes": "5",
    "discovery-interval-minutes": "15",
    # Level 0 · missed and stuck executions (requirement 3)
    "missed-run-tolerance-minutes": "10",
    "watchdog-lookback-minutes": "60",
    "stuck-median-multiplier": "2",
    "stuck-period-fraction": "0.5",
    "median-window-executions": "14",
    "median-min-executions": "5",
    # Heartbeat
    "heartbeat-missing-minutes": "10",
}
