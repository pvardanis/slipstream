# Split the bench-client secret JSON into the PEM files nginx reads. bench-proxy-up.sh
# fetches the secret and hands the JSON in via $SECRET_JSON (never argv, so it stays
# off the process list), then runs this with the client-cert, key and CA paths. Writes
# each file 0600 so the private key is owner-only regardless of the caller's umask, and
# fails with a named message when a field or the whole SecretString is absent — the
# host has no cert material to proxy with, so surfacing which field is missing beats a
# raw JSONDecodeError or a later handshake failure mid-sweep. Stdlib only: it runs on
# the host's system python3 before the bench container exists.
import json
import os
import sys


def write_owner_only(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)


def main():
    cert_path, key_path, ca_path = sys.argv[1], sys.argv[2], sys.argv[3]

    # --query SecretString --output text prints the literal "None" when the secret
    # holds a binary value instead of a string; guard it so the failure is an
    # actionable message rather than a raw JSONDecodeError traceback.
    raw = os.environ["SECRET_JSON"]
    if raw in ("", "None"):
        raise SystemExit("bench-client secret has no SecretString value")

    data = json.loads(raw)
    targets = (
        (cert_path, "client_cert_pem"),
        (key_path, "client_key_pem"),
        (ca_path, "ca_cert_pem"),
    )
    for path, field in targets:
        value = data.get(field)
        if not value:
            raise SystemExit(f"bench-client secret field {field} is missing or empty")
        write_owner_only(path, value)


if __name__ == "__main__":
    main()
