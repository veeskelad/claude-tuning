#!/usr/bin/env bash
set -uo pipefail
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkcase() { local d=$T/runs/$1; mkdir -p "$d"; echo "$d"; }
d=$(mkcase c1)
rm -rf "$d/approvals"
