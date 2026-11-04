#!/usr/bin/env bash
T=$(mktemp -d)
rm -rf "$T/x" 2>/dev/null
rm -rf -- "$T" >/dev/null 2>&1
