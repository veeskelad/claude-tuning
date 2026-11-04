#!/usr/bin/env bash
mkcase() { echo "/tmp/x"; }
d=$(mkcase c1)
rm -rf "$d/x"
