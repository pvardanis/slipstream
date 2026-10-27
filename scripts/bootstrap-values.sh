#!/usr/bin/env bash
# Checks for the bootstrap stack's operator-set values (the Grafana external ID and the
# operator email), sourced by grafana-cloud-setup.sh, which asks for them, and
# bootstrap.sh, which reads them back from the live resources.

# is_external_id VALUE succeeds for what AWS accepts as an external ID: 2 to 1224
# characters from this set. The length is checked apart from the pattern because a
# regex repeat count stops at 255.
# shellcheck disable=SC2317,SC2329 # called by name through ask_valid
is_external_id() { [[ "$1" =~ ^[[:alnum:]_+=,.@:/-]+$ && ${#1} -ge 2 && ${#1} -le 1224 ]]; }

# is_email VALUE succeeds for what the bootstrap stack's operator_email validation
# accepts, minus the characters that would need escaping in an HCL string.
# shellcheck disable=SC2317,SC2329 # called by name through ask_valid
is_email() { [[ "$1" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ && "$1" != *[\"\\\$%]* ]]; }
