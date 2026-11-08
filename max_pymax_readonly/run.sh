#!/bin/sh
set -eu
mkdir -p /data
chown -R pilot:pilot /data
chmod 700 /data
exec gosu pilot python -u /app/app.py
