#!/bin/sh
set -eu
mkdir -p /data
chown -R appuser:appuser /data
chmod 700 /data
exec gosu appuser python -u /app/app.py
