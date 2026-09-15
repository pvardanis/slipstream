# Read one field from the bench-client secret JSON and print it. bench-sweep.sh pipes
# the secret in on stdin (never argv, so the value stays off the process list) and
# passes the field name — the vLLM api-key — as the one argument. Fails with a named
# message when the field or the whole SecretString is absent, so the operator sees
# which field is missing over SSM rather than a raw JSONDecodeError or KeyError
# traceback. Stdlib only: it runs on the host's system python3 before the bench
# container exists.
import json
import sys


def main() -> None:
    field = sys.argv[1]

    # --query SecretString --output text prints the literal "None" when the secret
    # holds a binary value instead of a string; guard it so the failure is an
    # actionable message rather than a raw JSONDecodeError traceback.
    raw = sys.stdin.read()
    if raw.strip() in ("", "None"):
        raise SystemExit("bench-client secret has no SecretString value")

    value = json.loads(raw).get(field)
    if not value:
        raise SystemExit(f"bench-client secret field {field} is missing or empty")
    print(value)


if __name__ == "__main__":
    main()
