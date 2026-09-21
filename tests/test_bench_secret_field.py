# Tests for the host-side bench-client secret field reader that bench-sweep.sh runs
# over SSM: it reads the bench-client secret JSON on stdin and prints one named field
# (the vLLM api-key) so the value never lands on argv. Exercised as a subprocess with
# the system interpreter, the way the host invokes it, so the error branches (missing
# field, absent SecretString) surface a named message rather than a raw traceback.
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "terraform"
    / "bench-endpoint"
    / "bench_secret_field.py"
)


def run_field(secret_json, field):
    return subprocess.run(
        [sys.executable, str(SCRIPT), field],
        input=secret_json,
        capture_output=True,
        text=True,
        check=False,
    )


def test_prints_named_field():
    secret = json.dumps(
        {
            "client_cert_pem": "CERT",
            "client_key_pem": "KEY",
            "ca_cert_pem": "CA",
            "api_key": "sk-secret",
        }
    )
    result = run_field(secret, "api_key")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sk-secret"


def test_missing_field_fails_with_named_message():
    secret = json.dumps({"client_cert_pem": "CERT"})
    result = run_field(secret, "api_key")

    assert result.returncode != 0
    assert "api_key is missing or empty" in result.stderr


def test_empty_field_fails_with_named_message():
    secret = json.dumps({"api_key": ""})
    result = run_field(secret, "api_key")

    assert result.returncode != 0
    assert "api_key is missing or empty" in result.stderr


def test_none_secret_string_fails_clearly():
    result = run_field("None", "api_key")

    assert result.returncode != 0
    assert "no SecretString value" in result.stderr


def test_empty_secret_string_fails_clearly():
    result = run_field("", "api_key")

    assert result.returncode != 0
    assert "no SecretString value" in result.stderr
