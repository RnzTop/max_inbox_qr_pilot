#!/bin/sh
set -eu
chown -R pilot:pilot /data
chmod 700 /data
exec gosu pilot python -u /app/pilot.py
