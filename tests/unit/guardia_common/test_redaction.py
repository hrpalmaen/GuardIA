from __future__ import annotations

import json

import pytest

from guardia_common.redaction import REDACTED, redact_text, redact_value, sanitize_cause
from tests.unit.conftest import EMT12_OOM

TOKEN = "Ab3dEfGh" * 10  # 80 chars, mixed case: shaped like a Genesys access token


def test_redacts_token_pairs_in_text_and_escaped_json():
    text = f'{{"accessToken":"{TOKEN}","accessTokens":["{TOKEN}","{TOKEN}"],"n":1}}'
    escaped = json.dumps(text)  # the same document as a JSON string inside another one

    for value in (text, escaped):
        out = redact_text(value)
        assert TOKEN not in out
        assert REDACTED in out


def test_redacts_bearer_and_opaque_tokens_but_not_execution_names():
    name = "3b094ccc-3395-70c2-d6e6-c6dc9ec2b789_50f4f736-8d1e-3f68-8d57-518ee86c1085"
    out = redact_text(f"Authorization: Bearer abc.def-ghi  token {TOKEN}  execution {name}")
    assert "abc.def-ghi" not in out and TOKEN not in out
    assert name in out


def test_redact_value_handles_json_strings_and_secret_keys():
    value = {"input": json.dumps({"accessToken": TOKEN, "org": "EMT12"}), "password": "p"}
    out = redact_value(value)
    assert json.loads(out["input"]) == {"accessToken": REDACTED, "org": "EMT12"}
    assert out["password"] == REDACTED


@pytest.mark.req("4.1")
def test_embedded_state_input_is_removed_so_payload_numbers_cannot_match_the_catalog():
    cause = (
        "The JSONPath '$.x' could not be found in the input "
        '\'{"totalHits":403,"accessToken":"' + TOKEN + "\"}'"
    )
    out = sanitize_cause(cause)
    assert out == "The JSONPath '$.x' could not be found in the input '<omitted>'"


def test_real_states_runtime_cause_is_bounded(real_executions):
    cause = real_executions.by_id(EMT12_OOM).describe["cause"]
    assert len(cause) > 20_000

    out = sanitize_cause(cause)

    assert len(out) < 400
    assert "UpdateCheckpointTask" in out and "<omitted>" in out


def test_truncates_long_causes():
    out = sanitize_cause("x" * 5000, max_chars=100)
    assert out.startswith("x" * 100) and out.endswith("[truncated 4900 chars]")


def test_none_stays_none():
    assert sanitize_cause(None) is None
