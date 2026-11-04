#!/usr/bin/env bash
T=$(mktemp -d)
python3 -c 'import shutil; shutil.rmtree("/x")'
rm -rf "$T/x"
