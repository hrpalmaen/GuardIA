"""Provider adapters of the agent (T8): Azure OpenAI over urllib, Bedrock through Converse."""

from __future__ import annotations

import json
import urllib.error
from http.client import HTTPMessage

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from guardia_common.agent.adapters import AZURE_OPENAI, BEDROCK, build_client
from guardia_common.agent.adapters import azure_openai as azure
from guardia_common.agent.adapters.bedrock import BedrockClient, to_converse
from guardia_common.agent.models import (
    SUBMIT_TOOL,
    AgentConfigError,
    AgentProviderError,
    AgentTimeoutError,
    ToolSpec,
)

KEY = "az-key-SUPER-SECRET-0123456789"
ENDPOINT = "https://my-resource.openai.azure.com"
SECRET = json.dumps(
    {"endpoint": ENDPOINT, "api_version": "2025-01-01", "deployment": "gpt-41-mini", "api_key": KEY}
)
TOOLS = [
    ToolSpec("get_recent_runs", "runs", {"type": "object", "properties": {}}),
    ToolSpec(
        SUBMIT_TOOL, "decide", {"type": "object", "properties": {"summary_es": {"type": "string"}}}
    ),
]
MESSAGES = [
    {"role": "system", "content": "instrucciones"},
    {"role": "user", "content": "incidente"},
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "c1", "name": "get_recent_runs", "arguments": {}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "get_recent_runs", "content": '{"runs":[]}'},
]  # fmt: skip


