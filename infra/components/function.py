"""Shared Lambda layer and the `GuardiaFunction` construct (Fase B, B1).

Every GuardIA Lambda runs the same way: Python 3.12 on arm64, the shared layer
(`pydantic`, `croniter`, `guardia_common`), JSON logs kept 30 days, a timeout,
and a DLQ for asynchronous invocations (EventBridge targets). Each function
ships only its own folder, `src/lambdas/<name>/`, with `handler.handler` as the
entry point. boto3 comes with the runtime and is not packaged.

The layer is built with pip for the Lambda platform (manylinux arm64 wheels),
so `pydantic_core`, which is compiled, works in Lambda even when the build runs
on WSL. The build uses the local pip first and falls back to Docker if that
fails. Tests skip bundling with the `aws:cdk:bundling-stacks` context.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - runs pip with fixed arguments to build the layer
import sys
from pathlib import Path

import jsii
from aws_cdk import BundlingOptions, Duration, ILocalBundling, RemovalPolicy
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from config import SSM_PREFIX

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
LAMBDAS = SRC / "lambdas"

RUNTIME = lambda_.Runtime.PYTHON_3_12
ARCHITECTURE = lambda_.Architecture.ARM_64

# Pinned so every deploy ships the versions the tests ran with. pydantic pins
# its own pydantic-core. python-dateutil (from croniter) is pinned below 3
# because the layer comes before the runtime on sys.path, and the runtime's
# botocore requires python-dateutil<3.
LAYER_REQUIREMENTS = ("pydantic==2.14.3", "croniter==7.0.0", "python-dateutil==2.9.0.post0")

_PIP_PLATFORM_ARGS = (
    "--platform", "manylinux2014_aarch64",
    "--implementation", "cp",
    "--python-version", "3.12",
    "--only-binary=:all:",
)  # fmt: skip

_ASSET_EXCLUDE = ["**/__pycache__", "*.pyc"]


def _pip_install_args(target: str) -> list[str]:
    return ["install", "--quiet", "--no-compile", "--target", target,
            *_PIP_PLATFORM_ARGS, *LAYER_REQUIREMENTS]  # fmt: skip


@jsii.implements(ILocalBundling)
class _LocalLayerBundling:
    """Builds the layer with the pip of the interpreter running CDK."""

    def try_bundle(self, output_dir: str, *_args, **_kwargs) -> bool:
        target = Path(output_dir) / "python"
        result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [sys.executable, "-m", "pip", *_pip_install_args(str(target))],
            check=False,
        )
        if result.returncode != 0:
            print("GuardIA layer: local pip failed, falling back to Docker", file=sys.stderr)
            return False
        shutil.copytree(
            SRC / "guardia_common",
            target / "guardia_common",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        return True


def shared_layer(
    scope: Construct, construct_id: str = "SharedLayer", *, retain: bool = True
) -> lambda_.LayerVersion:
    """`retain` keeps old layer versions when they are replaced or the stack is
    deleted (production); False deletes them (test account)."""
    docker_command = " && ".join([
        "pip " + " ".join(_pip_install_args("/asset-output/python")),
        "cp -r /asset-input/guardia_common /asset-output/python/",
    ])  # fmt: skip
    return lambda_.LayerVersion(
        scope,
        construct_id,
        code=lambda_.Code.from_asset(
            str(SRC),
            exclude=["lambdas", "guardia_local", "*.egg-info", *_ASSET_EXCLUDE],
            bundling=BundlingOptions(
                image=RUNTIME.bundling_image,
                command=["bash", "-c", docker_command],
                local=_LocalLayerBundling(),
            ),
        ),
        compatible_runtimes=[RUNTIME],
        compatible_architectures=[ARCHITECTURE],
        description="GuardIA dependencies: " + ", ".join(LAYER_REQUIREMENTS) + ", guardia_common",
        removal_policy=RemovalPolicy.RETAIN if retain else RemovalPolicy.DESTROY,
    )


class GuardiaFunction(Construct):
    """One GuardIA Lambda, configured like all the others.

    `name` is the folder under `src/lambdas/`. `GUARDIA_SSM_PREFIX` is always
    set; the rest of the environment is what the handler documents.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        name: str,
        layer: lambda_.ILayerVersion,
        environment: dict[str, str] | None = None,
        timeout_seconds: int = 30,
        memory_size: int = 256,
        description: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        code_dir = LAMBDAS / name
        if not (code_dir / "handler.py").is_file():
            raise ValueError(f"no Lambda at src/lambdas/{name}/handler.py")

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.dlq = sqs.Queue(
            self,
            "Dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.function = lambda_.Function(
            self,
            "Function",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(code_dir), exclude=_ASSET_EXCLUDE),
            layers=[layer],
            environment={"GUARDIA_SSM_PREFIX": SSM_PREFIX, **(environment or {})},
            timeout=Duration.seconds(timeout_seconds),
            memory_size=memory_size,
            description=description or f"GuardIA {name}",
            logging_format=lambda_.LoggingFormat.JSON,
            log_group=self.log_group,
            dead_letter_queue=self.dlq,
            retry_attempts=2,
        )
