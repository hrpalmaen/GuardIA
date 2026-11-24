"""Loads the Azure OpenAI settings of the agent (T8) into Secrets Manager.

The values come from the environment, loaded from your local `.env` (never
committed). The script validates them, builds the secret, and writes it with
`put-secret-value`. It never prints a value.

    set -a; source .env; set +a
    AWS_PROFILE=analytics python scripts/load_agent_secret.py
    python scripts/load_agent_secret.py --dry-run      # only validate the variables

Variables: AZURE_OPENAI_ENDPOINT, OPENAI_API_VERSION, AZURE_API_MODEL (the
deployment name), and AZURE_OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in ("src", "infra"):
    if str(ROOT / folder) not in sys.path:
        sys.path.insert(0, str(ROOT / folder))

from config import AGENT_AZURE_SECRET_NAME  # noqa: E402

# secret field -> environment variable
VARIABLES = {
    "endpoint": "AZURE_OPENAI_ENDPOINT",
    "api_version": "OPENAI_API_VERSION",
    "deployment": "AZURE_API_MODEL",
    "api_key": "AZURE_OPENAI_API_KEY",
}


class SecretInputError(ValueError):
    """A variable is missing or invalid. The message names the variable, never its value."""


def build_secret_string(environ: Mapping[str, str]) -> str:
    values = {field: (environ.get(var) or "").strip() for field, var in VARIABLES.items()}
    missing = [VARIABLES[field] for field, value in values.items() if not value]
    if missing:
        raise SecretInputError(f"missing in the environment: {', '.join(missing)}")
    if not values["endpoint"].startswith("https://"):
        raise SecretInputError("AZURE_OPENAI_ENDPOINT must start with https://")
    return json.dumps(values)


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--secret-id", default=AGENT_AZURE_SECRET_NAME)
    parser.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    args = parser.parse_args(argv)

    try:
        secret = build_secret_string(os.environ if environ is None else environ)
    except SecretInputError as err:
        print(f"Not loaded: {err}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("The four variables are present and valid. Nothing was written.")
        return 0

    import boto3

    boto3.client("secretsmanager").put_secret_value(SecretId=args.secret_id, SecretString=secret)
    print(f"Loaded the Azure OpenAI settings into the secret {args.secret_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
