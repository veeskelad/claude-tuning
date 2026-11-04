#!/usr/bin/env bash
set -uo pipefail
C=ch-test
TMP="$(mktemp -d)"; trap 'docker rm -f "$C" >/dev/null 2>&1; rm -rf "$TMP"' EXIT
echo ok