class Response:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class Opener:
    """Plays answers in order; an exception in the list is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = HTTPMessage()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(f"{ENDPOINT}/secret-path", code, "msg", headers, None)


def azure_answer(**message):
    return {
        "choices": [{"message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }


def azure_client(opener, sleeps=None):
    settings = azure.AzureSettings.from_secret_string(SECRET)
    ticks = iter(range(0, 1000))
    return azure.AzureOpenAIClient(
        settings,
        opener=opener,
        sleep=(sleeps if sleeps is not None else []).append,
        clock=lambda: float(next(ticks)) / 10,
    )


# --------------------------------------------------------------------------- Azure
def test_the_azure_settings_are_read_from_the_secret():
    settings = azure.AzureSettings.from_secret_string(SECRET)

    assert (settings.deployment, settings.api_version) == ("gpt-41-mini", "2025-01-01")
    assert KEY not in repr(settings) and ENDPOINT not in repr(settings)


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("not json", "not JSON"),
        ("[]", "not a JSON object"),
        (json.dumps({"endpoint": ENDPOINT}), "api_version"),
        (json.dumps({**json.loads(SECRET), "api_key": " "}), "api_key"),
        (json.dumps({**json.loads(SECRET), "endpoint": "http://insecure"}), "https"),
        (None, "not JSON"),
    ],
)
def test_a_bad_azure_secret_is_a_config_error_that_never_shows_its_values(secret, message):
    with pytest.raises(AgentConfigError, match=message) as error:
        azure.AzureSettings.from_secret_string(secret)

    assert KEY not in str(error.value) and "insecure" not in str(error.value)


def test_the_settings_are_read_once_per_container():
    azure.clear_settings_cache()

    class Secrets:
        calls = 0

        def get_secret_value(self, SecretId):  # noqa: N803
            Secrets.calls += 1
            return {"SecretString": SECRET}

    secrets = Secrets()
    first = azure.load_settings(secrets, "guardia/agent-azure-openai")
    second = azure.load_settings(secrets, "guardia/agent-azure-openai")
    azure.clear_settings_cache()

    assert first is second and Secrets.calls == 1


def test_the_azure_request_has_the_right_url_header_and_body():
    opener = Opener(azure_answer(content="ok"))

    azure_client(opener).complete(MESSAGES, TOOLS, timeout_s=30)

    request, timeout = opener.requests[0]
    assert request.full_url == (
        f"{ENDPOINT}/openai/deployments/gpt-41-mini/chat/completions?api-version=2025-01-01"
    )
    assert request.get_method() == "POST"
    assert request.get_header("Api-key") == KEY
    assert request.get_header("Content-type") == "application/json"
    assert 0 < timeout <= 30
    body = json.loads(request.data)
    assert body["temperature"] == 0 and body["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in body["tools"]] == ["get_recent_runs", SUBMIT_TOOL]
    assert body["tools"][0]["type"] == "function"
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    call = body["messages"][2]["tool_calls"][0]
    assert call["function"] == {"name": "get_recent_runs", "arguments": "{}"}
    assert body["messages"][3]["tool_call_id"] == "c1"
    assert "model" not in body  # Azure picks the model by the deployment in the URL


def test_the_last_turn_forces_the_decision_tool():
    opener = Opener(azure_answer(content="ok"))

    azure_client(opener).complete(MESSAGES, TOOLS, force_tool=SUBMIT_TOOL)

    body = json.loads(opener.requests[0][0].data)
    assert body["tool_choice"] == {"type": "function", "function": {"name": SUBMIT_TOOL}}


def test_the_azure_answer_becomes_a_turn_with_parsed_arguments_and_tokens():
    answer = azure_answer(
        content=None,
        tool_calls=[
            {"id": "t1", "type": "function",
             "function": {"name": SUBMIT_TOOL, "arguments": '{"summary_es": "hola"}'}},
            {"id": "t2", "type": "function", "function": {"name": "x", "arguments": "{not json"}},
        ],
    )  # fmt: skip

    turn = azure_client(Opener(answer)).complete(MESSAGES, TOOLS)

    assert [c.name for c in turn.tool_calls] == [SUBMIT_TOOL, "x"]
    assert turn.tool_calls[0].arguments == {"summary_es": "hola"}
    assert turn.tool_calls[1].arguments == {"_unparsed": True}
    assert (turn.input_tokens, turn.output_tokens) == (120, 30)


@pytest.mark.req("4.6")
def test_a_429_is_retried_once_after_the_wait_it_asks_for():
    sleeps: list[float] = []
    opener = Opener(http_error(429, "3"), azure_answer(content="ok"))

    turn = azure_client(opener, sleeps).complete(MESSAGES, TOOLS, timeout_s=30)

    assert turn.text == "ok" and sleeps == [3.0] and len(opener.requests) == 2


@pytest.mark.req("4.6")
def test_two_429_in_a_row_is_a_provider_error():
    sleeps: list[float] = []
    opener = Opener(http_error(429, "1"), http_error(429, "1"))

    with pytest.raises(AgentProviderError, match="429"):
        azure_client(opener, sleeps).complete(MESSAGES, TOOLS, timeout_s=30)

    assert len(opener.requests) == 2


def test_a_429_whose_wait_does_not_fit_in_the_time_left_is_not_retried():
    sleeps: list[float] = []
    opener = Opener(http_error(429, "10"))

    with pytest.raises(AgentProviderError):
        azure_client(opener, sleeps).complete(MESSAGES, TOOLS, timeout_s=5)

    assert sleeps == [] and len(opener.requests) == 1


@pytest.mark.req("12.3")
@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (http_error(500), AgentProviderError),
        (http_error(401), AgentProviderError),
        (TimeoutError("timed out calling https://my-resource.openai.azure.com"), AgentTimeoutError),
        (urllib.error.URLError(TimeoutError("t")), AgentTimeoutError),
        (
            urllib.error.URLError("name resolution for my-resource.openai.azure.com"),
            AgentProviderError,
        ),
        (ConnectionResetError("reset by my-resource.openai.azure.com"), AgentProviderError),
    ],
)
def test_failures_never_show_the_address_or_the_key(failure, kind):
    with pytest.raises(kind) as error:
        azure_client(Opener(failure)).complete(MESSAGES, TOOLS)

    text = f"{error.value!s} {error.value!r} {error.value.__cause__!r}"
    assert KEY not in text and "my-resource" not in text and "secret-path" not in text


@pytest.mark.parametrize("body", [b"<html>", b"[1, 2]", json.dumps({"choices": []}).encode()])
def test_an_answer_that_is_not_usable_is_a_provider_error(body):
    with pytest.raises(AgentProviderError):
        azure_client(Opener(body)).complete(MESSAGES, TOOLS)


def test_no_time_left_is_a_timeout_before_calling_the_provider():
    opener = Opener()

    with pytest.raises(AgentTimeoutError):
        azure_client(opener).complete(MESSAGES, TOOLS, timeout_s=0)

    assert opener.requests == []


# ------------------------------------------------------------------------- Bedrock
CONVERSE_ANSWER = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [
                {"text": "reviso"},
                {"toolUse": {"toolUseId": "u1", "name": "get_recent_runs", "input": {}}},
            ],
        }
    },
    "stopReason": "tool_use",
    "usage": {"inputTokens": 80, "outputTokens": 20, "totalTokens": 100},
    "metrics": {"latencyMs": 250},
}


class Runtime:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.requests = answer, error, []

    def converse(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.answer


def test_the_messages_are_converted_to_the_converse_format():
    messages = [
        *MESSAGES,
        {"role": "tool", "tool_call_id": "c2", "name": "x", "content": "{}"},
    ]

    system, converted = to_converse(messages)

    assert system == [{"text": "instrucciones"}]
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert converted[1]["content"] == [
        {"toolUse": {"toolUseId": "c1", "name": "get_recent_runs", "input": {}}}
    ]
    results = converted[2]["content"]  # both tool results travel in one user message
    assert [r["toolResult"]["toolUseId"] for r in results] == ["c1", "c2"]


def test_the_bedrock_request_uses_tool_config_and_zero_temperature():
    runtime = Runtime(CONVERSE_ANSWER)

    turn = BedrockClient(runtime, "amazon.nova-pro-v1:0").complete(MESSAGES, TOOLS)

    request = runtime.requests[0]
    assert request["modelId"] == "amazon.nova-pro-v1:0"
    assert request["inferenceConfig"]["temperature"] == 0
    assert request["toolConfig"]["toolChoice"] == {"auto": {}}
    spec = request["toolConfig"]["tools"][0]["toolSpec"]
    assert spec["name"] == "get_recent_runs" and "json" in spec["inputSchema"]
    assert turn.text == "reviso" and turn.tool_calls[0].id == "u1"
    assert (turn.input_tokens, turn.output_tokens) == (80, 20)


def test_the_last_turn_forces_the_decision_tool_in_bedrock():
    runtime = Runtime(CONVERSE_ANSWER)

    BedrockClient(runtime, "m").complete(MESSAGES, TOOLS, force_tool=SUBMIT_TOOL)

    assert runtime.requests[0]["toolConfig"]["toolChoice"] == {"tool": {"name": SUBMIT_TOOL}}


def test_a_bedrock_request_that_botocore_validates():
    stub = pytest.importorskip("botocore.stub")
    import boto3

    runtime = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = stub.Stubber(runtime)
    stubber.add_response("converse", CONVERSE_ANSWER)
    with stubber:
        turn = BedrockClient(runtime, "amazon.nova-pro-v1:0").complete(
            MESSAGES, TOOLS, force_tool=SUBMIT_TOOL
        )

    stubber.assert_no_pending_responses()
    assert turn.tool_calls[0].name == "get_recent_runs"


@pytest.mark.parametrize(
    ("error", "kind", "text"),
    [
        (ClientError({"Error": {"Code": "ThrottlingException", "Message": "arn:secret"}}, "X"),
         AgentProviderError, "ThrottlingException"),
        (ClientError({"Error": {"Code": "ModelTimeoutException", "Message": "m"}}, "Converse"),
         AgentTimeoutError, "timed out"),
        (ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
         AgentTimeoutError, "timed out"),
    ],
)  # fmt: skip
def test_bedrock_failures_become_provider_errors_without_provider_text(error, kind, text):
    with pytest.raises(kind) as raised:
        BedrockClient(Runtime(error=error), "m").complete(MESSAGES, TOOLS)

    assert text in str(raised.value) and "arn:secret" not in str(raised.value)


def test_a_bedrock_answer_without_a_message_is_a_provider_error():
    with pytest.raises(AgentProviderError):
        BedrockClient(Runtime({"unexpected": 1}), "m").complete(MESSAGES, TOOLS)


# ------------------------------------------------------------------------- factory
def test_the_factory_builds_each_provider_and_refuses_the_rest():
    azure.clear_settings_cache()

    class Secrets:
        def get_secret_value(self, SecretId):  # noqa: N803
            return {"SecretString": SECRET}

    assert isinstance(
        build_client(AZURE_OPENAI, secrets=Secrets(), secret_id="guardia/agent-azure-openai"),  # noqa: S106
        azure.AzureOpenAIClient,
    )
    assert isinstance(build_client(BEDROCK, bedrock_runtime=Runtime(), model_id="m"), BedrockClient)
    azure.clear_settings_cache()
    for kwargs in (
        {"provider": None},
        {"provider": "other"},
        {"provider": AZURE_OPENAI},
        {"provider": BEDROCK, "model_id": "m"},
        {"provider": BEDROCK, "bedrock_runtime": Runtime()},
    ):
        with pytest.raises(AgentConfigError):
            build_client(**kwargs)
