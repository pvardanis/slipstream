# Tests for the host-side bench-client secret splitter that bench-proxy-up.sh runs
# over SSM: it reads the bench-client secret JSON from $SECRET_JSON and writes the
# three PEM fields to the cert paths nginx reads. Exercised as a subprocess with the
# system interpreter, the way the host invokes it, so the error branches that have
# regressed before (missing field, absent SecretString) stay covered.
import json
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "terraform"
    / "baseline"
    / "bench-proxy-secret-split.py"
)


def run_split(secret_json, tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    ca = tmp_path / "ca.crt"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(cert), str(key), str(ca)],
        env={"SECRET_JSON": secret_json},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, cert, key, ca


def test_writes_three_pem_fields(tmp_path):
    secret = json.dumps(
        {
            "client_cert_pem": "CERT",
            "client_key_pem": "KEY",
            "ca_cert_pem": "CA",
            "api_key": "unused-here",
        }
    )
    result, cert, key, ca = run_split(secret, tmp_path)

    assert result.returncode == 0, result.stderr
    assert cert.read_text() == "CERT"
    assert key.read_text() == "KEY"
    assert ca.read_text() == "CA"


def test_written_files_are_owner_only(tmp_path):
    secret = json.dumps(
        {"client_cert_pem": "CERT", "client_key_pem": "KEY", "ca_cert_pem": "CA"}
    )
    _, cert, key, ca = run_split(secret, tmp_path)

    for path in (cert, key, ca):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_missing_field_fails_with_named_message(tmp_path):
    secret = json.dumps({"client_cert_pem": "CERT", "client_key_pem": "KEY"})
    result, *_ = run_split(secret, tmp_path)

    assert result.returncode != 0
    assert "ca_cert_pem is missing or empty" in result.stderr


def test_empty_field_fails_with_named_message(tmp_path):
    secret = json.dumps(
        {"client_cert_pem": "CERT", "client_key_pem": "", "ca_cert_pem": "CA"}
    )
    result, *_ = run_split(secret, tmp_path)

    assert result.returncode != 0
    assert "client_key_pem is missing or empty" in result.stderr


def test_none_secret_string_fails_clearly(tmp_path):
    result, *_ = run_split("None", tmp_path)

    assert result.returncode != 0
    assert "no SecretString value" in result.stderr


def test_empty_secret_string_fails_clearly(tmp_path):
    result, *_ = run_split("", tmp_path)

    assert result.returncode != 0
    assert "no SecretString value" in result.stderr
